"""Mistake sentinels — 'learn from every mistake'.

Deterministic detectors over the ledger + shipped artifacts. They never fix
anything; they surface structured incidents + proposals. Three classes the
operator chose:

  * content flops    — matured videos whose reach is well below the cohort, with
                       a diagnosis of which reach levers each missed.
  * quality escapes  — shipped videos that broke a durable rule (missing title
                       anchor; a residual ``[VERIFY: ...]`` fact-check tag left
                       in script_FINAL.txt).
  * engineering      — the 0-duration upload footgun symptom (views but no
                       watch-time/retention data at all).

Strategy drift is handled by the experiment ledger (did a change move its
metric?), not re-implemented here. Run-time-only footguns (OAuth weekly revoke,
stale idea-gen same-day re-run, sub-agent backgrounding) are detected in
start.md at run time, not from the ledger.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from . import paths as learning_paths
from .analysis import AnalysisReport
from .ledger import LedgerRow
from .maturity import eligible_rows

log = logging.getLogger("learning.sentinels")

_VERIFY_TAG_RE = re.compile(r"\[VERIFY:", re.IGNORECASE)
_ANON_SOURCE_RE = re.compile(
    r"\b(a|one|some)\s+(reddit|x|twitter|hacker\s*news)?\s*(user|developer|dev|engineer|person)s?\b",
    re.IGNORECASE,
)
# Reach below this fraction of the cohort median = a flop.
_FLOP_FRACTION = 0.4


@dataclass
class Incident:
    type: str            # content_flop | quality_escape | engineering
    severity: str        # info | warn
    topic_id: str
    video_id: str | None
    detail: str
    suggestion: str
    ts: str = ""


@dataclass
class SentinelResult:
    incidents: list[Incident] = field(default_factory=list)
    flop_lever_counts: dict[str, int] = field(default_factory=dict)
    summary: str = ""


def diagnose_flops(rows: list[LedgerRow], report: AnalysisReport) -> tuple[list[Incident], dict[str, int]]:
    cohort_median = report.cohort_median_views
    if not cohort_median:
        return [], {}
    threshold = cohort_median * _FLOP_FRACTION
    lever_counts: dict[str, int] = {}
    incidents: list[Incident] = []
    for r in eligible_rows(rows):
        if r.views is None or r.views >= threshold:
            continue
        missed = []
        if r.title_anchor_present is False:
            missed.append("no_anchor")
        if r.duration_s is not None and r.duration_s >= 38.0:
            missed.append("over_38s")
        if r.hook_formula in (None, "UNTAGGED", "EDITED", "NO_HOOK_LOG"):
            missed.append("untagged_formula")
        if not r.cluster or r.cluster.upper() in ("DROP", "PRE"):
            missed.append("weak_or_no_cluster")
        for m in missed:
            lever_counts[m] = lever_counts.get(m, 0) + 1
        incidents.append(Incident(
            type="content_flop", severity="info", topic_id=r.topic_id, video_id=r.video_id,
            detail=(f"{r.views} views (< {threshold:.0f} = {_FLOP_FRACTION:.0%} of cohort median "
                    f"{cohort_median:.0f}); missed levers: {', '.join(missed) or 'none obvious'}"),
            suggestion=("retire this angle or re-cut with the missing levers: "
                        + (", ".join(missed) if missed else "review hook/topic fit")),
        ))
    return incidents, lever_counts


def detect_quality_escapes(channel_root: Path | str, rows: list[LedgerRow]) -> list[Incident]:
    """Durable-RULE violations only (residual fact-check tag / anonymous source).

    Soft levers like a missing title anchor are NOT escapes — they surface via
    flop diagnosis when they actually correlate with low reach, which keeps this
    detector low-noise and meaningful.
    """
    channel_root = Path(channel_root)
    drafts = channel_root / "02_scripts" / "_drafts"
    incidents: list[Incident] = []
    for r in eligible_rows(rows):
        # Residual [VERIFY:] tag left in the final script (a gate-2 escape).
        script = drafts / r.topic_id / "script_FINAL.txt"
        if script.exists():
            try:
                text = script.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _VERIFY_TAG_RE.search(text):
                incidents.append(Incident(
                    type="quality_escape", severity="warn", topic_id=r.topic_id, video_id=r.video_id,
                    detail="script_FINAL.txt still contains a [VERIFY: ...] fact-check tag",
                    suggestion="resolve or strip the tag before publish (durable rule #6)"))
            elif _ANON_SOURCE_RE.search(text):
                incidents.append(Incident(
                    type="quality_escape", severity="info", topic_id=r.topic_id, video_id=r.video_id,
                    detail="script_FINAL.txt cites an anonymous source ('a developer/user…')",
                    suggestion="replace with a named handle + retrievable URL (cited-observation rule)"))
    return incidents


def detect_zero_duration(rows: list[LedgerRow]) -> list[Incident]:
    """Symptom of the 0-duration / RedirectMissingLocation upload footgun: a
    matured video with views but NO watch-time/retention data at all."""
    incidents: list[Incident] = []
    for r in eligible_rows(rows):
        if (r.views and r.views > 0
                and (r.avg_view_duration_sec in (None, 0, 0.0))
                and (r.avg_view_pct in (None, 0, 0.0))):
            incidents.append(Incident(
                type="engineering", severity="warn", topic_id=r.topic_id, video_id=r.video_id,
                detail=f"{r.views} views but zero watch-time/retention — possible broken (P0D) upload",
                suggestion="verify the YouTube video duration; if dur=P0D, re-upload with chunksize=-1"))
    return incidents


def run_sentinels(
    channel_root: Path | str,
    rows: list[LedgerRow],
    report: AnalysisReport,
    today: date,
    *,
    write: bool = False,
) -> SentinelResult:
    flops, lever_counts = diagnose_flops(rows, report)
    escapes = detect_quality_escapes(channel_root, rows)
    broken = detect_zero_duration(rows)
    incidents = flops + escapes + broken
    ts = today.isoformat()
    for inc in incidents:
        inc.ts = ts

    if write and incidents:
        learning_paths.ensure_state_dir(channel_root)
        path = learning_paths.incidents_jsonl(channel_root)
        with path.open("a", encoding="utf-8") as f:
            for inc in incidents:
                f.write(json.dumps(asdict(inc), ensure_ascii=False) + "\n")

    top_lever = max(lever_counts, key=lever_counts.get) if lever_counts else None
    summary = (
        f"{len(flops)} flops, {len(escapes)} quality escapes, {len(broken)} broken-upload suspects"
        + (f"; flops most often miss '{top_lever}' ({lever_counts[top_lever]}x)" if top_lever else "")
    )
    log.info("sentinels: %s", summary)
    return SentinelResult(incidents=incidents, flop_lever_counts=lever_counts, summary=summary)


__all__ = [
    "Incident", "SentinelResult",
    "diagnose_flops", "detect_quality_escapes", "detect_zero_duration", "run_sentinels",
]
