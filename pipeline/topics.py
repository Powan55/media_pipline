"""Topic-history utilities for ShadowVerse.

The pipeline doesn't have a single canonical metadata file per topic — topic
text lives in CLI args at run time and gets templated into `script_PROMPT.txt`.
This module reconstructs the topic-history view by scanning the topic dirs.

Used by:
  - prompts/02_idea_generation.md `{RECENT_TOPICS}` substitution (anti-cannibalization)
  - daily_batch.py to allocate the next topic-id of the day without collisions
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("topics")

_TOPIC_ID_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{3})$")
_TOPIC_LINE_RE = re.compile(r"^TOPIC:\s*(.+?)$", re.MULTILINE)
_ANGLE_LINE_RE = re.compile(r"^ANGLE:\s*(.+?)$", re.MULTILINE)

QA_MARKER_SUFFIX = "_master_QA_APPROVED.marker"


@dataclass
class TopicRecord:
    topic_id: str              # "2026-05-05_001"
    date: datetime             # date parsed from topic_id prefix
    topic: str                 # one-sentence topic (read from script_PROMPT.txt)
    angle: str                 # one-sentence angle
    has_master: bool           # gate-3 reached (master.mp4 exists)
    has_qa_marker: bool        # gate-3 cleared (operator-approved)


def _parse_topic_id_date(topic_id: str) -> datetime | None:
    """Parse a topic-id like '2026-05-05_001' into a datetime, or None on bad input."""
    m = _TOPIC_ID_RE.match(topic_id)
    if not m:
        return None
    y, mo, d, _seq = m.groups()
    try:
        return datetime(int(y), int(mo), int(d))
    except ValueError:
        return None


def list_recent_topics(channel_root: Path, *, days: int = 30) -> list[TopicRecord]:
    """All topic records from the last `days` days, sorted newest first."""
    drafts_dir = channel_root / "02_scripts" / "_drafts"
    if not drafts_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    masters_dir = channel_root / "04_renders" / "_final_master"

    out: list[TopicRecord] = []
    for sub in drafts_dir.iterdir():
        if not sub.is_dir():
            continue
        topic_id = sub.name
        d = _parse_topic_id_date(topic_id)
        if d is None or d < cutoff:
            continue

        topic_text = ""
        angle_text = ""
        prompt_path = sub / "script_PROMPT.txt"
        if prompt_path.exists():
            content = prompt_path.read_text(encoding="utf-8", errors="replace")
            tm = _TOPIC_LINE_RE.search(content)
            am = _ANGLE_LINE_RE.search(content)
            if tm:
                topic_text = tm.group(1).strip()
            if am:
                angle_text = am.group(1).strip()

        master_path = masters_dir / f"{topic_id}_master.mp4"
        marker_path = masters_dir / f"{topic_id}{QA_MARKER_SUFFIX}"

        out.append(TopicRecord(
            topic_id=topic_id,
            date=d,
            topic=topic_text,
            angle=angle_text,
            has_master=master_path.exists(),
            has_qa_marker=marker_path.exists(),
        ))

    out.sort(key=lambda r: (r.date, r.topic_id), reverse=True)
    return out


def format_for_prompt(records: list[TopicRecord], *, max_items: int = 30) -> str:
    """Format a TopicRecord list for the `{RECENT_TOPICS}` substitution slot.

    Output is a markdown bullet list with topic-id + status + topic text. The
    idea-generation LLM reads this and avoids proposing direct rehashes.
    """
    if not records:
        return "(no recent topics yet)"
    lines: list[str] = []
    for r in records[:max_items]:
        if r.has_qa_marker:
            status = "approved"
        elif r.has_master:
            status = "rendered (no QA)"
        else:
            status = "drafted"
        topic_preview = r.topic if r.topic else "(topic text not recovered)"
        lines.append(f"- {r.topic_id} [{status}]: {topic_preview}")
    return "\n".join(lines)


def is_topic_id_uploaded(topic_id: str, channel_root: Path) -> bool:
    """Return True if `topic_id` has a recorded YouTube upload.

    Stricter than `is_topic_id_shipped`: only checks upload-evidence sources,
    NOT the gate-3 QA marker (which can exist for mid-flight topics awaiting
    variants/metadata/upload).

      1. `<channel_root>/06_published/<YYYY-MM>/<topic_id>/` exists as a directory
         (post-upload archive populated by `tools/archive_published.py`)
      2. A row in `<channel_root>/01_research/upload_log.csv` has its `topic_id`
         column matching the argument

    Used by `daily_batch.py` to detect stale persisted picks_assignment entries
    that point to topic_ids whose YouTube upload has already completed. The
    gate-3 marker is intentionally NOT a signal here because marker-present
    means "operator approved the master, pipeline is mid-flight" — those IDs
    must keep their persisted assignment so re-runs resume from gate 3.
    """
    m = _TOPIC_ID_RE.match(topic_id)
    if m is not None:
        y, mo, _d, _seq = m.groups()
        archive_dir = channel_root / "06_published" / f"{y}-{mo}" / topic_id
        if archive_dir.is_dir():
            return True

    upload_log = channel_root / "01_research" / "upload_log.csv"
    if upload_log.exists():
        try:
            with upload_log.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("topic_id") == topic_id:
                        return True
        except OSError as e:
            log.warning("failed to read upload_log.csv (%s); ignoring upload-log source", e)

    return False


def is_topic_id_shipped(topic_id: str, channel_root: Path) -> bool:
    """Return True if `topic_id` is anywhere from gate-3 approval through uploaded.

    Three sources are consulted; any single match returns True:

      1. `<channel_root>/04_renders/_final_master/<topic_id>_master_QA_APPROVED.marker`
         exists (gate-3 cleared by operator — pipeline is mid-flight or beyond)
      2. `<channel_root>/06_published/<YYYY-MM>/<topic_id>/` exists as a directory
         (post-upload archive populated by `tools/archive_published.py`)
      3. A row in `<channel_root>/01_research/upload_log.csv` has its `topic_id`
         column matching the argument (recorded YouTube upload)

    Failure to read the upload log is logged but not raised; sources 1 and 2 still
    answer authoritatively.

    Used by `next_topic_id_for_date` to skip already-allocated sequence numbers
    even when the drafts dir was relocated by cleanup_orphans. For "is this
    topic_id done with the pipeline / safe to recycle" decisions in
    daily_batch, use the stricter `is_topic_id_uploaded` instead — the marker
    is too aggressive there because mid-flight topics must keep their persisted
    assignment to resume past gate 3.
    """
    marker = channel_root / "04_renders" / "_final_master" / f"{topic_id}{QA_MARKER_SUFFIX}"
    if marker.exists():
        return True

    m = _TOPIC_ID_RE.match(topic_id)
    if m is not None:
        y, mo, _d, _seq = m.groups()
        archive_dir = channel_root / "06_published" / f"{y}-{mo}" / topic_id
        if archive_dir.is_dir():
            return True

    upload_log = channel_root / "01_research" / "upload_log.csv"
    if upload_log.exists():
        try:
            with upload_log.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("topic_id") == topic_id:
                        return True
        except OSError as e:
            log.warning("failed to read upload_log.csv (%s); ignoring source 3", e)

    return False


def next_topic_id_for_date(channel_root: Path, date: datetime | None = None) -> str:
    """Allocate the next free `YYYY-MM-DD_NNN` topic-id for the given date.

    The sequence number increments past two collision sources:
      - existing `<date>_NNN` topic dirs under `02_scripts/_drafts/`
      - shipped sequences per `is_topic_id_shipped()` (gate-3 marker, archive dir,
        or upload_log.csv row), even if the corresponding draft dir was deleted

    Default date is **UTC** today. Earlier versions defaulted to local time, which
    caused a cross-date allocation regression on 2026-05-08: daily_batch.py keys
    its daily_dir on UTC, but a late-night LOCAL EDT run had next_topic_id_for_date
    return yesterday-LOCAL-prefixed IDs that had already been shipped earlier the
    same LOCAL day. Stage 7.5 halt prevented master overwrite, but VO WAVs were
    regenerated under the colliding topic_id.
    """
    date = date or datetime.now(timezone.utc)
    date_prefix = date.strftime("%Y-%m-%d")
    drafts_dir = channel_root / "02_scripts" / "_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    used: set[int] = set()
    for sub in drafts_dir.iterdir():
        if not sub.is_dir():
            continue
        m = _TOPIC_ID_RE.match(sub.name)
        if not m:
            continue
        y, mo, d, seq = m.groups()
        if f"{y}-{mo}-{d}" == date_prefix:
            used.add(int(seq))

    seq = 1
    while True:
        candidate = f"{date_prefix}_{seq:03d}"
        if seq not in used and not is_topic_id_shipped(candidate, channel_root):
            return candidate
        seq += 1
