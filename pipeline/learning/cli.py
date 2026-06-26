"""Command-line entry point for the self-improving loop.

    python -m learning.cli --report     # build ledger -> analyze -> write dashboard
    python -m learning.cli --dry-run     # run the full loop, apply NOTHING
    python -m learning.cli --apply       # run the full loop, auto-apply <=1 safe knob

``--report`` is fully implemented here (Phase 1). ``--dry-run`` / ``--apply``
delegate to ``learning.loop.run_loop`` once it exists (Phase 3); until then they
fall back to the read-only report with a notice. The whole CLI is fail-soft: any
error prints a message and returns non-zero WITHOUT raising, so a scheduler /
start.md step can log-and-continue.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from . import paths as learning_paths
from .analysis import analyze
from .csv_health import audit_analytics_csv
from .dashboard import render_dashboard, write_dashboard
from .ledger import build_learning_ledger
from .maturity import maturity_summary

log = logging.getLogger("learning.cli")

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CHANNEL_ROOT = Path(r"C:\ContentOps\channels\ShadowVerse")


def _load_config() -> dict:
    """Read config.yaml (best-effort). Returns {} if unreadable."""
    try:
        import yaml

        cfg_path = PIPELINE_ROOT / "config.yaml"
        if cfg_path.exists():
            return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read config.yaml: %s", exc)
    return {}


def _resolve(config: dict) -> tuple[Path, dict]:
    channel_root = Path(
        (config.get("paths") or {}).get("channel_root") or _DEFAULT_CHANNEL_ROOT
    )
    learning = config.get("learning") or {}
    return channel_root, learning


def cmd_report(channel_root: Path, learning: dict, *, today: date | None = None) -> int:
    today = today or date.today()
    datestr = today.isoformat()
    retention_floor = float(learning.get("retention_floor_pct", 25.0))
    min_sample = int(learning.get("min_sample", 5))

    rows = build_learning_ledger(channel_root, today=today)
    report = analyze(rows, retention_floor_pct=retention_floor, min_sample=min_sample)
    maturity = maturity_summary(rows)
    health = audit_analytics_csv(channel_root / "01_research" / "_weekly_analytics.csv")

    content = render_dashboard(report, datestr=datestr, maturity=maturity, csv_health=health)
    out = write_dashboard(content, learning_paths.dashboard_md(channel_root, datestr))

    print(f"Eligible videos: {report.eligible_n} | cohort median views: "
          f"{report.cohort_median_views} | leaders: {len(report.leaders)}")
    if report.leaders:
        print("Top reach levers:")
        for fs in report.leaders[:5]:
            print(f"  - {fs.dimension}={fs.value}: median views "
                  f"{fs.median_views:.0f} (n={fs.n}, {fs.evidence})")
    print(f"Dashboard written: {out}")
    return 0


def cmd_loop(channel_root: Path, learning: dict, *, apply: bool, today: date | None = None) -> int:
    try:
        from .loop import run_loop  # Phase 3
    except ImportError:
        print("(learning loop not wired yet — falling back to read-only report)")
        return cmd_report(channel_root, learning, today=today)
    result = run_loop(channel_root, learning, apply=apply, today=today)
    print(result.summary() if hasattr(result, "summary") else result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShadowVerse self-improving loop.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="Read-only dashboard (default).")
    mode.add_argument("--dry-run", action="store_true", help="Run the loop, apply nothing.")
    mode.add_argument("--apply", action="store_true", help="Run the loop, auto-apply safe knobs.")
    parser.add_argument("--channel-root", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = _load_config()
    channel_root, learning = _resolve(config)
    if args.channel_root:
        channel_root = Path(args.channel_root)

    try:
        if args.apply:
            return cmd_loop(channel_root, learning, apply=True)
        if args.dry_run:
            return cmd_loop(channel_root, learning, apply=False)
        return cmd_report(channel_root, learning)
    except Exception as exc:  # noqa: BLE001 — CLI must never crash a scheduler
        log.error("learning CLI failed (non-fatal): %s", exc, exc_info=True)
        print(f"learning loop error (non-fatal): {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
