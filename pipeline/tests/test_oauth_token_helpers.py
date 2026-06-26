"""Tests for the shared OAuth token health helpers (audit H3).

Coverage:
- `log_token_expiry_health` emits INFO at >=24h, WARNING <24h, ERROR <6h.
- `log_token_expiry_health` is a no-op (and never raises) when `.expiry` is None.
- `refresh_with_translation` translates `RefreshError` into a `RuntimeError`
  whose message contains the 7-day testing-status policy hint, the fix command
  pointer (`youtube_oauth_init.py --force`), and the long-term-fix pointer.
- The original `RefreshError` is chained via `__cause__` so diagnostics aren't lost.
- On successful refresh, the token JSON is persisted to disk.

No live Google API calls. `Credentials` is mocked via SimpleNamespace stand-ins
so the tests stay fast and deterministic. The underlying refresh() is intercepted
with `unittest.mock.patch.object`.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.auth.exceptions import RefreshError  # noqa: E402

from tools import oauth_token_helpers  # noqa: E402


# ---------------------------------------------------------------------------
# log_token_expiry_health
# ---------------------------------------------------------------------------


class TestExpiryHealthLogging(unittest.TestCase):
    """Verify the right log level fires at each remaining-hours threshold."""

    def _make_creds(self, hours_remaining: float) -> SimpleNamespace:
        # google-auth uses naive UTC datetimes for .expiry — match that shape.
        expiry = datetime.utcnow() + timedelta(hours=hours_remaining)
        return SimpleNamespace(expiry=expiry)

    def test_info_at_well_above_24h(self) -> None:
        creds = self._make_creds(hours_remaining=72.0)
        with self.assertLogs("oauth.test", level="INFO") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        # Exactly one record, level INFO, contains the hours line.
        self.assertTrue(any(r.levelname == "INFO" for r in cm.records))
        self.assertFalse(any(r.levelname == "WARNING" for r in cm.records))
        self.assertFalse(any(r.levelname == "ERROR" for r in cm.records))
        self.assertTrue(
            any("expiry health" in r.getMessage() for r in cm.records),
            f"records were: {[r.getMessage() for r in cm.records]}",
        )

    def test_warning_below_24h(self) -> None:
        creds = self._make_creds(hours_remaining=12.0)
        with self.assertLogs("oauth.test", level="WARNING") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        self.assertTrue(any(r.levelname == "WARNING" for r in cm.records))
        self.assertFalse(any(r.levelname == "ERROR" for r in cm.records))

    def test_warning_at_exactly_below_24h_boundary(self) -> None:
        # 23.9h sits clearly inside the WARNING window.
        creds = self._make_creds(hours_remaining=23.9)
        with self.assertLogs("oauth.test", level="WARNING") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        self.assertTrue(any(r.levelname == "WARNING" for r in cm.records))

    def test_error_below_6h(self) -> None:
        creds = self._make_creds(hours_remaining=2.5)
        with self.assertLogs("oauth.test", level="ERROR") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        self.assertTrue(any(r.levelname == "ERROR" for r in cm.records))
        # ERROR record should include the fix-command pointer so operators see it.
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertTrue(
            any("youtube_oauth_init.py --force" in r.getMessage() for r in error_records),
            f"error records were: {[r.getMessage() for r in error_records]}",
        )

    def test_error_when_already_expired(self) -> None:
        # Negative hours-remaining still classifies as ERROR — token expired,
        # caller will trigger refresh next.
        creds = self._make_creds(hours_remaining=-1.0)
        with self.assertLogs("oauth.test", level="ERROR") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        self.assertTrue(any(r.levelname == "ERROR" for r in cm.records))

    def test_no_expiry_attribute_does_not_raise(self) -> None:
        # creds without .expiry (or .expiry=None) is a no-op, never raises.
        creds = SimpleNamespace(expiry=None)
        # Use DEBUG level so the debug message gets captured.
        with self.assertLogs("oauth.test", level="DEBUG") as cm:
            oauth_token_helpers.log_token_expiry_health(
                creds, logger=logging.getLogger("oauth.test")
            )
        self.assertTrue(
            any("no .expiry attribute" in r.getMessage() for r in cm.records),
            f"records were: {[r.getMessage() for r in cm.records]}",
        )


# ---------------------------------------------------------------------------
# refresh_with_translation
# ---------------------------------------------------------------------------


class TestRefreshTranslation(unittest.TestCase):
    """Verify RefreshError → RuntimeError translation and successful-refresh persist."""

    def test_refresh_error_translates_to_runtime_error(self) -> None:
        """A google-auth RefreshError must surface as RuntimeError with the
        7-day testing-status policy hint + fix command pointer."""
        # Build a mock Credentials whose .refresh() raises RefreshError.
        mock_creds = mock.MagicMock()
        mock_creds.refresh.side_effect = RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        token_path = Path("does/not/matter.json")

        with self.assertRaises(RuntimeError) as ctx:
            oauth_token_helpers.refresh_with_translation(
                mock_creds,
                token_path=token_path,
                logger=logging.getLogger("oauth.test"),
            )

        msg = str(ctx.exception)
        # All three required pointers must be present in the translated message.
        self.assertIn("revoked or expired", msg)
        self.assertIn("7-day testing-status", msg)
        self.assertIn("youtube_oauth_init.py --force", msg)
        self.assertIn("in-production status", msg)
        # The original RefreshError must be chained via __cause__ so the
        # diagnostic is preserved.
        self.assertIsInstance(ctx.exception.__cause__, RefreshError)

    def test_successful_refresh_persists_token(self) -> None:
        """On successful refresh, the helper persists the new JSON to disk atomically."""
        mock_creds = mock.MagicMock()
        mock_creds.refresh.return_value = None
        mock_creds.to_json.return_value = '{"token": "fresh"}'

        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "token.json"
            oauth_token_helpers.refresh_with_translation(
                mock_creds,
                token_path=token_path,
                logger=logging.getLogger("oauth.test"),
            )
            mock_creds.refresh.assert_called_once()
            # Persisted atomically: final file holds the content, no .tmp sibling left.
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"token": "fresh"}')
            self.assertEqual(
                list(Path(td).glob("token.json.*.tmp")), [],
                "atomic write left a temp sibling behind",
            )

    def test_refresh_error_logged_at_error_level(self) -> None:
        """The translated message should also fire at logging.ERROR so
        operators see the actionable text even when callers swallow the
        exception (defense in depth)."""
        mock_creds = mock.MagicMock()
        mock_creds.refresh.side_effect = RefreshError("invalid_grant")

        with self.assertLogs("oauth.test", level="ERROR") as cm:
            with self.assertRaises(RuntimeError):
                oauth_token_helpers.refresh_with_translation(
                    mock_creds,
                    token_path=Path("token.json"),
                    logger=logging.getLogger("oauth.test"),
                )

        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertTrue(
            any("youtube_oauth_init.py --force" in r.getMessage() for r in error_records),
            f"error records were: {[r.getMessage() for r in error_records]}",
        )


# ---------------------------------------------------------------------------
# _atomic_write_text (M-10 / R2): atomic token persistence
# ---------------------------------------------------------------------------


class TestAtomicWriteText(unittest.TestCase):
    """The token write must be atomic (temp sibling + os.replace) so a concurrent
    /start -auto sub-agent never reads a torn token."""

    def test_writes_content_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "token.json"
            oauth_token_helpers._atomic_write_text(target, '{"a": 1}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_overwrites_existing_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "token.json"
            target.write_text("OLD", encoding="utf-8")
            oauth_token_helpers._atomic_write_text(target, "NEW")
            self.assertEqual(target.read_text(encoding="utf-8"), "NEW")
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_failed_replace_cleans_temp(self) -> None:
        """If os.replace fails, the temp sibling must not be left behind."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "token.json"
            with mock.patch.object(
                oauth_token_helpers.os, "replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    oauth_token_helpers._atomic_write_text(target, "data")
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


# ---------------------------------------------------------------------------
# M3 (WORKFLOW_AUDIT_2026-05-31): wrong-scope-but-valid token pre-check
# ---------------------------------------------------------------------------


class TestWrongScopeToken(unittest.TestCase):
    """A structurally-valid token carrying the WRONG scopes must raise an
    actionable RuntimeError at LOAD time in BOTH credential-load paths
    (tools/youtube_upload.load_credentials AND analytics_pull._load_credentials),
    instead of surfacing later as an untranslated HTTP 403 insufficientPermissions.

    The check uses `creds.has_scopes(SCOPES)`; the message reuses the shared
    `_FIX_COMMAND_POINTER` (no forked wording). The refresh path is NOT exercised
    here — a narrower-scope token short-circuits before refresh.
    """

    def _narrow_scope_creds(self):
        """A creds stand-in that is otherwise valid but lacks a required scope.

        `has_scopes(required)` returns False for the real SCOPES set, mirroring a
        token minted before `yt-analytics.readonly` was added. `.valid` is True so
        we prove the raise comes from the scope check, not the validity check.
        """
        creds = mock.MagicMock()
        creds.has_scopes.return_value = False
        creds.valid = True
        creds.expired = False
        # Give a REAL expiry so log_token_expiry_health (called before the scope
        # check) can do its datetime math / format without a MagicMock blowing up.
        creds.expiry = datetime.utcnow() + timedelta(hours=72)
        return creds

    @staticmethod
    def _present_token_path():
        """A TOKEN_PATH stand-in that reports the token file exists.

        `Path.exists` is read-only on a concrete WindowsPath instance, so we swap
        the whole module-level TOKEN_PATH for a mock. `from_authorized_user_file`
        is itself mocked, so the path VALUE handed to it is irrelevant.
        """
        tp = mock.MagicMock()
        tp.exists.return_value = True
        return tp

    def test_wrong_scope_token_raises_actionable_upload(self) -> None:
        from tools import youtube_upload

        creds = self._narrow_scope_creds()
        with mock.patch.object(
            youtube_upload.Credentials,
            "from_authorized_user_file",
            return_value=creds,
        ), mock.patch.object(
            youtube_upload, "TOKEN_PATH", self._present_token_path()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                youtube_upload.load_credentials()

        msg = str(ctx.exception)
        self.assertIn("scopes", msg)
        self.assertIn("youtube_oauth_init.py --force", msg)
        # The check fired via has_scopes (not the validity guard).
        creds.has_scopes.assert_called_once_with(youtube_upload.SCOPES)

    def test_wrong_scope_token_raises_actionable_analytics(self) -> None:
        import analytics_pull

        creds = self._narrow_scope_creds()
        with mock.patch.object(
            analytics_pull.Credentials,
            "from_authorized_user_file",
            return_value=creds,
        ), mock.patch.object(
            analytics_pull, "TOKEN_PATH", self._present_token_path()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                analytics_pull._load_credentials()

        msg = str(ctx.exception)
        self.assertIn("scopes", msg)
        self.assertIn("youtube_oauth_init.py --force", msg)
        creds.has_scopes.assert_called_once_with(analytics_pull.SCOPES)


if __name__ == "__main__":
    unittest.main()
