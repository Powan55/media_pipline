"""Cold-archive media-integrity sweep — periodic guard over the master vault.

Context (2026-06-07)
--------------------
The archived master ``2026-05-22_002_master.mp4`` was found truncated
(5.25 MB vs ~28 MB, "moov atom not found"). It had been corrupted IN PLACE
~10 minutes AFTER its gate-3 approval (master mtime 2:43 vs marker mtime 2:33)
and then sat undetected for ~2 weeks. It was recovered losslessly by remuxing
the surviving YouTube stream-copy variant back into the master.

The gap that let it rot silently: ``tools.media_integrity`` runs only as a
render -> export gate, so once a master lands in ``04_renders/_final_master/``
nothing ever looks at it again. Silent truncation / bit-rot in the cold archive
goes unnoticed for weeks.

This tool fills that gap. It REUSES ``tools.media_integrity.check_integrity``
(no re-implemented ffprobe/ffmpeg logic) over every ``*.mp4`` master, prints a
PASS / FAIL report, and exits non-zero if any file fails — suitable for a
Windows Scheduled Task. With ``--include-published`` it also sweeps the
``06_published`` cold backups.

Recovery (--auto-recover)
-------------------------
The YouTube variant ``<id>_yt.mp4`` is a pure stream-copy of the master
(byte-identical in ``05_exports/youtube`` and the ``06_published`` mirror), so a
corrupt master can be rebuilt LOSSLESSLY by remuxing a surviving variant back
into the master container::

    ffmpeg -nostdin -v error -i <id>_yt.mp4 -c copy -map 0 -movflags +faststart -f mp4 <master>

``--auto-recover`` does exactly that, mirroring ``render_master``'s
``.part``-promote pattern so a half-written recovery can never masquerade as a
finished file:

    1. verify the variant itself passes ``check_integrity`` (never promote junk),
    2. remux it to a ``<master>.new`` temp,
    3. verify ``<master>.new`` passes ``check_integrity``,
    4. back the corrupt master up to ``<master>.corrupt`` (preserved for forensics),
    5. atomically ``os.replace`` the verified ``.new`` into the canonical name.

Steps 1-3 leave the corrupt master untouched, so a failure at any point before
the final replace leaves the original in place. The ``<id>_yt.mp4`` in
``05_exports`` is tried first; the ``06_published`` mirror is the fallback.

CLI::

    python tools/integrity_sweep.py                      # sweep masters, report
    python tools/integrity_sweep.py --include-published  # + 06_published backups
    python tools/integrity_sweep.py --auto-recover       # rebuild corrupt masters
    python tools/integrity_sweep.py --json               # machine-readable report
    python tools/integrity_sweep.py --deep 2             # 2s decode/file (faster)

Exit codes: 0 = all PASS (after any recovery); 1 = one or more files still
FAIL; 2 = setup error (no config / masters dir missing). Mirrors
``render_reaper``'s convention (2 == setup error).

Stdlib + PyYAML only (PyYAML is already a pipeline dependency). Imports
``tools.media_integrity`` but NOT ``pipeline`` — stays runnable for a scheduled
task even if a heavier import is broken.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.media_integrity import MediaIntegrityError, check_integrity  # noqa: E402

log = logging.getLogger("pipeline.integrity_sweep")

# CLI exit codes — distinct so a scheduled task / CI can branch on the class.
SWEEP_EXIT_OK = 0
SWEEP_EXIT_FAIL = 1
SWEEP_EXIT_SETUP = 2

# Recovery temp/backup suffixes. Both end in something OTHER than `.mp4` so they
# never match the `*.mp4` sweep glob (no re-scan of a temp/backup) and never
# collide with render_lock's `*.part` / `*.render.lock`.
NEW_SUFFIX = ".new"
CORRUPT_SUFFIX = ".corrupt"

# Cold-archive sweeps want to catch mid-file rot, not just a truncated header,
# so by default we decode the WHOLE file. media_integrity bounds each decode
# subprocess at 30s, so a file that cannot fully decode in 30s surfaces as a
# FAIL to investigate — which, for a cold archive, is exactly the right signal.
WHOLE_FILE_DECODE_S = 86_400.0

# Recovery remux is a stream-copy (no re-encode) of a ~30-60s short; 120s is a
# generous ceiling so a wedged ffmpeg can't hang a scheduled task forever.
_REMUX_TIMEOUT_S = 120

# topic_id is `YYYY-MM-DD_NNN`; the `06_published` mirror is bucketed by `YYYY-MM`.
_TOPIC_MONTH_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}_\d+")


# ---------------------------------------------------------------------------
# Path helpers — mirror render_reaper / pipeline so all tools agree on layout.
# Replicated (not imported) so this tool stays standalone for a scheduled task,
# matching render_reaper's "does NOT import pipeline" philosophy.
# ---------------------------------------------------------------------------


def _channel_root_from_config(config_path: Path) -> Path:
    """Read ``paths.channel_root`` straight from the YAML (no .env / validation)."""
    import yaml

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    try:
        return Path(data["paths"]["channel_root"])
    except (KeyError, TypeError) as exc:
        raise KeyError(
            f"{config_path} has no paths.channel_root — cannot locate masters"
        ) from exc


def master_dir_for(channel_root: Path | str) -> Path:
    return Path(channel_root) / "04_renders" / "_final_master"


def published_dir_for(channel_root: Path | str) -> Path:
    return Path(channel_root) / "06_published"


def exports_youtube_dir_for(channel_root: Path | str) -> Path:
    return Path(channel_root) / "05_exports" / "youtube"


def _topic_id_from_master(master_path: Path | str) -> str:
    """``<id>_master.mp4`` -> ``<id>`` (mirrors the variant naming in pipeline)."""
    return Path(master_path).stem.removesuffix("_master")


def variant_candidates(master_path: Path | str, channel_root: Path | str) -> list[Path]:
    """Ordered YT stream-copy siblings to rebuild a master from.

    The YT variant is byte-identical in ``05_exports/youtube`` and the
    ``06_published`` mirror; try the hot export first, then the cold backup.
    """
    master_path = Path(master_path)
    channel_root = Path(channel_root)
    tid = _topic_id_from_master(master_path)
    out = [exports_youtube_dir_for(channel_root) / f"{tid}_yt.mp4"]
    m = _TOPIC_MONTH_RE.match(tid)
    if m:
        month = m.group(1)
        out.append(published_dir_for(channel_root) / month / tid / "youtube" / f"{tid}_yt.mp4")
    return out


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover — best-effort temp cleanup
        log.warning("integrity-sweep: could not remove %s: %s", path, exc)


def _jsonable_info(info: dict | None) -> dict | None:
    """Make a check_integrity dict JSON-safe (tuple resolution -> list)."""
    if not info:
        return info
    out = dict(info)
    res = out.get("video_resolution")
    if isinstance(res, tuple):
        out["video_resolution"] = list(res)
    return out


def _make_checker(
    deep_decode_seconds: float, min_size_bytes: int = 1_000_000
) -> Callable[[Path], dict]:
    """Bind sweep-wide check_integrity options into a ``checker(path) -> dict``.

    The returned callable raises ``FileNotFoundError`` / ``MediaIntegrityError``
    exactly like ``check_integrity`` — the sweep treats both as a FAIL.
    """

    def _check(path: Path) -> dict:
        return check_integrity(
            path,
            min_size_bytes=min_size_bytes,
            deep_decode_seconds=deep_decode_seconds,
        )

    return _check


# ---------------------------------------------------------------------------
# Lossless recovery (mirror of render_master's `.part`-promote pattern)
# ---------------------------------------------------------------------------


class RecoveryError(RuntimeError):
    """Raised when a master could not be losslessly rebuilt from a variant.

    Loud by design: recovery either fully succeeds (verified `.new` atomically
    promoted) or raises with a specific reason. The corrupt master is never
    touched unless a verified replacement is ready.
    """

    def __init__(self, master_path: Path | str, reason: str):
        self.master_path = Path(master_path)
        self.reason = reason
        super().__init__(f"recovery failed for {master_path}: {reason}")


def _remux_cmd(variant_path: Path, out_path: Path) -> list[str]:
    """Lossless stream-copy remux command (matches the 2026-06-07 recovery).

    ``-f mp4`` pins the muxer because the ``.new`` extension can't be mapped to
    one (same reason render_master pins it for `.part`, commit e4d4c16).
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-v", "error",
        "-i", str(variant_path),
        "-c", "copy",
        "-map", "0",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(out_path),
    ]


def recover_master_from_variant(
    master_path: Path | str,
    variant_path: Path | str,
    *,
    checker: Callable[[Path], dict],
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    logger: logging.Logger | None = None,
) -> dict:
    """Rebuild a corrupt/missing master by remuxing a sound stream-copy variant.

    Self-contained and fail-loud: verifies the variant, remuxes to ``.new``,
    verifies the ``.new``, backs the corrupt master up to ``.corrupt``, then
    atomically promotes. Raises :class:`RecoveryError` on any failure, leaving
    the original master in place (untouched until the final, verified replace).

    Returns a recovery-detail dict on success.
    """
    logger = logger or log
    master_path = Path(master_path)
    variant_path = Path(variant_path)
    new_path = master_path.with_name(master_path.name + NEW_SUFFIX)
    corrupt_path = master_path.with_name(master_path.name + CORRUPT_SUFFIX)

    if not variant_path.exists():
        raise RecoveryError(master_path, f"recovery variant not found: {variant_path}")

    # 1) The variant must itself be sound — never promote garbage over a master.
    try:
        checker(variant_path)
    except (FileNotFoundError, MediaIntegrityError) as exc:
        raise RecoveryError(
            master_path,
            f"recovery variant {variant_path.name} also failed integrity: {exc}",
        ) from exc

    # 2) Remux the stream-copy variant into a `.new` temp (mirror `.part`).
    try:
        if new_path.exists():
            new_path.unlink()
    except OSError as exc:
        raise RecoveryError(master_path, f"could not clear stale temp {new_path}: {exc}") from exc

    size_before = master_path.stat().st_size if master_path.exists() else 0
    cmd = _remux_cmd(variant_path, new_path)
    logger.warning(
        "integrity-sweep: recovering %s by remuxing %s -> %s",
        master_path.name, variant_path.name, new_path.name,
    )
    logger.debug("integrity-sweep: remux cmd: %s", " ".join(cmd))
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=_REMUX_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise RecoveryError(master_path, f"ffmpeg not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        _safe_unlink(new_path)
        raise RecoveryError(master_path, f"remux timed out after {exc.timeout}s") from exc

    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip() or "(no stderr)"
        _safe_unlink(new_path)
        raise RecoveryError(master_path, f"remux failed (rc={rc}): {stderr[-1000:]}")

    # 3) Verify the remuxed output BEFORE touching the corrupt master.
    try:
        info = checker(new_path)
    except (FileNotFoundError, MediaIntegrityError) as exc:
        _safe_unlink(new_path)
        raise RecoveryError(master_path, f"remuxed output failed integrity: {exc}") from exc

    # 4) Back up the corrupt original (forensics), then atomically promote.
    try:
        backed_up: Path | None = None
        if master_path.exists():
            os.replace(master_path, corrupt_path)
            backed_up = corrupt_path
        os.replace(new_path, master_path)
    except OSError as exc:
        raise RecoveryError(
            master_path,
            f"promote failed (corrupt backup="
            f"{corrupt_path if corrupt_path.exists() else 'n/a'}, "
            f"recovered file still at {new_path if new_path.exists() else 'n/a'}): {exc}",
        ) from exc

    size_after = master_path.stat().st_size
    logger.warning(
        "integrity-sweep: RECOVERED %s (%.2f MB -> %.2f MB) from %s; "
        "corrupt original preserved at %s",
        master_path.name, size_before / 1e6, size_after / 1e6, variant_path.name,
        backed_up.name if backed_up else "(none - master was missing)",
    )
    return {
        "ok": True,
        "master_path": str(master_path),
        "variant_path": str(variant_path),
        "corrupt_backup": str(backed_up) if backed_up else None,
        "size_bytes_before": size_before,
        "size_bytes_after": size_after,
        "info": _jsonable_info(info),
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


@dataclass
class SweepEntry:
    """Per-file result. ``recovered`` files are NOT counted as failures."""

    path: Path
    kind: str  # "master" | "published"
    ok: bool
    error: str | None = None
    info: dict | None = None
    recovered: bool = False
    recovery: dict | None = None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "ok": self.ok,
            "error": self.error,
            "info": self.info,
            "recovered": self.recovered,
            "recovery": self.recovery,
        }


def _iter_targets(masters_dir: Path, published_dir: Path | None) -> list[tuple[Path, str]]:
    """Masters (top-level ``*.mp4``) then, optionally, published backups (recursive)."""
    targets: list[tuple[Path, str]] = []
    for p in sorted(masters_dir.glob("*.mp4")):
        targets.append((p, "master"))
    if published_dir is not None:
        for p in sorted(published_dir.rglob("*.mp4")):
            targets.append((p, "published"))
    return targets


def _attempt_recovery(
    entry: SweepEntry,
    *,
    channel_root: Path | None,
    checker: Callable[[Path], dict],
    runner: Callable[..., "subprocess.CompletedProcess"],
    logger: logging.Logger,
) -> None:
    """Try each existing variant candidate until one recovers ``entry.path``."""
    master_path = entry.path
    if channel_root is None:
        logger.error(
            "integrity-sweep: cannot auto-recover %s — channel_root unknown", master_path.name
        )
        entry.recovery = {"ok": False, "reason": "channel_root unknown"}
        return

    candidates = variant_candidates(master_path, channel_root)
    existing = [c for c in candidates if c.exists()]
    if not existing:
        logger.error(
            "integrity-sweep: no recovery variant for %s (looked for: %s)",
            master_path.name, ", ".join(str(c) for c in candidates),
        )
        entry.recovery = {
            "ok": False,
            "reason": "no recovery variant found",
            "candidates": [str(c) for c in candidates],
        }
        return

    last_err: RecoveryError | None = None
    for cand in existing:
        try:
            detail = recover_master_from_variant(
                master_path, cand, checker=checker, runner=runner, logger=logger
            )
        except RecoveryError as exc:
            last_err = exc
            logger.warning(
                "integrity-sweep: recovery of %s via %s failed: %s; trying next candidate",
                master_path.name, cand.name, exc,
            )
            continue
        entry.recovered = True
        entry.recovery = detail
        return

    entry.recovery = {
        "ok": False,
        "reason": str(last_err) if last_err else "all variant candidates failed",
        "candidates": [str(c) for c in existing],
    }


def sweep(
    masters_dir: Path | str,
    *,
    published_dir: Path | str | None = None,
    channel_root: Path | str | None = None,
    auto_recover: bool = False,
    checker: Callable[[Path], dict] | None = None,
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    logger: logging.Logger | None = None,
) -> dict:
    """Check every ``*.mp4`` master (and optionally published backups).

    Each file is checked independently — one bad file never aborts the sweep, so
    the report surfaces ALL failures at once. When ``auto_recover`` is set, a
    failed MASTER (kind=="master") is rebuilt from its YT stream-copy variant if
    a sound one exists; published failures are reported only.

    Returns a report dict (see ``_build_report``). ``checker`` defaults to a
    whole-file ``check_integrity``; inject a fake for tests.
    """
    logger = logger or log
    masters_dir = Path(masters_dir)
    published_dir = Path(published_dir) if published_dir is not None else None
    if checker is None:
        checker = _make_checker(WHOLE_FILE_DECODE_S)

    targets = _iter_targets(masters_dir, published_dir)
    entries: list[SweepEntry] = []

    for path, kind in targets:
        try:
            info = checker(path)
        except (FileNotFoundError, MediaIntegrityError) as exc:
            logger.error("integrity-sweep: FAIL %s: %s", path.name, exc)
            entry = SweepEntry(path=path, kind=kind, ok=False, error=str(exc))
            if auto_recover and kind == "master":
                _attempt_recovery(
                    entry, channel_root=channel_root, checker=checker,
                    runner=runner, logger=logger,
                )
            entries.append(entry)
        else:
            logger.info("integrity-sweep: PASS %s", path.name)
            entries.append(SweepEntry(path=path, kind=kind, ok=True, info=_jsonable_info(info)))

    return _build_report(masters_dir, published_dir, auto_recover, entries)


def _build_report(
    masters_dir: Path,
    published_dir: Path | None,
    auto_recover: bool,
    entries: list[SweepEntry],
) -> dict:
    passed = sum(1 for e in entries if e.ok)
    recovered = sum(1 for e in entries if e.recovered)
    failed = sum(1 for e in entries if not e.ok and not e.recovered)
    return {
        "ok": failed == 0,
        "masters_dir": str(masters_dir),
        "published_dir": str(published_dir) if published_dir else None,
        "auto_recover": auto_recover,
        "total": len(entries),
        "passed": passed,
        "recovered": recovered,
        "failed": failed,
        "entries": [e.to_dict() for e in entries],
    }


def _exit_code(report: dict) -> int:
    return SWEEP_EXIT_FAIL if report["failed"] > 0 else SWEEP_EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_human_report(report: dict) -> None:
    print(f"Integrity sweep: {report['masters_dir']}")
    if report["published_dir"]:
        print(f"  + published backups: {report['published_dir']}")
    print(
        f"  {report['total']} file(s): {report['passed']} PASS, "
        f"{report['recovered']} RECOVERED, {report['failed']} FAIL"
    )
    for e in report["entries"]:
        if e["ok"] and not e["recovered"]:
            continue  # quiet on clean PASS; the per-file PASS is logged at INFO
        name = Path(e["path"]).name
        if e["recovered"]:
            rec = e["recovery"] or {}
            corrupt = Path(rec.get("corrupt_backup") or "").name or "n/a"
            variant = Path(rec.get("variant_path") or "").name or "n/a"
            print(f"  RECOVERED  {name}  <- {variant}  (corrupt saved: {corrupt})")
        else:
            print(f"  FAIL       {name}  {e['error']}")
            rec = e.get("recovery")
            if rec and not rec.get("ok"):
                print(f"             auto-recover: {rec.get('reason')}")
    if report["failed"] == 0:
        suffix = " (after recovery)" if report["recovered"] else ""
        print("OK - all files passed" + suffix)
    else:
        print(f"FAIL - {report['failed']} file(s) need attention")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ShadowVerse cold-archive media-integrity sweep. Verifies every "
            "master (and optionally published backups), exits non-zero on any "
            "failure. Suitable for a Windows Scheduled Task."
        ),
    )
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: <repo>/config.yaml)")
    parser.add_argument("--channel-root", default=None,
                        help="Override channel root (skips config read)")
    parser.add_argument("--include-published", action="store_true",
                        help="Also sweep the 06_published cold backups (recursive)")
    parser.add_argument("--auto-recover", action="store_true",
                        help="Rebuild a corrupt master losslessly from its "
                             "<id>_yt.mp4 stream-copy variant")
    parser.add_argument("--deep", type=float, default=WHOLE_FILE_DECODE_S,
                        help="Seconds of decode per file (default: whole file). "
                             "0 = ffprobe header check only (fastest).")
    parser.add_argument("--min-size", type=int, default=1_000_000,
                        help="Reject files smaller than N bytes (default: 1_000_000).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON on stdout")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging (ffprobe/ffmpeg cmds, frame counts).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.channel_root:
            channel_root = Path(args.channel_root)
        else:
            config_path = Path(args.config) if args.config else REPO_ROOT / "config.yaml"
            channel_root = _channel_root_from_config(config_path)
    except (FileNotFoundError, KeyError) as exc:
        print(f"integrity_sweep: {exc}", file=sys.stderr)
        return SWEEP_EXIT_SETUP

    masters_dir = master_dir_for(channel_root)
    if not masters_dir.is_dir():
        print(f"integrity_sweep: masters dir not found: {masters_dir}", file=sys.stderr)
        return SWEEP_EXIT_SETUP

    published_dir = published_dir_for(channel_root) if args.include_published else None
    if published_dir is not None and not published_dir.is_dir():
        log.warning(
            "integrity-sweep: --include-published set but %s does not exist; skipping",
            published_dir,
        )
        published_dir = None

    checker = _make_checker(args.deep, min_size_bytes=args.min_size)
    report = sweep(
        masters_dir,
        published_dir=published_dir,
        channel_root=channel_root,
        auto_recover=args.auto_recover,
        checker=checker,
    )

    if report["total"] == 0:
        log.warning("integrity-sweep: no .mp4 files found under %s", masters_dir)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human_report(report)

    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
