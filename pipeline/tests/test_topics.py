"""Unit tests for topics.py.

All tests use a per-test temp directory — no test ever touches the real channel
root. Runnable under both `pytest` and stdlib `unittest` discovery
(`python -m unittest tests.test_topics`).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from topics import (  # noqa: E402
    QA_MARKER_SUFFIX,
    is_topic_id_shipped,
    is_topic_id_uploaded,
    next_topic_id_for_date,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _TempChannelRoot:
    """Context manager that builds a minimal channel root in a tmpdir."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.mkdtemp(prefix="shadowverse-topics-test-")
        root = Path(self._tmp)
        (root / "02_scripts" / "_drafts").mkdir(parents=True)
        return root

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


def _add_marker(channel_root: Path, topic_id: str) -> Path:
    masters = channel_root / "04_renders" / "_final_master"
    masters.mkdir(parents=True, exist_ok=True)
    marker = masters / f"{topic_id}{QA_MARKER_SUFFIX}"
    marker.touch()
    return marker


def _add_archive(channel_root: Path, topic_id: str) -> Path:
    year_month = topic_id[:7]  # "2026-05-08_001" -> "2026-05"
    archive = channel_root / "06_published" / year_month / topic_id
    archive.mkdir(parents=True)
    return archive


def _add_upload_log_row(channel_root: Path, topic_id: str) -> Path:
    log_path = channel_root / "01_research" / "upload_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as f:
        if is_new:
            f.write("uploaded_at,topic_id,video_id,url,privacy,title\n")
        f.write(
            f"2026-01-01T00:00:00+00:00,{topic_id},vid_{topic_id},"
            f"https://example,public,Test\n"
        )
    return log_path


def _add_draft_dir(channel_root: Path, topic_id: str) -> Path:
    d = channel_root / "02_scripts" / "_drafts" / topic_id
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# is_topic_id_shipped
# ---------------------------------------------------------------------------


class IsTopicIdShippedTests(unittest.TestCase):

    def test_returns_false_for_unshipped_topic(self):
        with _TempChannelRoot() as root:
            self.assertFalse(is_topic_id_shipped("2026-05-08_001", root))

    def test_qa_marker_signals_shipped(self):
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_shipped("2026-05-08_001", root))
            self.assertFalse(is_topic_id_shipped("2026-05-08_002", root))

    def test_archive_dir_signals_shipped(self):
        with _TempChannelRoot() as root:
            _add_archive(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_shipped("2026-05-08_001", root))

    def test_upload_log_row_signals_shipped(self):
        with _TempChannelRoot() as root:
            _add_upload_log_row(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_shipped("2026-05-08_001", root))
            self.assertFalse(is_topic_id_shipped("2026-05-08_002", root))

    def test_unrelated_upload_log_rows_do_not_match(self):
        with _TempChannelRoot() as root:
            _add_upload_log_row(root, "2026-05-08_001")
            _add_upload_log_row(root, "2026-05-08_005")
            self.assertFalse(is_topic_id_shipped("2026-05-08_002", root))

    def test_missing_paths_treated_as_unshipped(self):
        with _TempChannelRoot() as root:
            self.assertFalse(is_topic_id_shipped("2026-05-08_001", root))

    def test_malformed_topic_id_falls_back_to_marker_only(self):
        with _TempChannelRoot() as root:
            _add_marker(root, "garbage-id")
            self.assertTrue(is_topic_id_shipped("garbage-id", root))


# ---------------------------------------------------------------------------
# is_topic_id_uploaded — stricter than is_topic_id_shipped; excludes QA marker
# ---------------------------------------------------------------------------


class IsTopicIdUploadedTests(unittest.TestCase):
    """is_topic_id_uploaded only checks upload-evidence (archive + upload_log),
    NOT the gate-3 marker. The split exists so daily_batch.py doesn't reallocate
    mid-flight topics that have an operator-approved marker but no upload yet."""

    def test_returns_false_for_unuploaded_topic(self):
        with _TempChannelRoot() as root:
            self.assertFalse(is_topic_id_uploaded("2026-05-08_001", root))

    def test_qa_marker_does_NOT_signal_uploaded(self):
        """The critical distinction from is_topic_id_shipped."""
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-08_001")
            self.assertFalse(is_topic_id_uploaded("2026-05-08_001", root))
            # but is_topic_id_shipped should still return True for the marker
            self.assertTrue(is_topic_id_shipped("2026-05-08_001", root))

    def test_archive_dir_signals_uploaded(self):
        with _TempChannelRoot() as root:
            _add_archive(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_uploaded("2026-05-08_001", root))

    def test_upload_log_row_signals_uploaded(self):
        with _TempChannelRoot() as root:
            _add_upload_log_row(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_uploaded("2026-05-08_001", root))
            self.assertFalse(is_topic_id_uploaded("2026-05-08_002", root))

    def test_marker_plus_upload_returns_true(self):
        """When both marker AND upload exist, the topic is uploaded (post-ship state)."""
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-08_001")
            _add_upload_log_row(root, "2026-05-08_001")
            self.assertTrue(is_topic_id_uploaded("2026-05-08_001", root))

    def test_midflight_topic_with_marker_only_is_not_uploaded(self):
        """The 2026-05-10 scenario: gate-3 approved but not yet uploaded.

        Without the split fix, daily_batch would have reallocated this topic to
        a fresh ID after the operator dropped the marker, instead of letting the
        pipeline resume from gate 3.
        """
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-10_001")
            _add_draft_dir(root, "2026-05-10_001")
            # No archive, no upload_log row — mid-flight
            self.assertFalse(is_topic_id_uploaded("2026-05-10_001", root))
            # And next_topic_id_for_date for THE SAME DATE skips the seq because
            # the drafts dir exists (broader shipped check would also catch it)
            d = datetime(2026, 5, 10, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-10_002")


# ---------------------------------------------------------------------------
# next_topic_id_for_date
# ---------------------------------------------------------------------------


class NextTopicIdForDateTests(unittest.TestCase):

    def test_empty_channel_returns_seq_001(self):
        with _TempChannelRoot() as root:
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_001")

    def test_skips_existing_draft_dirs_for_same_date(self):
        with _TempChannelRoot() as root:
            _add_draft_dir(root, "2026-05-08_001")
            _add_draft_dir(root, "2026-05-08_002")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_003")

    def test_does_not_skip_other_date_drafts(self):
        with _TempChannelRoot() as root:
            _add_draft_dir(root, "2026-05-07_001")
            _add_draft_dir(root, "2026-05-07_002")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_001")

    def test_skips_shipped_via_qa_marker(self):
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-08_001")
            _add_marker(root, "2026-05-08_002")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_003")

    def test_skips_shipped_via_archive(self):
        with _TempChannelRoot() as root:
            _add_archive(root, "2026-05-08_001")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_002")

    def test_skips_shipped_via_upload_log(self):
        with _TempChannelRoot() as root:
            _add_upload_log_row(root, "2026-05-08_001")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_002")

    def test_skips_shipped_seq_when_draft_dir_deleted(self):
        """Marker exists for _001 but draft dir was relocated by cleanup_orphans."""
        with _TempChannelRoot() as root:
            _add_marker(root, "2026-05-08_001")
            d = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, d), "2026-05-08_002")

    def test_default_date_is_utc_not_local(self):
        """Calling without a date arg uses UTC, not local time.

        This is the regression-fix behavior: the old default was `datetime.now()`
        which returned local time and caused yesterday-LOCAL-prefixed allocations
        when daily_batch.py (UTC-keyed) ran late at night LOCAL.

        We can't pin the system clock here, but we can verify that:
          (a) the returned ID's date prefix matches today UTC, and
          (b) it is the same as passing `datetime.now(timezone.utc)` explicitly.
        """
        with _TempChannelRoot() as root:
            implicit = next_topic_id_for_date(root)
            explicit = next_topic_id_for_date(root, datetime.now(timezone.utc))
            self.assertEqual(implicit[:10], explicit[:10])
            self.assertEqual(implicit[:10], datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def test_regression_2026_05_08_local_drift(self):
        """Reproduce the 2026-05-08 picks-allocation regression in code.

        Original failing scenario:
          - daily_batch ran at LOCAL 2026-05-07 evening EDT (UTC 2026-05-08 early
            hours), so daily_dir = _daily_2026-05-08 (UTC) but the allocator's
            default-local clock was 2026-05-07
          - On a LATER run on 2026-05-08 mid-day UTC, picks_assignment held stale
            references to _07_005/_07_006 (now uploaded + scheduled)
          - Old allocator on date=2026-05-07 would return _07_007 (next after
            existing drafts _001..._006), but daily_batch was reusing the stale
            persisted IDs without checking shipped state

        The fix is twofold; this test pins the allocator half: even when called
        with an EARLIER LOCAL date, allocator must skip sequences that have
        shipped via marker / archive / upload_log.
        """
        with _TempChannelRoot() as root:
            # Mirror the 2026-05-07 prior-day state at the moment of the regression
            for n in range(1, 5):
                _add_draft_dir(root, f"2026-05-07_{n:03d}")
            for n in (3, 4, 5, 6):
                _add_marker(root, f"2026-05-07_{n:03d}")
            for n in (5, 6):
                _add_upload_log_row(root, f"2026-05-07_{n:03d}")

            # Even called with the buggy LOCAL date 2026-05-07, allocator must not
            # return _005 or _006 — they're shipped. _007 is the correct next slot.
            local_buggy = datetime(2026, 5, 7)
            self.assertEqual(next_topic_id_for_date(root, local_buggy), "2026-05-07_007")

            # Called with the correct UTC date 2026-05-08, allocator returns _001.
            utc_today = datetime(2026, 5, 8, tzinfo=timezone.utc)
            self.assertEqual(next_topic_id_for_date(root, utc_today), "2026-05-08_001")


if __name__ == "__main__":
    unittest.main()
