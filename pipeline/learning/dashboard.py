"""Render the learning loop's read-only dashboard as Markdown.

Operator-facing snapshot: maturity breakdown, CSV health, reach-first per-feature
leaderboards (ranked by median views), and the top reach levers. Pure
formatting — takes already-computed objects, writes one Markdown file.
"""

from __future__ import annotations

from pathlib import Path

from .analysis import AnalysisReport, FeatureStat, TAIL_SMALL_N
from .csv_health import CsvHealthReport, format_report as format_csv_health


def _fmt_num(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _fmt_ci(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def _feature_table(features: list[FeatureStat]) -> list[str]:
    lines = [
        "| rank | value | n | median views | median avp% | reach-beat CI | evidence | retention |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for fs in features:
        ret = "⚠ below floor" if fs.retention_risk else "ok"
        lines.append(
            f"| {fs.rank} | {fs.value} | {fs.n} | {_fmt_num(fs.median_views)} | "
            f"{_fmt_num(fs.median_avg_view_pct)} | {_fmt_ci(fs.reach_hit_rate_ci)} | "
            f"{fs.evidence} | {ret} |"
        )
    return lines


def _tail_section(report: AnalysisReport) -> list[str]:
    """Right-tail / breakout watch — the median leaderboard is blind to the tail.

    Goal = a first single-video breakout (clearing the ~ceiling). This block
    tracks max / p90 / share-over-ceiling, a RAW breakout ledger (not a smoothed
    percentile), and a SEPARATE ceiling-clear leaderboard so a median lift can
    never be mistaken for breakout progress.
    """
    L: list[str] = ["## Right tail / breakout watch", ""]
    small = report.eligible_n < TAIL_SMALL_N
    caveat = f"  ⚠ small n (<{TAIL_SMALL_N}) — tail figures are noisy" if small else ""
    L.append(f"- Breakout ceiling: **{report.ceiling_views:,} views**{caveat}")
    L.append(f"- Max views: **{_fmt_num(report.max_views, 0)}**  ·  "
             f"p90 views: **{_fmt_num(report.p90_views, 0)}**")
    share = ("—" if report.share_over_ceiling is None
             else f"{report.share_over_ceiling:.0%}")
    L.append(f"- Videos over ceiling: **{report.count_over_ceiling}** "
             f"({share} of eligible)")
    L.append("")

    if report.breakout_videos:
        L.append("### Breakout ledger (videos over the ceiling)")
        L.append("")
        L.append("| topic_id | views | track | hook | dur(s) | anchor | slot | cluster |")
        L.append("|---|---|---|---|---|---|---|---|")
        for b in report.breakout_videos:
            anchor = "yes" if b.title_anchor_present else "no"
            L.append(
                f"| {b.topic_id} | {b.views:,} | {b.track or '—'} | "
                f"{b.hook_formula or '—'} | {_fmt_num(b.duration_s, 0)} | {anchor} | "
                f"{b.slot if b.slot is not None else '—'} | {b.cluster or '—'} |"
            )
        L.append("")
    else:
        L.append("_No video has cleared the breakout ceiling yet. The breakout is "
                 "the goal — keep instrumenting; this table is empty until craft × "
                 "topic produces the first outlier._")
        L.append("")

    if report.ceiling_leaders:
        L.append("### Ceiling-clear leaderboard (P(clear ceiling) — separate from median)")
        L.append("")
        L.append("| dimension | value | n | cleared | clear-rate | P(clear) CI |")
        L.append("|---|---|---|---|---|---|")
        for fs in report.ceiling_leaders[:10]:
            rate = f"{(fs.n_over_ceiling / fs.n):.0%}" if fs.n else "—"
            L.append(f"| {fs.dimension} | {fs.value} | {fs.n} | {fs.n_over_ceiling} | "
                     f"{rate} | {_fmt_ci(fs.ceiling_hit_rate_ci)} |")
        L.append("")

    return L


def render_dashboard(
    report: AnalysisReport,
    *,
    datestr: str,
    maturity: dict[str, int],
    csv_health: CsvHealthReport | None = None,
) -> str:
    L: list[str] = []
    L.append(f"# ShadowVerse learning dashboard — {datestr}")
    L.append("")
    L.append("> Reach-first (median views), retention as a guardrail floor. "
             "Read-only snapshot — auto-tuning (when enabled) acts only on "
             "features with `n ≥ min_sample` and no retention risk.")
    L.append("")
    L.append("## Cohort")
    L.append("")
    L.append(f"- Eligible (matured) videos: **{report.eligible_n}**")
    L.append(f"- Cohort median views: **{_fmt_num(report.cohort_median_views)}**")
    L.append(f"- Cohort median avg-view-%: **{_fmt_num(report.cohort_median_avg_view_pct)}**")
    L.append(f"- Retention floor: {report.retention_floor_pct:.1f}%  ·  min sample: {report.min_sample}")
    if report.hold_saturated:
        L.append("- ⚠ **hold_at_3s is saturated (~1.0)** — non-discriminating; "
                 "reach (views) is the ranking signal, not hold.")
    L.append("")
    L.append("- Maturity breakdown: "
             + ", ".join(f"`{k}`={v}" for k, v in sorted(maturity.items())))
    L.append("")

    L.extend(_tail_section(report))

    if report.leaders:
        L.append("## Top reach levers (evidence-backed, no retention risk)")
        L.append("")
        L.append("| dimension | value | n | median views | evidence |")
        L.append("|---|---|---|---|---|")
        for fs in report.leaders[:10]:
            L.append(f"| {fs.dimension} | {fs.value} | {fs.n} | "
                     f"{_fmt_num(fs.median_views)} | {fs.evidence} |")
        L.append("")
    else:
        L.append("## Top reach levers")
        L.append("")
        L.append("_No feature yet clears `n ≥ min_sample` + reach-positive + evidence. "
                 "Keep shipping; the loop needs more matured datapoints._")
        L.append("")

    L.append("## Per-dimension leaderboards (ranked by median views)")
    L.append("")
    for dr in report.dimensions:
        L.append(f"### {dr.dimension}")
        L.append("")
        if dr.features:
            L.extend(_feature_table(dr.features))
        else:
            L.append("_no data_")
        L.append("")

    if csv_health is not None:
        L.append("## Substrate health")
        L.append("")
        L.append("```")
        L.append(format_csv_health(csv_health))
        L.append("```")
        L.append("")

    return "\n".join(L)


def write_dashboard(content: str, out_path: Path | str) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return out_path


__all__ = ["render_dashboard", "write_dashboard"]
