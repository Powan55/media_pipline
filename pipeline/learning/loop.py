"""The self-improving loop orchestrator.

One cycle: build ledger -> evaluate active experiments (revert losers) -> analyze
(reach-first) -> run mistake sentinels -> tune (auto-apply <=1 safe knob + queue
proposals) -> write dashboard. Fail-soft at the top: any error logs and returns
a no-op ``LoopResult(status="error")`` so a learning bug can NEVER block video
production in ``/start -auto``.

Two modes:
  * apply=False (dry-run / report): read + analyze + write dashboard + write
    proposals. Mutates NO config, opens/closes NO experiments, writes NO incidents.
  * apply=True AND config ``learning.apply_enabled``: also reverts losing
    experiments, auto-applies <=1 safe knob (opening an experiment), and writes
    incidents. The double-gate (CLI flag AND config) is intentional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config_io, experiments
from . import paths as learning_paths
from .analysis import analyze
from .csv_health import audit_analytics_csv
from .dashboard import render_dashboard, write_dashboard
from .ledger import build_learning_ledger
from .maturity import maturity_summary
from .sentinels import run_sentinels
from .tuner import run_tuner

log = logging.getLogger("learning.loop")

PIPELINE_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LoopResult:
    status: str                       # "ok" | "error"
    apply_enabled: bool
    eligible_n: int = 0
    reverts: list = field(default_factory=list)
    applied: list = field(default_factory=list)
    proposed_n: int = 0
    sentinel_summary: str = ""
    dashboard_path: str | None = None
    proposals_path: str | None = None
    error: str | None = None

    def summary(self) -> str:
        if self.status == "error":
            return f"learning loop ERROR (non-fatal): {self.error}"
        mode = "APPLY" if self.apply_enabled else "report/propose-only"
        parts = [
            f"learning loop [{mode}]: {self.eligible_n} eligible videos",
            f"{len(self.applied)} auto-applied",
            f"{len(self.reverts)} reverted",
            f"{self.proposed_n} proposed",
        ]
        if self.sentinel_summary:
            parts.append(self.sentinel_summary)
        return " | ".join(parts)


def _load_full_config(config_path: Path) -> dict:
    try:
        import yaml

        if config_path.exists():
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not load full config from %s: %s", config_path, exc)
    return {}


def run_loop(
    channel_root: Path | str,
    learning_config: dict,
    *,
    apply: bool,
    today: date | None = None,
    config_path: Path | str | None = None,
) -> LoopResult:
    today = today or date.today()
    config_path = Path(config_path) if config_path else (PIPELINE_ROOT / "config.yaml")
    apply_enabled = bool(apply and (learning_config or {}).get("apply_enabled", False))

    try:
        retention_floor = float((learning_config or {}).get("retention_floor_pct", 25.0))
        min_sample = int((learning_config or {}).get("min_sample", 5))
        max_active = int((learning_config or {}).get("max_active_experiments", 2))
        max_per_cycle = int((learning_config or {}).get("max_auto_applies_per_cycle", 1))

        rows = build_learning_ledger(channel_root, today=today)

        # 1. Evaluate active experiments and revert losers FIRST (apply mode only).
        reverts = []
        if apply_enabled:
            outcomes = experiments.evaluate_active(
                channel_root, rows, today, retention_floor_pct=retention_floor)
            for o in outcomes:
                if o.revert_to is not None:
                    try:
                        config_io.revert_knob(config_path, o.target, o.revert_to)
                        reverts.append(o)
                        log.info("auto-reverted %s -> %s (%s)", o.target, o.revert_to, o.note)
                    except config_io.ConfigEditError as exc:
                        log.warning("revert failed for %s: %s", o.target, exc)

        # 2. Reach-first analysis (config-independent).
        report = analyze(rows, retention_floor_pct=retention_floor, min_sample=min_sample)

        # 3. Mistake sentinels (write incidents only in apply mode).
        sent = run_sentinels(channel_root, rows, report, today, write=apply_enabled)

        # 4. Tuner — fresh config so current knob values reflect any reverts.
        full_config = _load_full_config(config_path)
        tuner_res = run_tuner(
            channel_root, full_config, report, today,
            config_path=config_path, apply_enabled=apply_enabled,
            max_active_experiments=max_active, max_auto_applies_per_cycle=max_per_cycle)

        # 5. Dashboard (always written — pure report artifact).
        maturity = maturity_summary(rows)
        health = audit_analytics_csv(Path(channel_root) / "01_research" / "_weekly_analytics.csv")
        content = render_dashboard(report, datestr=today.isoformat(),
                                   maturity=maturity, csv_health=health)
        dash = write_dashboard(content, learning_paths.dashboard_md(channel_root, today.isoformat()))

        return LoopResult(
            status="ok", apply_enabled=apply_enabled, eligible_n=report.eligible_n,
            reverts=reverts, applied=tuner_res.applied, proposed_n=len(tuner_res.proposed),
            sentinel_summary=sent.summary, dashboard_path=str(dash),
            proposals_path=tuner_res.proposals_path)
    except Exception as exc:  # noqa: BLE001 — loop must never break the pipeline
        log.error("learning loop failed (non-fatal): %s", exc, exc_info=True)
        return LoopResult(status="error", apply_enabled=apply_enabled, error=str(exc))


__all__ = ["LoopResult", "run_loop"]
