"""Archive per-platform variants into 06_published/<YYYY-MM>/<topic_id>/ as a cold-backup snapshot.

After a successful YouTube upload (or via --backfill for already-published topics), this module
copies the YT/TT/IG variant files from the canonical hot-tier `05_exports/<platform>/` into a
dated cold-backup tree at `06_published/<YYYY-MM>/<topic_id>/<platform>/<basename>`. The hot-tier
files stay put — this is a copy, not a move.

Wave 3 will wire `archive_topic()` into `tools/youtube_upload.py` as a post-upload hook so each
shipped video gets archived automatically. Until then `--backfill` walks gate-3-approved masters
in `04_renders/_final_master/*_QA_APPROVED.marker` and archives each one.

CLI:
    python tools/archive_published.py --topic-id 2026-05-06_003
    python tools/archive_published.py --backfill
    python tools/archive_published.py --backfill --only 2026-05-05_001 2026-05-07_004
    python tools/archive_published.py --topic-id 2026-05-06_003 --force

Refuses to overwrite existing archive entries unless `--force` is passed (or `force=True` kwarg
on the public functions). Operator-only escape hatch — re-archiving destroys the prior snapshot.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger("archive_published")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

# Source variant naming under 05_exports/<platform>/. Suffix matches what the pipeline writes.
PLATFORM_SUFFIX: dict[str, str] = {
    "youtube": "_yt.mp4",
    "tiktok": "_tt.mp4",
    "instagram": "_ig.mp4",
}

# Marker filename pattern from `await_final_qa` in pipeline.py:
#   <master_stem>_QA_APPROVED.marker, where <master_stem> is `<topic_id>_master`.
QA_MARKER_SUFFIX = "_master_QA_APPROVED.marker"


def _load_channel_root() -> Path:
    """Read `paths.channel_root` from config.yaml. Used only by the CLI default."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return Path(config["paths"]["channel_root"])


def _source_path(channel_root: Path, topic_id: str, platform: str) -> Path:
    """Resolve `05_exports/<platform>/<topic_id><suffix>` for the given platform."""
    suffix = PLATFORM_SUFFIX[platform]
    return channel_root / "05_exports" / platform / f"{topic_id}{suffix}"


def _dest_dir(channel_root: Path, topic_id: str, platform: str, when: datetime) -> Path:
    """Resolve `06_published/<YYYY-MM>/<topic_id>/<platform>/` for the given platform."""
    yyyy_mm = when.strftime("%Y-%m")
    return channel_root / "06_published" / yyyy_mm / topic_id / platform


def archive_topic(
    topic_id: str,
    *,
    channel_root: Path,
    when: datetime | None = None,
    skip_missing: bool = False,
    force: bool = False,
) -> dict[str, Path]:
    """Copy per-platform variants from 05_exports/ into 06_published/<YYYY-MM>/<topic_id>/.

    Args:
        topic_id: Topic id (e.g. "2026-05-06_003"). Used to resolve source filenames and the
            destination subdirectory.
        channel_root: Channel root path (e.g. C:\\ContentOps\\channels\\ShadowVerse).
        when: Datetime used for the YYYY-MM destination subdir. Defaults to now() at call time.
        skip_missing: If True, skip platforms whose source variant is missing. If False (default),
            raise FileNotFoundError on the first missing source.
        force: If True, overwrite an existing destination file. If False (default), raise
            FileExistsError. Operator-only escape hatch.

    Returns:
        Dict mapping platform -> archived destination path. Skipped platforms are omitted.

    Raises:
        FileNotFoundError: A required variant is missing and skip_missing=False.
        FileExistsError: A destination file already exists and force=False.
    """
    when = when or datetime.now()
    archived: dict[str, Path] = {}

    log.info("archiving topic %s under channel_root=%s", topic_id, channel_root)

    for platform in PLATFORM_SUFFIX:
        src = _source_path(channel_root, topic_id, platform)
        if not src.exists():
            if skip_missing:
                log.warning("skipping %s: source variant missing (%s)", platform, src)
                continue
            raise FileNotFoundError(
                f"source variant missing for topic {topic_id}, platform {platform}: {src}"
            )

        dest_dir = _dest_dir(channel_root, topic_id, platform, when)
        dest = dest_dir / src.name
        if dest.exists() and not force:
            raise FileExistsError(
                f"archive already exists for {topic_id}/{platform}: {dest}. "
                f"Re-run with --force to overwrite (operator-only)."
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.info("archived %s -> %s", src.name, dest)
        archived[platform] = dest

    return archived


def _discover_qa_approved_topic_ids(channel_root: Path) -> list[str]:
    """Walk `04_renders/_final_master/` for `*_master_QA_APPROVED.marker` files."""
    masters_dir = channel_root / "04_renders" / "_final_master"
    if not masters_dir.exists():
        log.warning("no masters dir at %s", masters_dir)
        return []
    topic_ids: list[str] = []
    for marker in sorted(masters_dir.glob(f"*{QA_MARKER_SUFFIX}")):
        # Marker name: "<topic_id>_master_QA_APPROVED.marker" -> strip suffix to get topic_id.
        topic_id = marker.name[: -len(QA_MARKER_SUFFIX)]
        topic_ids.append(topic_id)
    return topic_ids


def backfill_all(
    channel_root: Path,
    *,
    when: datetime | None = None,
    only_topic_ids: list[str] | None = None,
    skip_missing: bool = True,
    force: bool = False,
) -> list[dict]:
    """Archive every gate-3-approved topic found under 04_renders/_final_master/.

    Args:
        channel_root: Channel root path.
        when: Datetime used for the YYYY-MM destination subdir. Defaults to now() per call to
            archive_topic. Pass an explicit datetime to keep all results in the same bucket.
        only_topic_ids: If provided, only archive topics whose id is in this list.
        skip_missing: Forwarded to archive_topic. Default True for backfill (a missing variant
            shouldn't abort the whole backfill — log + continue).
        force: Forwarded to archive_topic.

    Returns:
        List of {"topic_id", "archived" | "error"} dicts in discovery order. On per-topic
        failure, the entry has key "error" with the exception message instead of "archived".
    """
    discovered = _discover_qa_approved_topic_ids(channel_root)
    if only_topic_ids is not None:
        wanted = set(only_topic_ids)
        discovered = [t for t in discovered if t in wanted]
        missing = wanted - set(discovered)
        if missing:
            log.warning("--only ids not found among QA-approved markers: %s", sorted(missing))

    log.info("backfill: %d topic(s) to archive", len(discovered))

    results: list[dict] = []
    for topic_id in discovered:
        try:
            archived = archive_topic(
                topic_id,
                channel_root=channel_root,
                when=when,
                skip_missing=skip_missing,
                force=force,
            )
            results.append({"topic_id": topic_id, "archived": archived})
        except (FileNotFoundError, FileExistsError, OSError) as e:
            log.error("backfill failed for %s: %s", topic_id, e)
            results.append({"topic_id": topic_id, "error": str(e)})

    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns 0 on success, non-zero on per-topic failure."""
    parser = argparse.ArgumentParser(
        description=(
            "Archive ShadowVerse video variants from 05_exports/ into 06_published/ as a "
            "cold-backup snapshot. Copy-only (preserves mtime); does not touch hot-tier."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--topic-id",
        help="Archive a single topic by id (e.g. 2026-05-06_003).",
    )
    mode.add_argument(
        "--backfill",
        action="store_true",
        help="Walk 04_renders/_final_master/ for *_QA_APPROVED.marker files and archive each.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="TOPIC_ID",
        help="With --backfill, restrict archiving to these topic ids.",
    )
    parser.add_argument(
        "--channel-root",
        default=None,
        help="Override channel_root path. Defaults to config.yaml `paths.channel_root`.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing archive entries (operator-only escape hatch).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.only and not args.backfill:
        parser.error("--only requires --backfill")

    channel_root = Path(args.channel_root) if args.channel_root else _load_channel_root()
    if not channel_root.exists():
        log.error("channel_root does not exist: %s", channel_root)
        return 2

    if args.topic_id:
        try:
            archive_topic(
                args.topic_id,
                channel_root=channel_root,
                skip_missing=False,
                force=args.force,
            )
        except (FileNotFoundError, FileExistsError, OSError) as e:
            log.error("archive failed for %s: %s", args.topic_id, e)
            return 1
        return 0

    # --backfill mode
    results = backfill_all(
        channel_root,
        only_topic_ids=args.only,
        skip_missing=True,
        force=args.force,
    )
    failures = [r for r in results if "error" in r]
    successes = [r for r in results if "archived" in r]
    log.info("backfill complete: %d archived, %d failed", len(successes), len(failures))
    if failures:
        for r in failures:
            log.error("  failed: %s -- %s", r["topic_id"], r["error"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
