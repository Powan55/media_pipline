"""Render lock + heartbeat — structural guard against orphaned/detached renders.

Context (2026-06-02 cycle-24 + 2026-06-05 recurrence)
-----------------------------------------------------
In the dual-video ``/start -auto`` shape, two sub-agents each run
``pipeline.py`` end-to-end. The recurring footgun: a sub-agent launches the
render with ``run_in_background`` and YIELDS to await a completion
notification instead of blocking. When the sub-agent returns, the harness
kills its detached process tree mid-encode — the video freezes with a partial
file on disk and no signal, forcing an apex (main-loop) rescue.

The prompt-level prohibition ("never background the render") was tried and
**failed twice**. This module is the STRUCTURAL fix: it makes a
backgrounded/detached render impossible to *silently* orphan.

How it works
------------
* ``render_master`` encodes to ``<master>.mp4.part`` and atomically renames to
  the final name only on success — a killed render never leaves a file that
  looks finished (see ``part_path_for``).
* A :class:`RenderLock` is held around the render. A daemon thread rewrites a
  heartbeat timestamp into the lock file every few seconds. The lock records
  the PID, host, and the launching argv.
* On entry, if a prior lock exists:
    - **orphaned** (heartbeat stale OR the PID is provably dead) → log LOUDLY,
      delete the stale ``.part``, and steal the lock. This is the self-heal:
      the next foreground invocation resumes cleanly with no manual cleanup.
    - **live** (fresh heartbeat + live PID) → another render is genuinely in
      progress; wait up to a bounded budget, then steal if it goes stale or
      raise :class:`RenderLockBusy` — never silently double-encode.
* :func:`find_orphaned_locks` lets a reaper (``tools/render_reaper.py``) or
  the operator enumerate frozen renders and replay their captured argv.

Heartbeat staleness is the AUTHORITATIVE orphan signal (fully cross-platform).
PID-liveness is a best-effort fast-path that only ever *accelerates* a steal
when a process is provably dead; when liveness is unknown, staleness governs.
So a premature steal (which would risk a double-encode) cannot happen from an
uncertain liveness probe.

Stdlib only — no third-party deps, importable without touching ffmpeg/config.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("pipeline.render_lock")

# Tunables. Deliberately module-level constants (NOT config.yaml keys) so this
# guard never depends on — or risks touching — sacred config. render_master may
# pass overrides read from config.render.* with these as safe defaults.
HEARTBEAT_INTERVAL_S: float = 5.0
STALE_AFTER_S: float = 45.0
LOCK_SUFFIX: str = ".render.lock"
PART_SUFFIX: str = ".part"
LOCK_VERSION: int = 1


# ---------------------------------------------------------------------------
# Path helpers — render_master and the lock MUST agree on these names.
# ---------------------------------------------------------------------------


def lock_path_for(master_path: Path | str) -> Path:
    """``.../<topic_id>_master.mp4`` → ``.../<topic_id>_master.mp4.render.lock``."""
    master_path = Path(master_path)
    return master_path.with_name(master_path.name + LOCK_SUFFIX)


def part_path_for(master_path: Path | str) -> Path:
    """``.../<topic_id>_master.mp4`` → ``.../<topic_id>_master.mp4.part``.

    render_master encodes here and atomically promotes to the final name on
    success. A killed render leaves only the ``.part`` — never a half-written
    file at the canonical master path.
    """
    master_path = Path(master_path)
    return master_path.with_name(master_path.name + PART_SUFFIX)


# ---------------------------------------------------------------------------
# PID liveness — best-effort, cross-platform. Optional[bool]; None == unknown.
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool | None:
    """Best-effort liveness probe for ``pid`` on the LOCAL host.

    Returns True (alive), False (provably dead / no such pid), or None when we
    cannot tell. Callers MUST treat None conservatively (rely on heartbeat
    staleness) so an uncertain probe never triggers a premature steal.
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — alive.
            return True
        except OSError:
            return None
        return True
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return None


def _pid_alive_windows(pid: int) -> bool | None:
    """Windows liveness via OpenProcess + GetExitCodeProcess.

    Conservative: returns True on access-denied (the process exists), False
    only when the kernel reports no such PID or a concrete exit code, and None
    on any unexpected ctypes failure.
    """
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                return True  # exists, just not queryable by us
            if err == ERROR_INVALID_PARAMETER:
                return False  # no such process
            return None
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return None
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — liveness is best-effort; never raise.
        return None


# ---------------------------------------------------------------------------
# Lock record + (de)serialization
# ---------------------------------------------------------------------------


@dataclass
class LockInfo:
    """The on-disk lock record (atomic-replaced JSON)."""

    topic_id: str
    pid: int
    hostname: str
    started_at: float
    heartbeat_at: float
    master_path: str
    argv: list[str] | None = None
    cwd: str | None = None
    executable: str | None = None
    version: int = LOCK_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "topic_id": self.topic_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "master_path": self.master_path,
            "argv": self.argv,
            "cwd": self.cwd,
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LockInfo":
        return cls(
            topic_id=str(d.get("topic_id", "")),
            pid=int(d.get("pid", -1)),
            hostname=str(d.get("hostname", "")),
            started_at=float(d.get("started_at", 0.0)),
            heartbeat_at=float(d.get("heartbeat_at", 0.0)),
            master_path=str(d.get("master_path", "")),
            argv=list(d["argv"]) if d.get("argv") is not None else None,
            cwd=d.get("cwd"),
            executable=d.get("executable"),
            version=int(d.get("version", LOCK_VERSION)),
        )


def read_lock(lock_path: Path | str) -> LockInfo | None:
    """Read a lock file. Returns None if missing or unusable.

    Writes go through :func:`_write_lock_atomic` (write-temp + ``os.replace``),
    so a reader never observes a half-written file. A genuinely corrupt/foreign
    lock is logged and treated as absent.
    """
    lock_path = Path(lock_path)
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("render-lock: cannot read %s: %s", lock_path, exc)
        return None
    try:
        return LockInfo.from_dict(json.loads(raw))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("render-lock: ignoring unparseable lock %s: %s", lock_path, exc)
        return None


def _write_lock_atomic(lock_path: Path, info: LockInfo) -> None:
    tmp = lock_path.with_name(f"{lock_path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(info.to_dict()), encoding="utf-8")
    os.replace(tmp, lock_path)  # atomic on Windows + POSIX (same dir)


# ---------------------------------------------------------------------------
# Orphan classification
# ---------------------------------------------------------------------------


@dataclass
class LockState:
    orphaned: bool
    reason: str
    age: float  # seconds since last heartbeat


def lock_state(
    info: LockInfo,
    *,
    now: float | None = None,
    stale_after: float = STALE_AFTER_S,
    clock: Callable[[], float] = time.time,
) -> LockState:
    """Classify a lock as orphaned (recoverable) or live.

    Orphaned if the PID is *provably* dead (fast path, local host only) OR the
    heartbeat is older than ``stale_after``. Heartbeat staleness is the
    authority; PID-death only accelerates an otherwise-inevitable steal.
    """
    now = clock() if now is None else now
    age = now - info.heartbeat_at
    same_host = info.hostname == socket.gethostname()
    alive = pid_alive(info.pid) if same_host else None
    if alive is False:
        return LockState(True, f"pid {info.pid} not alive", age)
    if age > stale_after:
        return LockState(True, f"heartbeat stale ({age:.0f}s > {stale_after:.0f}s)", age)
    return LockState(False, "live", age)


def find_orphaned_locks(
    master_dir: Path | str,
    *,
    now: float | None = None,
    stale_after: float = STALE_AFTER_S,
    clock: Callable[[], float] = time.time,
) -> list[tuple[LockInfo, LockState]]:
    """Scan ``master_dir`` for ``*.render.lock`` files that are orphaned."""
    master_dir = Path(master_dir)
    now = clock() if now is None else now
    out: list[tuple[LockInfo, LockState]] = []
    if not master_dir.is_dir():
        return out
    for p in sorted(master_dir.glob(f"*{LOCK_SUFFIX}")):
        info = read_lock(p)
        if info is None:
            continue
        st = lock_state(info, now=now, stale_after=stale_after, clock=clock)
        if st.orphaned:
            out.append((info, st))
    return out


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RenderLockBusy(RuntimeError):
    """Raised when another render for this topic is genuinely in progress.

    Loud by design: a second invocation must never silently double-encode or
    silently no-op. Re-running after the in-flight render finishes (or goes
    stale and is auto-stolen) succeeds.
    """

    def __init__(self, topic_id: str, holder: LockInfo, waited_s: float):
        self.topic_id = topic_id
        self.holder = holder
        super().__init__(
            f"render for topic_id={topic_id} is already in progress "
            f"(pid={holder.pid} host={holder.hostname}); waited {waited_s:.0f}s "
            f"without it completing or going stale. Refusing to double-encode. "
            f"Re-run once the in-flight render finishes — if it was orphaned it "
            f"will be auto-stolen after the staleness window."
        )


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


class RenderLock:
    """Context manager held around a render to make orphaning non-silent.

    Usage::

        with RenderLock(master_path, topic_id=tid, argv=sys.argv):
            master = _render_with_integrity_retry(...)

    Injectables (``clock``, ``sleep``, ``pid``, ``hostname``) exist for tests;
    production uses the real wall clock / process identity.
    """

    def __init__(
        self,
        master_path: Path | str,
        *,
        topic_id: str,
        argv: list[str] | None = None,
        cwd: str | None = None,
        executable: str | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
        wait_budget_s: float | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ):
        self.master_path = Path(master_path)
        self.lock_path = lock_path_for(self.master_path)
        self.part_path = part_path_for(self.master_path)
        self.topic_id = topic_id
        self.argv = list(argv) if argv is not None else None
        self.cwd = cwd
        self.executable = executable
        self.interval = float(heartbeat_interval_s)
        self.stale_after = float(stale_after_s)
        self.wait_budget = (
            float(wait_budget_s)
            if wait_budget_s is not None
            else self.stale_after + 2 * self.interval
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

    def acquire(self) -> "RenderLock":
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
                    "render-lock: STEALING orphaned render lock for topic_id=%s "
                    "(prior pid=%s host=%s — %s). Cleaning partial render and "
                    "resuming.",
                    existing.topic_id, existing.pid, existing.hostname, st.reason,
                )
                self._cleanup_partial()
                break
            # Live holder — wait, then steal-if-stale or refuse. Never
            # silently double-encode.
            if waited >= self.wait_budget:
                raise RenderLockBusy(self.topic_id, existing, waited)
            step = min(self.interval, self.wait_budget - waited)
            self.log.info(
                "render-lock: topic_id=%s render appears in progress "
                "(pid=%s, last heartbeat %.0fs ago); waiting %.0fs (%.0f/%.0f)...",
                existing.topic_id, existing.pid, st.age, step,
                waited, self.wait_budget,
            )
            self._sleep(step)
            waited += step

        self._started_at = self._clock()
        self._write()
        self._held = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"render-lock-hb-{self.topic_id}",
            daemon=True,
        )
        self._thread.start()
        self.log.info(
            "render-lock: acquired for topic_id=%s (pid=%s, heartbeat every %.0fs)",
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
                self.log.warning("render-lock: could not remove %s: %s", self.lock_path, exc)
        self._held = False
        self.log.info("render-lock: released for topic_id=%s", self.topic_id)

    # -- internals ---------------------------------------------------------

    def _cleanup_partial(self) -> None:
        try:
            if self.part_path.exists():
                self.part_path.unlink()
                self.log.warning("render-lock: removed stale partial render %s", self.part_path)
        except OSError as exc:
            self.log.warning("render-lock: could not remove partial %s: %s", self.part_path, exc)

    def _write(self) -> None:
        info = LockInfo(
            topic_id=self.topic_id,
            pid=self.pid,
            hostname=self.hostname,
            started_at=self._started_at,
            heartbeat_at=self._clock(),
            master_path=str(self.master_path),
            argv=self.argv,
            cwd=self.cwd,
            executable=self.executable,
        )
        _write_lock_atomic(self.lock_path, info)

    def _heartbeat_loop(self) -> None:
        # Event.wait(interval) returns True when release() sets the stop event.
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except Exception:  # noqa: BLE001 — a heartbeat hiccup must not crash the render.
                self.log.exception("render-lock: heartbeat write failed for %s", self.topic_id)

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "RenderLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
