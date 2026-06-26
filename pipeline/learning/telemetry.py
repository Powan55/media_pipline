"""Append-only telemetry the learning loop consumes.

Currently one writer: ``append_script_quality`` persists the Stage-1.5
self-scored quality dimensions per topic so the loop can later correlate craft
scores against downstream reach. It is called from
``pipeline.evaluate_script_quality`` inside a try/except so a telemetry failure
can never break the quality gate or block a render.

Schema of ``01_research/script_quality_log.jsonl`` (one JSON object per line):
    {"topic_id": str, "dims": {dim: float, ...}, "weighted_total": float,
     "ts": ISO-8601 UTC string}

Stdlib only. The writer creates the parent directory if needed and appends a
single line; concurrent appends are line-atomic on the platforms we target
(each record is well under the OS pipe-buffer write-atomicity limit).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("learning.telemetry")

SCRIPT_QUALITY_LOG_NAME = "script_quality_log.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_script_quality(
    channel_root: Path | str,
    *,
    topic_id: str,
    dims: dict[str, float],
    weighted_total: float,
    now_iso: str | None = None,
) -> Path:
    """Append one Stage-1.5 quality record to the per-channel JSONL log.

    Args:
        channel_root: e.g. ``C:/ContentOps/channels/ShadowVerse``. The log is
            written under ``<channel_root>/01_research/``.
        topic_id: Canonical topic id, e.g. ``2026-06-19_001``.
        dims: The parsed per-dimension quality scores (0..1 each).
        weighted_total: The Stage-1.5 weighted-total score.
        now_iso: Override timestamp for deterministic tests. Defaults to UTC now.

    Returns:
        The path written to.

    Raises:
        OSError / ValueError on a genuine I/O or serialization failure. Callers
        in the pipeline hot path MUST wrap this in try/except — telemetry is
        never allowed to break the gate.
    """
    if not channel_root:
        raise ValueError("channel_root is required for quality telemetry")
    research = Path(channel_root) / "01_research"
    research.mkdir(parents=True, exist_ok=True)
    out_path = research / SCRIPT_QUALITY_LOG_NAME

    record = {
        "topic_id": topic_id,
        "dims": {k: float(v) for k, v in (dims or {}).items()},
        "weighted_total": float(weighted_total),
        "ts": now_iso or _utc_now_iso(),
    }
    line = json.dumps(record, ensure_ascii=False)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    log.debug("appended script-quality telemetry for %s", topic_id)
    return out_path


def load_quality_log(path: Path | str) -> dict[str, dict]:
    """Read the quality JSONL into ``topic_id -> latest record``.

    Last write wins for a repeated topic_id (a re-scored draft). Missing / empty
    file yields ``{}``. Malformed lines are skipped with a warning — never fatal.
    """
    out: dict[str, dict] = {}
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return out
    with p.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("quality log line %d not valid JSON: %s", lineno, exc)
                continue
            topic_id = rec.get("topic_id")
            if not topic_id:
                continue
            out[topic_id] = rec
    return out


__all__ = ["append_script_quality", "load_quality_log", "SCRIPT_QUALITY_LOG_NAME"]
