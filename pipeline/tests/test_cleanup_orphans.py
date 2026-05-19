"""Tests for tools/cleanup_orphans.py.

Uses stdlib ``unittest`` to avoid adding pytest as a dependency. Run via:
    python -m unittest tests.test_cleanup_orphans -v

All tests synthesize a fresh temp directory tree per test (via
``tempfile.TemporaryDirectory``) — no test ever touches the real
``02_scripts/_drafts/`` channel root.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Make the repo root importable so `tools.cleanup_orphans` resolves regardless
# of the cwd the test runner uses.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import cleanup_orphans  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_dir(parent: Path, name: str, *, age_hours: float, has_final: bool) -> Path:
    """Create a topic dir under ``parent`` with given mtime offset and FINAL flag."""
    d = parent / name
    d.mkdir()
    if has_final:
        (d / cleanup_orphans.FINAL_SCRIPT_NAME).write_text("ok", encoding="utf-8")
    target = time.time() - age_hours * 3600.0
    os.utime(d, (target, target))
    return d


# ---------------------------------------------------------------------------
# find_orphans
# ---------------------------------------------------------------------------


class TestFindOrphans(unittest.TestCase):
    """Coverage of the read-only orphan detector."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_picks_old_dirs_without_final(self) -> None:
        """Only the >48h dir without FINAL should qualify."""
        a = _make_dir(self.tmp_path, "topic_A", age_hours=72.0, has_final=False)
        _make_dir(self.tmp_path, "topic_B", age_hours=72.0, has_final=True)
        _make_dir(self.tmp_path, "topic_C", age_hours=1.0, has_final=False)
        (self.tmp_path / "_orphans").mkdir()

        result = cleanup_orphans.find_orphans(self.tmp_path, min_age_hours=48.0)
        self.assertEqual(result, [a])

    def test_min_age_threshold(self) -> None:
        """A near-zero threshold should pull in every non-`_` dir without FINAL."""
        a = _make_dir(self.tmp_path, "topic_A", age_hours=0.5, has_final=False)
        b = _make_dir(self.tmp_path, "topic_B", age_hours=2.0, has_final=False)
        # Excluded: has FINAL.
        _make_dir(self.tmp_path, "topic_C", age_hours=2.0, has_final=True)
        (self.tmp_path / "_orphans").mkdir()

        result = cleanup_orphans.find_orphans(self.tmp_path, min_age_hours=0.001)
        self.assertEqual(sorted(result), sorted([a, b]))

    def test_skips_underscore_prefixed(self) -> None:
        """Any dir name starting with `_` is infrastructure, never an orphan."""
        for name in ("_orphans", "_daily_2026-05-08", "_archive"):
            d = self.tmp_path / name
            d.mkdir()
            target = time.time() - 365 * 24 * 3600.0  # very old
            os.utime(d, (target, target))

        result = cleanup_orphans.find_orphans(self.tmp_path, min_age_hours=48.0)
        self.assertEqual(result, [])

    def test_skips_orphans_subdir_itself(self) -> None:
        """The `_orphans` move target dir must never be re-flagged as an orphan."""
        # Pre-existing orphan archive plus a real orphan candidate.
        orphans_root = self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR / "2026-04"
        orphans_root.mkdir(parents=True)
        (orphans_root / "topic_old").mkdir()
        a = _make_dir(self.tmp_path, "topic_A", age_hours=72.0, has_final=False)

        result = cleanup_orphans.find_orphans(self.tmp_path, min_age_hours=48.0)
        self.assertEqual(result, [a])

    def test_ignores_files(self) -> None:
        """Top-level files in drafts_dir must not appear in the result list."""
        stray = self.tmp_path / "stray.txt"
        stray.write_text("hello", encoding="utf-8")
        target = time.time() - 7 * 24 * 3600.0
        os.utime(stray, (target, target))

        a = _make_dir(self.tmp_path, "topic_A", age_hours=72.0, has_final=False)

        result = cleanup_orphans.find_orphans(self.tmp_path, min_age_hours=48.0)
        self.assertEqual(result, [a])
        self.assertNotIn(stray, result)

    def test_raises_on_missing_drafts_dir(self) -> None:
        missing = self.tmp_path / "does_not_exist"
        with self.assertRaises(FileNotFoundError):
            cleanup_orphans.find_orphans(missing)

    def test_raises_on_non_directory_drafts_arg(self) -> None:
        """Passing a file path (not a directory) must raise NotADirectoryError."""
        f = self.tmp_path / "not_a_dir.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(NotADirectoryError):
            cleanup_orphans.find_orphans(f)


# ---------------------------------------------------------------------------
# move_to_orphans
# ---------------------------------------------------------------------------


class TestMoveToOrphans(unittest.TestCase):
    """Coverage of the destructive move step."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.fixed_now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_target_subdir(self) -> None:
        """``move_to_orphans`` should create the YYYY-MM bucket and move the tree."""
        src = _make_dir(self.tmp_path, "topic_X", age_hours=72.0, has_final=False)
        (src / "marker.txt").write_text("payload", encoding="utf-8")

        moves = cleanup_orphans.move_to_orphans(
            [src], drafts_dir=self.tmp_path, now=self.fixed_now
        )

        expected_dst = (
            self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR / "2026-05" / "topic_X"
        )
        self.assertEqual(moves, [(src, expected_dst)])
        self.assertTrue(expected_dst.is_dir())
        self.assertEqual(
            (expected_dst / "marker.txt").read_text(encoding="utf-8"), "payload"
        )
        self.assertFalse(src.exists())

    def test_refuses_existing_target(self) -> None:
        """Pre-existing destination must raise FileExistsError; src untouched."""
        src = _make_dir(self.tmp_path, "topic_Y", age_hours=72.0, has_final=False)
        (src / "marker.txt").write_text("payload", encoding="utf-8")

        pre_existing = (
            self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR / "2026-05" / "topic_Y"
        )
        pre_existing.mkdir(parents=True)
        (pre_existing / "old.txt").write_text("already here", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            cleanup_orphans.move_to_orphans(
                [src], drafts_dir=self.tmp_path, now=self.fixed_now
            )

        # Source untouched.
        self.assertTrue(src.exists())
        self.assertEqual(
            (src / "marker.txt").read_text(encoding="utf-8"), "payload"
        )
        # Pre-existing dst untouched.
        self.assertEqual(
            (pre_existing / "old.txt").read_text(encoding="utf-8"), "already here"
        )

    def test_empty_list_is_noop(self) -> None:
        """Empty input yields an empty result; the bucket dir is created idempotently."""
        moves = cleanup_orphans.move_to_orphans(
            [], drafts_dir=self.tmp_path, now=self.fixed_now
        )
        self.assertEqual(moves, [])

    def test_target_dir_creation_failure_propagates(self) -> None:
        """If we cannot create the YYYY-MM target dir, the OSError must surface."""
        src = _make_dir(self.tmp_path, "topic_E", age_hours=72.0, has_final=False)

        with mock.patch(
            "pathlib.Path.mkdir", side_effect=OSError("simulated mkdir failure")
        ):
            with self.assertRaises(OSError):
                cleanup_orphans.move_to_orphans(
                    [src], drafts_dir=self.tmp_path, now=self.fixed_now
                )

        # Source preserved on failure.
        self.assertTrue(src.exists())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    """End-to-end CLI behavior, including the dry-run-by-default safety contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """Invoke ``cleanup_orphans.main`` and capture (rc, stdout, stderr)."""
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cleanup_orphans.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_dry_run_no_filesystem_change(self) -> None:
        """Default invocation (no --execute) must list orphans and change nothing."""
        a = _make_dir(self.tmp_path, "topic_A", age_hours=72.0, has_final=False)
        _make_dir(self.tmp_path, "topic_B", age_hours=72.0, has_final=True)

        rc, out, _ = self._run_cli(["--drafts-dir", str(self.tmp_path)])

        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn(str(a), out)
        # No move happened: original still exists, _orphans not created.
        self.assertTrue(a.exists())
        self.assertFalse((self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR).exists())

    def test_execute_moves_dirs(self) -> None:
        """With --execute the orphan dir is moved and an audit line is emitted."""
        src = _make_dir(self.tmp_path, "topic_Z", age_hours=72.0, has_final=False)
        (src / "payload.txt").write_text("hi", encoding="utf-8")
        # Re-stamp mtime: writing a child file just bumped the dir's mtime to now.
        target = time.time() - 72.0 * 3600.0
        os.utime(src, (target, target))

        rc, out, _ = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--execute"]
        )

        self.assertEqual(rc, 0)
        self.assertIn("moved 1 dirs", out)
        self.assertFalse(src.exists())

        bucket = datetime.now(timezone.utc).strftime("%Y-%m")
        dst = self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR / bucket / "topic_Z"
        self.assertTrue(dst.is_dir())
        self.assertEqual(
            (dst / "payload.txt").read_text(encoding="utf-8"), "hi"
        )

    def test_execute_refuses_collision(self) -> None:
        """--execute against a pre-existing destination must exit non-zero."""
        src = _make_dir(self.tmp_path, "topic_Q", age_hours=72.0, has_final=False)
        bucket = datetime.now(timezone.utc).strftime("%Y-%m")
        pre = self.tmp_path / cleanup_orphans.ORPHANS_SUBDIR / bucket / "topic_Q"
        pre.mkdir(parents=True)
        (pre / "old.txt").write_text("x", encoding="utf-8")
        # Re-stamp src mtime: defensive against fs jitter from the sibling mkdir.
        target = time.time() - 72.0 * 3600.0
        os.utime(src, (target, target))

        rc, _, _ = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--execute"]
        )
        self.assertEqual(rc, 1)
        # Source preserved on collision.
        self.assertTrue(src.exists())

    def test_execute_is_idempotent_after_clean_run(self) -> None:
        """A second --execute pass after the first should be a no-op (zero moves)."""
        _make_dir(self.tmp_path, "topic_I", age_hours=72.0, has_final=False)

        rc1, out1, _ = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--execute"]
        )
        self.assertEqual(rc1, 0)
        self.assertIn("moved 1 dirs", out1)

        rc2, out2, _ = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--execute"]
        )
        self.assertEqual(rc2, 0)
        self.assertIn("moved 0 dirs", out2)

    def test_quiet_sends_output_to_stderr(self) -> None:
        """--quiet routes the dry-run listing to stderr, leaving stdout clean."""
        _make_dir(self.tmp_path, "topic_A", age_hours=72.0, has_final=False)

        rc, out, err = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--quiet"]
        )

        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", err)
        self.assertNotIn("DRY RUN", out)

    def test_min_age_hours_threshold(self) -> None:
        """The --min-age-hours flag should be honored by the CLI."""
        fresh = _make_dir(
            self.tmp_path, "topic_fresh", age_hours=1.0, has_final=False
        )

        # Default 48h: fresh dir not listed.
        rc1, out_default, _ = self._run_cli(["--drafts-dir", str(self.tmp_path)])
        self.assertEqual(rc1, 0)
        self.assertIn("would move 0 dirs", out_default)
        self.assertNotIn(str(fresh), out_default)

        # Lower threshold: fresh dir is listed.
        rc2, out_low, _ = self._run_cli(
            ["--drafts-dir", str(self.tmp_path), "--min-age-hours", "0.001"]
        )
        self.assertEqual(rc2, 0)
        self.assertIn("would move 1 dirs", out_low)
        self.assertIn(str(fresh), out_low)

    def test_dry_run_skips_daily_pseudo_dirs(self) -> None:
        """`_daily_<DATE>` pseudo-topic dirs must never appear in the candidate list."""
        # Ancient _daily_ dir that would otherwise qualify.
        daily = self.tmp_path / "_daily_2026-05-08"
        daily.mkdir()
        target = time.time() - 365 * 24 * 3600.0
        os.utime(daily, (target, target))

        rc, out, _ = self._run_cli(["--drafts-dir", str(self.tmp_path)])

        self.assertEqual(rc, 0)
        self.assertIn("would move 0 dirs", out)
        self.assertNotIn(str(daily), out)


if __name__ == "__main__":
    unittest.main()
