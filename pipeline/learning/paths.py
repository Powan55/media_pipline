"""Canonical locations for the learning loop's RUNTIME artifacts.

Code lives in the pipeline repo (``_pipeline/learning/*.py``); runtime state
(the derived ledger, the experiment journal, dashboards, proposals) lives under
the channel root, which is production scratch (NOT git-synced). They are kept
separate so the journal/dashboards never get committed and the code stays clean.

All artifacts sit under ``<channel_root>/01_research/_learning/`` — the
underscore prefix matches the existing convention for derived research files
(``_weekly_analytics.csv``, ``_video_clusters.csv``).
"""

from __future__ import annotations

from pathlib import Path

STATE_DIRNAME = "_learning"


def state_dir(channel_root: Path | str) -> Path:
    return Path(channel_root) / "01_research" / STATE_DIRNAME


def ensure_state_dir(channel_root: Path | str) -> Path:
    d = state_dir(channel_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_csv(channel_root: Path | str) -> Path:
    return state_dir(channel_root) / "learning_ledger.csv"


def experiments_jsonl(channel_root: Path | str) -> Path:
    return state_dir(channel_root) / "experiments.jsonl"


def dashboard_md(channel_root: Path | str, datestr: str) -> Path:
    return state_dir(channel_root) / f"dashboard_{datestr}.md"


def proposals_md(channel_root: Path | str, datestr: str) -> Path:
    return state_dir(channel_root) / f"proposed_changes_{datestr}.md"


def proposed_memory_md(channel_root: Path | str) -> Path:
    return state_dir(channel_root) / "proposed_memory_updates.md"


def incidents_jsonl(channel_root: Path | str) -> Path:
    return state_dir(channel_root) / "incidents.jsonl"


__all__ = [
    "STATE_DIRNAME",
    "state_dir",
    "ensure_state_dir",
    "ledger_csv",
    "experiments_jsonl",
    "dashboard_md",
    "proposals_md",
    "proposed_memory_md",
    "incidents_jsonl",
]
