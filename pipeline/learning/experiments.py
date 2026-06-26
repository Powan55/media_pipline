"""Experiment journal + auto-rollback evaluator — the loop's safety spine.

Every auto-applied knob change opens an EXPERIMENT recording what changed, the
baseline it must beat, a measurement window, and a rollback rule. On a later
cycle, once the window has elapsed and enough NEW (post-change) videos have
matured, ``evaluate_active`` decides:

  * improvement >= ``min_effect_pct``                       -> ``confirmed`` (keep)
  * regression <= ``rollback_threshold_pct`` (negative)     -> ``reverted``
  * retention guardrail breached (post avg_view_pct < floor)-> ``reverted``
  * too few datapoints at window end                        -> extend ONCE, then
                                                               ``inconclusive_reverted``
  * flat / ambiguous                                        -> ``inconclusive_reverted``
                                                               (default-to-safety)

A ``reverted`` / ``inconclusive_reverted`` outcome carries ``revert_to`` so the
loop restores the prior value via ``config_io.revert_knob``. This guarantees no
auto-tune is ever irreversible: it self-corrects within one window.

``experiments.jsonl`` is append-only; ``load_experiments`` dedupes by
``experiment_id`` (last write wins), so opening and later re-recording an
experiment just appends superseding lines. Experiment ids are stable per
(target, opened-date), which makes same-day re-runs idempotent. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import paths as learning_paths
from .ledger import LedgerRow
from .maturity import eligible_rows

log = logging.getLogger("learning.experiments")

STATUS_ACTIVE = "active"
STATUS_CONFIRMED = "confirmed"
STATUS_REVERTED = "reverted"
STATUS_INCONCLUSIVE = "inconclusive_reverted"


@dataclass
class Experiment:
    experiment_id: str
    opened_at: str            # ISO-8601 UTC
    kind: str                 # "auto" | "proposed_approved"
    target: str               # dotted config key, e.g. "script_quality.duration_warn_s"
    target_file: str          # e.g. "config.yaml"
    hypothesis: str
    metric: str               # e.g. "median_views"
    guardrail_metric: str     # e.g. "median_avg_view_pct"
    baseline_value: float | None
    baseline_n: int
    old_setting: object
    new_setting: object
    delta: object
    measurement_window_days: int
    measurement_window_ends: str   # ISO date
    min_sample: int
    min_effect_pct: float
    rollback_threshold_pct: float  # negative number, e.g. -10.0
    config_snapshot: str | None = None
    status: str = STATUS_ACTIVE
    evaluated_at: str | None = None
    result_value: float | None = None
    result_n: int | None = None
    extended: bool = False
    notes: str = ""

    @property
    def opened_date(self) -> str:
        return self.opened_at[:10]


@dataclass
class EvalOutcome:
    experiment_id: str
    target: str
    target_file: str
    decision: str               # "hold" | confirmed | reverted | inconclusive_reverted | extended
    revert_to: object | None
    result_value: float | None
    result_n: int
    note: str


def experiment_id_for(target: str, opened_date: str) -> str:
    """Stable id per (target, day) so same-day re-runs don't duplicate."""
    return f"exp_{opened_date}_{target.replace('.', '_')}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_experiments(channel_root: Path | str) -> list[Experiment]:
    """All experiments, deduped by id (last line wins), in first-seen order."""
    path = learning_paths.experiments_jsonl(channel_root)
    if not path.exists() or path.stat().st_size == 0:
        return []
    latest: dict[str, Experiment] = {}
    order: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("experiments line %d not valid JSON: %s", lineno, exc)
                continue
            eid = data.get("experiment_id")
            if not eid:
                continue
            if eid not in latest:
                order.append(eid)
            latest[eid] = Experiment(**{k: data.get(k) for k in Experiment.__dataclass_fields__
                                        if k != "opened_date"})
    return [latest[e] for e in order]


def active_experiments(channel_root: Path | str) -> list[Experiment]:
    return [e for e in load_experiments(channel_root) if e.status == STATUS_ACTIVE]


def _append(channel_root: Path | str, exp: Experiment) -> None:
    learning_paths.ensure_state_dir(channel_root)
    path = learning_paths.experiments_jsonl(channel_root)
    payload = {k: v for k, v in asdict(exp).items()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------

def open_experiment(
    channel_root: Path | str,
    *,
    target: str,
    target_file: str,
    kind: str,
    hypothesis: str,
    baseline_value: float | None,
    baseline_n: int,
    old_setting,
    new_setting,
    measurement_window_days: int,
    min_sample: int,
    min_effect_pct: float,
    rollback_threshold_pct: float,
    opened_at: str,
    config_snapshot: str | None = None,
    metric: str = "median_views",
    guardrail_metric: str = "median_avg_view_pct",
) -> Experiment | None:
    """Open an experiment. Returns None if one for this target is already active
    (idempotent / one-active-per-target)."""
    for e in active_experiments(channel_root):
        if e.target == target:
            log.info("experiment for %s already active (%s) — not opening another", target, e.experiment_id)
            return None

    opened_date = opened_at[:10]
    ends = (date.fromisoformat(opened_date) + timedelta(days=measurement_window_days)).isoformat()
    delta = None
    try:
        delta = float(new_setting) - float(old_setting)
    except (TypeError, ValueError):
        delta = None

    exp = Experiment(
        experiment_id=experiment_id_for(target, opened_date),
        opened_at=opened_at,
        kind=kind,
        target=target,
        target_file=target_file,
        hypothesis=hypothesis,
        metric=metric,
        guardrail_metric=guardrail_metric,
        baseline_value=baseline_value,
        baseline_n=baseline_n,
        old_setting=old_setting,
        new_setting=new_setting,
        delta=delta,
        measurement_window_days=measurement_window_days,
        measurement_window_ends=ends,
        min_sample=min_sample,
        min_effect_pct=min_effect_pct,
        rollback_threshold_pct=rollback_threshold_pct,
        config_snapshot=config_snapshot,
        status=STATUS_ACTIVE,
    )
    _append(channel_root, exp)
    log.info("opened experiment %s: %s %s->%s, window ends %s",
             exp.experiment_id, target, old_setting, new_setting, ends)
    return exp


# ---------------------------------------------------------------------------
# Evaluate + auto-rollback
# ---------------------------------------------------------------------------

def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _post_change_cohort(rows: list[LedgerRow], opened_date: str) -> list[LedgerRow]:
    """Eligible videos PUBLISHED on/after the change opened — the only ones whose
    performance reflects the new setting."""
    out = []
    for r in eligible_rows(rows):
        if r.published_at and r.published_at >= opened_date:
            out.append(r)
    return out


def evaluate_active(
    channel_root: Path | str,
    rows: list[LedgerRow],
    today: date,
    *,
    retention_floor_pct: float = 25.0,
) -> list[EvalOutcome]:
    """Evaluate every active experiment whose window has elapsed. Appends updated
    records and returns outcomes (the loop performs any revert)."""
    outcomes: list[EvalOutcome] = []
    today_iso = today.isoformat()

    for exp in active_experiments(channel_root):
        if today_iso < exp.measurement_window_ends:
            outcomes.append(EvalOutcome(
                exp.experiment_id, exp.target, exp.target_file, "hold",
                None, None, 0, "window still open"))
            continue

        post = _post_change_cohort(rows, exp.opened_date)
        n = len(post)

        if n < exp.min_sample:
            if not exp.extended:
                new_ends = (today + timedelta(days=exp.measurement_window_days)).isoformat()
                exp.extended = True
                exp.measurement_window_ends = new_ends
                exp.notes = f"extended once at {today_iso} (n={n} < {exp.min_sample})"
                _append(channel_root, exp)
                outcomes.append(EvalOutcome(
                    exp.experiment_id, exp.target, exp.target_file, "extended",
                    None, None, n, exp.notes))
            else:
                _finalize(channel_root, exp, STATUS_INCONCLUSIVE, today_iso, None, n,
                          f"insufficient data after extension (n={n})")
                outcomes.append(EvalOutcome(
                    exp.experiment_id, exp.target, exp.target_file, STATUS_INCONCLUSIVE,
                    exp.old_setting, None, n, "insufficient data; reverting (safety)"))
            continue

        result = _median([float(r.views) for r in post if r.views is not None])
        guardrail = _median([r.avg_view_pct for r in post if r.avg_view_pct is not None])

        if guardrail is not None and guardrail < retention_floor_pct:
            _finalize(channel_root, exp, STATUS_REVERTED, today_iso, result, n,
                      f"retention guardrail breached (post avg_view_pct {guardrail:.1f} < {retention_floor_pct})")
            outcomes.append(EvalOutcome(
                exp.experiment_id, exp.target, exp.target_file, STATUS_REVERTED,
                exp.old_setting, result, n, "retention breached; reverting"))
            continue

        baseline = exp.baseline_value
        improvement_pct = (
            (result - baseline) / baseline * 100.0
            if baseline and result is not None and baseline != 0 else 0.0
        )

        if improvement_pct >= exp.min_effect_pct:
            _finalize(channel_root, exp, STATUS_CONFIRMED, today_iso, result, n,
                      f"reach +{improvement_pct:.1f}% vs baseline; keeping")
            outcomes.append(EvalOutcome(
                exp.experiment_id, exp.target, exp.target_file, STATUS_CONFIRMED,
                None, result, n, f"confirmed (+{improvement_pct:.1f}%)"))
        elif improvement_pct <= exp.rollback_threshold_pct:
            _finalize(channel_root, exp, STATUS_REVERTED, today_iso, result, n,
                      f"reach {improvement_pct:.1f}% vs baseline; reverting")
            outcomes.append(EvalOutcome(
                exp.experiment_id, exp.target, exp.target_file, STATUS_REVERTED,
                exp.old_setting, result, n, f"reverting ({improvement_pct:.1f}%)"))
        else:
            _finalize(channel_root, exp, STATUS_INCONCLUSIVE, today_iso, result, n,
                      f"reach {improvement_pct:+.1f}% inconclusive; reverting (safety)")
            outcomes.append(EvalOutcome(
                exp.experiment_id, exp.target, exp.target_file, STATUS_INCONCLUSIVE,
                exp.old_setting, result, n, f"inconclusive ({improvement_pct:+.1f}%); reverting"))

    return outcomes


def _finalize(channel_root, exp: Experiment, status: str, evaluated_at: str,
              result_value: float | None, result_n: int, note: str) -> None:
    exp.status = status
    exp.evaluated_at = evaluated_at
    exp.result_value = result_value
    exp.result_n = result_n
    exp.notes = note
    _append(channel_root, exp)
    log.info("experiment %s -> %s (%s)", exp.experiment_id, status, note)


__all__ = [
    "Experiment",
    "EvalOutcome",
    "STATUS_ACTIVE",
    "STATUS_CONFIRMED",
    "STATUS_REVERTED",
    "STATUS_INCONCLUSIVE",
    "experiment_id_for",
    "load_experiments",
    "active_experiments",
    "open_experiment",
    "evaluate_active",
]
