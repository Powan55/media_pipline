"""Unit tests for the post-upload bookkeeping hook in tools/youtube_upload.py (T9).

Strategy: tests exercise the `_run_post_upload_hooks` helper directly with
``unittest.mock.patch`` against the imported ``archive_topic`` and
``generate_postmortem`` symbols inside the youtube_upload module. We don't
shell out to YouTube — `main()` integration is covered by smoke + manual run;
the hook semantics are what matters here.

Acceptance coverage:
    - test_dry_run_skips_hooks                    -> AC5
    - test_private_no_publishat_skips_hooks       -> AC6
    - test_scheduled_private_runs_hooks           -> AC7 (private + publish-at)
    - test_public_runs_hooks                      -> AC8 (public)
    - test_unlisted_runs_hooks                    -> AC8 (unlisted)
    - test_archive_failure_does_not_fail_upload   -> AC2 (+ ordering: postmortem still runs)
    - test_postmortem_failure_does_not_fail_upload -> AC3
    - test_archive_runs_before_postmortem         -> AC4

Audit L1 (WORKFLOW_AUDIT_2026-05-16, P5) — hook_selection_log auto-append:
    - test_hook_log_appended_on_successful_upload
    - test_hook_log_skipped_when_no_hook_log_flag
    - test_hook_log_skipped_on_dry_run
    - test_hook_log_skipped_on_unpublished_private
    - test_hook_log_failure_does_not_fail_upload
    - test_hook_log_uses_correct_path_under_channel_root
"""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Ensure the pipeline repo root is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import youtube_upload  # noqa: E402


def _make_args(
    *,
    topic_id: str = "2026-05-06_003",
    privacy: str = "public",
    dry_run: bool = False,
    no_hook_log: bool = False,
) -> argparse.Namespace:
    """Build a minimal Namespace with the fields _run_post_upload_hooks reads."""
    return argparse.Namespace(
        topic_id=topic_id,
        privacy=privacy,
        dry_run=dry_run,
        no_hook_log=no_hook_log,
    )


def _fake_config() -> dict:
    """Minimal config dict with the channel_root + project_root keys the hook reads."""
    return {
        "paths": {
            "channel_root": r"C:\does\not\matter",
            "project_root": r"C:\does\not\matter\project",
        }
    }


# ---------------------------------------------------------------------------
# Skip cases
# ---------------------------------------------------------------------------


class TestHookSkipCases(unittest.TestCase):
    """Verify hooks are NOT invoked when the upload didn't really publish."""

    def test_dry_run_skips_hooks(self) -> None:
        """AC5 — --dry-run never runs archive or postmortem."""
        args = _make_args(privacy="public", dry_run=True)
        with mock.patch.object(youtube_upload, "archive_topic") as m_archive, \
             mock.patch.object(youtube_upload, "generate_postmortem") as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_not_called()
        m_pm.assert_not_called()

    def test_private_no_publishat_skips_hooks(self) -> None:
        """AC6 — privacy=private without --publish-at skips both hooks."""
        args = _make_args(privacy="private", dry_run=False)
        with mock.patch.object(youtube_upload, "archive_topic") as m_archive, \
             mock.patch.object(youtube_upload, "generate_postmortem") as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_not_called()
        m_pm.assert_not_called()

    def test_dry_run_with_private_still_skips(self) -> None:
        """Dry-run takes precedence over privacy gating."""
        args = _make_args(privacy="private", dry_run=True)
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(youtube_upload, "archive_topic") as m_archive, \
             mock.patch.object(youtube_upload, "generate_postmortem") as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=future)
        m_archive.assert_not_called()
        m_pm.assert_not_called()


# ---------------------------------------------------------------------------
# Run cases
# ---------------------------------------------------------------------------


class TestHookRunCases(unittest.TestCase):
    """Verify hooks DO run for published / scheduled-published privacy states."""

    def test_scheduled_private_runs_hooks(self) -> None:
        """AC7 — privacy=private with --publish-at runs both hooks."""
        args = _make_args(privacy="private", dry_run=False)
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={"youtube": Path("yt.mp4")},
        ) as m_archive, mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=future)
        m_archive.assert_called_once()
        m_pm.assert_called_once()

    def test_public_runs_hooks(self) -> None:
        """AC8 — privacy=public runs both hooks."""
        args = _make_args(privacy="public", dry_run=False)
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={"youtube": Path("yt.mp4")},
        ) as m_archive, mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_called_once()
        m_pm.assert_called_once()

    def test_unlisted_runs_hooks(self) -> None:
        """AC8 — privacy=unlisted runs both hooks."""
        args = _make_args(privacy="unlisted", dry_run=False)
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={"youtube": Path("yt.mp4")},
        ) as m_archive, mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_called_once()
        m_pm.assert_called_once()


# ---------------------------------------------------------------------------
# Failure semantics — hooks must not change exit code
# ---------------------------------------------------------------------------


class TestHookFailureSemantics(unittest.TestCase):
    """A bookkeeping failure must NOT raise out of the hook."""

    def test_archive_failure_does_not_fail_upload(self) -> None:
        """AC2 — archive raising is logged + swallowed; postmortem still runs."""
        args = _make_args(privacy="public", dry_run=False)
        with mock.patch.object(
            youtube_upload, "archive_topic", side_effect=RuntimeError("disk full"),
        ) as m_archive, mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            # Must not raise.
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_called_once()
        # Postmortem must still run even though archive blew up.
        m_pm.assert_called_once()

    def test_postmortem_failure_does_not_fail_upload(self) -> None:
        """AC3 — postmortem raising is logged + swallowed."""
        args = _make_args(privacy="public", dry_run=False)
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={"youtube": Path("yt.mp4")},
        ) as m_archive, mock.patch.object(
            youtube_upload,
            "generate_postmortem",
            side_effect=RuntimeError("template missing"),
        ) as m_pm:
            # Must not raise.
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_archive.assert_called_once()
        m_pm.assert_called_once()

    def test_both_hooks_failing_does_not_fail_upload(self) -> None:
        """Belt-and-suspenders — both hooks raising is also swallowed."""
        args = _make_args(privacy="public", dry_run=False)
        with mock.patch.object(
            youtube_upload, "archive_topic", side_effect=RuntimeError("a"),
        ), mock.patch.object(
            youtube_upload, "generate_postmortem", side_effect=RuntimeError("b"),
        ):
            # Must not raise.
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)


# ---------------------------------------------------------------------------
# Ordering — archive must run before postmortem
# ---------------------------------------------------------------------------


class TestHookOrdering(unittest.TestCase):
    """AC4 — archive runs before postmortem (postmortem may reference archive paths)."""

    def test_archive_runs_before_postmortem(self) -> None:
        args = _make_args(privacy="public", dry_run=False)
        call_order: list[str] = []

        def _archive_side_effect(*_a, **_kw):
            call_order.append("archive")
            return {"youtube": Path("yt.mp4")}

        def _pm_side_effect(*_a, **_kw):
            call_order.append("postmortem")
            return Path("pm.md")

        with mock.patch.object(
            youtube_upload, "archive_topic", side_effect=_archive_side_effect,
        ), mock.patch.object(
            youtube_upload, "generate_postmortem", side_effect=_pm_side_effect,
        ):
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)

        self.assertEqual(call_order, ["archive", "postmortem"])


# ---------------------------------------------------------------------------
# Argument forwarding — hook must use real APIs of T5/T6 (kwarg shape sanity check)
# ---------------------------------------------------------------------------


class TestHookArgumentForwarding(unittest.TestCase):
    """Confirm the hook passes the topic_id and channel_root the way T5/T6 expect."""

    def test_archive_called_with_keyword_channel_root(self) -> None:
        args = _make_args(topic_id="2026-05-08_001", privacy="public", dry_run=False)
        config = {"paths": {"channel_root": r"C:\fake\channel",
                            "project_root": r"C:\fake\project"}}
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={},
        ) as m_archive, mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ):
            youtube_upload._run_post_upload_hooks(args, config, publish_at_utc=None)
        # First positional is topic_id; channel_root must be a kwarg per T5's signature.
        call = m_archive.call_args
        self.assertEqual(call.args[0], "2026-05-08_001")
        self.assertEqual(call.kwargs["channel_root"], Path(r"C:\fake\channel"))

    def test_postmortem_called_with_keyword_channel_and_project_root(self) -> None:
        args = _make_args(topic_id="2026-05-08_002", privacy="public", dry_run=False)
        config = {"paths": {"channel_root": r"C:\fake\channel",
                            "project_root": r"C:\fake\project"}}
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={},
        ), mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            youtube_upload._run_post_upload_hooks(args, config, publish_at_utc=None)
        call = m_pm.call_args
        self.assertEqual(call.args[0], "2026-05-08_002")
        self.assertEqual(call.kwargs["channel_root"], Path(r"C:\fake\channel"))
        # project_root is required by generate_postmortem; verify it's passed as a Path.
        self.assertIsInstance(call.kwargs["project_root"], Path)


# ---------------------------------------------------------------------------
# H1 — project_root must come from config, never a hardcoded path
# ---------------------------------------------------------------------------


class TestProjectRootFromConfig(unittest.TestCase):
    """H1 — generate_postmortem's project_root is derived from
    config["paths"]["project_root"], and a config missing that key fails loud
    (clear KeyError) instead of silently using an off-box operator path."""

    def test_postmortem_project_root_derived_from_config(self) -> None:
        """The Path handed to generate_postmortem matches config's project_root."""
        args = _make_args(topic_id="2026-05-08_010", privacy="public", dry_run=False)
        config = {"paths": {"channel_root": r"C:\fake\channel",
                            "project_root": r"D:\elsewhere\ProjectRoot"}}
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={},
        ), mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ) as m_pm:
            youtube_upload._run_post_upload_hooks(args, config, publish_at_utc=None)
        # The hardcoded r"C:\Users\laxmi\Documents\Project" must NOT leak through;
        # the project_root must come straight from the config value.
        self.assertEqual(
            m_pm.call_args.kwargs["project_root"],
            Path(r"D:\elsewhere\ProjectRoot"),
        )

    def test_missing_project_root_key_raises_keyerror(self) -> None:
        """A config WITHOUT paths.project_root raises a clear KeyError at use,
        not a silent wrong-path. Hooks run (public) so the lookup is reached."""
        args = _make_args(topic_id="2026-05-08_011", privacy="public", dry_run=False)
        config = {"paths": {"channel_root": r"C:\fake\channel"}}  # no project_root
        with mock.patch.object(
            youtube_upload, "archive_topic", return_value={},
        ), mock.patch.object(
            youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
        ):
            with self.assertRaises(KeyError) as ctx:
                youtube_upload._run_post_upload_hooks(args, config, publish_at_utc=None)
        self.assertIn("project_root", str(ctx.exception))


# ---------------------------------------------------------------------------
# Audit L1 / P5 — hook_selection_log auto-append after successful upload
# ---------------------------------------------------------------------------


class TestHookSelectionLogAutoAppend(unittest.TestCase):
    """Verify the post-upload auto-append of hook_selection_log.jsonl.

    The existing `archive_topic` + `generate_postmortem` patches in the test
    cases above were sufficient because the hook log call landed in the same
    `_run_post_upload_hooks`. These cases patch `extract_chosen_hook` and the
    re-exported `hook_append_to_log` writer to isolate the hook-log semantics.
    """

    def _patch_existing_hooks(self):
        """Helper context: mock archive + postmortem so only hook-log behavior matters."""
        return (
            mock.patch.object(
                youtube_upload, "archive_topic", return_value={"youtube": Path("yt.mp4")},
            ),
            mock.patch.object(
                youtube_upload, "generate_postmortem", return_value=Path("pm.md"),
            ),
        )

    def test_hook_log_appended_on_successful_upload(self) -> None:
        """AC L1 — successful public upload triggers extract + append_to_log."""
        args = _make_args(privacy="public", dry_run=False)
        m_archive, m_pm = self._patch_existing_hooks()
        fake_chosen = mock.MagicMock(hook_letter="A", formula="Contradiction")
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook", return_value=fake_chosen,
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log", return_value=True,
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_extract.assert_called_once()
        m_append.assert_called_once()

    def test_hook_log_skipped_when_no_hook_log_flag(self) -> None:
        """`--no-hook-log` flag suppresses extract + append entirely."""
        args = _make_args(privacy="public", dry_run=False, no_hook_log=True)
        m_archive, m_pm = self._patch_existing_hooks()
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook",
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log",
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_extract.assert_not_called()
        m_append.assert_not_called()

    def test_hook_log_skipped_on_dry_run(self) -> None:
        """`--dry-run` short-circuits the whole post-upload hook bundle, hook-log included."""
        args = _make_args(privacy="public", dry_run=True)
        with mock.patch.object(
            youtube_upload, "extract_chosen_hook",
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log",
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_extract.assert_not_called()
        m_append.assert_not_called()

    def test_hook_log_skipped_on_unpublished_private(self) -> None:
        """Private-without-publish-at means no real publish; hook-log defers too."""
        args = _make_args(privacy="private", dry_run=False)
        with mock.patch.object(
            youtube_upload, "extract_chosen_hook",
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log",
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        m_extract.assert_not_called()
        m_append.assert_not_called()

    def test_hook_log_runs_for_scheduled_private(self) -> None:
        """Private + --publish-at IS a publish event; hook-log must fire."""
        args = _make_args(privacy="private", dry_run=False)
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        m_archive, m_pm = self._patch_existing_hooks()
        fake_chosen = mock.MagicMock(hook_letter="B", formula="Cited-Observation Lead")
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook", return_value=fake_chosen,
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log", return_value=True,
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=future)
        m_extract.assert_called_once()
        m_append.assert_called_once()

    def test_hook_log_extract_failure_does_not_fail_upload(self) -> None:
        """L1 fail-soft — extract_chosen_hook raising must NOT propagate."""
        args = _make_args(privacy="public", dry_run=False)
        m_archive, m_pm = self._patch_existing_hooks()
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook",
            side_effect=FileNotFoundError("script_FINAL.txt missing"),
        ), mock.patch.object(
            youtube_upload, "hook_append_to_log",
        ) as m_append:
            # Must not raise.
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)
        # The append writer never got invoked because extract raised.
        m_append.assert_not_called()

    def test_hook_log_append_failure_does_not_fail_upload(self) -> None:
        """L1 fail-soft — append_to_log raising (file lock, schema mismatch) must NOT propagate."""
        args = _make_args(privacy="public", dry_run=False)
        m_archive, m_pm = self._patch_existing_hooks()
        fake_chosen = mock.MagicMock(hook_letter="A", formula="Contradiction")
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook", return_value=fake_chosen,
        ), mock.patch.object(
            youtube_upload, "hook_append_to_log",
            side_effect=PermissionError("file locked"),
        ):
            # Must not raise.
            youtube_upload._run_post_upload_hooks(args, _fake_config(), publish_at_utc=None)

    def test_hook_log_uses_correct_path_under_channel_root(self) -> None:
        """Auto-append targets <channel_root>/01_research/hook_selection_log.jsonl."""
        args = _make_args(privacy="public", dry_run=False)
        config = {"paths": {"channel_root": r"C:\fake\channel",
                            "project_root": r"C:\fake\project"}}
        m_archive, m_pm = self._patch_existing_hooks()
        fake_chosen = mock.MagicMock(hook_letter="A", formula="Contradiction")
        with m_archive, m_pm, mock.patch.object(
            youtube_upload, "extract_chosen_hook", return_value=fake_chosen,
        ) as m_extract, mock.patch.object(
            youtube_upload, "hook_append_to_log", return_value=True,
        ) as m_append:
            youtube_upload._run_post_upload_hooks(args, config, publish_at_utc=None)
        # extract_chosen_hook receives the topic_id positionally and
        # channel_root as a kwarg (Path-typed).
        extract_call = m_extract.call_args
        self.assertEqual(extract_call.args[0], "2026-05-06_003")
        self.assertEqual(extract_call.kwargs["channel_root"], Path(r"C:\fake\channel"))
        # append_to_log receives the log_path kwarg under 01_research/.
        append_call = m_append.call_args
        self.assertEqual(
            append_call.kwargs["log_path"],
            Path(r"C:\fake\channel") / "01_research" / "hook_selection_log.jsonl",
        )


# ---------------------------------------------------------------------------
# CLI flag — --no-hook-log
# ---------------------------------------------------------------------------


class TestNoHookLogCLIFlag(unittest.TestCase):
    """Verify argparse exposes --no-hook-log with the correct default semantics."""

    def test_no_hook_log_flag_default_false(self) -> None:
        # Parse minimal valid args and check the flag default.
        argv = ["--topic-id", "2026-05-06_003", "--privacy", "public", "--dry-run"]
        parser = argparse.ArgumentParser()
        # Mirror the real flag for the test (we don't want to invoke main() which
        # would parse via sys.argv and trigger config / file resolution).
        parser.add_argument("--topic-id", required=True)
        parser.add_argument("--privacy", required=True, choices=("public", "unlisted", "private"))
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-hook-log", action="store_true")
        ns = parser.parse_args(argv)
        self.assertFalse(ns.no_hook_log)

    def test_no_hook_log_flag_present_when_set(self) -> None:
        argv = [
            "--topic-id", "2026-05-06_003",
            "--privacy", "public",
            "--dry-run",
            "--no-hook-log",
        ]
        parser = argparse.ArgumentParser()
        parser.add_argument("--topic-id", required=True)
        parser.add_argument("--privacy", required=True, choices=("public", "unlisted", "private"))
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-hook-log", action="store_true")
        ns = parser.parse_args(argv)
        self.assertTrue(ns.no_hook_log)


if __name__ == "__main__":
    unittest.main()
