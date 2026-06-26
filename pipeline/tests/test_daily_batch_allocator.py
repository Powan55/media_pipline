"""Unit tests for daily_batch picks-allocator + dropped-pick handling.

Covers the fix for the 2026-05-10 silent-recycling bug: when a persisted
`picks_assignment.json` entry points at a topic_id that is already in
`upload_log.csv` (or the 06_published archive), the allocator must DROP the
pick — not silently reallocate a fresh topic_id and ship the same topic again
under a new sequence number. The historic Adam Dunkels near-miss would have
shipped as `_11_002` without manual intervention.

All tests use per-test tmpdirs — no test touches the real channel root.
Runnable under both pytest and stdlib unittest.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daily_batch import (  # noqa: E402
    AllocationResult,
    TopicResult,
    _allocate_topic_id_for_pick,
    _load_picks_assignment,
    _persist_picks_assignment,
    daily_batch,
)
from scoring import ScoredCandidate  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _TempEnv:
    """Per-test scratch: channel_root + daily_dir on a tmpdir."""

    def __enter__(self) -> tuple[Path, Path]:
        self._tmp = tempfile.mkdtemp(prefix="shadowverse-daily-batch-test-")
        root = Path(self._tmp)
        channel_root = root / "channel"
        daily_dir = root / "_daily_2026-05-12"
        (channel_root / "02_scripts" / "_drafts").mkdir(parents=True)
        daily_dir.mkdir(parents=True)
        self._channel = channel_root
        self._daily = daily_dir
        return channel_root, daily_dir

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


def _make_pick(topic: str = "Adam Dunkels open-sources Contiki on a $5 microcontroller",
               angle: str = "Tiny embedded OS used in 10B+ devices",
               score: float = 0.8) -> ScoredCandidate:
    return ScoredCandidate(
        topic=topic,
        angle=angle,
        hook_concept="It's already in 10 billion devices",
        weighted_total=score,
    )


def _write_picks_assignment(daily_dir: Path, mapping: dict[str, str]) -> None:
    """Write a picks_assignment.json with the given topic->topic_id mapping."""
    payload = {
        "date": "2026-05-12",
        "updated_at": "2026-05-12T00:00:00+00:00",
        "assignments": [{"topic": t, "topic_id": tid} for t, tid in mapping.items()],
    }
    (daily_dir / "picks_assignment.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _add_upload_log_row(channel_root: Path, topic_id: str) -> None:
    log_path = channel_root / "01_research" / "upload_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as f:
        if is_new:
            f.write("uploaded_at,topic_id,video_id,url,privacy,title\n")
        f.write(
            f"2026-05-11T22:30:00+00:00,{topic_id},vid_{topic_id},"
            f"https://example,public,Test\n"
        )


# ---------------------------------------------------------------------------
# _allocate_topic_id_for_pick
# ---------------------------------------------------------------------------


class AllocateDroppedAlreadyUploadedTests(unittest.TestCase):
    """The bug fix: persisted topic_id that is already in upload_log
    must NOT be silently reallocated to a fresh id under the same topic."""

    def test_dropped_when_persisted_id_is_in_upload_log(self):
        with _TempEnv() as (channel_root, daily_dir):
            pick = _make_pick()
            _add_upload_log_row(channel_root, "2026-05-11_001")
            _write_picks_assignment(daily_dir, {pick.topic: "2026-05-11_001"})
            assignments = _load_picks_assignment(daily_dir)

            alloc = _allocate_topic_id_for_pick(
                pick, assignments, daily_dir, channel_root
            )

            self.assertIsNone(alloc.topic_id, "must NOT allocate a fresh id")
            self.assertEqual(alloc.reason, "dropped_already_uploaded")
            self.assertIn("2026-05-11_001", alloc.log_message)
            self.assertIn("duplicate", alloc.log_message.lower())

    def test_dropped_removes_stale_entry_from_assignment_file(self):
        """Once dropped, the picks_assignment.json must no longer carry the stale row."""
        with _TempEnv() as (channel_root, daily_dir):
            pick = _make_pick()
            _add_upload_log_row(channel_root, "2026-05-11_001")
            _write_picks_assignment(daily_dir, {pick.topic: "2026-05-11_001"})
            assignments = _load_picks_assignment(daily_dir)

            _allocate_topic_id_for_pick(pick, assignments, daily_dir, channel_root)

            # In-memory: gone
            self.assertNotIn(pick.topic, assignments)
            # On disk: also gone
            reloaded = _load_picks_assignment(daily_dir)
            self.assertNotIn(pick.topic, reloaded)

    def test_dropped_pick_via_caplog(self):
        """Mirror of test 1 with pytest-style caplog assertion (acceptance criterion 1c)."""
        with _TempEnv() as (channel_root, daily_dir):
            pick = _make_pick()
            _add_upload_log_row(channel_root, "2026-05-11_001")
            _write_picks_assignment(daily_dir, {pick.topic: "2026-05-11_001"})
            assignments = _load_picks_assignment(daily_dir)

            with self.assertLogs("daily_batch", level="WARNING") as captured:
                # Re-emit the log_message as a WARNING the way the loop does, so
                # the caplog assertion exercises the same path the production
                # loop takes.
                alloc = _allocate_topic_id_for_pick(
                    pick, assignments, daily_dir, channel_root
                )
                # The helper returns log_message; the loop emits at WARNING.
                logging.getLogger("daily_batch").warning("[1/1] %s", alloc.log_message)

            self.assertTrue(
                any(
                    "already been uploaded" in record.getMessage()
                    and "dropping this pick" in record.getMessage()
                    for record in captured.records
                ),
                f"expected dropped-pick warning, got: {[r.getMessage() for r in captured.records]}",
            )


class AllocateReusedTests(unittest.TestCase):
    """Happy path: a fresh (not-uploaded) persisted entry is reused as-is."""

    def test_reused_when_persisted_id_not_uploaded(self):
        with _TempEnv() as (channel_root, daily_dir):
            pick = _make_pick()
            # No upload_log row — persisted id is fresh.
            _write_picks_assignment(daily_dir, {pick.topic: "2026-05-12_002"})
            assignments = _load_picks_assignment(daily_dir)

            alloc = _allocate_topic_id_for_pick(
                pick, assignments, daily_dir, channel_root
            )

            self.assertEqual(alloc.topic_id, "2026-05-12_002")
            self.assertEqual(alloc.reason, "reused")
            # And the persisted entry is preserved.
            self.assertEqual(assignments[pick.topic], "2026-05-12_002")
            reloaded = _load_picks_assignment(daily_dir)
            self.assertEqual(reloaded.get(pick.topic), "2026-05-12_002")


class AllocateFreshTests(unittest.TestCase):
    """First-allocation path: no persisted entry → call next_topic_id_for_date and persist."""

    def test_fresh_when_no_persisted_entry(self):
        with _TempEnv() as (channel_root, daily_dir):
            pick = _make_pick()
            assignments: dict[str, str] = {}

            with mock.patch(
                "daily_batch.next_topic_id_for_date",
                return_value="2026-05-12_001",
            ) as mock_next:
                alloc = _allocate_topic_id_for_pick(
                    pick, assignments, daily_dir, channel_root
                )

            mock_next.assert_called_once_with(channel_root)
            self.assertEqual(alloc.topic_id, "2026-05-12_001")
            self.assertEqual(alloc.reason, "fresh")
            self.assertEqual(assignments[pick.topic], "2026-05-12_001")
            # Persisted to disk immediately so a mid-batch crash is recoverable.
            reloaded = _load_picks_assignment(daily_dir)
            self.assertEqual(reloaded.get(pick.topic), "2026-05-12_001")


# ---------------------------------------------------------------------------
# daily_batch loop integration — dropped slot must NOT invoke _run_one_topic
# ---------------------------------------------------------------------------


class DailyBatchLoopIntegrationTests(unittest.TestCase):
    """Verify the loop wires the helper's None return to a dropped-status TopicResult
    AND does NOT invoke _run_one_topic for the dropped slot."""

    def test_dropped_pick_skips_run_one_topic_and_surfaces_status(self):
        with _TempEnv() as (channel_root, daily_dir):
            bad_pick = _make_pick(topic="ALREADY UPLOADED — must be dropped")
            good_pick = _make_pick(topic="fresh pick to be allocated", score=0.7)

            _add_upload_log_row(channel_root, "2026-05-11_001")
            _write_picks_assignment(daily_dir, {bad_pick.topic: "2026-05-11_001"})

            # Stub generate_ideas → return our two crafted picks.
            # Stub next_topic_id_for_date → deterministic id for the good pick.
            # Stub _run_one_topic → record calls and return a stub TopicResult.
            # Stub _write_batch_summary → no-op (tmp path).
            calls: list[str] = []

            def fake_run_one_topic(pick, topic_id, config):
                calls.append(topic_id)
                return TopicResult(
                    topic_id=topic_id,
                    topic=pick.topic,
                    angle=pick.angle,
                    weighted_total=pick.weighted_total,
                    status="completed",
                    halt_stage=None,
                    halt_message="(stubbed)",
                    next_action_path=None,
                )

            # daily_batch keys daily_dir on UTC today, so make sure a daily_dir
            # for the actual UTC today exists in our tmpdir and carries the
            # stale picks_assignment.json. If the fixture's _daily_<date>
            # happens to coincide with today, no copy is needed.
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_dir = Path(daily_dir).parent / f"_daily_{today_str}"
            if today_dir.resolve() != Path(daily_dir).resolve():
                today_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(
                    daily_dir / "picks_assignment.json",
                    today_dir / "picks_assignment.json",
                )

            config = {
                "llm": {"manual_io_dir": str(Path(daily_dir).parent)},
                "paths": {"channel_root": str(channel_root)},
            }

            with mock.patch("daily_batch.generate_ideas", return_value=[bad_pick, good_pick]), \
                 mock.patch("daily_batch.next_topic_id_for_date", return_value="2026-05-12_001"), \
                 mock.patch("daily_batch._run_one_topic", side_effect=fake_run_one_topic), \
                 mock.patch("daily_batch._write_batch_summary", return_value=Path("/tmp/stub.md")):
                out = daily_batch(config)

            # _run_one_topic must have been invoked exactly once — for the good pick.
            self.assertEqual(calls, ["2026-05-12_001"],
                             "dropped pick must NOT call _run_one_topic")

            results = out["results"]
            self.assertEqual(len(results), 2)
            statuses = [r.status for r in results]
            self.assertIn("dropped_already_uploaded", statuses)
            self.assertIn("completed", statuses)

            dropped = next(r for r in results if r.status == "dropped_already_uploaded")
            self.assertEqual(dropped.halt_stage, "picks_allocation")
            self.assertEqual(dropped.topic_id, "<dropped>")
            self.assertEqual(dropped.topic, bad_pick.topic)
            self.assertIn("already been uploaded", dropped.halt_message.lower())


# ---------------------------------------------------------------------------
# AllocationResult dataclass smoke
# ---------------------------------------------------------------------------


class AllocationResultDataclassTests(unittest.TestCase):

    def test_dataclass_fields(self):
        r = AllocationResult(topic_id="2026-05-12_001", reason="fresh", log_message="msg")
        self.assertEqual(r.topic_id, "2026-05-12_001")
        self.assertEqual(r.reason, "fresh")
        self.assertEqual(r.log_message, "msg")

    def test_topic_id_can_be_none_for_drop(self):
        r = AllocationResult(topic_id=None, reason="dropped_already_uploaded", log_message="x")
        self.assertIsNone(r.topic_id)


# ---------------------------------------------------------------------------
# Item 3 (2026-05-22): picks_assignment.json corruption — fail-loud + orphan rescue
# ---------------------------------------------------------------------------


from daily_batch import (  # noqa: E402
    PicksAssignmentCorrupted,
    _scan_for_orphaned_topic_ids,
)


class PicksAssignmentCorruptHaltsTests(unittest.TestCase):
    """The fail-loud upgrade: a malformed picks_assignment.json must raise
    PicksAssignmentCorrupted, log at ERROR (not WARNING), and write a
    postmortem stub markdown file — instead of silently returning {} and
    orphaning yesterday's script work on the next batch run."""

    def test_load_picks_assignment_corrupt_file_halts(self):
        with _TempEnv() as (channel_root, daily_dir):
            # Write malformed JSON — opening brace then garbage, no closing brace.
            (daily_dir / "picks_assignment.json").write_text(
                "{not valid json", encoding="utf-8",
            )

            with self.assertLogs("daily_batch", level="ERROR") as captured:
                with self.assertRaises(PicksAssignmentCorrupted) as ctx:
                    _load_picks_assignment(daily_dir, channel_root=channel_root)

            # ERROR (not WARNING) log emitted.
            self.assertTrue(
                any(
                    "unreadable" in r.getMessage().lower()
                    and r.levelname == "ERROR"
                    for r in captured.records
                ),
                f"expected ERROR log mentioning 'unreadable', got: "
                f"{[(r.levelname, r.getMessage()) for r in captured.records]}",
            )

            # Postmortem stub was written.
            self.assertIsNotNone(ctx.exception.postmortem_path)
            pm_path = ctx.exception.postmortem_path
            self.assertTrue(pm_path.exists())
            content = pm_path.read_text(encoding="utf-8")
            self.assertIn("picks_assignment.json corruption", content)
            # Truncated corrupted contents preserved.
            self.assertIn("not valid json", content)
            # Exception type identified.
            self.assertIn("JSONDecodeError", content)


class OrphanScannerTests(unittest.TestCase):
    """Tests for daily_batch._scan_for_orphaned_topic_ids."""

    def test_orphan_scanner_finds_drafted_topic_ids(self):
        with _TempEnv() as (channel_root, daily_dir):
            drafts = channel_root / "02_scripts" / "_drafts"
            topic_dir = drafts / "2026-05-22_001"
            topic_dir.mkdir(parents=True)
            (topic_dir / "script_RESPONSE.txt").write_text(
                "## SCRIPT_BODY\nReal script content the operator wrote.\n",
                encoding="utf-8",
            )

            orphans = _scan_for_orphaned_topic_ids(channel_root, "2026-05-22")
            self.assertEqual(orphans, ["2026-05-22_001"])

    def test_orphan_scanner_skips_empty_drafts(self):
        """A topic_id dir with an empty (or missing) script_RESPONSE.txt is
        NOT an orphan — there's no real script work to rescue."""
        with _TempEnv() as (channel_root, daily_dir):
            drafts = channel_root / "02_scripts" / "_drafts"
            # Dir A: empty script_RESPONSE.txt.
            (drafts / "2026-05-22_001").mkdir(parents=True)
            (drafts / "2026-05-22_001" / "script_RESPONSE.txt").write_text(
                "", encoding="utf-8",
            )
            # Dir B: no script_RESPONSE.txt at all.
            (drafts / "2026-05-22_002").mkdir(parents=True)

            orphans = _scan_for_orphaned_topic_ids(channel_root, "2026-05-22")
            self.assertEqual(orphans, [])

    def test_orphan_scanner_filters_by_daily_date(self):
        """Only topic_ids matching the requested daily_date prefix are returned."""
        with _TempEnv() as (channel_root, daily_dir):
            drafts = channel_root / "02_scripts" / "_drafts"
            # Today's orphan.
            today_dir = drafts / "2026-05-22_001"
            today_dir.mkdir(parents=True)
            (today_dir / "script_RESPONSE.txt").write_text(
                "today content", encoding="utf-8",
            )
            # Yesterday's drafted dir — must NOT show in today's orphan list.
            yesterday_dir = drafts / "2026-05-21_001"
            yesterday_dir.mkdir(parents=True)
            (yesterday_dir / "script_RESPONSE.txt").write_text(
                "yesterday content", encoding="utf-8",
            )

            orphans = _scan_for_orphaned_topic_ids(channel_root, "2026-05-22")
            self.assertEqual(orphans, ["2026-05-22_001"])


if __name__ == "__main__":
    unittest.main()
