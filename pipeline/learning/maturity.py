"""Data-maturity gating — the single chokepoint that keeps the loop off noisy
or immature data.

The ledger already stamps each row with ``eligible`` (matured >= floor days AND
has an analytics row AND views >= the views floor) and ``quarantine_reason``.
This module centralizes the two questions the rest of the loop asks:

  * which rows may we learn from at all?  -> ``eligible_rows``
  * does a feature cohort have enough datapoints to act on? -> ``meets_min_sample``

Keeping these here (rather than re-deriving filters in analysis / tuner) means
"what counts as mature" has exactly one definition. Stdlib only.
"""

from __future__ import annotations

from typing import Iterable

from .ledger import MIN_VIEWS_FLOOR, LedgerRow  # noqa: F401  (MIN_VIEWS_FLOOR re-exported)

# Default minimum cohort size before any feature value may drive an auto-tune.
# Matches the leaderboard's evidence thresholds (n<3 insufficient, n>=6 strong).
DEFAULT_MIN_SAMPLE: int = 5


def eligible_rows(rows: Iterable[LedgerRow]) -> list[LedgerRow]:
    """Rows that have matured and carry a real analytics datapoint.

    ``eligible`` already encodes: matured (>= maturity_floor_days live) AND an
    analytics row exists AND views >= the views floor. Immature / unscored /
    not-uploaded / low-views rows are excluded.
    """
    return [r for r in rows if r.eligible]


def meets_min_sample(n: int, min_sample: int = DEFAULT_MIN_SAMPLE) -> bool:
    """True when a feature cohort is large enough to act on."""
    return n >= min_sample


def maturity_summary(rows: Iterable[LedgerRow]) -> dict[str, int]:
    """Count rows by quarantine reason (empty reason -> 'eligible')."""
    out: dict[str, int] = {}
    for r in rows:
        key = r.quarantine_reason or "eligible"
        out[key] = out.get(key, 0) + 1
    return out


__all__ = [
    "DEFAULT_MIN_SAMPLE",
    "MIN_VIEWS_FLOOR",
    "eligible_rows",
    "meets_min_sample",
    "maturity_summary",
]
