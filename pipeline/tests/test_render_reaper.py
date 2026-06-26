"""Unit tests for tools.render_reaper — orphaned-render detection + resume.

The reaper turns a silently-frozen render (cycle-24 / 2026-06-05 footgun) into
an explicit signal apex can branch on, and can replay an orphan's captured argv
foreground to drive it to completion. These tests use a fake runner so no real
pipeline.py / ffmpeg is invoked.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.render_lock import LockInfo, lock_path_for  # noqa: E402
from tools.render_reaper import (  # noqa: E402
    _channel_root_from_config,
    main,
    master_dir_for,
    reap,
)


def _write_lock(master_path: Path, info: LockInfo) -> None:
    lock_path_for(master_path).write_text(json.dumps(info.to_dict()), encoding="utf-8")


def _orphan_lock(master_path: Path, *, argv=None) -> None:
    _write_lock(master_path, LockInfo(
        topic_id=master_path.name.replace("_master.mp4", ""),
        pid=4242, hostname="ghost-host-not-this-machine",
        started_at=time.time() - 10_000, heartbeat_at=time.time() - 10_000,
        master_path=str(master_path), argv=argv,
    ))


def _live_lock(master_path: Path) -> None:
    _write_lock(master_path, LockInfo(
        topic_id=master_path.name.replace("_master.mp4", ""),
        pid=os.getpid(), hostname=socket.gethostname(),
        started_at=time.time(), heartbeat_at=time.time(),
        master_path=str(master_path),
    ))


class ReapDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reap-test-"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_dir_reports_no_orphans(self) -> None:
        report = reap(self.tmp)
        self.assertEqual(report["orphans"], [])

    def test_detects_orphan_but_not_live(self) -> None:
        _orphan_lock(self.tmp / "2026-06-05_001_master.mp4")
        _live_lock(self.tmp / "2026-06-05_002_master.mp4")
        report = reap(self.tmp, stale_after=45.0)
        ids = {o["topic_id"] for o in report["orphans"]}
        self.assertEqual(ids, {"2026-06-05_001"})


class ReapResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reap-resume-"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resume_replays_argv_and_clears_orphan(self) -> None:
        master = self.tmp / "2026-06-05_001_master.mp4"
        _orphan_lock(master, argv=["pipeline.py", "--topic-id", "2026-06-05_001",
                                    "--topic", "t", "--angle", "a", "--hook", "h"])
        calls: list[list[str]] = []

        def fake_runner(cmd, cwd=None):
            # Simulate the resumed pipeline.py: it steals the stale lock and,
            # on reaching gate-3, releases it (remove the lock file). Exit 3 ==
            # intentional halt after the render landed.
            calls.append(list(cmd))
            lock_path_for(master).unlink()
            return types.SimpleNamespace(returncode=3)

        report = reap(self.tmp, resume=True, stale_after=45.0, runner=fake_runner)
        self.assertEqual(len(calls), 1)
        # The captured argv was replayed under the recorded python executable.
        self.assertIn("--topic-id", calls[0])
        self.assertIn("2026-06-05_001", calls[0])
        self.assertTrue(report["resumed"][0]["ok"])
        self.assertEqual(report["remaining"], [])  # freeze cleared

    def test_resume_without_argv_is_flagged_not_silently_skipped(self) -> None:
        master = self.tmp / "2026-06-05_009_master.mp4"
        _orphan_lock(master, argv=None)  # older lock, no captured argv

        def fake_runner(cmd, cwd=None):  # pragma: no cover - must not be called
            raise AssertionError("runner should not be called without argv")

        report = reap(self.tmp, resume=True, stale_after=45.0, runner=fake_runner)
        self.assertFalse(report["resumed"][0]["resumed"])
        self.assertIn("argv", report["resumed"][0]["reason"])
        # Still orphaned — surfaced loudly, never silently dropped.
        self.assertEqual(report["remaining"], ["2026-06-05_009"])


class ReapCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reap-cli-"))
        self.master_dir = master_dir_for(self.tmp)
        self.master_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exit_zero_when_clean(self) -> None:
        rc = main(["--channel-root", str(self.tmp)])
        self.assertEqual(rc, 0)

    def test_exit_one_when_orphan_and_no_resume(self) -> None:
        _orphan_lock(self.master_dir / "2026-06-05_001_master.mp4")
        rc = main(["--channel-root", str(self.tmp), "--json"])
        self.assertEqual(rc, 1)

    def test_missing_config_returns_usage_error(self) -> None:
        rc = main(["--config", str(self.tmp / "does_not_exist.yaml")])
        self.assertEqual(rc, 2)

    def test_human_report_is_cp1252_console_safe(self) -> None:
        """Regression: the human report must not print a glyph the Windows
        console (cp1252) can't encode. A stray U+2714 once crashed the clean-run
        path with UnicodeEncodeError and a wrong exit code. Reproduce by routing
        stdout through a strict cp1252 writer."""
        import io

        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                               errors="strict", newline="")
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = main(["--channel-root", str(self.tmp)])  # clean (empty) dir
            buf.flush()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        printed = buf.buffer.getvalue().decode("cp1252")
        self.assertIn("No orphaned renders", printed)


class ChannelRootResolutionTests(unittest.TestCase):
    def test_reads_channel_root_from_yaml(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="reap-cfg-"))
        try:
            cfg = tmp / "config.yaml"
            cfg.write_text(
                "paths:\n  channel_root: C:/ContentOps/channels/ShadowVerse\n",
                encoding="utf-8",
            )
            root = _channel_root_from_config(cfg)
            self.assertEqual(root, Path("C:/ContentOps/channels/ShadowVerse"))
            self.assertEqual(master_dir_for(root).name, "_final_master")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
