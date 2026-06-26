"""Render-orphan reaper — make a frozen/detached render LOUD and recoverable.

Companion to ``tools/render_lock.py``. Scans the master output dir for
``*.render.lock`` files whose render was orphaned (heartbeat stale or PID dead)
— the cycle-24 / 2026-06-05 footgun where a sub-agent backgrounded the render
and yielded, leaving the encode frozen mid-flight.

Two jobs:

1. **Detect** — turn a silent freeze into an explicit, machine-checkable
   signal. Exit code 1 (orphans present) / 0 (clean). ``--json`` emits a report
   apex can branch on.
2. **Resume** (``--resume``) — replay each orphan's captured launch argv
   FOREGROUND (blocking). The replayed ``pipeline.py`` invocation steals the
   stale lock, cleans the partial, and re-renders idempotently to completion.

Intended use in ``/start -auto``: after both sub-agents return, apex runs this
once. A clean run is a cheap no-op; an orphan is surfaced and (optionally)
driven to completion without a bespoke manual rescue.

    python tools/render_reaper.py                 # detect, exit 1 if any
    python tools/render_reaper.py --json          # machine-readable report
    python tools/render_reaper.py --resume        # detect + replay foreground

Stdlib + PyYAML only (PyYAML is already a pipeline dependency). Does NOT import
pipeline.py, so it stays runnable even if a heavier import is broken.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.render_lock import (  # noqa: E402
    STALE_AFTER_S,
    find_orphaned_locks,
    read_lock,
)

log = logging.getLogger("pipeline.render_reaper")


def _channel_root_from_config(config_path: Path) -> Path:
    """Read ``paths.channel_root`` straight from the YAML (no .env / validation)."""
    import yaml

    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    try:
        return Path(data["paths"]["channel_root"])
    except (KeyError, TypeError) as exc:
        raise KeyError(
            f"{config_path} has no paths.channel_root — cannot locate masters"
        ) from exc


def master_dir_for(channel_root: Path) -> Path:
    return Path(channel_root) / "04_renders" / "_final_master"


def _resume_one(
    entry: dict,
    *,
    runner: Callable[..., "subprocess.CompletedProcess"],
    logger: logging.Logger,
) -> dict:
    """Replay one orphaned render's captured argv foreground (blocking)."""
    argv = entry.get("argv")
    executable = entry.get("executable") or sys.executable
    cwd = entry.get("cwd")
    if not argv:
        logger.error(
            "reaper: orphan topic_id=%s has no captured argv — cannot auto-resume; "
            "resume manually with `pipeline.py --topic-id %s ...`",
            entry.get("topic_id"), entry.get("topic_id"),
        )
        return {"topic_id": entry.get("topic_id"), "resumed": False,
                "reason": "no captured argv"}
    cmd = [executable, *argv]
    logger.warning(
        "reaper: resuming orphaned render topic_id=%s FOREGROUND: %s (cwd=%s)",
        entry.get("topic_id"), " ".join(str(c) for c in cmd), cwd,
    )
    try:
        proc = runner(cmd, cwd=cwd)
    except OSError as exc:
        logger.error("reaper: failed to launch resume for %s: %s",
                     entry.get("topic_id"), exc)
        return {"topic_id": entry.get("topic_id"), "resumed": False,
                "reason": f"launch failed: {exc}"}
    rc = getattr(proc, "returncode", None)
    # exit 3 == intentional pipeline halt (e.g. gate-3) AFTER the render landed —
    # that is success from the reaper's POV (the render is no longer orphaned).
    ok = rc in (0, 3)
    return {"topic_id": entry.get("topic_id"), "resumed": True,
            "returncode": rc, "ok": ok, "cmd": [str(c) for c in cmd]}


def reap(
    master_dir: Path | str,
    *,
    resume: bool = False,
    stale_after: float = STALE_AFTER_S,
    now: float | None = None,
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    logger: logging.Logger | None = None,
) -> dict:
    """Find orphaned render locks and optionally resume them foreground.

    Returns a report dict: ``{master_dir, orphans[], resumed[], remaining[]}``.
    ``orphans`` is the pre-resume snapshot; ``remaining`` is re-scanned after a
    resume so callers can tell whether the freeze actually cleared.
    """
    logger = logger or log
    master_dir = Path(master_dir)
    found = find_orphaned_locks(master_dir, now=now, stale_after=stale_after)
    report: dict = {
        "master_dir": str(master_dir),
        "orphans": [],
        "resumed": [],
        "remaining": [],
    }
    for info, st in found:
        report["orphans"].append({
            "topic_id": info.topic_id,
            "pid": info.pid,
            "host": info.hostname,
            "reason": st.reason,
            "age_s": round(st.age, 1),
            "master_path": info.master_path,
            "argv": info.argv,
            "cwd": info.cwd,
            "executable": info.executable,
        })

    if report["orphans"]:
        logger.warning(
            "reaper: %d orphaned render(s) in %s: %s",
            len(report["orphans"]), master_dir,
            ", ".join(o["topic_id"] for o in report["orphans"]),
        )
    else:
        logger.info("reaper: no orphaned renders in %s", master_dir)

    if resume and report["orphans"]:
        for entry in report["orphans"]:
            report["resumed"].append(_resume_one(entry, runner=runner, logger=logger))
        # Re-scan: did the replay actually clear the freeze?
        remaining = find_orphaned_locks(master_dir, stale_after=stale_after)
        report["remaining"] = [info.topic_id for info, _ in remaining]
        if report["remaining"]:
            logger.error(
                "reaper: %d render(s) STILL orphaned after resume: %s",
                len(report["remaining"]), ", ".join(report["remaining"]),
            )

    return report


def _exit_code(report: dict, *, resume: bool) -> int:
    if not report["orphans"]:
        return 0
    if not resume:
        return 1
    return 1 if report["remaining"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShadowVerse render-orphan reaper")
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: <repo>/config.yaml)")
    parser.add_argument("--channel-root", default=None,
                        help="Override channel root (skips config read)")
    parser.add_argument("--stale-after", type=float, default=STALE_AFTER_S,
                        help=f"Seconds since last heartbeat before a lock is orphaned "
                             f"(default {STALE_AFTER_S:.0f})")
    parser.add_argument("--resume", action="store_true",
                        help="Replay each orphaned render's argv FOREGROUND (blocking)")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON on stdout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.channel_root:
            channel_root = Path(args.channel_root)
        else:
            config_path = Path(args.config) if args.config else REPO_ROOT / "config.yaml"
            channel_root = _channel_root_from_config(config_path)
    except (FileNotFoundError, KeyError) as exc:
        print(f"render_reaper: {exc}", file=sys.stderr)
        return 2

    report = reap(master_dir_for(channel_root), resume=args.resume,
                  stale_after=args.stale_after)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if not report["orphans"]:
            print("No orphaned renders. OK")
        else:
            print(f"Orphaned renders ({len(report['orphans'])}):")
            for o in report["orphans"]:
                print(f"  - {o['topic_id']}  pid={o['pid']} host={o['host']}  "
                      f"{o['reason']}  ({o['age_s']}s)")
            if args.resume:
                for r in report["resumed"]:
                    state = "ok" if r.get("ok") else f"FAILED ({r.get('reason') or r.get('returncode')})"
                    print(f"    resume {r['topic_id']}: {state}")
                if report["remaining"]:
                    print(f"  STILL orphaned after resume: {', '.join(report['remaining'])}")

    return _exit_code(report, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
