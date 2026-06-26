"""Unit tests for tools.render_lock — the structural orphaned-render guard.

Context: 2026-06-02 cycle-24 + 2026-06-05 recurrence. Sub-agents in the
dual-video `/start -auto` shape backgrounded the render and yielded; the
detached encode was killed on return, freezing the video mid-flight with no
signal. The prompt-level "never background" prohibition failed twice, so the
fix is structural: a render lock + heartbeat + atomic `.part` write that make a
detached render impossible to *silently* orphan.

These tests assert the load-bearing invariants:
  * heartbeat staleness / dead-PID classification,
  * acquire/release round-trip + a live heartbeat that advances,
  * an orphaned lock is STOLEN and its partial cleaned (the self-heal),
  * a genuinely-live lock is NOT silently double-encoded (RenderLockBusy),
  * find_orphaned_locks surfaces only the orphans,
  * pipeline wiring: render_master promotes a `.part` atomically and the render
    is held under a RenderLock.

Runnable under pytest and stdlib unittest. No ffmpeg, no real channel root.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.render_lock import (  # noqa: E402
    LockInfo,
    RenderLock,
    RenderLockBusy,
    find_orphaned_locks,
    lock_path_for,
    lock_state,
    part_path_for,
    pid_alive,
    read_lock,
)


def _write_raw_lock(lock_path: Path, info: LockInfo) -> None:
    lock_path.write_text(json.dumps(info.to_dict()), encoding="utf-8")


def _ghost_lock(master_path: Path, *, heartbeat_at: float, argv=None) -> LockInfo:
    """A lock from a 'ghost' host so PID-liveness is skipped and staleness governs."""
    return LockInfo(
        topic_id=master_path.name.replace("_master.mp4", ""),
        pid=4242,
        hostname="ghost-host-not-this-machine",
        started_at=heartbeat_at,
        heartbeat_at=heartbeat_at,
        master_path=str(master_path),
        argv=argv,
    )


class PathHelperTests(unittest.TestCase):
    def test_lock_and_part_naming(self) -> None:
        master = Path("/x/04_renders/_final_master/2026-06-05_001_master.mp4")
        self.assertEqual(lock_path_for(master).name, "2026-06-05_001_master.mp4.render.lock")
        self.assertEqual(part_path_for(master).name, "2026-06-05_001_master.mp4.part")
        # Both live alongside the master so os.replace stays atomic (same volume).
        self.assertEqual(lock_path_for(master).parent, master.parent)
        self.assertEqual(part_path_for(master).parent, master.parent)


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertIs(pid_alive(os.getpid()), True)

    def test_nonpositive_pid_is_dead(self) -> None:
        self.assertIs(pid_alive(0), False)
        self.assertIs(pid_alive(-1), False)

    def test_reaped_child_is_not_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        # Provably dead (False) on both POSIX and Windows; tolerate None only if
        # the platform genuinely can't tell.
        self.assertIn(pid_alive(proc.pid), (False, None))


class LockStateTests(unittest.TestCase):
    def test_fresh_local_lock_is_live(self) -> None:
        master = Path(tempfile.gettempdir()) / "2026-06-05_001_master.mp4"
        info = LockInfo(
            topic_id="t", pid=os.getpid(), hostname=socket.gethostname(),
            started_at=1000.0, heartbeat_at=1000.0, master_path=str(master),
        )
        st = lock_state(info, now=1001.0, stale_after=45.0)
        self.assertFalse(st.orphaned)

    def test_stale_heartbeat_is_orphaned(self) -> None:
        master = Path(tempfile.gettempdir()) / "2026-06-05_001_master.mp4"
        # Current pid (alive) but heartbeat far in the past → staleness wins.
        info = LockInfo(
            topic_id="t", pid=os.getpid(), hostname=socket.gethostname(),
            started_at=0.0, heartbeat_at=0.0, master_path=str(master),
        )
        st = lock_state(info, now=1000.0, stale_after=45.0)
        self.assertTrue(st.orphaned)
        self.assertIn("stale", st.reason)

    def test_dead_pid_is_orphaned_even_with_fresh_heartbeat(self) -> None:
        master = Path(tempfile.gettempdir()) / "2026-06-05_001_master.mp4"
        info = LockInfo(
            topic_id="t", pid=0, hostname=socket.gethostname(),  # pid 0 → dead
            started_at=1000.0, heartbeat_at=1000.0, master_path=str(master),
        )
        st = lock_state(info, now=1000.0, stale_after=45.0)
        self.assertTrue(st.orphaned)
        self.assertIn("not alive", st.reason)

    def test_foreign_host_fresh_heartbeat_is_live(self) -> None:
        master = Path(tempfile.gettempdir()) / "2026-06-05_001_master.mp4"
        info = _ghost_lock(master, heartbeat_at=1000.0)
        st = lock_state(info, now=1001.0, stale_after=45.0)
        self.assertFalse(st.orphaned)  # can't probe foreign pid; heartbeat fresh


class RenderLockRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rl-test-"))
        self.master = self.tmp / "2026-06-05_001_master.mp4"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_writes_lock_then_release_removes_it(self) -> None:
        lock = RenderLock(
            self.master, topic_id="2026-06-05_001",
            argv=["pipeline.py", "--topic-id", "2026-06-05_001"],
            heartbeat_interval_s=0.05, stale_after_s=1.0,
        )
        lock.acquire()
        try:
            info = read_lock(lock_path_for(self.master))
            self.assertIsNotNone(info)
            self.assertEqual(info.topic_id, "2026-06-05_001")
            self.assertEqual(info.pid, os.getpid())
            self.assertEqual(info.argv, ["pipeline.py", "--topic-id", "2026-06-05_001"])
        finally:
            lock.release()
        self.assertFalse(lock_path_for(self.master).exists())

    def test_context_manager_releases(self) -> None:
        with RenderLock(self.master, topic_id="t", heartbeat_interval_s=0.05):
            self.assertTrue(lock_path_for(self.master).exists())
        self.assertFalse(lock_path_for(self.master).exists())

    def test_heartbeat_advances(self) -> None:
        lock = RenderLock(self.master, topic_id="t", heartbeat_interval_s=0.05,
                          stale_after_s=5.0)
        lock.acquire()
        try:
            first = read_lock(lock_path_for(self.master)).heartbeat_at
            time.sleep(0.25)  # several heartbeat intervals
            second = read_lock(lock_path_for(self.master)).heartbeat_at
            self.assertGreater(second, first, "heartbeat did not advance")
        finally:
            lock.release()

    def test_steals_orphaned_lock_and_cleans_partial(self) -> None:
        # Pre-stage a stale ghost lock + a leftover partial render.
        lock_path = lock_path_for(self.master)
        part_path = part_path_for(self.master)
        _write_raw_lock(lock_path, _ghost_lock(self.master, heartbeat_at=time.time() - 10_000))
        part_path.write_bytes(b"half-written encode")

        lock = RenderLock(self.master, topic_id="2026-06-05_001",
                          heartbeat_interval_s=0.05, stale_after_s=45.0)
        lock.acquire()
        try:
            # Partial was removed; lock is now ours.
            self.assertFalse(part_path.exists(), "stale .part not cleaned on steal")
            info = read_lock(lock_path)
            self.assertEqual(info.pid, os.getpid())
            self.assertEqual(info.hostname, socket.gethostname())
        finally:
            lock.release()

    def test_live_lock_is_not_double_encoded(self) -> None:
        # A genuinely-live holder: current pid + host, fresh heartbeat. The
        # second acquirer must refuse rather than silently double-encode.
        fixed_now = 1000.0
        lock_path = lock_path_for(self.master)
        live = LockInfo(
            topic_id="2026-06-05_001", pid=os.getpid(), hostname=socket.gethostname(),
            started_at=fixed_now, heartbeat_at=fixed_now, master_path=str(self.master),
        )
        _write_raw_lock(lock_path, live)

        contender = RenderLock(
            self.master, topic_id="2026-06-05_001",
            heartbeat_interval_s=0.05, stale_after_s=45.0, wait_budget_s=0.2,
            clock=lambda: fixed_now,       # heartbeat never ages → stays live
            sleep=lambda s: None,          # don't actually block the test
        )
        with self.assertRaises(RenderLockBusy):
            contender.acquire()
        # The original live lock is untouched.
        self.assertEqual(read_lock(lock_path).pid, os.getpid())


class FindOrphanedLocksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rl-scan-"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_only_orphans(self) -> None:
        live_master = self.tmp / "2026-06-05_001_master.mp4"
        dead_master = self.tmp / "2026-06-05_002_master.mp4"
        _write_raw_lock(
            lock_path_for(live_master),
            LockInfo(topic_id="2026-06-05_001", pid=os.getpid(),
                     hostname=socket.gethostname(), started_at=time.time(),
                     heartbeat_at=time.time(), master_path=str(live_master)),
        )
        _write_raw_lock(
            lock_path_for(dead_master),
            _ghost_lock(dead_master, heartbeat_at=time.time() - 10_000),
        )
        orphans = find_orphaned_locks(self.tmp, stale_after=45.0)
        ids = {info.topic_id for info, _ in orphans}
        self.assertEqual(ids, {"2026-06-05_002"})

    def test_empty_dir_is_clean(self) -> None:
        self.assertEqual(find_orphaned_locks(self.tmp), [])


class PipelineWiringTests(unittest.TestCase):
    """Source-level wiring: the integration points exist and are correct."""

    def test_master_output_path_helper(self) -> None:
        import pipeline
        config = {"paths": {"channel_root": "C:/ContentOps/channels/ShadowVerse"}}
        p = pipeline._master_output_path(config, "2026-06-05_001")
        self.assertEqual(p.name, "2026-06-05_001_master.mp4")
        self.assertEqual(p.parent.name, "_final_master")

    def test_render_master_promotes_part_atomically(self) -> None:
        src = (REPO_ROOT / "pipeline.py").read_text(encoding="utf-8")
        body = src[src.find("def render_master("):src.find("def _decode_smoke_check(")]
        self.assertIn("part_path = part_path_for(master_path)", body)
        self.assertIn("os.replace(part_path, master_path)", body,
                      "render_master must atomically promote the .part to the master")
        # ffmpeg must target the .part, never the canonical master directly.
        self.assertNotIn("str(master_path), **out_kwargs", body)

    def test_run_for_topic_holds_render_lock(self) -> None:
        src = (REPO_ROOT / "pipeline.py").read_text(encoding="utf-8")
        body = src[src.find("def run_for_topic("):src.find("def _check_media_integrity(")]
        self.assertIn("from tools.render_lock import RenderLock", body)
        self.assertIn("with RenderLock(", body)
        self.assertIn("_render_with_integrity_retry(", body)


if __name__ == "__main__":
    unittest.main()
