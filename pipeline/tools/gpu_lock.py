"""Machine-wide GPU mutex — serialize GPU-heavy work across parallel pipelines.

Context (architecture review 2026-06-19, finding H-4)
-----------------------------------------------------
In the dual-video ``/start -auto`` shape, two sub-agents each run ``pipeline.py``
end-to-end. ``RenderLock`` serializes the render PER topic_id, but nothing stops
the two DIFFERENT topics from doing GPU work at the same time. On the operator's
RTX 4050 Mobile (6 GB VRAM), two ``faster-whisper large-v3`` loads (~3 GB each in
float16) plus two NVENC sessions can exhaust the card → OOM / thrash.

This is a coarse, machine-wide GPU mutex: only ONE topic does GPU work (caption
transcription + render) at a time across the whole machine. The non-GPU stages
(idea/script/fact-check/assets/TTS/loudnorm) of both topics still overlap freely;
only the GPU tail is serialized.

Design
------
* Same heartbeat + stale-steal mechanism as ``tools/render_lock.py`` (reuses its
  ``LockInfo`` / ``read_lock`` / ``lock_state`` helpers), so a crashed holder's
  lock is detected stale and stolen rather than deadlocking the other pipeline.
* The lock file lives in the system temp dir with a ``.lock`` suffix (NOT
  ``.render.lock``, and NOT in the masters dir), so ``tools/render_reaper.py`` —
  which scans the masters dir for ``*.render.lock`` and REPLAYS their argv —
  never mistakes the GPU lock for an orphaned render and re-runs anything.
* ``acquire`` BLOCKS until the GPU is free (unlike RenderLock, which fails fast):
  the holder will release after its bounded GPU span. After ``wait_budget_s`` of
  a genuinely-live, still-heartbeating holder it raises :class:`GpuLockBusy`
  (fail loud — something is wedged) rather than waiting forever.
* Opt-in via ``config.render.gpu_lock_enabled``; when off, :func:`gpu_lock_from_config`
  returns a no-op context so single-machine/legacy behavior is byte-for-byte
  unchanged. When on but uncontended (single video), acquire is instant.

Stdlib only — no third-party deps.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from tools.render_lock import (
    HEARTBEAT_INTERVAL_S,
    STALE_AFTER_S,
    LockInfo,
    lock_state,
    read_lock,
)

log = logging.getLogger("pipeline.gpu_lock")

# Default machine-wide lock location. One file per machine — both /start -auto
# sub-agents resolve the same path, so the mutex is shared across processes.
DEFAULT_GPU_LOCK_PATH = Path(tempfile.gettempdir()) / "shadowverse_gpu.lock"


class GpuLockBusy(RuntimeError):
    """Raised when the GPU is held by a live, heartbeating holder past the wait budget.

    Loud by design: rather than wait forever on a wedged holder, surface it so the
    operator/apex can investigate. A normal GPU span (whisper + NVENC) is a couple
    of minutes; hitting the (much larger) budget means something is stuck.
    """

    def __init__(self, topic_id: str, holder: LockInfo, waited_s: float):
        self.topic_id = topic_id
        self.holder = holder
        super().__init__(
            f"GPU is busy (held by pid={holder.pid} host={holder.hostname} "
            f"topic={holder.topic_id}); waited {waited_s:.0f}s without it releasing "
            f"or going stale. Refusing to run concurrent GPU work on a shared card. "
            f"Re-run once the in-flight GPU work finishes — a crashed holder is "
            f"auto-stolen after the staleness window."
        )


def _atomic_write(lock_path: Path, info: LockInfo) -> None:
    """Write the lock record atomically (temp sibling + os.replace).

    Mirrors render_lock's atomic write so a reader never sees a half-written file.
    Kept local (not imported from render_lock's private helper) so this module
    only depends on render_lock's PUBLIC surface.
    """
    tmp = lock_path.with_name(f"{lock_path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(info.to_dict()), encoding="utf-8")
    os.replace(tmp, lock_path)


class GpuLock:
    """Context manager held around the GPU-heavy span of one topic's pipeline.

    Usage::

        with GpuLock(lock_path, topic_id=tid):
            captions = generate_captions(...)
            master = render_master(...)

    Injectables (``clock``, ``sleep``, ``pid``, ``hostname``) exist for tests;
    production uses the real wall clock / process identity.
    """

    def __init__(
        self,
        lock_path: Path | str = DEFAULT_GPU_LOCK_PATH,
        *,
        topic_id: str,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
        wait_budget_s: float | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ):
        self.lock_path = Path(lock_path)
        self.topic_id = topic_id
        self.interval = float(heartbeat_interval_s)
        self.stale_after = float(stale_after_s)
        # Wait well beyond a normal GPU span (whisper + NVENC ≈ minutes) before
        # giving up on a live holder; a crashed holder is stolen at stale_after.
        self.wait_budget = (
            float(wait_budget_s) if wait_budget_s is not None
            else max(900.0, self.stale_after * 4)
        )
        self.pid = pid if pid is not None else os.getpid()
        self.hostname = hostname or socket.gethostname()
        self._clock = clock
        self._sleep = sleep
        self.log = logger or log
        self._started_at: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._held = False

    # -- acquire / release -------------------------------------------------

    def acquire(self) -> "GpuLock":
        waited = 0.0
        while True:
            existing = read_lock(self.lock_path)
            if existing is None:
                break
            st = lock_state(
                existing, now=self._clock(), stale_after=self.stale_after,
                clock=self._clock,
            )
            if st.orphaned:
                self.log.warning(
                    "gpu-lock: STEALING orphaned GPU lock (prior pid=%s host=%s "
                    "topic=%s — %s)",
                    existing.pid, existing.hostname, existing.topic_id, st.reason,
                )
                break
            if waited >= self.wait_budget:
                raise GpuLockBusy(self.topic_id, existing, waited)
            step = min(self.interval, self.wait_budget - waited)
            self.log.info(
                "gpu-lock: GPU busy (holder pid=%s topic=%s, heartbeat %.0fs ago); "
                "waiting %.0fs (%.0f/%.0f)...",
                existing.pid, existing.topic_id, st.age, step, waited, self.wait_budget,
            )
            self._sleep(step)
            waited += step

        self._started_at = self._clock()
        self._write()
        self._held = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"gpu-lock-hb-{self.topic_id}",
            daemon=True,
        )
        self._thread.start()
        self.log.info(
            "gpu-lock: acquired for topic_id=%s (pid=%s, heartbeat every %.0fs)",
            self.topic_id, self.pid, self.interval,
        )
        return self

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1.0)
            self._thread = None
        if not self._held:
            return
        # Only remove the lock if it is still ours (defends against a stealer).
        current = read_lock(self.lock_path)
        if current is not None and current.pid == self.pid and current.hostname == self.hostname:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log.warning("gpu-lock: could not remove %s: %s", self.lock_path, exc)
        self._held = False
        self.log.info("gpu-lock: released for topic_id=%s", self.topic_id)

    # -- internals ---------------------------------------------------------

    def _write(self) -> None:
        info = LockInfo(
            topic_id=self.topic_id,
            pid=self.pid,
            hostname=self.hostname,
            started_at=self._started_at,
            heartbeat_at=self._clock(),
            master_path=str(self.lock_path),  # not a render; reuse the field for debug
        )
        _atomic_write(self.lock_path, info)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except Exception:  # noqa: BLE001 — a heartbeat hiccup must not crash the run.
                self.log.exception("gpu-lock: heartbeat write failed for %s", self.topic_id)

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "GpuLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def gpu_lock_from_config(config: dict, topic_id: str):
    """Return a GpuLock context if ``render.gpu_lock_enabled`` is set, else a no-op.

    Off by default so single-machine / legacy behavior is unchanged. The lock path
    can be overridden via ``render.gpu_lock_path`` (defaults to a machine-wide file
    in the system temp dir).
    """
    rcfg = (config.get("render") or {}) if config else {}
    if not bool(rcfg.get("gpu_lock_enabled", False)):
        return contextlib.nullcontext()
    lock_path = rcfg.get("gpu_lock_path") or DEFAULT_GPU_LOCK_PATH
    return GpuLock(Path(lock_path), topic_id=topic_id)
