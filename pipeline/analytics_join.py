"""Join hook selections, upload log, and weekly analytics into per-topic rows.

This module is a pure read-only joiner that produces the input the hook A/B
leaderboard math (Slice 4) and the human-readable report (Slice 5) consume.

Sources joined:
- Slice 1 hook selection JSONL (one record per topic_id; the source-of-truth
  universe of "videos we have hook data for").
- The existing `upload_log.csv` (topic_id -> youtube video_id mapping).
- The existing `_weekly_analytics.csv` (append-only, multiple pull_dates per
  video_id — the latest pull_date wins).

Eligibility rule (operator decision, 2026-05-12):
    `views >= 70` AND `hold_at_3s is not None` -> eligible for the leaderboard.
    Anything else gets a `reason` string explaining why and is excluded by
    Slice 4. The threshold is configurable via `eligibility_min_views` so
    tests and future tuning don't need to monkey-patch a constant.

Stdlib only — no pandas, no agent frameworks. The CSV reader is
backwards-compatible with the additive `hold_at_3s` /
`traffic_source_shorts_pct` columns appended on 2026-05-08: when the CSV
header predates them but newer rows append the values positionally,
csv.DictReader stuffs the extras under key `None` as a list. We pick those
out and graft them onto the row dict before parsing.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger("analytics_join")


# Order of trailing columns appended after the original 12-column header.
# Keep this in sync with analytics_pull.VideoMetrics if more columns are added.
_TRAILING_ANALYTICS_COLUMNS: tuple[str, ...] = (
    "hold_at_3s",
    "traffic_source_shorts_pct",
)

# Eligibility reasons. Surface as strings so the report (Slice 5) can render
# them directly. Keep this set frozen — Slice 4 / Slice 5 dispatch on values.
REASON_LOW_VIEWS = "low_views"
REASON_NO_HOLD_DATA = "no_hold_data"
REASON_NO_ANALYTICS_ROW = "no_analytics_row"
REASON_NOT_UPLOADED = "not_uploaded"
REASON_NO_HOOK_LOG = "no_hook_log"

# Sentinel formula values used when the JSONL row is missing or marks the
# topic as edited / untagged. Kept here so Slice 5's renderer can branch on
# them without re-deriving strings.
FORMULA_EDITED = "EDITED"
FORMULA_UNTAGGED = "UNTAGGED"
FORMULA_NO_HOOK_LOG = "NO_HOOK_LOG"


@dataclass(frozen=True)
class HookPerformanceRow:
    """One denormalized row per topic_id, ready for leaderboard math.

    Mirrors the contract Slice 4 consumes via Protocol; do not add or
    reorder fields without coordinating with Slice 4.

    Attributes:
        topic_id: Canonical topic id, e.g. "2026-05-10_001".
        video_id: YouTube video id if the topic was uploaded, else None.
        hook_letter: "A" / "B" / "C" if a variant was picked; None otherwise.
        hook_text: Human-readable hook string. Always populated.
        formula: Canonical formula name (e.g. "Named-Actor"), or one of the
            module-level FORMULA_* sentinels.
        views: Latest pull's view count, or None if no analytics row exists.
        hold_at_3s: 3-second retention ratio in [0, 1], or None.
        avg_view_pct: Average view percentage 0..100 (CSV native scale).
        days_live: `today - published_at` in days, or None if not uploaded /
            no analytics row.
        eligible_for_leaderboard: True iff views >= eligibility_min_views
            AND hold_at_3s is not None.
        reason: Populated when eligible_for_leaderboard is False; one of the
            REASON_* sentinels.
    """

    topic_id: str
    video_id: str | None
    hook_letter: str | None
    hook_text: str
    formula: str
    views: int | None
    hold_at_3s: float | None
    avg_view_pct: float | None
    days_live: int | None
    eligible_for_leaderboard: bool
    reason: str | None


# ---------------------------------------------------------------------------
# Pure parsers (small helpers — easy to unit test in isolation)
# ---------------------------------------------------------------------------


def _coerce_optional_int(raw: str | None) -> int | None:
    """Parse a CSV cell into int, treating empty / None as missing."""
    if raw is None or raw == "":
        return None
    return int(raw)


def _coerce_optional_float(raw: str | None) -> float | None:
    """Parse a CSV cell into float, treating empty / None as missing."""
    if raw is None or raw == "":
        return None
    return float(raw)


def _parse_published_at(raw: str | None) -> date | None:
    """Parse the published_at field. Accepts plain date or ISO datetime."""
    if not raw:
        return None
    # Strip a trailing Z or +00:00 if present so date.fromisoformat is happy.
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            log.warning("could not parse published_at=%r", raw)
            return None


def _normalize_analytics_row(row: dict) -> dict:
    """Graft positionally-appended trailing columns onto the row dict.

    csv.DictReader puts extra trailing values under key `None` as a list.
    The analytics CSV's header was frozen before `hold_at_3s` and
    `traffic_source_shorts_pct` were added (2026-05-08), so newer rows
    have 14 values against a 12-column header. This helper makes the row
    look like a 14-column one regardless of whether the header was
    upgraded.
    """
    extras = row.pop(None, None)
    if extras:
        for col_name, value in zip(_TRAILING_ANALYTICS_COLUMNS, extras):
            # Don't overwrite an explicit header-driven value with the
            # positional fallback; header wins if both somehow exist.
            row.setdefault(col_name, value)
    for col_name in _TRAILING_ANALYTICS_COLUMNS:
        row.setdefault(col_name, "")
    return row


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------


def _load_hook_log(log_path: Path) -> dict[str, dict]:
    """Read the hook selection JSONL into a topic_id -> row dict.

    Empty / missing files yield an empty dict (not an error) — that's the
    expected state on a brand-new install before Slice 1 has logged
    anything yet.
    """
    out: dict[str, dict] = {}
    if not log_path.exists() or log_path.stat().st_size == 0:
        return out
    with log_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("hook log line %d not valid JSON: %s", lineno, exc)
                continue
            topic_id = row.get("topic_id")
            if not topic_id:
                log.warning("hook log line %d missing topic_id", lineno)
                continue
            # Last write wins if the JSONL has duplicate topic_ids — the
            # latest entry reflects the latest manual edit / re-pick.
            out[topic_id] = row
    return out


def _load_upload_log(upload_log_path: Path) -> dict[str, str]:
    """Read upload_log.csv into a topic_id -> video_id dict.

    If the same topic_id appears more than once (re-uploads), the LAST row
    wins. Old rows are kept for forensic history but should not influence
    the join.
    """
    out: dict[str, str] = {}
    if not upload_log_path.exists() or upload_log_path.stat().st_size == 0:
        return out
    with upload_log_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            topic_id = (row.get("topic_id") or "").strip()
            video_id = (row.get("video_id") or "").strip()
            if not topic_id or not video_id:
                continue
            out[topic_id] = video_id
    return out


def _load_latest_analytics(analytics_csv_path: Path) -> dict[str, dict]:
    """Read the analytics CSV into a video_id -> latest_row dict.

    "Latest" means max `pull_date`. The CSV is append-only with multiple
    rows per video_id — every weekly pull adds a fresh row.
    """
    out: dict[str, dict] = {}
    if not analytics_csv_path.exists() or analytics_csv_path.stat().st_size == 0:
        return out
    with analytics_csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = _normalize_analytics_row(dict(raw))
            video_id = (row.get("video_id") or "").strip()
            pull_date_str = (row.get("pull_date") or "").strip()
            if not video_id or not pull_date_str:
                continue
            existing = out.get(video_id)
            if existing is None or pull_date_str > (existing.get("pull_date") or ""):
                out[video_id] = row
    return out


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------


def _hook_fields(hook_row: dict) -> tuple[str | None, str, str]:
    """Extract (hook_letter, hook_text, formula) from a JSONL row.

    Mirrors the locked Slice 1 schema. Missing / unexpected values fall
    back to the FORMULA_* sentinels so Slice 5's renderer always has a
    string to print.
    """
    hook_letter = hook_row.get("hook_letter")
    hook_text = hook_row.get("hook_text") or ""
    formula = hook_row.get("formula")
    if not formula:
        formula = FORMULA_UNTAGGED
    if hook_letter is not None and not isinstance(hook_letter, str):
        hook_letter = str(hook_letter)
    return hook_letter, hook_text, formula


def _build_row(
    *,
    topic_id: str,
    hook_row: dict,
    upload_index: dict[str, str],
    analytics_index: dict[str, dict],
    today: date,
    eligibility_min_views: int,
) -> HookPerformanceRow:
    """Assemble one HookPerformanceRow from the three source indexes."""
    hook_letter, hook_text, formula = _hook_fields(hook_row)
    video_id = upload_index.get(topic_id)

    if video_id is None:
        return HookPerformanceRow(
            topic_id=topic_id,
            video_id=None,
            hook_letter=hook_letter,
            hook_text=hook_text,
            formula=formula,
            views=None,
            hold_at_3s=None,
            avg_view_pct=None,
            days_live=None,
            eligible_for_leaderboard=False,
            reason=REASON_NOT_UPLOADED,
        )

    analytics_row = analytics_index.get(video_id)
    if analytics_row is None:
        return HookPerformanceRow(
            topic_id=topic_id,
            video_id=video_id,
            hook_letter=hook_letter,
            hook_text=hook_text,
            formula=formula,
            views=None,
            hold_at_3s=None,
            avg_view_pct=None,
            days_live=None,
            eligible_for_leaderboard=False,
            reason=REASON_NO_ANALYTICS_ROW,
        )

    views = _coerce_optional_int(analytics_row.get("views"))
    hold_at_3s = _coerce_optional_float(analytics_row.get("hold_at_3s"))
    avg_view_pct = _coerce_optional_float(analytics_row.get("avg_view_pct"))
    published_at = _parse_published_at(analytics_row.get("published_at"))
    days_live = (today - published_at).days if published_at is not None else None

    if views is None or views < eligibility_min_views:
        reason = REASON_LOW_VIEWS
        eligible = False
    elif hold_at_3s is None:
        reason = REASON_NO_HOLD_DATA
        eligible = False
    else:
        reason = None
        eligible = True

    return HookPerformanceRow(
        topic_id=topic_id,
        video_id=video_id,
        hook_letter=hook_letter,
        hook_text=hook_text,
        formula=formula,
        views=views,
        hold_at_3s=hold_at_3s,
        avg_view_pct=avg_view_pct,
        days_live=days_live,
        eligible_for_leaderboard=eligible,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def join_hooks_to_analytics(
    channel_root: Path,
    *,
    log_path: Path | None = None,
    upload_log_path: Path | None = None,
    analytics_csv_path: Path | None = None,
    today: date | None = None,
    eligibility_min_views: int = 70,
) -> list[HookPerformanceRow]:
    """Join hook selections, the upload log, and weekly analytics.

    The JSONL is the source-of-truth universe — only topic_ids present in
    the JSONL produce output rows. Orphan upload-log entries (uploaded
    without ever logging a hook pick) are intentionally skipped; Slice 6 /
    Slice 8 own that gap.

    Args:
        channel_root: Repo-root for ShadowVerse, e.g.
            ``C:/ContentOps/channels/ShadowVerse``. Used to resolve the
            three default source paths under ``01_research/``.
        log_path: Override for the hook selection JSONL.
        upload_log_path: Override for the upload log CSV.
        analytics_csv_path: Override for the weekly analytics CSV.
        today: Override "today" for deterministic date math in tests.
            Defaults to ``date.today()``.
        eligibility_min_views: Minimum views to be leaderboard-eligible.
            Defaults to 70 (operator decision 2026-05-12).

    Returns:
        One ``HookPerformanceRow`` per topic_id in the JSONL, in input
        order (insertion-order preserved by the dict).
    """
    research_root = Path(channel_root) / "01_research"
    log_path = log_path or research_root / "hook_selection_log.jsonl"
    upload_log_path = upload_log_path or research_root / "upload_log.csv"
    analytics_csv_path = (
        analytics_csv_path or research_root / "_weekly_analytics.csv"
    )
    today = today or date.today()

    log.info(
        "joining hooks=%s upload=%s analytics=%s today=%s min_views=%d",
        log_path, upload_log_path, analytics_csv_path, today, eligibility_min_views,
    )

    hook_index = _load_hook_log(log_path)
    upload_index = _load_upload_log(upload_log_path)
    analytics_index = _load_latest_analytics(analytics_csv_path)

    rows: list[HookPerformanceRow] = []
    for topic_id, hook_row in hook_index.items():
        rows.append(
            _build_row(
                topic_id=topic_id,
                hook_row=hook_row,
                upload_index=upload_index,
                analytics_index=analytics_index,
                today=today,
                eligibility_min_views=eligibility_min_views,
            )
        )
    log.info("emitted %d rows (%d eligible)",
             len(rows), sum(1 for r in rows if r.eligible_for_leaderboard))
    return rows


__all__ = [
    "FORMULA_EDITED",
    "FORMULA_NO_HOOK_LOG",
    "FORMULA_UNTAGGED",
    "HookPerformanceRow",
    "REASON_LOW_VIEWS",
    "REASON_NO_ANALYTICS_ROW",
    "REASON_NOT_UPLOADED",
    "REASON_NO_HOLD_DATA",
    "REASON_NO_HOOK_LOG",
    "join_hooks_to_analytics",
]


def _iter_eligible(rows: Iterable[HookPerformanceRow]) -> Iterable[HookPerformanceRow]:
    """Convenience: filter to leaderboard-eligible rows. Used by Slice 4."""
    return (r for r in rows if r.eligible_for_leaderboard)
