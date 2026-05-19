"""Hook formula leaderboard — CLI + Markdown report.

Renders a per-formula leaderboard from the hook-selection JSONL plus the
weekly analytics CSV. This is Slice 5 of the Sprint 4 hook-A/B retrospective:
it wires Slice 3 (`analytics_join.join_hooks_to_analytics`) into Slice 4
(`hook_leaderboard_stats.formula_medians` + `rank_formulas`), then renders
the result as a four-section Markdown report:

    1. Cohort summary       — eligibility funnel
    2. Formula leaderboard  — ranked table with Wilson CI + evidence labels
    3. Per-video appendix   — every topic the JSONL knows about
    4. Coverage gaps        — formulas in `viral_hooks.md` with zero
                              eligible shipped videos

Reads:
  - hook_selection_log.jsonl   (Slice 1 output, under <channel_root>/01_research/)
  - upload_log.csv             (existing)
  - _weekly_analytics.csv      (existing, append-only)
  - prompts/library/viral_hooks.md (for the canonical formula list)

Writes:
  - <project_root>/audit_2026-05-07/leaderboards/hook_leaderboard_<UTC-DATE>.md
    Override the directory with `--output-dir`. `--dry-run` prints to stdout
    instead of writing.

CLI:
    python hook_leaderboard.py
    python hook_leaderboard.py --config <path>
    python hook_leaderboard.py --output-dir <dir>
    python hook_leaderboard.py --dry-run

Determinism: the rendered Markdown is byte-identical when run twice on the
same data, modulo the `<UTC-DATE>` heading line. The leaderboard table is
sorted by (rank, formula); the per-video appendix by topic_id; the coverage
gap list alphabetically. Stdlib only — no jinja2, no pandas, no agent
frameworks.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from analytics_join import (
    FORMULA_EDITED,
    FORMULA_NO_HOOK_LOG,
    FORMULA_UNTAGGED,
    REASON_LOW_VIEWS,
    REASON_NOT_UPLOADED,
    REASON_NO_ANALYTICS_ROW,
    REASON_NO_HOLD_DATA,
    REASON_NO_HOOK_LOG,
    HookPerformanceRow,
    join_hooks_to_analytics,
)
from hook_leaderboard_stats import (
    FormulaRank,
    formula_medians,
    rank_formulas,
)

log = logging.getLogger("hook_leaderboard")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Default project root — the OneDrive-synced strategy/reference home.
_DEFAULT_PROJECT_ROOT = Path(r"C:\Users\laxmi\Documents\Project")

# Default channel root — production scratch (NOT OneDrive-synced).
_DEFAULT_CHANNEL_ROOT = Path(r"C:\ContentOps\channels\ShadowVerse")

# Default report subdirectory under the project root. The audit-date stem
# matches the existing strategy folder (`audit_2026-05-07/`) where
# leaderboards live alongside the rest of the hook-A/B research.
_DEFAULT_REPORT_SUBDIR = Path("audit_2026-05-07") / "leaderboards"

# viral_hooks.md lives in the pipeline repo, alongside the other prompt
# library files. The CLI accepts an override.
_DEFAULT_VIRAL_HOOKS_MD = Path("prompts") / "library" / "viral_hooks.md"

# Eligibility threshold (operator decision 2026-05-12, locked in Slice 3).
_DEFAULT_MIN_VIEWS = 70

# Hard-coded fallback list of canonical formula names, used when the
# viral_hooks.md file is missing or unparseable. Mirrors the 11 entries the
# library currently defines (see slice 5 spec, 2026-05-12).
_FALLBACK_FORMULAS: tuple[str, ...] = (
    "Contradiction",
    "Specific-Number Promise",
    "Result-First Mid-Action",
    "Comparison Frame",
    "Anti-Pattern Setup",
    "Specific-Question",
    "Measured-Claim",
    "Cited-Observation Lead",
    "Format-Branded",
    "You're Doing It Wrong",
    "Result-First / Mid-Action",
)

# Regex matching `### N. <formula header>` in viral_hooks.md.
_FORMULA_HEADER_RE = re.compile(
    r"^\s*###\s+(?P<num>\d+)\s*\.\s*(?P<title>.+?)\s*$"
)

# Reason -> human-readable label, for the cohort-summary breakdown.
_REASON_LABELS: dict[str, str] = {
    REASON_NOT_UPLOADED: "not yet uploaded",
    REASON_NO_ANALYTICS_ROW: "no analytics row yet",
    REASON_LOW_VIEWS: "below view threshold",
    REASON_NO_HOLD_DATA: "no hold@3s data",
    REASON_NO_HOOK_LOG: "no hook log entry",
}


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeaderboardReport:
    """In-memory representation of the rendered report.

    Each section is exposed individually for tests that want to assert on
    one part without parsing the whole document. `full_markdown` is the
    concatenation that `write_report` persists to disk.
    """

    cohort_summary: str
    formula_table: str
    per_video_appendix: str
    coverage_gaps: str
    full_markdown: str


# ---------------------------------------------------------------------------
# viral_hooks.md parser
# ---------------------------------------------------------------------------

def _canonicalize_formula_name(raw: str) -> str:
    """Strip the leading 'The ' and trailing parenthetical from a header.

    `### 1. The Contradiction Hook (Theo / Fireship signature)` -> `Contradiction`.
    The operator's spec defines the canonical short names; this helper
    produces them deterministically from the header text.
    """
    title = raw.strip()
    # Strip trailing parenthetical (signature / attribution).
    paren_idx = title.find("(")
    if paren_idx != -1:
        title = title[:paren_idx].rstrip()
    # Drop leading 'The ' if present.
    if title.lower().startswith("the "):
        title = title[4:].strip()
    # Strip trailing ' Hook' suffix — most headers wear it.
    if title.lower().endswith(" hook"):
        title = title[:-5].rstrip()
    # Strip surrounding quotes AFTER the 'The ... Hook' wrapper is gone, so
    # `The "You're Doing It Wrong" Hook` -> `You're Doing It Wrong` (no quotes).
    # Done in a small loop because some headers wrap the inner title in
    # smart-quote pairs that survive a single .strip() pass.
    for _ in range(2):
        title = title.strip().strip('"').strip("'").strip()
    return title.strip()


def _extract_canonical_formulas(viral_hooks_md_path: Path) -> list[str]:
    """Return the canonical formula names from `viral_hooks.md`.

    Loose regex matches `### N. <text>` so adding a 12th formula to the
    file doesn't require code changes. Returns the hard-coded fallback
    list when the file is missing or no headers parse.
    """
    if not viral_hooks_md_path.exists():
        log.warning(
            "viral_hooks.md not found at %s; using fallback formula list",
            viral_hooks_md_path,
        )
        return list(_FALLBACK_FORMULAS)

    names: list[str] = []
    try:
        text = viral_hooks_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("could not read %s: %s; using fallback", viral_hooks_md_path, exc)
        return list(_FALLBACK_FORMULAS)

    for line in text.splitlines():
        m = _FORMULA_HEADER_RE.match(line)
        if not m:
            continue
        canonical = _canonicalize_formula_name(m.group("title"))
        if canonical:
            names.append(canonical)

    if not names:
        log.warning("no formula headers parsed from %s; using fallback", viral_hooks_md_path)
        return list(_FALLBACK_FORMULAS)
    return names


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _format_float(value: float | None, *, places: int = 2) -> str:
    """Render an optional float with the given decimal places, '—' for None."""
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def _format_int(value: int | None) -> str:
    """Render an optional int, '—' for None."""
    if value is None:
        return "—"
    return f"{value}"


def _format_ci(ci: tuple[float, float] | None) -> str:
    """Render a Wilson CI tuple as `[lo, hi]` with 2 decimals."""
    if ci is None:
        return "—"
    return f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def _format_eligible(eligible: bool) -> str:
    """ASCII checkmark / dash for eligibility (avoids unicode surprises)."""
    return "yes" if eligible else "no"


def _top_examples_for_formula(
    formula: str, eligible_rows: list[HookPerformanceRow], k: int = 3,
) -> str:
    """Render up to `k` top eligible rows for a formula as `topic_id (Nv)`.

    Selection order: views desc; tiebreak topic_id asc (deterministic).
    """
    matching = [r for r in eligible_rows if r.formula == formula and r.views is not None]
    matching.sort(key=lambda r: (-(r.views or 0), r.topic_id))
    if not matching:
        return "—"
    parts = [f"{r.topic_id} ({r.views}v)" for r in matching[:k]]
    return ", ".join(parts)


def _render_cohort_summary(
    rows: list[HookPerformanceRow],
    eligible_rows: list[HookPerformanceRow],
) -> str:
    """Render the cohort-summary section as Markdown."""
    total = len(rows)
    eligible_n = len(eligible_rows)
    ineligible = [r for r in rows if not r.eligible_for_leaderboard]
    reason_counts = Counter((r.reason or "unknown") for r in ineligible)

    lines: list[str] = ["## Cohort summary", ""]
    lines.append(f"- Total topics in hook log: {total}")
    lines.append(f"- Eligible for leaderboard: {eligible_n}")

    if reason_counts:
        lines.append("- Ineligible breakdown:")
        for reason in sorted(reason_counts):
            label = _REASON_LABELS.get(reason, reason)
            lines.append(f"    - {label}: {reason_counts[reason]}")
    else:
        lines.append("- Ineligible breakdown: (none)")

    if eligible_rows:
        # Date range comes from the analytics row's published_at (proxied via
        # days_live + today). The join already filtered to rows with a
        # published_at, so days_live is reliable here.
        live_days = [r.days_live for r in eligible_rows if r.days_live is not None]
        if live_days:
            lines.append(
                f"- Days-live range of eligible videos: {min(live_days)} to {max(live_days)}"
            )
        else:
            lines.append("- Days-live range of eligible videos: (unavailable)")
    else:
        lines.append("- No eligible videos yet — leaderboard is empty.")

    return "\n".join(lines)


def _render_formula_table(
    ranked: list[FormulaRank],
    eligible_rows: list[HookPerformanceRow],
) -> str:
    """Render the formula leaderboard table.

    Sort key (after the rank assignment Slice 4 produces): (rank asc,
    formula asc). The alphabetical formula tiebreaker is the operator's
    determinism requirement — Slice 4 uses dict insertion order on ties,
    which would render non-deterministically on re-runs.
    """
    lines: list[str] = ["## Formula leaderboard", ""]
    if not ranked:
        lines.append("_No eligible videos yet — leaderboard cannot be ranked._")
        return "\n".join(lines)

    # Stable secondary sort by formula name to make output deterministic.
    sorted_ranked = sorted(ranked, key=lambda r: (r.rank, r.formula))

    lines.append(
        "| Rank | Formula | n | Median hold@3s | Median AVD% | "
        "Wilson CI (above cohort median) | Evidence | Top examples |"
    )
    lines.append(
        "|---:|---|---:|---:|---:|---|---|---|"
    )
    for r in sorted_ranked:
        examples = _top_examples_for_formula(r.formula, eligible_rows)
        lines.append(
            f"| {r.rank} | {r.formula} | {r.n} | "
            f"{_format_float(r.median_hold_at_3s, places=2)} | "
            f"{_format_float(r.median_avg_view_pct, places=1)} | "
            f"{_format_ci(r.wilson_ci_above_cohort_median)} | "
            f"{r.evidence_strength} | {examples} |"
        )
    return "\n".join(lines)


def _render_per_video_appendix(rows: list[HookPerformanceRow]) -> str:
    """Render the per-video appendix table sorted by topic_id ascending."""
    lines: list[str] = ["## Per-video appendix", ""]
    if not rows:
        lines.append("_No topics in the hook selection log yet._")
        return "\n".join(lines)

    lines.append(
        "| topic_id | video_id | hook_letter | formula | views | hold@3s | "
        "AVD% | days_live | eligible | reason |"
    )
    lines.append(
        "|---|---|---|---|---:|---:|---:|---:|---|---|"
    )
    for r in sorted(rows, key=lambda x: x.topic_id):
        lines.append(
            f"| {r.topic_id} | {r.video_id or '—'} | "
            f"{r.hook_letter or '—'} | {r.formula} | "
            f"{_format_int(r.views)} | {_format_float(r.hold_at_3s, places=2)} | "
            f"{_format_float(r.avg_view_pct, places=1)} | "
            f"{_format_int(r.days_live)} | "
            f"{_format_eligible(r.eligible_for_leaderboard)} | "
            f"{r.reason or '—'} |"
        )
    return "\n".join(lines)


def _render_coverage_gaps(
    canonical_formulas: list[str],
    eligible_rows: list[HookPerformanceRow],
) -> str:
    """Render the coverage-gap section.

    A gap is a canonical formula in `viral_hooks.md` whose name is NOT
    present (exact-match) in any eligible row. Rows whose formula is one
    of the FORMULA_* sentinels (EDITED / UNTAGGED / NO_HOOK_LOG) do NOT
    count as coverage of any canonical formula.
    """
    sentinel_set = {FORMULA_EDITED, FORMULA_UNTAGGED, FORMULA_NO_HOOK_LOG}
    covered = {
        r.formula for r in eligible_rows
        if r.formula not in sentinel_set
    }
    gaps = sorted({f for f in canonical_formulas if f not in covered})

    lines: list[str] = [
        "## Coverage gaps",
        "",
        "Formulas in `prompts/library/viral_hooks.md` with **zero** "
        "eligible shipped videos:",
        "",
    ]
    if not gaps:
        lines.append("_All canonical formulas have at least one eligible video. "
                     "No coverage gaps._")
        return "\n".join(lines)

    for name in gaps:
        lines.append(f"- {name}")
    lines.append("")
    lines.append("(These are formulas the channel has not yet tested — "
                 "schedule a video using each to expand the dataset.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def render_report(
    channel_root: Path,
    *,
    log_path: Path | None = None,
    upload_log_path: Path | None = None,
    analytics_csv_path: Path | None = None,
    viral_hooks_md_path: Path | None = None,
    today: date | None = None,
    eligibility_min_views: int = _DEFAULT_MIN_VIEWS,
) -> LeaderboardReport:
    """Build the leaderboard report. Pure — no file writes, no network.

    Args:
        channel_root: ShadowVerse channel root, e.g.
            ``C:/ContentOps/channels/ShadowVerse``.
        log_path: Override for `hook_selection_log.jsonl`.
        upload_log_path: Override for `upload_log.csv`.
        analytics_csv_path: Override for `_weekly_analytics.csv`.
        viral_hooks_md_path: Override for `prompts/library/viral_hooks.md`.
            Defaults to that path RELATIVE to the cwd. Falls back to the
            hard-coded 11-formula list if missing.
        today: Override "today" for deterministic tests. Defaults to
            ``datetime.now(timezone.utc).date()`` so the rendered date
            line is reproducible across machines.
        eligibility_min_views: Min views for leaderboard eligibility. Same
            knob Slice 3 exposes — defaults to 70 (operator decision).

    Returns:
        A frozen ``LeaderboardReport`` with each section pre-rendered and
        ``full_markdown`` ready to write.
    """
    today = today or datetime.now(timezone.utc).date()
    if viral_hooks_md_path is None:
        viral_hooks_md_path = Path.cwd() / _DEFAULT_VIRAL_HOOKS_MD

    rows = join_hooks_to_analytics(
        channel_root,
        log_path=log_path,
        upload_log_path=upload_log_path,
        analytics_csv_path=analytics_csv_path,
        today=today,
        eligibility_min_views=eligibility_min_views,
    )
    eligible_rows = [r for r in rows if r.eligible_for_leaderboard]

    # Slice 4 takes ANY rows (it filters internally) — pass the full set so
    # we don't double-filter and silently change semantics.
    ranked = rank_formulas(rows)

    canonical_formulas = _extract_canonical_formulas(viral_hooks_md_path)

    cohort = _render_cohort_summary(rows, eligible_rows)
    table = _render_formula_table(ranked, eligible_rows)
    appendix = _render_per_video_appendix(rows)
    gaps = _render_coverage_gaps(canonical_formulas, eligible_rows)

    header_lines = [
        f"# Hook Formula Leaderboard — {today.isoformat()}",
        "",
        "> Generated by `hook_leaderboard.py`. Eligible cohort: views >= "
        f"{eligibility_min_views} AND hold_at_3s populated.",
        "> Primary KPI: `hold_at_3s` (Shorts swipe-gate retention). "
        "Tiebreaker: `avg_view_pct` (overall AVD%).",
        "> Evidence strengths: `insufficient` (n<3) | `weak` (n=3..5 OR "
        "n>=6 but CI fails) | `strong` (n>=6 AND CI[0]>0.5).",
    ]
    full = "\n".join([
        "\n".join(header_lines),
        "",
        cohort,
        "",
        table,
        "",
        appendix,
        "",
        gaps,
        "",
    ])

    return LeaderboardReport(
        cohort_summary=cohort,
        formula_table=table,
        per_video_appendix=appendix,
        coverage_gaps=gaps,
        full_markdown=full,
    )


def write_report(
    report: LeaderboardReport,
    output_dir: Path,
    *,
    today: date | None = None,
) -> Path:
    """Write `report.full_markdown` to `<output_dir>/hook_leaderboard_<date>.md`.

    Creates parent dirs if missing. Returns the written path. Idempotent
    on the date stem — re-running on the same UTC date overwrites, which
    is the desired behavior for cron-style refresh.
    """
    today = today or datetime.now(timezone.utc).date()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"hook_leaderboard_{today.isoformat()}.md"
    target.write_text(report.full_markdown, encoding="utf-8")
    log.info("wrote leaderboard report to %s", target)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_paths_from_config(
    config_path: Path | None,
) -> tuple[Path, Path]:
    """Resolve (channel_root, project_root) from a YAML config if provided.

    Returns the operator-defaults when no config is given. The config file
    is the same `config.yaml` the rest of the pipeline reads — we look up
    `paths.channel_root` only. Project root is not in the config, so the
    hard-coded default is used unless `--output-dir` is set on the CLI.
    """
    if config_path is None:
        return _DEFAULT_CHANNEL_ROOT, _DEFAULT_PROJECT_ROOT

    try:
        import yaml  # noqa: PLC0415 - lazy import; only the CLI path needs PyYAML
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load --config. Install it or omit --config "
            "and pass paths via the other CLI flags."
        ) from exc

    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    paths = config.get("paths") or {}
    channel_root = Path(paths.get("channel_root", _DEFAULT_CHANNEL_ROOT))
    return channel_root, _DEFAULT_PROJECT_ROOT


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns 0 on success, 1 on a recoverable error.

    On `--dry-run`, prints `full_markdown` to stdout instead of writing
    a file. Useful for piping into a viewer or grep-checking the output
    before persisting.
    """
    parser = argparse.ArgumentParser(
        description="Render the ShadowVerse hook formula leaderboard.",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config.yaml (default: use built-in path defaults).",
    )
    parser.add_argument(
        "--channel-root", default=None,
        help="Channel root (overrides --config). Default: "
             rf"{_DEFAULT_CHANNEL_ROOT}.",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Directory to write the leaderboard markdown (default: "
             rf"{_DEFAULT_PROJECT_ROOT / _DEFAULT_REPORT_SUBDIR}).",
    )
    parser.add_argument(
        "--viral-hooks-md", default=None,
        help="Path to prompts/library/viral_hooks.md (default: "
             "<cwd>/prompts/library/viral_hooks.md).",
    )
    parser.add_argument(
        "--min-views", type=int, default=_DEFAULT_MIN_VIEWS,
        help=f"Eligibility threshold (default: {_DEFAULT_MIN_VIEWS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the rendered markdown to stdout instead of writing it.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config_channel_root, project_root = _resolve_paths_from_config(
            Path(args.config) if args.config else None,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        log.error("config error: %s", exc)
        return 1

    channel_root = Path(args.channel_root) if args.channel_root else config_channel_root
    output_dir = (
        Path(args.output_dir) if args.output_dir
        else project_root / _DEFAULT_REPORT_SUBDIR
    )
    viral_hooks_md_path = Path(args.viral_hooks_md) if args.viral_hooks_md else None

    log.info(
        "rendering leaderboard channel_root=%s output_dir=%s dry_run=%s",
        channel_root, output_dir, args.dry_run,
    )

    report = render_report(
        channel_root,
        viral_hooks_md_path=viral_hooks_md_path,
        eligibility_min_views=args.min_views,
    )

    if args.dry_run:
        # stdout — UTF-8 unconditionally so Windows consoles don't mangle
        # the markdown bullets / quotes.
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.write(report.full_markdown)
        return 0

    written = write_report(report, output_dir)
    log.info("done -> %s", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LeaderboardReport",
    "render_report",
    "write_report",
]
