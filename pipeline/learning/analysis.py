"""Reach-first performance analysis over the learning ledger.

Objective (operator decision): optimize REACH — median ``views`` — with
``avg_view_pct`` (retention) as a guardrail FLOOR, never the target. This
matches the breakout finding that reach (not retention) is the bottleneck and
that ``hold_at_3s`` is saturated (~1.0 across the corpus) and therefore
non-discriminating — so we rank on views, and only use a retention floor to
veto a reach-positive change that would tank watch-through.

For each feature dimension (hook formula, duration bucket, title anchor, slot,
cluster, track) we group the matured/eligible rows by value and compute:
  * n, median_views (reach), median_avg_view_pct (retention guardrail metric),
  * a reach hit-rate Wilson CI: P(this value's videos beat the COHORT median on
    views) — the views-analog of the existing hook leaderboard's hold hit-rate,
    reusing ``hook_leaderboard_stats.wilson_score_ci`` + ``evidence_strength``,
  * ``retention_risk``: median_avg_view_pct < retention_floor_pct.

Pure functions, stdlib + reuse only. No I/O, no config mutation.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Callable

from hook_leaderboard_stats import evidence_strength, wilson_score_ci

from .ledger import LedgerRow
from .maturity import DEFAULT_MIN_SAMPLE, eligible_rows

log = logging.getLogger("learning.analysis")

DEFAULT_RETENTION_FLOOR_PCT: float = 25.0
# Above this cohort-median hold_at_3s we treat the metric as saturated and warn
# that hold is not a usable discriminator (rank on views instead).
_HOLD_SATURATION = 0.99

# A single video clearing this view count is the channel's current breakout
# ceiling (~1.2K observed as of 2026-06). The median leaderboard is blind to the
# right tail by construction (it ranks on P(beats median)); this threshold drives
# a SEPARATE breakout view — P(clear ceiling), share-over-ceiling, and a raw
# breakout ledger — so a reach-positive median lift can never be mistaken for
# breakout progress. Added 2026-06-24 (analytics deep-dive).
DEFAULT_CEILING_VIEWS: int = 1000
# Below this many eligible videos the p90 / tail figures are noisy — surfaces flag it.
TAIL_SMALL_N: int = 40


@dataclass
class FeatureStat:
    dimension: str
    value: str
    n: int
    median_views: float | None
    median_avg_view_pct: float | None
    median_traffic_shorts_pct: float | None
    reach_hit_rate_ci: tuple[float, float] | None  # Wilson CI: beats cohort median views
    evidence: str
    retention_risk: bool
    rank: int = 0
    # Right-tail (breakout) instrumentation — Wilson CI for P(this value's videos
    # clear the breakout ceiling), kept SEPARATE from reach_hit_rate (beats median).
    ceiling_hit_rate_ci: tuple[float, float] | None = None
    n_over_ceiling: int = 0


@dataclass
class BreakoutVideo:
    """One eligible video above the breakout ceiling, with the features the loop
    tracks — the raw right-tail ledger (not a smoothed percentile)."""
    topic_id: str
    video_id: str | None
    title: str | None
    views: int
    track: str | None
    hook_formula: str | None
    duration_s: float | None
    title_anchor_present: bool | None
    slot: int | None
    cluster: str | None
    avg_view_pct: float | None = None


@dataclass
class DimensionReport:
    dimension: str
    cohort_n: int
    features: list[FeatureStat] = field(default_factory=list)


@dataclass
class AnalysisReport:
    eligible_n: int
    cohort_median_views: float | None
    cohort_median_avg_view_pct: float | None
    retention_floor_pct: float
    min_sample: int
    hold_saturated: bool
    dimensions: list[DimensionReport] = field(default_factory=list)
    leaders: list[FeatureStat] = field(default_factory=list)
    # Right-tail (breakout) instrumentation — the median fields above cannot see a
    # breakout; these make the right tail first-class. Defaults keep older callers safe.
    ceiling_views: int = DEFAULT_CEILING_VIEWS
    max_views: float | None = None
    p90_views: float | None = None
    count_over_ceiling: int = 0
    share_over_ceiling: float | None = None
    breakout_videos: list[BreakoutVideo] = field(default_factory=list)
    ceiling_leaders: list[FeatureStat] = field(default_factory=list)


def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _percentile(vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 100]). None on empty. Stdlib only."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    rank = max(1, math.ceil((q / 100.0) * len(s)))
    return float(s[min(rank, len(s)) - 1])


def _duration_bucket(r: LedgerRow) -> str | None:
    if r.duration_s is None:
        return None
    return "<38s" if r.duration_s < 38.0 else ">=38s"


def _anchor_value(r: LedgerRow) -> str | None:
    if r.title_anchor_present is None:
        return None
    return "anchor" if r.title_anchor_present else "no_anchor"


# (dimension name, value extractor). A None value drops the row from that dim.
DIMENSIONS: list[tuple[str, Callable[[LedgerRow], str | None]]] = [
    ("hook_formula", lambda r: r.hook_formula or None),
    ("duration_bucket", _duration_bucket),
    ("title_anchor", _anchor_value),
    ("slot", lambda r: f"slot_{r.slot}" if r.slot is not None else None),
    ("cluster", lambda r: r.cluster or None),
    ("track", lambda r: r.track or None),
]


def _analyze_dimension(
    rows: list[LedgerRow],
    dimension: str,
    key_fn: Callable[[LedgerRow], str | None],
    cohort_median_views: float | None,
    retention_floor_pct: float,
    ceiling_views: int,
) -> DimensionReport:
    groups: dict[str, list[LedgerRow]] = {}
    for r in rows:
        k = key_fn(r)
        if k:
            groups.setdefault(k, []).append(r)

    feats: list[FeatureStat] = []
    for value, grp in groups.items():
        views = [r.views for r in grp if r.views is not None]
        avps = [r.avg_view_pct for r in grp if r.avg_view_pct is not None]
        traffics = [r.traffic_source_shorts_pct for r in grp if r.traffic_source_shorts_pct is not None]
        med_views = _median([float(v) for v in views])
        med_avp = _median(avps)
        if views and cohort_median_views is not None:
            successes = sum(1 for v in views if v > cohort_median_views)
            wilson = wilson_score_ci(successes, len(views))
            evidence = evidence_strength(len(views), wilson)
        else:
            wilson, evidence = None, "insufficient"
        # Right-tail: how many of this value's videos cleared the breakout ceiling.
        n_over_ceiling = sum(1 for v in views if v > ceiling_views)
        ceiling_ci = wilson_score_ci(n_over_ceiling, len(views)) if views else None
        feats.append(
            FeatureStat(
                dimension=dimension,
                value=value,
                n=len(grp),
                median_views=med_views,
                median_avg_view_pct=med_avp,
                median_traffic_shorts_pct=_median(traffics),
                reach_hit_rate_ci=wilson,
                evidence=evidence,
                retention_risk=(med_avp is not None and med_avp < retention_floor_pct),
                ceiling_hit_rate_ci=ceiling_ci,
                n_over_ceiling=n_over_ceiling,
            )
        )

    feats.sort(
        key=lambda fs: fs.median_views if fs.median_views is not None else float("-inf"),
        reverse=True,
    )
    for i, fs in enumerate(feats, start=1):
        fs.rank = i
    return DimensionReport(dimension=dimension, cohort_n=len(rows), features=feats)


def analyze(
    rows: list[LedgerRow],
    *,
    retention_floor_pct: float = DEFAULT_RETENTION_FLOOR_PCT,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    ceiling_views: int = DEFAULT_CEILING_VIEWS,
) -> AnalysisReport:
    """Reach-first analysis of the ledger. Operates only on eligible rows."""
    cohort = eligible_rows(rows)
    all_views = [float(r.views) for r in cohort if r.views is not None]
    all_avp = [r.avg_view_pct for r in cohort if r.avg_view_pct is not None]
    all_hold = [r.hold_at_3s for r in cohort if r.hold_at_3s is not None]
    cohort_median_views = _median(all_views)
    hold_saturated = bool(all_hold) and (_median(all_hold) or 0.0) >= _HOLD_SATURATION

    # Right-tail (breakout) stats — the median fields are blind to the tail.
    max_views = max(all_views) if all_views else None
    p90_views = _percentile(all_views, 90.0)
    count_over_ceiling = sum(1 for v in all_views if v > ceiling_views)
    share_over_ceiling = (count_over_ceiling / len(all_views)) if all_views else None
    breakout_videos = sorted(
        (
            BreakoutVideo(
                topic_id=r.topic_id, video_id=r.video_id, title=r.title,
                views=int(r.views), track=r.track, hook_formula=r.hook_formula,
                duration_s=r.duration_s, title_anchor_present=r.title_anchor_present,
                slot=r.slot, cluster=r.cluster, avg_view_pct=r.avg_view_pct,
            )
            for r in cohort
            if r.views is not None and r.views > ceiling_views
        ),
        key=lambda b: b.views, reverse=True,
    )

    dimensions = [
        _analyze_dimension(cohort, dim, key_fn, cohort_median_views,
                           retention_floor_pct, ceiling_views)
        for dim, key_fn in DIMENSIONS
    ]

    # Leaders: reach-positive feature values with real evidence and no retention
    # risk — the actionable signal the tuner maps to config knobs.
    leaders: list[FeatureStat] = []
    for dr in dimensions:
        for fs in dr.features:
            if (
                fs.n >= min_sample
                and fs.evidence in ("weak", "strong")
                and cohort_median_views is not None
                and fs.median_views is not None
                and fs.median_views > cohort_median_views
                and not fs.retention_risk
            ):
                leaders.append(fs)
    leaders.sort(
        key=lambda fs: (fs.evidence == "strong", fs.median_views or 0.0), reverse=True
    )

    # Ceiling leaders: feature values whose videos actually CLEAR the breakout
    # ceiling. Ranked by observed clear-rate (n_over_ceiling / n). Strictly
    # separate from `leaders` (median-beat) so a floor-lift is never read as a
    # breakout signal. With small n and rare >ceiling events this is often empty —
    # that itself is the honest message ("no feature has produced a breakout yet").
    ceiling_leaders: list[FeatureStat] = [
        fs
        for dr in dimensions
        for fs in dr.features
        if fs.n >= min_sample and fs.n_over_ceiling > 0
    ]
    ceiling_leaders.sort(
        key=lambda fs: (fs.n_over_ceiling / fs.n if fs.n else 0.0, fs.n_over_ceiling),
        reverse=True,
    )

    report = AnalysisReport(
        eligible_n=len(cohort),
        cohort_median_views=cohort_median_views,
        cohort_median_avg_view_pct=_median(all_avp),
        retention_floor_pct=retention_floor_pct,
        min_sample=min_sample,
        hold_saturated=hold_saturated,
        dimensions=dimensions,
        leaders=leaders,
        ceiling_views=ceiling_views,
        max_views=max_views,
        p90_views=p90_views,
        count_over_ceiling=count_over_ceiling,
        share_over_ceiling=share_over_ceiling,
        breakout_videos=breakout_videos,
        ceiling_leaders=ceiling_leaders,
    )
    log.info(
        "analysis: %d eligible, cohort median views=%s, max=%s, >%d ceiling=%d "
        "(%s), %d leaders, %d ceiling-leaders, hold_saturated=%s",
        report.eligible_n, cohort_median_views, max_views, ceiling_views,
        count_over_ceiling,
        (f"{share_over_ceiling:.0%}" if share_over_ceiling is not None else "n/a"),
        len(leaders), len(ceiling_leaders), hold_saturated,
    )
    return report


__all__ = [
    "FeatureStat",
    "BreakoutVideo",
    "DimensionReport",
    "AnalysisReport",
    "DEFAULT_RETENTION_FLOOR_PCT",
    "DEFAULT_CEILING_VIEWS",
    "TAIL_SMALL_N",
    "DIMENSIONS",
    "analyze",
]
