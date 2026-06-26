"""Tuner: auto-apply the SAFE candidates (bounded + reversible + logged as
experiments) and route PROPOSE candidates to the weekly-review surface.

Guarantees enforced here:
  * at most ``max_auto_applies_per_cycle`` knobs applied per run,
  * never exceed ``max_active_experiments`` open at once,
  * every auto-apply opens an experiment (so it self-reverts if it doesn't help),
  * PROPOSE candidates are only WRITTEN to a file, never applied.

When ``apply_enabled`` is false the tuner still writes the proposals/dashboard
inputs but applies nothing — the report/propose-only default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config_io, experiments
from . import paths as learning_paths
from .analysis import AnalysisReport
from .policy import SAFE_AUTO, Candidate, propose_changes

log = logging.getLogger("learning.tuner")

MEASUREMENT_WINDOW_DAYS = 7


@dataclass
class AppliedChange:
    candidate: Candidate
    old_value: str | None
    new_value: object
    experiment_id: str | None
    snapshot: str | None


@dataclass
class TunerResult:
    applied: list[AppliedChange] = field(default_factory=list)
    proposed: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (key, reason)
    proposals_path: str | None = None
    apply_enabled: bool = False


def _coerce(raw, value_type):
    if raw is None:
        return None
    try:
        return value_type(raw) if value_type in (int, float) else raw
    except (ValueError, TypeError):
        return raw


def _opened_at(today: date) -> str:
    # Deterministic timestamp from the cycle date (stable experiment ids per day).
    return f"{today.isoformat()}T00:00:00+00:00"


def _pu_row(idx: int, c: Candidate) -> str:
    sacred = "Y" if c.sacred else "N"
    return (f"| PU-{idx} | {c.category} | {c.target_file} | "
            f"{c.key} -> {c.proposed_value} | {c.rationale} | {c.impact_effort} | "
            f"{sacred} | {c.traceability} | propose |")


def write_proposals(channel_root, today: date, safe: list[Candidate],
                    propose: list[Candidate], applied: list[AppliedChange]) -> str:
    learning_paths.ensure_state_dir(channel_root)
    out = learning_paths.proposals_md(channel_root, today.isoformat())
    L: list[str] = [f"# Learning-loop change proposals — {today.isoformat()}", ""]

    L.append("## Auto-applied this cycle (SAFE-AUTO — reversible, logged as experiments)")
    L.append("")
    if applied:
        L.append("| key | old -> new | experiment | snapshot |")
        L.append("|---|---|---|---|")
        for a in applied:
            L.append(f"| {a.candidate.key} | {a.old_value} -> {a.new_value} | "
                     f"{a.experiment_id} | {Path(a.snapshot).name if a.snapshot else '—'} |")
    else:
        L.append("_none this cycle._")
    L.append("")

    L.append("## Proposed (require operator approval — fold into weekly PIPELINE_UPDATES)")
    L.append("")
    if propose:
        L.append("| PU# | Category | Target file | Change | Rationale | Impact/Effort | Sacred? | Traceability | Verdict |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for i, c in enumerate(propose, start=1):
            L.append(_pu_row(i, c))
    else:
        L.append("_no operator-gated proposals this cycle._")
    L.append("")
    out.write_text("\n".join(L), encoding="utf-8")
    return str(out)


def run_tuner(
    channel_root: Path | str,
    config: dict,
    report: AnalysisReport,
    today: date,
    *,
    config_path: Path | str,
    apply_enabled: bool,
    max_active_experiments: int = 2,
    max_auto_applies_per_cycle: int = 1,
) -> TunerResult:
    candidates = propose_changes(report, config)
    safe = [c for c in candidates if c.klass == SAFE_AUTO]
    propose = [c for c in candidates if c.klass != SAFE_AUTO]

    result = TunerResult(proposed=propose, apply_enabled=apply_enabled)

    if not apply_enabled:
        for c in safe:
            result.skipped.append((c.key, "apply disabled (report/propose-only)"))
        result.proposals_path = write_proposals(channel_root, today, safe, propose, result.applied)
        return result

    active_exps = experiments.active_experiments(channel_root)
    active = len(active_exps)
    active_targets = {e.target for e in active_exps}
    applies = 0
    for c in safe:
        if c.key in active_targets:
            # An experiment for this knob is still measuring — don't keep nudging
            # it; wait for the verdict (confirm/revert) before the next move.
            result.skipped.append((c.key, "experiment already active for this knob"))
            continue
        if applies >= max_auto_applies_per_cycle:
            result.skipped.append((c.key, "per-cycle apply cap reached"))
            continue
        if active >= max_active_experiments:
            result.skipped.append((c.key, "max active experiments reached"))
            continue
        spec = c.spec
        try:
            res = config_io.apply_knob(config_path, c.key, c.proposed_value)
        except config_io.ConfigEditError as exc:
            result.skipped.append((c.key, f"apply refused: {exc}"))
            continue
        if not res.changed:
            result.skipped.append((c.key, "already at proposed value"))
            continue
        exp = experiments.open_experiment(
            channel_root, target=c.key, target_file="config.yaml", kind="auto",
            hypothesis=c.rationale, baseline_value=c.baseline_value, baseline_n=c.baseline_n,
            old_setting=_coerce(res.old_value, spec.value_type if spec else float),
            new_setting=c.proposed_value,
            measurement_window_days=MEASUREMENT_WINDOW_DAYS,
            min_sample=spec.min_sample if spec else 5,
            min_effect_pct=spec.min_effect_pct if spec else 5.0,
            rollback_threshold_pct=spec.rollback_threshold_pct if spec else -10.0,
            opened_at=_opened_at(today), config_snapshot=res.snapshot,
        )
        result.applied.append(AppliedChange(
            candidate=c, old_value=res.old_value, new_value=c.proposed_value,
            experiment_id=exp.experiment_id if exp else None, snapshot=res.snapshot))
        applies += 1
        active += 1
        log.info("auto-applied %s: %s -> %s", c.key, res.old_value, c.proposed_value)

    result.proposals_path = write_proposals(channel_root, today, safe, propose, result.applied)
    return result


__all__ = ["AppliedChange", "TunerResult", "run_tuner", "write_proposals", "MEASUREMENT_WINDOW_DAYS"]
