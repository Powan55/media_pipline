"""Tests for the machine-wide GPU mutex (architecture review H-4).

GpuLock serializes the GPU span (whisper captions + NVENC render) across the two
parallel /start -auto topics so they don't exhaust the 6 GB card. It reuses
render_lock's heartbeat + stale-steal mechanism but blocks (rather than failing
fast) and lives outside the masters dir so the render reaper never touches it.

Injectables (clock / sleep) make the busy/stale paths deterministic without real
waiting; the heartbeat interval is set large where a background beat would race
an assertion.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import gpu_lock  # noqa: E402
from tools.gpu_lock import GpuLock, GpuLockBusy, gpu_lock_from_config  # noqa: E402
from tools.render_lock import LockInfo, read_lock  # noqa: E402

_HOST = socket.gethostname()


def _prewrite(lock_path: Path, *, topic_id: str, pid: int, heartbeat_at: float) -> None:
    gpu_lock._atomic_write(lock_path, LockInfo(
        topic_id=topic_id, pid=pid, hostname=_HOST,
        started_at=heartbeat_at, heartbeat_at=heartbeat_at, master_path=str(lock_path),
    ))


class TestAcquireRelease(unittest.TestCase):
    def test_acquire_when_free_creates_lock_release_removes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "gpu.lock"
            g = GpuLock(lp, topic_id="t1", heartbeat_interval_s=3600)
            g.acquire()
            info = read_lock(lp)
            self.assertIsNotNone(info)
            self.assertEqual(info.pid, os.getpid())
            self.assertEqual(info.topic_id, "t1")
            g.release()
            self.assertFalse(lp.exists())

    def test_context_manager_acquires_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "gpu.lock"
            with GpuLock(lp, topic_id="t1", heartbeat_interval_s=3600):
                self.assertTrue(lp.exists())
            self.assertFalse(lp.exists())


class TestStealAndBusy(unittest.TestCase):
    def test_steals_stale_lock(self) -> None:
        """An alive holder with an ancient heartbeat is stolen (staleness is the
        authority, independent of PID liveness)."""
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "gpu.lock"
            _prewrite(lp, topic_id="old", pid=os.getpid(), heartbeat_at=100.0)
            # Clock far in the future -> heartbeat age >> stale_after -> orphaned.
            g = GpuLock(lp, topic_id="new", heartbeat_interval_s=3600,
                        clock=lambda: 100_000.0, sleep=lambda s: None)
            g.acquire()
            try:
                info = read_lock(lp)
                self.assertEqual(info.topic_id, "new")  # stolen
                self.assertEqual(info.pid, os.getpid())
            finally:
                g.release()

    def test_busy_live_holder_raises_after_budget(self) -> None:
        """A fresh, live holder is waited on, then GpuLockBusy is raised once the
        wait budget is exhausted — never a silent double-acquire."""
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "gpu.lock"
            _prewrite(lp, topic_id="holder", pid=os.getpid(), heartbeat_at=1000.0)
            g = GpuLock(lp, topic_id="waiter", clock=lambda: 1000.0,
                        sleep=lambda s: None, wait_budget_s=2.0)
            with self.assertRaises(GpuLockBusy):
                g.acquire()
            # The holder's lock is untouched.
            self.assertEqual(read_lock(lp).topic_id, "holder")


class TestReleaseStealerDefense(unittest.TestCase):
    def test_release_does_not_remove_foreign_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "gpu.lock"
            g = GpuLock(lp, topic_id="mine", heartbeat_interval_s=3600)
            g.acquire()
            # A stealer overwrites the lock with a different pid/host.
            _prewrite_foreign = LockInfo(
                topic_id="thief", pid=os.getpid() + 1, hostname="otherhost",
                started_at=0.0, heartbeat_at=0.0, master_path=str(lp),
            )
            gpu_lock._atomic_write(lp, _prewrite_foreign)
            g.release()  # must NOT remove a lock that is no longer ours
            info = read_lock(lp)
            self.assertIsNotNone(info)
            self.assertEqual(info.topic_id, "thief")


class TestFromConfig(unittest.TestCase):
    def test_disabled_returns_nullcontext(self) -> None:
        for cfg in ({}, {"render": {}}, {"render": {"gpu_lock_enabled": False}}):
            ctx = gpu_lock_from_config(cfg, "t1")
            self.assertIsInstance(ctx, contextlib.nullcontext)
            with ctx:  # no-op, creates no file
                pass

    def test_enabled_returns_gpu_lock_at_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = str(Path(td) / "g.lock")
            ctx = gpu_lock_from_config(
                {"render": {"gpu_lock_enabled": True, "gpu_lock_path": lp}}, "t1",
            )
            self.assertIsInstance(ctx, GpuLock)
            self.assertEqual(str(ctx.lock_path), lp)
            with ctx:
                self.assertTrue(Path(lp).exists())
            self.assertFalse(Path(lp).exists())

    def test_enabled_default_path_is_machine_wide(self) -> None:
        ctx = gpu_lock_from_config({"render": {"gpu_lock_enabled": True}}, "t1")
        self.assertIsInstance(ctx, GpuLock)
        self.assertEqual(ctx.lock_path, gpu_lock.DEFAULT_GPU_LOCK_PATH)


if __name__ == "__main__":
    unittest.main()
