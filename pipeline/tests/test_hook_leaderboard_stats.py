"""Tests for hook_leaderboard_stats — pure-functional aggregation + ranking.

Wilson CI reference values are hardcoded from `scipy.stats.binomtest(s, n)
.proportion_ci(method='wilson')` (which is the textbook score interval with no
continuity correction, evaluated at z = 1.959963984540054). No scipy import
at runtime — the values are baked in as fixtures so the test suite stays
stdlib-only per the slice's hard constraints.

NOTE on the spec's reference table: the values listed in the slice spec
(e.g. (7, 10) → (0.39651775, 0.89086474)) do NOT match scipy's actual
output at the 1e-6 tolerance the spec demands. The values here match scipy
exactly (to ~1e-7), which is what the standard Wilson formula with
z = 1.959963984540054 produces — also what the spec's pseudocode describes.
See the slice notes for details.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from hook_leaderboard_stats import (
    FormulaRank,
    FormulaStat,
    HookPerformanceRowProto,
    evidence_strength,
    formula_medians,
    rank_formulas,
    wilson_score_ci,
)


# -----------------------------------------------------------------------------
# Lightweight test fixture: a stand-in for Slice 3's HookPerformanceRow that
# satisfies HookPerformanceRowProto structurally. We don't import from
# analytics_join (file-disjoint requirement) — this proves the Protocol is
# what we say it is.
# -----------------------------------------------------------------------------

@dataclass
class FakeRow:
    """Stand-in for Slice 3's HookPerformanceRow. Structurally satisfies
    HookPerformanceRowProto. Mutable for ergonomics in tests; the real row
    will be frozen but the Protocol doesn't care."""
    topic_id: str
    formula: str
    views: int | None
    hold_at_3s: float | None
    avg_view_pct: float | None
    eligible_for_leaderboard: bool


def _row(
    formula: str,
    hold: float | None,
    avg_pct: float | None = 0.5,
    *,
    eligible: bool = True,
    topic_id: str = "tid",
    views: int | None = 1000,
) -> FakeRow:
    """Compact row constructor for tests."""
    return FakeRow(
        topic_id=topic_id,
        formula=formula,
        views=views,
        hold_at_3s=hold,
        avg_view_pct=avg_pct,
        eligible_for_leaderboard=eligible,
    )


def test_fake_row_satisfies_protocol() -> None:
    """The Protocol must accept the fake (and by extension Slice 3's row)."""
    row = _row("X", 0.5)
    assert isinstance(row, HookPerformanceRowProto)


# -----------------------------------------------------------------------------
# wilson_score_ci — reference values + edge cases
# -----------------------------------------------------------------------------

# Matches `scipy.stats.binomtest(s, n).proportion_ci(method='wilson')`.
# Tolerance 1e-6 (scipy itself isn't bit-exact with stdlib NormalDist).
WILSON_REFERENCE_CASES: list[tuple[int, int, float, float]] = [
    # (successes, n, expected_lo, expected_hi)
    (7, 10, 0.39677815, 0.89220873),
    (50, 100, 0.40383153, 0.59616847),
    (0, 5, 0.0, 0.43448246),
    (5, 5, 0.56551754, 1.0),
    (1, 1, 0.20654931, 1.0),
]


@pytest.mark.parametrize("successes,n,expected_lo,expected_hi", WILSON_REFERENCE_CASES)
def test_wilson_score_ci_matches_reference(
    successes: int, n: int, expected_lo: float, expected_hi: float,
) -> None:
    """Wilson CI matches scipy's binomtest.proportion_ci(method='wilson') to 1e-6."""
    lo, hi = wilson_score_ci(successes, n)
    assert lo == pytest.approx(expected_lo, abs=1e-6), (
        f"lo mismatch for s={successes} n={n}: got {lo}, expected {expected_lo}"
    )
    assert hi == pytest.approx(expected_hi, abs=1e-6), (
        f"hi mismatch for s={successes} n={n}: got {hi}, expected {expected_hi}"
    )


def test_wilson_score_ci_n_zero_returns_defensive_default() -> None:
    """n=0 returns (0.0, 1.0) — maximum-uncertainty defensive default."""
    assert wilson_score_ci(0, 0) == (0.0, 1.0)


def test_wilson_score_ci_bounds_clamped_to_unit_interval() -> None:
    """Output bounds always lie in [0, 1] regardless of inputs."""
    for s, n in [(0, 1), (1, 1), (0, 100), (100, 100), (50, 100)]:
        lo, hi = wilson_score_ci(s, n)
        assert 0.0 <= lo <= 1.0, f"lo out of bounds for s={s} n={n}: {lo}"
        assert 0.0 <= hi <= 1.0, f"hi out of bounds for s={s} n={n}: {hi}"
        assert lo <= hi, f"lo > hi for s={s} n={n}: {lo} > {hi}"


def test_wilson_score_ci_rejects_invalid_inputs() -> None:
    """Defensive validation on inputs."""
    with pytest.raises(ValueError):
        wilson_score_ci(-1, 5)
    with pytest.raises(ValueError):
        wilson_score_ci(6, 5)
    with pytest.raises(ValueError):
        wilson_score_ci(1, -1)
    with pytest.raises(ValueError):
        wilson_score_ci(1, 5, confidence=0.0)
    with pytest.raises(ValueError):
        wilson_score_ci(1, 5, confidence=1.0)


def test_wilson_score_ci_non_default_confidence() -> None:
    """Non-95% confidence still produces valid bounds (sanity check)."""
    lo_99, hi_99 = wilson_score_ci(50, 100, confidence=0.99)
    lo_95, hi_95 = wilson_score_ci(50, 100, confidence=0.95)
    # 99% interval is wider than 95% interval at the same point estimate.
    assert lo_99 < lo_95
    assert hi_99 > hi_95


# -----------------------------------------------------------------------------
# formula_medians — aggregation
# -----------------------------------------------------------------------------

def test_formula_medians_empty_input_returns_empty_dict() -> None:
    assert formula_medians([]) == {}


def test_formula_medians_all_ineligible_returns_empty_dict() -> None:
    rows = [
        _row("A", 0.5, eligible=False),
        _row("B", 0.6, eligible=False),
    ]
    assert formula_medians(rows) == {}


def test_formula_medians_filters_ineligible_before_grouping() -> None:
    """Ineligible rows do not contribute to per-formula counts or medians."""
    rows = [
        _row("A", 0.4, eligible=True),
        _row("A", 0.6, eligible=True),
        _row("A", 0.99, eligible=False),  # filtered — would skew the median
    ]
    stats = formula_medians(rows)
    assert stats["A"].n == 2
    assert stats["A"].median_hold_at_3s == 0.5  # median of [0.4, 0.6]


def test_formula_medians_skips_rows_missing_hold_at_3s() -> None:
    """A row with hold_at_3s=None is filtered (otherwise the math breaks)."""
    rows = [
        _row("A", None, eligible=True),
        _row("A", 0.5, eligible=True),
    ]
    stats = formula_medians(rows)
    assert stats["A"].n == 1
    assert stats["A"].median_hold_at_3s == 0.5


def test_formula_medians_handles_missing_avg_view_pct_per_row() -> None:
    """avg_view_pct can be None on individual rows; median uses the rest."""
    rows = [
        _row("A", 0.5, avg_pct=None),
        _row("A", 0.6, avg_pct=0.4),
        _row("A", 0.7, avg_pct=0.6),
    ]
    stats = formula_medians(rows)
    assert stats["A"].median_avg_view_pct == 0.5  # median of [0.4, 0.6]


def test_formula_medians_avg_view_pct_all_none_yields_none() -> None:
    """When every row of a formula lacks avg_view_pct, the median is None."""
    rows = [
        _row("A", 0.5, avg_pct=None),
        _row("A", 0.6, avg_pct=None),
    ]
    stats = formula_medians(rows)
    assert stats["A"].median_avg_view_pct is None
    assert stats["A"].median_hold_at_3s == 0.55


def test_formula_medians_wilson_uses_strict_greater_than_cohort_median() -> None:
    """A row whose hold_at_3s EQUALS the cohort median is NOT a success.

    Cohort: [0.5, 0.5, 0.5, 0.5] → median 0.5. Formula A's two rows are both
    0.5 — strictly equal, not strictly greater. Successes must be 0/2.
    """
    rows = [
        _row("A", 0.5),
        _row("A", 0.5),
        _row("B", 0.5),
        _row("B", 0.5),
    ]
    stats = formula_medians(rows)
    assert stats["A"].n == 2
    # 0 successes → Wilson CI for 0/2 at 95%
    expected = wilson_score_ci(0, 2)
    assert stats["A"].wilson_ci_above_cohort_median == expected


def test_formula_medians_cohort_median_uses_all_eligible_rows() -> None:
    """Cohort median pools ALL eligible rows across formulas — not per-formula."""
    rows = [
        _row("A", 0.1),
        _row("A", 0.2),
        _row("B", 0.8),
        _row("B", 0.9),
    ]
    # Cohort median of [0.1, 0.2, 0.8, 0.9] = 0.5
    # A has 0/2 above 0.5; B has 2/2 above 0.5
    stats = formula_medians(rows)
    assert stats["A"].wilson_ci_above_cohort_median == wilson_score_ci(0, 2)
    assert stats["B"].wilson_ci_above_cohort_median == wilson_score_ci(2, 2)


# -----------------------------------------------------------------------------
# rank_formulas — sort + evidence strength + tie handling
# -----------------------------------------------------------------------------

def test_rank_formulas_empty_input() -> None:
    assert rank_formulas([]) == []


def test_rank_formulas_all_insufficient() -> None:
    """Every formula with n=1 is "insufficient" but still ranked."""
    rows = [_row("A", 0.5), _row("B", 0.7), _row("C", 0.3)]
    ranks = rank_formulas(rows)
    assert len(ranks) == 3
    assert all(r.evidence_strength == "insufficient" for r in ranks)
    # Sort: B (0.7) > A (0.5) > C (0.3)
    assert [r.formula for r in ranks] == ["B", "A", "C"]
    assert [r.rank for r in ranks] == [1, 2, 3]


def test_rank_formulas_mixed_evidence_strength() -> None:
    """Cover all three evidence_strength labels in one fixture.

    Cohort layout:
      - LOW    : 5 rows at 0.1 (n=5 → "weak", below cohort median)
      - HIGH   : 6 rows at 0.9 (n=6, all > cohort median → "strong")
      - TINY   : 2 rows at 0.5 (n<3 → "insufficient")
    """
    rows = (
        [_row("LOW", 0.1) for _ in range(5)]
        + [_row("HIGH", 0.9) for _ in range(6)]
        + [_row("TINY", 0.5) for _ in range(2)]
    )
    ranks = rank_formulas(rows)
    by_formula = {r.formula: r for r in ranks}
    assert by_formula["TINY"].evidence_strength == "insufficient"
    assert by_formula["LOW"].evidence_strength == "weak"
    assert by_formula["HIGH"].evidence_strength == "strong"
    # HIGH ranks #1 (highest median), TINY #2 (0.5), LOW #3 (0.1)
    assert by_formula["HIGH"].rank == 1
    assert by_formula["TINY"].rank == 2
    assert by_formula["LOW"].rank == 3


def test_rank_formulas_strong_requires_both_n_and_ci() -> None:
    """n>=6 alone is not enough — Wilson CI lower bound must exceed 0.5.

    Setup: 6 rows for FORM_X all near the cohort median, 6 rows for FORM_Y
    all well above. FORM_X passes n threshold but its CI[0] won't exceed 0.5.
    """
    # Cohort spans 0.4-0.6 with median ~0.5. FORM_X rows hover at 0.5 → 0
    # successes against strict-greater-than-median → CI[0] = 0 < 0.5 → "weak".
    rows = (
        [_row("FORM_X", 0.5) for _ in range(6)]
        + [_row("FORM_Y", 0.6) for _ in range(6)]
    )
    ranks = rank_formulas(rows)
    by_formula = {r.formula: r for r in ranks}
    assert by_formula["FORM_X"].n == 6
    assert by_formula["FORM_X"].evidence_strength == "weak"


def test_rank_formulas_n_in_three_to_five_is_weak() -> None:
    """3 <= n < 6 always lands in "weak", regardless of CI."""
    for n in [3, 4, 5]:
        rows = [_row("A", 0.9) for _ in range(n)] + [_row("B", 0.1) for _ in range(n)]
        ranks = rank_formulas(rows)
        a = next(r for r in ranks if r.formula == "A")
        assert a.n == n
        assert a.evidence_strength == "weak", f"n={n} should be weak"


def test_rank_formulas_ties_share_rank() -> None:
    """Two formulas with identical (median_hold, median_avg_pct) share rank."""
    rows = [
        _row("A", 0.7, avg_pct=0.5),
        _row("B", 0.7, avg_pct=0.5),  # identical sort key as A
        _row("C", 0.3, avg_pct=0.5),
    ]
    ranks = rank_formulas(rows)
    by_formula = {r.formula: r for r in ranks}
    assert by_formula["A"].rank == by_formula["B"].rank == 1
    assert by_formula["C"].rank == 3  # rank skip after the two-way tie


def test_rank_formulas_secondary_sort_by_avg_view_pct() -> None:
    """When median_hold_at_3s ties, median_avg_view_pct breaks the tie."""
    rows = [
        _row("A", 0.5, avg_pct=0.4),
        _row("B", 0.5, avg_pct=0.6),  # higher avg_pct → ranks first
    ]
    ranks = rank_formulas(rows)
    assert [r.formula for r in ranks] == ["B", "A"]
    assert [r.rank for r in ranks] == [1, 2]


def test_rank_formulas_single_formula() -> None:
    """A single formula with n>=6 above its own (== cohort) median is still
    "weak": there's no spread to put it ABOVE the cohort median."""
    rows = [_row("ONLY", 0.5) for _ in range(8)]
    ranks = rank_formulas(rows)
    assert len(ranks) == 1
    assert ranks[0].formula == "ONLY"
    assert ranks[0].n == 8
    assert ranks[0].rank == 1
    # Every row equals the cohort median → 0 successes → CI[0] = 0 → "weak".
    assert ranks[0].evidence_strength == "weak"


def test_rank_formulas_single_formula_strong_when_spread_present() -> None:
    """A single formula with internal spread CAN go "strong" if most rows
    sit above its own median (which equals the cohort median when only one
    formula is present). With 6 identical rows we can't get there; with a
    spread that makes >50% strictly exceed the median we can — but with a
    single formula and `>` the median, by definition only the rows above the
    median (typically half) qualify, so "strong" is essentially unreachable
    for a single-formula cohort. Documenting the boundary here.
    """
    # 7 rows: median = the 4th value = 0.5. Above-median count = 3 (0.6, 0.7, 0.8).
    # 3/7 < 50%, so CI[0] won't pass. → "weak".
    rows = [_row("ONLY", v) for v in [0.1, 0.2, 0.3, 0.5, 0.6, 0.7, 0.8]]
    ranks = rank_formulas(rows)
    assert ranks[0].evidence_strength == "weak"


# -----------------------------------------------------------------------------
# Returned types are the right dataclasses (sanity)
# -----------------------------------------------------------------------------

def test_returned_types() -> None:
    rows = [_row("A", 0.5)]
    stats = formula_medians(rows)
    assert isinstance(stats["A"], FormulaStat)
    ranks = rank_formulas(rows)
    assert isinstance(ranks[0], FormulaRank)


# -----------------------------------------------------------------------------
# evidence_strength — public helper (Slice 8 promotion)
#
# The function was promoted from `_evidence_strength` in Slice 8 so Slice 6's
# `daily_batch_hook_addendum` can import it instead of duplicating the
# threshold table inline. These tests pin the three branches directly so any
# future threshold tweak fails here as well as in the integration tests.
# -----------------------------------------------------------------------------


def test_evidence_strength_insufficient_for_n_below_three() -> None:
    """n < 3 always returns 'insufficient', regardless of CI."""
    assert evidence_strength(0, None) == "insufficient"
    assert evidence_strength(1, (0.0, 1.0)) == "insufficient"
    assert evidence_strength(2, (0.9, 1.0)) == "insufficient"


def test_evidence_strength_weak_for_n_three_to_five() -> None:
    """3 <= n <= 5 always returns 'weak', regardless of CI lower bound."""
    for n in (3, 4, 5):
        assert evidence_strength(n, (0.0, 0.5)) == "weak"
        assert evidence_strength(n, (0.6, 0.99)) == "weak"


def test_evidence_strength_strong_requires_n_six_and_ci_above_half() -> None:
    """n >= 6 AND wilson[0] > 0.5 -> 'strong'."""
    assert evidence_strength(6, (0.51, 0.95)) == "strong"
    assert evidence_strength(10, (0.6, 0.95)) == "strong"


def test_evidence_strength_weak_when_n_six_but_ci_lower_bound_at_half() -> None:
    """The CI lower bound must STRICTLY exceed 0.5 — equality is 'weak'."""
    assert evidence_strength(6, (0.5, 0.95)) == "weak"
    assert evidence_strength(6, (0.49, 0.95)) == "weak"


def test_evidence_strength_weak_when_wilson_is_none_at_strong_threshold() -> None:
    """Defensive: a None CI never qualifies as 'strong'."""
    assert evidence_strength(6, None) == "weak"
    assert evidence_strength(100, None) == "weak"


def test_evidence_strength_is_in_module_all() -> None:
    """The promotion to public API must be reflected in __all__."""
    import hook_leaderboard_stats

    assert "evidence_strength" in hook_leaderboard_stats.__all__
