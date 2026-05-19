"""Unit tests for tools/archive_published.py.

All tests use a per-test temp directory (`tempfile.mkdtemp`) — no test ever touches
the real `05_exports/` or `06_published/` trees. Runnable under both `pytest` and
the stdlib `unittest` discovery (`python -m unittest tests.test_archive_published`).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# Make the repo root importable so `from tools.archive_published import ...` works
# regardless of where the test runner is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.archive_published import (  # noqa: E402
    PLATFORM_SUFFIX,
    archive_topic,
    backfill_all,
    main,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_variant(channel_root: Path, topic_id: str, platform: str, body: bytes) -> Path:
    """Create a fake `05_exports/<platform>/<topic_id>_<suffix>.mp4` with the given bytes."""
    suffix = PLATFORM_SUFFIX[platform]
    path = channel_root / "05_exports" / platform / f"{topic_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _make_qa_marker(channel_root: Path, topic_id: str) -> Path:
    """Create the gate-3 marker `04_renders/_final_master/<topic_id>_master_QA_APPROVED.marker`."""
    path = (
        channel_root
        / "04_renders"
        / "_final_master"
        / f"{topic_id}_master_QA_APPROVED.marker"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _make_master(channel_root: Path, topic_id: str) -> Path:
    """Create a placeholder `<topic_id>_master.mp4` next to the marker (mirrors pipeline layout)."""
    path = channel_root / "04_renders" / "_final_master" / f"{topic_id}_master.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"master")
    return path


class _ArchiveTestBase(unittest.TestCase):
    """Base class that wires a fresh tmp dir per test and cleans it up on teardown."""

    def setUp(self) -> None:
        self._tmp_root = Path(tempfile.mkdtemp(prefix="archive_test_"))
        self.channel_root = self._tmp_root / "channel"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# archive_topic — happy path + edge cases
# ---------------------------------------------------------------------------


class ArchiveTopicTests(_ArchiveTestBase):
    def test_archive_topic_copies_three_variants(self) -> None:
        topic_id = "2026-05-06_003"
        yt = _make_variant(self.channel_root, topic_id, "youtube", b"yt-bytes")
        tt = _make_variant(self.channel_root, topic_id, "tiktok", b"tt-bytes")
        ig = _make_variant(self.channel_root, topic_id, "instagram", b"ig-bytes")

        when = datetime(2026, 5, 6, 12, 0, 0)
        result = archive_topic(topic_id, channel_root=self.channel_root, when=when)

        expected_root = self.channel_root / "06_published" / "2026-05" / topic_id
        self.assertEqual(
            result,
            {
                "youtube": expected_root / "youtube" / f"{topic_id}_yt.mp4",
                "tiktok": expected_root / "tiktok" / f"{topic_id}_tt.mp4",
                "instagram": expected_root / "instagram" / f"{topic_id}_ig.mp4",
            },
        )
        for platform, dest in result.items():
            self.assertTrue(dest.exists(), f"{platform} dest missing: {dest}")
        # Bytes preserved (and source untouched — copy, not move).
        self.assertEqual(result["youtube"].read_bytes(), b"yt-bytes")
        self.assertEqual(result["tiktok"].read_bytes(), b"tt-bytes")
        self.assertEqual(result["instagram"].read_bytes(), b"ig-bytes")
        self.assertTrue(yt.exists() and tt.exists() and ig.exists(), "sources should not be moved")

    def test_archive_topic_missing_variant_raises_when_strict(self) -> None:
        topic_id = "2026-05-06_004"
        # Only yt + tt; instagram intentionally missing.
        _make_variant(self.channel_root, topic_id, "youtube", b"yt")
        _make_variant(self.channel_root, topic_id, "tiktok", b"tt")

        with self.assertRaises(FileNotFoundError) as ctx:
            archive_topic(topic_id, channel_root=self.channel_root, skip_missing=False)
        self.assertIn("instagram", str(ctx.exception))

    def test_archive_topic_skip_missing(self) -> None:
        topic_id = "2026-05-06_005"
        _make_variant(self.channel_root, topic_id, "youtube", b"yt")
        _make_variant(self.channel_root, topic_id, "tiktok", b"tt")
        # instagram missing on purpose

        when = datetime(2026, 5, 6, 12, 0, 0)
        result = archive_topic(
            topic_id, channel_root=self.channel_root, when=when, skip_missing=True
        )

        self.assertEqual(set(result.keys()), {"youtube", "tiktok"})
        self.assertNotIn("instagram", result)
        self.assertTrue(result["youtube"].exists())
        self.assertTrue(result["tiktok"].exists())

    def test_archive_refuses_overwrite_then_force(self) -> None:
        topic_id = "2026-05-06_006"
        _make_variant(self.channel_root, topic_id, "youtube", b"yt-v1")
        _make_variant(self.channel_root, topic_id, "tiktok", b"tt-v1")
        _make_variant(self.channel_root, topic_id, "instagram", b"ig-v1")

        when = datetime(2026, 5, 6, 12, 0, 0)
        first = archive_topic(topic_id, channel_root=self.channel_root, when=when)
        self.assertTrue(first["youtube"].exists())

        # Re-running without force must refuse.
        with self.assertRaises(FileExistsError):
            archive_topic(topic_id, channel_root=self.channel_root, when=when)

        # Now mutate sources, re-run with force=True, assert overwrite happened.
        _make_variant(self.channel_root, topic_id, "youtube", b"yt-v2")
        _make_variant(self.channel_root, topic_id, "tiktok", b"tt-v2")
        _make_variant(self.channel_root, topic_id, "instagram", b"ig-v2")
        second = archive_topic(
            topic_id, channel_root=self.channel_root, when=when, force=True
        )
        self.assertEqual(second["youtube"].read_bytes(), b"yt-v2")
        self.assertEqual(second["tiktok"].read_bytes(), b"tt-v2")
        self.assertEqual(second["instagram"].read_bytes(), b"ig-v2")

    def test_cli_force_round_trips_through_main(self) -> None:
        """CLI surface: --force flag must round-trip into the kwarg."""
        topic_id = "2026-05-06_007"
        _make_variant(self.channel_root, topic_id, "youtube", b"yt-v1")
        _make_variant(self.channel_root, topic_id, "tiktok", b"tt-v1")
        _make_variant(self.channel_root, topic_id, "instagram", b"ig-v1")

        cli_args = [
            "--topic-id", topic_id,
            "--channel-root", str(self.channel_root),
        ]
        self.assertEqual(main(cli_args), 0)
        # Second run without --force returns non-zero (FileExistsError caught and logged).
        self.assertEqual(main(cli_args), 1)
        # With --force returns 0 again.
        self.assertEqual(main(cli_args + ["--force"]), 0)


# ---------------------------------------------------------------------------
# backfill_all
# ---------------------------------------------------------------------------


class BackfillAllTests(_ArchiveTestBase):
    def test_backfill_picks_up_qa_markers(self) -> None:
        approved_topics = ["2026-05-05_001", "2026-05-06_001"]
        unapproved_topic = "2026-05-07_001"

        for tid in approved_topics:
            _make_master(self.channel_root, tid)
            _make_qa_marker(self.channel_root, tid)
            _make_variant(self.channel_root, tid, "youtube", b"yt")
            _make_variant(self.channel_root, tid, "tiktok", b"tt")
            _make_variant(self.channel_root, tid, "instagram", b"ig")

        # Third topic has a master but no marker -> should be skipped.
        _make_master(self.channel_root, unapproved_topic)
        _make_variant(self.channel_root, unapproved_topic, "youtube", b"yt")
        _make_variant(self.channel_root, unapproved_topic, "tiktok", b"tt")
        _make_variant(self.channel_root, unapproved_topic, "instagram", b"ig")

        when = datetime(2026, 5, 8, 12, 0, 0)
        results = backfill_all(self.channel_root, when=when)

        archived_ids = {r["topic_id"] for r in results if "archived" in r}
        self.assertEqual(archived_ids, set(approved_topics))
        self.assertNotIn(unapproved_topic, archived_ids)
        # No failures.
        self.assertTrue(all("archived" in r for r in results))
        # Destinations exist for every approved topic.
        for tid in approved_topics:
            yt_dest = (
                self.channel_root / "06_published" / "2026-05" / tid
                / "youtube" / f"{tid}_yt.mp4"
            )
            self.assertTrue(yt_dest.exists())

    def test_backfill_only_filter(self) -> None:
        topics = ["A_topic", "B_topic", "C_topic"]
        for tid in topics:
            _make_master(self.channel_root, tid)
            _make_qa_marker(self.channel_root, tid)
            _make_variant(self.channel_root, tid, "youtube", b"yt")
            _make_variant(self.channel_root, tid, "tiktok", b"tt")
            _make_variant(self.channel_root, tid, "instagram", b"ig")

        when = datetime(2026, 5, 8, 12, 0, 0)
        results = backfill_all(
            self.channel_root, when=when, only_topic_ids=["A_topic"]
        )

        archived_ids = {r["topic_id"] for r in results if "archived" in r}
        self.assertEqual(archived_ids, {"A_topic"})
        # B and C not archived
        for tid in ("B_topic", "C_topic"):
            self.assertFalse((self.channel_root / "06_published" / "2026-05" / tid).exists())

    def test_backfill_handles_missing_variants_gracefully(self) -> None:
        """skip_missing=True (backfill default) should let a partially-rendered topic still archive."""
        tid = "partial_topic"
        _make_qa_marker(self.channel_root, tid)
        _make_variant(self.channel_root, tid, "youtube", b"yt")
        # tt + ig missing on purpose

        when = datetime(2026, 5, 8, 12, 0, 0)
        results = backfill_all(self.channel_root, when=when)
        self.assertEqual(len(results), 1)
        self.assertIn("archived", results[0])
        self.assertEqual(set(results[0]["archived"].keys()), {"youtube"})


if __name__ == "__main__":
    unittest.main()
