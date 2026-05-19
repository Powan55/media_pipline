"""Per-video postmortem markdown generator for ShadowVerse.

Emits ``<topic_id>.md`` into ``<project_root>/Channels/ShadowVerse/postmortems/``,
populated from the topic's render artifacts and (when available) the upload log.

The generator runs once per published video — typically wired into the post-upload
hook (Wave 3). It gives the operator a per-video review surface for compounding
learning. The content is intentionally a stub: numeric performance fields are left
blank for the operator to fill at the 24h / 7d / 30d review checkpoints.

If ``Channels/ShadowVerse/postmortems/_TEMPLATE.md`` exists, it is used as a
``str.format()``-style template (substitutes ``{topic_id}``, ``{slug}``,
``{hook_formula}``, ``{thumbnail_pattern}``, ``{render_date}``, ``{upload_date}``,
``{video_url}``, ``{duration_s}``). Missing data values *and* unknown placeholder
keys both render as ``_(not yet captured)_`` so a weekly-style template doesn't
crash on per-video fields. If the template is missing, an inline default template
is used.

Usage:
    # Single topic
    python tools/postmortem_stub.py --topic-id 2026-05-06_003

    # All topics with QA markers (skips topics that already have a postmortem)
    python tools/postmortem_stub.py --backfill

    # Programmatic (wave-3 post-upload hook)
    from tools.postmortem_stub import generate_postmortem
    generate_postmortem(
        topic_id="2026-05-06_003",
        channel_root=Path(r"C:\\ContentOps\\channels\\ShadowVerse"),
        project_root=Path(r"C:\\Users\\laxmi\\Documents\\Project"),
    )

CLI exit codes: 0 = success, 1 = failure (missing artifacts, write error, etc.).
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger("postmortem_stub")

NOT_CAPTURED = "_(not yet captured)_"

DEFAULT_TEMPLATE = """# Postmortem — {topic_id}

**Slug:** {slug}
**Hook formula:** {hook_formula}
**Thumbnail pattern:** {thumbnail_pattern}
**Render date:** {render_date}
**Upload date:** {upload_date}
**Video URL:** {video_url}
**Duration:** {duration_s} s

## Day 1 baseline (fill within 24-48h post-publish)
- 24h views:
- 24h avg-view %:
- 24h 3-sec retention %:

## Day 7 / Day 30 trend (fill at first weekly review)
- 7-day views:
- 7-day AVD trend:
- 30-day views:

## What I tried
-

## What I'd do differently
-

## Replication notes
-
"""

# Match `Title: ...` line in metadata_RESPONSE.txt under a `## YOUTUBE SHORTS`
# (or `YOUTUBE SHORTS:`) section. Mirrors pipeline._parse_metadata_response but
# kept narrow on purpose — we only need the title here, not the full bundle.
_TITLE_RE = re.compile(
    r"(?im)^\s*(?:##\s*)?YOUTUBE\s*SHORTS\s*:?\s*\n"
    r"(?:.*?\n){0,15}?"
    r"\s*[-*]?\s*\*?\*?Title\*?\*?\s*:\s*(?P<title>.+?)\s*$",
    re.MULTILINE,
)
# Match `Pattern: foo` field inside the COVER section.
_PATTERN_RE = re.compile(
    r"(?im)^\s*(?:##\s*)?COVER(?:\s*/\s*THUMBNAIL(?:\s+CONCEPT)?)?\s*:?\s*\n"
    r"(?:.*?\n){0,30}?"
    r"\s*[-*]?\s*\*?\*?Pattern\*?\*?\s*:\s*(?P<pattern>.+?)\s*$",
    re.MULTILINE,
)
# Match `hook_formula: foo` line anywhere in script_FINAL.txt (case-insensitive).
_HOOK_FORMULA_RE = re.compile(
    r"(?im)^\s*[-*]?\s*\*?\*?hook[_\s-]*formula\*?\*?\s*:\s*(?P<formula>.+?)\s*$",
)

# Tolerate both bare 'hh:mm:ss' and decimal-second ffprobe outputs.
FFPROBE_BIN = "ffprobe"


@dataclass
class PostmortemData:
    """Bundle of per-video data the postmortem template renders."""

    topic_id: str
    slug: str
    render_date: datetime
    upload_date: datetime | None
    video_url: str | None
    hook_formula: str | None
    thumbnail_pattern: str | None
    duration_s: float | None


class _SafeFormatDict(dict):
    """Dict that returns ``NOT_CAPTURED`` for missing keys, used with ``str.format_map``.

    Both the field-is-None case (e.g., upload_date when not yet uploaded) and the
    placeholder-not-in-our-set case (e.g., a weekly template that uses ``{title}``)
    resolve to the same sentinel — keeps the postmortem markdown valid either way.
    """

    def __missing__(self, key: str) -> str:
        return NOT_CAPTURED


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read a text file with utf-8, falling back to utf-8-sig for BOM-tagged files."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")


def _extract_youtube_title(metadata_path: Path) -> str | None:
    """Pull the youtube_title from a topic's ``metadata_RESPONSE.txt``.

    Returns None if the file is missing or no Title field can be located.
    """
    if not metadata_path.exists():
        return None
    text = _read_text(metadata_path)
    m = _TITLE_RE.search(text)
    if not m:
        return None
    title = m.group("title").strip().strip("*").strip()
    return title or None


def _extract_thumbnail_pattern(metadata_path: Path) -> str | None:
    """Pull ``Pattern: ...`` from the COVER section of metadata_RESPONSE.txt."""
    if not metadata_path.exists():
        return None
    text = _read_text(metadata_path)
    m = _PATTERN_RE.search(text)
    if not m:
        return None
    pattern = m.group("pattern").strip().strip("*").strip()
    return pattern or None


def _extract_hook_formula(script_final_path: Path) -> str | None:
    """Pull ``hook_formula: ...`` from script_FINAL.txt if present."""
    if not script_final_path.exists():
        return None
    text = _read_text(script_final_path)
    m = _HOOK_FORMULA_RE.search(text)
    if not m:
        return None
    formula = m.group("formula").strip().strip("*").strip()
    return formula or None


def _probe_duration(video_path: Path) -> float | None:
    """Return the duration of ``video_path`` in seconds via ffprobe, or None on failure.

    We log + swallow the error: a missing duration shouldn't block postmortem creation.
    """
    if not video_path.exists():
        return None
    try:
        proc = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log.warning("ffprobe not on PATH; skipping duration probe for %s", video_path.name)
        return None
    if proc.returncode != 0:
        log.warning(
            "ffprobe failed for %s (exit=%d): %s",
            video_path.name, proc.returncode, (proc.stderr or "").strip()[:200],
        )
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("ffprobe returned non-numeric duration %r for %s", raw, video_path.name)
        return None


def _lookup_upload_row(
    upload_log_csv: Path | None, topic_id: str,
) -> tuple[datetime | None, str | None]:
    """Find the most recent upload_log.csv row for ``topic_id``.

    Returns ``(upload_date, video_url)``. If the CSV is missing or the topic_id
    isn't present, both fields are None. Schema (per youtube_upload.append_upload_log):
    ``uploaded_at, topic_id, video_id, url, privacy, title``.

    Robust to: missing file, header-only file, malformed rows (missing columns),
    unparseable timestamps.
    """
    if upload_log_csv is None or not upload_log_csv.exists():
        return None, None

    last_row: dict[str, str] | None = None
    try:
        with upload_log_csv.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("topic_id", "").strip() == topic_id:
                    last_row = row  # keep the latest matching row
    except OSError as e:
        log.warning("could not read upload_log %s: %s", upload_log_csv, e)
        return None, None

    if last_row is None:
        return None, None

    uploaded_at_raw = (last_row.get("uploaded_at") or "").strip()
    video_url = (last_row.get("url") or "").strip() or None
    upload_date: datetime | None = None
    if uploaded_at_raw:
        try:
            # youtube_upload.py writes RFC 3339 with timespec='seconds' — fromisoformat
            # handles 'Z' suffix in 3.11+ and explicit offsets like '-04:00'.
            upload_date = datetime.fromisoformat(uploaded_at_raw.replace("Z", "+00:00"))
        except ValueError:
            log.warning(
                "could not parse uploaded_at=%r for %s; leaving upload_date blank",
                uploaded_at_raw, topic_id,
            )
    return upload_date, video_url


def _format_dt(value: datetime | None) -> str:
    """Render a datetime for the markdown field. None → NOT_CAPTURED."""
    if value is None:
        return NOT_CAPTURED
    # Date-only is plenty granular for postmortem review.
    return value.strftime("%Y-%m-%d %H:%M") if value.tzinfo else value.strftime("%Y-%m-%d %H:%M")


def _format_optional(value: str | None) -> str:
    """Render an optional string field. None / empty → NOT_CAPTURED."""
    if value is None or not str(value).strip():
        return NOT_CAPTURED
    return str(value).strip()


def _format_duration(value: float | None) -> str:
    if value is None:
        return NOT_CAPTURED
    return f"{value:.1f}"


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def _gather_data(
    topic_id: str,
    channel_root: Path,
    upload_log_csv: Path | None,
) -> PostmortemData:
    """Assemble PostmortemData by reading the topic's artifacts."""
    master_path = channel_root / "04_renders" / "_final_master" / f"{topic_id}_master.mp4"
    if not master_path.exists():
        raise FileNotFoundError(
            f"master not found: {master_path}. Topic {topic_id!r} is not yet "
            f"render-ready; cannot generate a postmortem."
        )

    metadata_path = channel_root / "02_scripts" / "_drafts" / topic_id / "metadata_RESPONSE.txt"
    script_final_path = channel_root / "02_scripts" / "_drafts" / topic_id / "script_FINAL.txt"

    title = _extract_youtube_title(metadata_path)
    slug = title or topic_id  # fall back to topic_id as slug per AC1

    render_date = datetime.fromtimestamp(master_path.stat().st_mtime)
    upload_date, video_url = _lookup_upload_row(upload_log_csv, topic_id)
    hook_formula = _extract_hook_formula(script_final_path)
    thumbnail_pattern = _extract_thumbnail_pattern(metadata_path)
    duration_s = _probe_duration(master_path)

    return PostmortemData(
        topic_id=topic_id,
        slug=slug,
        render_date=render_date,
        upload_date=upload_date,
        video_url=video_url,
        hook_formula=hook_formula,
        thumbnail_pattern=thumbnail_pattern,
        duration_s=duration_s,
    )


def _render(template_text: str, data: PostmortemData) -> str:
    """Substitute fields into ``template_text`` using ``str.format_map``.

    Missing keys (placeholders we don't supply, or fields whose value is None)
    render as ``NOT_CAPTURED`` via the _SafeFormatDict mapping.
    """
    fields: dict[str, str] = {
        "topic_id": data.topic_id,
        "slug": _format_optional(data.slug),
        "hook_formula": _format_optional(data.hook_formula),
        "thumbnail_pattern": _format_optional(data.thumbnail_pattern),
        "render_date": _format_dt(data.render_date),
        "upload_date": _format_dt(data.upload_date),
        "video_url": _format_optional(data.video_url),
        "duration_s": _format_duration(data.duration_s),
    }
    return template_text.format_map(_SafeFormatDict(fields))


def generate_postmortem(
    topic_id: str,
    *,
    channel_root: Path,
    project_root: Path,
    upload_log_csv: Path | None = None,
    template_path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Generate the per-video postmortem markdown for ``topic_id``.

    Args:
        topic_id: Topic id, e.g. ``2026-05-06_003``.
        channel_root: ``C:\\ContentOps\\channels\\ShadowVerse`` (or test tmp).
        project_root: ``C:\\Users\\laxmi\\Documents\\Project`` (or test tmp).
            The postmortem lands at ``<project_root>/Channels/ShadowVerse/postmortems/<topic_id>.md``.
        upload_log_csv: Path to ``upload_log.csv``. Defaults to
            ``<channel_root>/01_research/upload_log.csv``.
        template_path: Path to the postmortem template. Defaults to
            ``<project_root>/Channels/ShadowVerse/postmortems/_TEMPLATE.md``.
            If the file is missing, falls back to the bundled DEFAULT_TEMPLATE.
        overwrite: If False (default) and the target file exists, raises
            ``FileExistsError``. If True, the existing file is overwritten.

    Returns:
        Path of the written postmortem file.

    Raises:
        FileNotFoundError: if the topic's master mp4 is missing (topic not yet
            rendered, so postmortem generation is premature).
        FileExistsError: if the target postmortem already exists and ``overwrite``
            is False.
    """
    channel_root = Path(channel_root)
    project_root = Path(project_root)

    postmortems_dir = project_root / "Channels" / "ShadowVerse" / "postmortems"
    postmortems_dir.mkdir(parents=True, exist_ok=True)

    target = postmortems_dir / f"{topic_id}.md"
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"refusing to overwrite existing postmortem at {target}; "
            f"pass overwrite=True / --overwrite to replace."
        )

    if upload_log_csv is None:
        upload_log_csv = channel_root / "01_research" / "upload_log.csv"
    if template_path is None:
        template_path = postmortems_dir / "_TEMPLATE.md"

    data = _gather_data(topic_id, channel_root, upload_log_csv)

    if template_path.exists():
        template_text = _read_text(template_path)
        log.info("using template at %s", template_path)
    else:
        template_text = DEFAULT_TEMPLATE
        log.info("template not found at %s; using bundled default", template_path)

    rendered = _render(template_text, data)
    target.write_text(rendered, encoding="utf-8")
    log.info("wrote postmortem for %s -> %s", topic_id, target)
    return target


# ---------------------------------------------------------------------------
# Backfill mode
# ---------------------------------------------------------------------------


def _iter_qa_approved_topics(channel_root: Path) -> Iterable[str]:
    """Yield topic_ids for every ``*_master_QA_APPROVED.marker`` under final_master."""
    final_master = channel_root / "04_renders" / "_final_master"
    if not final_master.exists():
        return
    for marker in sorted(final_master.glob("*_master_QA_APPROVED.marker")):
        # filename = <topic_id>_master_QA_APPROVED.marker
        name = marker.name[: -len("_master_QA_APPROVED.marker")]
        if name:
            yield name


def _backfill(
    channel_root: Path,
    project_root: Path,
    upload_log_csv: Path | None,
    overwrite: bool,
) -> tuple[int, int, list[tuple[str, str]]]:
    """Run generate_postmortem for every QA-approved topic.

    Returns ``(generated, skipped, errors)`` where ``errors`` is a list of
    ``(topic_id, message)`` tuples. Per-topic exceptions are aggregated, not raised.
    """
    generated = 0
    skipped = 0
    errors: list[tuple[str, str]] = []
    for topic_id in _iter_qa_approved_topics(channel_root):
        try:
            generate_postmortem(
                topic_id,
                channel_root=channel_root,
                project_root=project_root,
                upload_log_csv=upload_log_csv,
                overwrite=overwrite,
            )
            generated += 1
        except FileExistsError:
            log.info("skipping %s (postmortem already exists; pass --overwrite to replace)",
                     topic_id)
            skipped += 1
        except FileNotFoundError as e:
            log.warning("skipping %s: %s", topic_id, e)
            errors.append((topic_id, str(e)))
        except Exception as e:  # noqa: BLE001 — aggregate-and-report per AC6
            log.exception("error generating postmortem for %s", topic_id)
            errors.append((topic_id, f"{type(e).__name__}: {e}"))
    return generated, skipped, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a per-video postmortem markdown for a ShadowVerse topic.",
    )
    parser.add_argument("--topic-id", help="Topic id, e.g. 2026-05-06_003.")
    parser.add_argument(
        "--channel-root",
        default=r"C:\ContentOps\channels\ShadowVerse",
        help="Channel root (default: %(default)s).",
    )
    parser.add_argument(
        "--project-root",
        default=r"C:\Users\laxmi\Documents\Project",
        help="Project root that holds Channels/ShadowVerse/postmortems/ (default: %(default)s).",
    )
    parser.add_argument(
        "--upload-log-csv",
        default=None,
        help="Path to upload_log.csv (default: <channel_root>/01_research/upload_log.csv).",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Path to a postmortem template (default: "
             "<project_root>/Channels/ShadowVerse/postmortems/_TEMPLATE.md).",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing postmortem files.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Generate postmortems for every topic with a QA-approved marker.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.topic_id and not args.backfill:
        parser.error("either --topic-id or --backfill is required")
    if args.topic_id and args.backfill:
        parser.error("--topic-id and --backfill are mutually exclusive")

    channel_root = Path(args.channel_root)
    project_root = Path(args.project_root)
    upload_log_csv = Path(args.upload_log_csv) if args.upload_log_csv else None
    template_path = Path(args.template) if args.template else None

    if args.backfill:
        generated, skipped, errors = _backfill(
            channel_root=channel_root,
            project_root=project_root,
            upload_log_csv=upload_log_csv,
            overwrite=args.overwrite,
        )
        log.info("backfill: generated=%d skipped=%d errors=%d",
                 generated, skipped, len(errors))
        for topic_id, msg in errors:
            log.error("  %s: %s", topic_id, msg)
        return 1 if errors else 0

    try:
        generate_postmortem(
            args.topic_id,
            channel_root=channel_root,
            project_root=project_root,
            upload_log_csv=upload_log_csv,
            template_path=template_path,
            overwrite=args.overwrite,
        )
    except (FileExistsError, FileNotFoundError) as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
