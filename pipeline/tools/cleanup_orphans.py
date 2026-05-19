r"""Move orphaned topic-draft directories out of `02_scripts/_drafts/`.

Background: an early `picks_assignment` bug spawned topic-id directories under
`<channel_root>/02_scripts/_drafts/` that never produced a `script_FINAL.txt`.
This utility walks the drafts root, identifies dirs that (a) lack
`script_FINAL.txt` AND (b) have not been touched in `--min-age-hours` (default
48h), and relocates them to `_drafts/_orphans/<YYYY-MM>/<topic_id>/` for cold
storage. Anything starting with `_` (e.g. `_orphans`, `_daily_<DATE>`) and any
top-level files are skipped.

This is destructive-in-spirit: the operator has signed off on `_drafts/`
content, so we do not delete it — we move it. Per CLAUDE.md "always confirm
before deleting", **dry-run is the default** (no flag needed). Live moves
require the explicit `--execute` flag. If a destination already exists the
move is refused (`FileExistsError`) so the operator can resolve the collision
manually rather than silently overwrite.

CLI:
    # Dry-run: list what would move, change nothing.
    python tools/cleanup_orphans.py
    python tools/cleanup_orphans.py --drafts-dir D:\some\other\_drafts
    python tools/cleanup_orphans.py --min-age-hours 72

    # Live: actually move the directories.
    python tools/cleanup_orphans.py --execute
    python tools/cleanup_orphans.py --execute --quiet

Reads:
    config.yaml (paths.channel_root) — to derive default drafts_dir when
    `--drafts-dir` is not provided.

Writes (only with `--execute`):
    Moves <drafts_dir>/<topic_id>/ -> <drafts_dir>/_orphans/<YYYY-MM>/<topic_id>/
    Audit log line per move on stdout (or stderr if --quiet):
        <ISO timestamp>\t<src>\t<dst>

Exit codes:
    0 — success (dry-run always; or live with all moves succeeded)
    1 — partial / total failure during a live move (e.g. FileExistsError)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger("cleanup_orphans")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

ORPHANS_SUBDIR = "_orphans"
FINAL_SCRIPT_NAME = "script_FINAL.txt"
DEFAULT_MIN_AGE_HOURS = 48.0


def load_config() -> dict:
    """Load `config.yaml` from the pipeline repo root. Fails loud if missing."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def default_drafts_dir(config: dict) -> Path:
    """Derive the default drafts dir from `config.paths.channel_root`."""
    channel_root = Path(config["paths"]["channel_root"])
    return channel_root / "02_scripts" / "_drafts"


def find_orphans(
    drafts_dir: Path,
    *,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    now: datetime | None = None,
) -> list[Path]:
    """Return topic-dir paths that are orphans by definition.

    An orphan is a *directory* directly under ``drafts_dir`` such that:
      (a) it does not contain ``script_FINAL.txt`` AND
      (b) its mtime is older than ``min_age_hours``.

    Excluded:
      - any entry whose name starts with ``_`` (``_orphans``, ``_daily_<DATE>``,
        ``_archive``, etc.) — these are infrastructure dirs, not topic drafts.
      - any non-directory entry (stray top-level files).

    This function is read-only. It never modifies the filesystem.
    """
    if not drafts_dir.exists():
        raise FileNotFoundError(f"drafts_dir does not exist: {drafts_dir}")
    if not drafts_dir.is_dir():
        raise NotADirectoryError(f"drafts_dir is not a directory: {drafts_dir}")

    now = now or datetime.now(timezone.utc)
    min_age_seconds = min_age_hours * 3600.0

    orphans: list[Path] = []
    for entry in sorted(drafts_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        if (entry / FINAL_SCRIPT_NAME).exists():
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        age_seconds = (now - mtime).total_seconds()
        if age_seconds < min_age_seconds:
            continue
        orphans.append(entry)
    return orphans


def move_to_orphans(
    orphan_dirs: list[Path],
    *,
    drafts_dir: Path,
    now: datetime | None = None,
) -> list[tuple[Path, Path]]:
    """Move each orphan dir into ``drafts_dir/_orphans/<YYYY-MM>/<basename>/``.

    Uses ``shutil.move`` (not copy + delete) to preserve the move semantics
    across the same filesystem. The ``<YYYY-MM>`` bucket is derived from
    ``now`` (UTC) so all orphans archived in a single run land together.

    If a destination already exists, raises ``FileExistsError`` immediately.
    The operator must resolve manually rather than overwrite. Any orphan dirs
    listed *after* a failure are not attempted — caller can re-run after the
    collision is cleared.

    Returns a list of ``(src, dst)`` tuples for the audit log.
    """
    now = now or datetime.now(timezone.utc)
    bucket = now.strftime("%Y-%m")
    target_root = drafts_dir / ORPHANS_SUBDIR / bucket
    target_root.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[Path, Path]] = []
    for src in orphan_dirs:
        dst = target_root / src.name
        if dst.exists():
            raise FileExistsError(
                f"refusing to overwrite existing destination: {dst}. "
                f"resolve manually (move or remove the existing dir) and re-run."
            )
        log.info("moving %s -> %s", src, dst)
        shutil.move(str(src), str(dst))
        moved.append((src, dst))
    return moved


def _format_size(path: Path) -> str:
    """Best-effort recursive size of a directory tree, formatted for stdout."""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        return "?"
    if total < 1024:
        return f"{total} B"
    if total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total / (1024 * 1024):.1f} MB"


def _format_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        return "?"


def _print_audit_line(src: Path, dst: Path, *, quiet: bool) -> None:
    """Emit one tab-separated audit line: `<ISO ts>\\t<src>\\t<dst>`."""
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{src}\t{dst}"
    stream = sys.stderr if quiet else sys.stdout
    print(line, file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move orphaned topic-draft dirs (no script_FINAL.txt and older than "
            "--min-age-hours) into _drafts/_orphans/<YYYY-MM>/. Dry-run is the "
            "default; pass --execute to actually move."
        ),
    )
    parser.add_argument(
        "--drafts-dir",
        default=None,
        type=Path,
        help=(
            "Path to the _drafts directory to scan. Defaults to "
            "<config.paths.channel_root>/02_scripts/_drafts/."
        ),
    )
    parser.add_argument(
        "--min-age-hours",
        default=DEFAULT_MIN_AGE_HOURS,
        type=float,
        help=f"Minimum dir mtime age in hours to qualify as orphan (default {DEFAULT_MIN_AGE_HOURS}).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move the orphan dirs. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Send audit lines to stderr instead of stdout (logger output unaffected).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.drafts_dir is None:
        config = load_config()
        drafts_dir = default_drafts_dir(config)
    else:
        drafts_dir = args.drafts_dir

    log.info("drafts_dir:    %s", drafts_dir)
    log.info("min_age_hours: %.2f", args.min_age_hours)
    log.info("mode:          %s", "EXECUTE (live)" if args.execute else "DRY RUN")

    try:
        orphans = find_orphans(drafts_dir, min_age_hours=args.min_age_hours)
    except (FileNotFoundError, NotADirectoryError) as e:
        log.error("%s", e)
        return 1

    if not args.execute:
        header = f"DRY RUN -- would move {len(orphans)} dirs"
        stream = sys.stderr if args.quiet else sys.stdout
        print(header, file=stream)
        for src in orphans:
            print(
                f"  {src}\tsize={_format_size(src)}\tmtime={_format_mtime(src)}",
                file=stream,
            )
        return 0

    try:
        moved = move_to_orphans(orphans, drafts_dir=drafts_dir)
    except FileExistsError as e:
        log.error("%s", e)
        return 1
    except OSError as e:
        log.error("OS error during move: %s", e)
        return 1

    for src, dst in moved:
        _print_audit_line(src, dst, quiet=args.quiet)

    summary = f"moved {len(moved)} dirs"
    stream = sys.stderr if args.quiet else sys.stdout
    print(summary, file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
