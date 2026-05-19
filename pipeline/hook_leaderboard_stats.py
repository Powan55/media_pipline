"""Hook-formula leaderboard statistics for ShadowVerse A/B retrospective.

Pure-functional statistics module. No I/O, no external dependencies — stdlib only.
Consumes a Sequence of row-like objects produced by Slice 3's `analytics_join`
(`HookPerformanceRow`) without importing from that module: a structural Protocol
keeps the slices file-disjoint so they can ship in parallel and evolve
independently.

The leaderboard answers one question per formula: "given this formula's eligible
videos, how often does it beat the cohort median on `hold_at_3s`?". The Wilson
score interval converts that hit-rate into a 95 % confidence range that's still
well-defined for tiny `n` (the realistic regime for a freshly-launched channel
where each formula has 1-10 datapoints). The `evidence_strength` label is the
operator-facing summary so the leaderboard reader can tell at a glance whether
a high median ranking is grounded ("strong") or noisy ("weak"/"insufficient").

Public API:
  - HookPerformanceRowProto  Protocol that Slice 3's dataclass satisfies
  - FormulaStat              dataclass: per-formula aggregate
  - FormulaRank              dataclass: leaderboard row (FormulaStat + rank + label)
  - formula_medians(rows)    aggregate eligible rows per formula
  - rank_formulas(rows)      ranked leaderboard with evidence labels
  - wilson_score_ci(s, n)    Wilson 95 % score interval (no continuity correction)
  - evidence_strength(n, ci) operator-facing label for (n, wilson_ci) pair

Key design choices (locked, do not change without operator approval):
  - `cohort_median` is computed across ALL eligible rows' `hold_at_3s`,
    not per-formula — the comparison is "this formula vs the field", which
    matches what the operator wants from the leaderboard.
  - "Beats the cohort median" uses strict `>` (not `>=`). At the median
    boundary, a tie is NOT counted as a success. This is the standard
    convention for the Mann-Whitney-style proportion-above-median test
    and avoids inflating evidence for formulas whose median equals the
    cohort median (which would otherwise show a 100 % "above" rate trivially).
  - The Wilson interval has no continuity correction — it's the textbook
    score interval, which is what `scipy.stats.binomtest(...).proportion_ci(
    method='wilson')` returns at the standard z = 1.959963984540054.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

log = logging.getLogger("hook_leaderboard_stats")

__all__ = [
    "HookPerformanceRowProto",
    "FormulaStat",
    "FormulaRank",
    "wilson_score_ci",
    "formula_medians",
    "rank_formulas",
    "evidence_strength",
]

# Standard normal inverse-CDF at the 0.975 quantile (95 % two-sided z-score).
# Precomputed via `statistics.NormalDist().inv_cdf(0.975)` to avoid recomputing
# inside `wilson_score_ci` on every call. For non-default confidences the
# function falls through to NormalDist anyway.
_Z_95: float = 1.959963984540054

# evidence_strength thresholds (operator-locked — see module docstring).
_INSUFFICIENT_N: int = 3            # n < 3 is "insufficient" regardless of CI
_STRONG_N: int = 6                  # n >= 6 is the floor for "strong"
_STRONG_CI_LOWER_BOUND: float = 0.5  # CI[0] must exceed 0.5 for "strong"


# -----------------------------------------------------------------------------
# Row contract — structural Protocol so we don't import from analytics_join
# -----------------------------------------------------------------------------

@runtime_checkable
class HookPerformanceRowProto(Protocol):
    """Structural contract Slice 3's `HookPerformanceRow` dataclass satisfies.

    Only the attributes this module reads are listed. Slice 3's frozen dataclass
    has additional fields (publish date, title, etc.) that aren't relevant here.
    `runtime_checkable` lets callers use `isinstance(row, HookPerformanceRowProto)`
    if they want — not required by this module's logic.
    """
    topic_id: str
    formula: str
    views: int | None
    hold_at_3s: float | None
    avg_view_pct: float | None
    eligible_for_leaderboard: bool


# -----------------------------------------------------------------------------
# Public dataclasses
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FormulaStat:
    """Aggregated stats for one hook formula across the eligible cohort."""
    formula: str
    n: int                                                       # eligible-row count
    median_hold_at_3s: float | None                              # None when n == 0
    median_avg_view_pct: float | None                            # None when n == 0
    # Wilson 95 % CI for the proportion of THIS formula's videos whose
    # `hold_at_3s` strictly exceeds the COHORT median (across all eligible
    # formulas). None when n == 0 — meaningless without datapoints.
    wilson_ci_above_cohort_median: tuple[float, float] | None


@dataclass(frozen=True)
class FormulaRank:
    """One row in the ranked leaderboard."""
    formula: str
    n: int
    median_hold_at_3s: float | None
    median_avg_view_pct: float | None
    wilson_ci_above_cohort_median: tuple[float, float] | None
    rank: int                                                    # 1-indexed; ties share rank
    evidence_strength: str                                       # see module docstring


# -----------------------------------------------------------------------------
# Wilson score interval (no continuity correction)
# -----------------------------------------------------------------------------

def wilson_score_ci(
    successes: int, n: int, *, confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Returns (lo, hi) in [0, 1].

    For `n == 0` returns `(0.0, 1.0)` defensively — callers that care should
    inspect `n` first. This matches the convention "no data → maximum uncertainty".

    Standard formula (no continuity correction):
        z      = inverse-CDF normal at `1 - (1 - confidence) / 2`
        center = (p + z^2 / (2n)) / (1 + z^2 / n)
        half   = z * sqrt((p(1-p) + z^2 / (4n)) / n) / (1 + z^2 / n)
        ci     = (max(0, center - half), min(1, center + half))

    For `confidence=0.95` uses the precomputed `_Z_95` constant; otherwise
    computes z via `statistics.NormalDist().inv_cdf`.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if not 0 <= successes <= n:
        raise ValueError(f"successes must be in [0, n], got successes={successes}, n={n}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")

    if n == 0:
        return (0.0, 1.0)

    if confidence == 0.95:
        z = _Z_95
    else:
        z = statistics.NormalDist().inv_cdf(1 - (1 - confidence) / 2)

    p = successes / n
    z_sq = z * z
    denom = 1.0 + z_sq / n
    center = (p + z_sq / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z_sq / (4 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# -----------------------------------------------------------------------------
# Aggregation + ranking
# -----------------------------------------------------------------------------

def _filter_eligible(rows: Sequence[HookPerformanceRowProto]) -> list[HookPerformanceRowProto]:
    """Keep only rows where `eligible_for_leaderboard` is True AND `hold_at_3s`
    is non-None. A row missing `hold_at_3s` can't contribute to medians or the
    Wilson hit-rate and would otherwise crash the math; filter defensively."""
    return [r for r in rows if r.eligible_for_leaderboard and r.hold_at_3s is not None]


def _cohort_median_hold(eligible: Sequence[HookPerformanceRowProto]) -> float | None:
    """Median `hold_at_3s` across all eligible rows. None if the cohort is empty."""
    holds = [r.hold_at_3s for r in eligible if r.hold_at_3s is not None]
    if not holds:
        return None
    return statistics.median(holds)


def _group_by_formula(
    eligible: Sequence[HookPerformanceRowProto],
) -> dict[str, list[HookPerformanceRowProto]]:
    """Group eligible rows by formula name. Order-preserving (insertion order)."""
    groups: dict[str, list[HookPerformanceRowProto]] = {}
    for r in eligible:
        groups.setdefault(r.formula, []).append(r)
    return groups


def formula_medians(
    rows: Sequence[HookPerformanceRowProto],
) -> dict[str, FormulaStat]:
    """Aggregate eligible rows per formula. Ineligible rows are filtered out
    BEFORE grouping, so a formula with zero eligible rows simply doesn't appear
    in the result. Returns an empty dict when there are no eligible rows.

    Each `FormulaStat`:
      - `median_hold_at_3s`, `median_avg_view_pct` use `statistics.median`
        (handles even/odd cohort sizes natively).
      - `wilson_ci_above_cohort_median` is the 95 % Wilson CI for the
        proportion of this formula's videos whose `hold_at_3s` strictly
        exceeds the COHORT median (computed across all eligible rows).
        Ties at the median count as failure (`>` not `>=`) — see module
        docstring for the rationale.
      - `avg_view_pct` is filtered to non-None values before taking the median;
        if no row has it, the field is None even when n > 0.
    """
    eligible = _filter_eligible(rows)
    if not eligible:
        return {}

    cohort_med = _cohort_median_hold(eligible)
    groups = _group_by_formula(eligible)

    out: dict[str, FormulaStat] = {}
    for formula, group_rows in groups.items():
        n = len(group_rows)
        holds = [r.hold_at_3s for r in group_rows if r.hold_at_3s is not None]
        avg_pcts = [r.avg_view_pct for r in group_rows if r.avg_view_pct is not None]

        median_hold = statistics.median(holds) if holds else None
        median_avg = statistics.median(avg_pcts) if avg_pcts else None

        # Strict "> cohort_median" — ties at the boundary are NOT successes.
        successes = sum(1 for h in holds if cohort_med is not None and h > cohort_med)
        wilson = wilson_score_ci(successes, n) if n > 0 else None

        out[formula] = FormulaStat(
            formula=formula,
            n=n,
            median_hold_at_3s=median_hold,
            median_avg_view_pct=median_avg,
            wilson_ci_above_cohort_median=wilson,
        )
    return out


def evidence_strength(n: int, wilson: tuple[float, float] | None) -> str:
    """Map ``(n, wilson_ci)`` to one of ``"insufficient" | "weak" | "strong"``.

    Rules (locked — see module-level threshold constants):
      - ``n < 3``                                    -> ``"insufficient"``
      - ``n >= 6`` AND ``wilson[0] > 0.5``           -> ``"strong"``
      - otherwise (``3 <= n <= 5``, or ``n>=6`` with
        a Wilson CI lower bound at-or-below 0.5)    -> ``"weak"``

    This function is the single source of truth for the leaderboard's
    evidence-strength labels. Slice 6's ``daily_batch_hook_addendum``
    imports it directly so the addendum's footer stays in lockstep with
    the leaderboard report — promoting this helper to the module's public
    API removed a duplicate threshold table that was flagged as a
    drift risk during Slice 6 QA.

    Downstream consumers should import from here rather than re-implement:

        from hook_leaderboard_stats import evidence_strength
    """
    if n < _INSUFFICIENT_N:
        return "insufficient"
    if n >= _STRONG_N and wilson is not None and wilson[0] > _STRONG_CI_LOWER_BOUND:
        return "strong"
    return "weak"


def rank_formulas(
    rows: Sequence[HookPerformanceRowProto],
) -> list[FormulaRank]:
    """Rank formulas. Sort key: (median_hold_at_3s desc, median_avg_view_pct desc).

    Ties on the (composite) sort key share the same `rank` integer (so ranks
    can skip — e.g. two-way tie at 1 means the next entry is rank 3, not 2).

    `evidence_strength` rules (locked):
      - n < 3                                          → "insufficient"
      - n >= 6 AND wilson_ci_above_cohort_median[0] > 0.5  → "strong"
      - otherwise                                      → "weak"

    Formulas whose median is None (shouldn't happen for non-empty groups since
    eligibility requires non-None `hold_at_3s`, but defensive) sort last.
    Returns empty list if no eligible rows.
    """
    stats = formula_medians(rows)
    if not stats:
        return []

    # Sort key: descending on median_hold_at_3s then median_avg_view_pct.
    # None medians sort last — represent as -inf for the ordering.
    def sort_key(s: FormulaStat) -> tuple[float, float]:
        h = s.median_hold_at_3s if s.median_hold_at_3s is not None else float("-inf")
        a = s.median_avg_view_pct if s.median_avg_view_pct is not None else float("-inf")
        # Negate for descending sort (so we can use Python's stable ascending sort).
        return (-h, -a)

    sorted_stats = sorted(stats.values(), key=sort_key)

    # Assign ranks: ties on the sort key share the same rank, and the next
    # distinct sort key gets rank = (1 + count of all entries before it).
    ranks: list[int] = []
    prev_key: tuple[float, float] | None = None
    prev_rank: int = 0
    for i, s in enumerate(sorted_stats, start=1):
        key = sort_key(s)
        if key == prev_key:
            ranks.append(prev_rank)
        else:
            ranks.append(i)
            prev_rank = i
            prev_key = key

    return [
        FormulaRank(
            formula=s.formula,
            n=s.n,
            median_hold_at_3s=s.median_hold_at_3s,
            median_avg_view_pct=s.median_avg_view_pct,
            wilson_ci_above_cohort_median=s.wilson_ci_above_cohort_median,
            rank=rank,
            evidence_strength=evidence_strength(s.n, s.wilson_ci_above_cohort_median),
        )
        for s, rank in zip(sorted_stats, ranks)
    ]
