"""Daily idea-generation stage for ShadowVerse.

Reads today's trend artifact + recent-topics history + style guide, builds the
substituted prompt from `prompts/02_idea_generation.md`, halts via the manual
LLM stage, and on resume parses the JSON response, scores each candidate, and
returns the top N picks.

Halt IO lives under a `_daily_<UTC-DATE>` pseudo-topic dir so the existing
manual-LLM convention applies without polluting per-topic dirs.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pipeline import ManualLLMHalt, load_prompt, load_style_guide
from scoring import ScoredCandidate, rank_candidates
from topics import format_for_prompt, list_recent_topics
from trend_pull import pull_all

log = logging.getLogger("idea_generation")


def _format_trend_candidates(trends_json_path: Path, *, max_items: int = 50) -> str:
    """Render the trend artifact as an indexed list for the `{TREND_CANDIDATES}` slot.

    Indexed so the idea-gen LLM can refer to source candidates by `[N]` in its
    `source_indexes` field, which the verifier later uses to confirm citations.
    """
    if not trends_json_path.exists():
        return "(no trend artifact found)"
    payload = json.loads(trends_json_path.read_text(encoding="utf-8"))
    candidates = (payload.get("candidates") or [])[:max_items]
    if not candidates:
        return "(trend artifact has no candidates)"
    lines: list[str] = []
    for i, c in enumerate(candidates):
        title = (c.get("title") or "").strip()
        url = c.get("url", "")
        source = c.get("source", "")
        tag = c.get("tag", "")
        score = c.get("score")
        score_str = f" [score={score}]" if score is not None else ""
        summary = (c.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 200:
            summary = summary[:200] + "..."
        lines.append(
            f"[{i}] ({source}, tag={tag}){score_str} {title}\n"
            f"    url: {url}\n"
            f"    summary: {summary}"
        )
    return "\n".join(lines)


def _ensure_trends_artifact(channel_root: Path, *, force_refresh: bool = False) -> Path:
    """Return today's `trends_<UTC-DATE>.json` path. Run pull_all if missing."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = channel_root / "01_research"
    artifact = out_dir / f"trends_{today}.json"
    if force_refresh or not artifact.exists():
        log.info("trend artifact missing or stale; running pull_all")
        artifact_path, _ = pull_all(out_dir)
        if artifact_path is None:
            raise RuntimeError("pull_all returned no artifact path (unexpected)")
        return artifact_path
    return artifact


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def _parse_idea_response(raw: str) -> list[dict]:
    """Tolerantly parse the LLM's idea-gen JSON response.

    Strips a single fenced code block if present (some chat LLMs wrap JSON in ```json```).
    """
    stripped = raw.strip()
    stripped = _FENCE_OPEN_RE.sub("", stripped)
    stripped = _FENCE_CLOSE_RE.sub("", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"idea-gen response is not valid JSON ({e}). First 500 chars:\n{stripped[:500]}"
        ) from e
    if not isinstance(parsed, list):
        raise ValueError(
            f"idea-gen response must be a JSON list, got {type(parsed).__name__}"
        )
    return parsed


def _build_audit_payload(
    ranked: list[ScoredCandidate],
    *,
    n_target: int,
    n_picks: int,
    trends_path: Path,
) -> dict:
    """Build the `idea_generation_RANKED.json` audit payload.

    Extracted from `generate_ideas` so the per-candidate field set has a single
    source of truth and is unit-testable without driving the full pipeline.
    Field order on each candidate mirrors `ScoredCandidate` for easier diffing.
    """
    return {
        "ranked_at": datetime.now(timezone.utc).isoformat(),
        "n_target": n_target,
        "n_picks": n_picks,
        "trends_artifact": str(trends_path),
        "ranked": [
            {
                "topic": s.topic,
                "angle": s.angle,
                "hook_concept": s.hook_concept,
                "why_now": s.why_now,
                "audience": s.audience,
                "source_indexes": s.source_indexes,
                "cited_observation_candidate": s.cited_observation_candidate,
                "counter_conventional_bonus": s.counter_conventional_bonus,
                "ai_vendor_bonus": s.ai_vendor_bonus,
                "named_human_bonus": s.named_human_bonus,
                "weighted_total": s.weighted_total,
                "rationale": s.rationale,
            }
            for s in ranked
        ],
    }


def generate_ideas(
    config: dict,
    *,
    n_target: int = 10,
    n_picks: int = 2,
    force_refresh_trends: bool = False,
) -> list[ScoredCandidate]:
    """Daily idea generation.

    Halts at manual LLM stage if the response file doesn't exist yet. On resume
    (response file present), parses, scores, ranks, returns the top n_picks.
    """
    channel_root = Path(config["paths"]["channel_root"])
    trends_path = _ensure_trends_artifact(channel_root, force_refresh=force_refresh_trends)

    style_guide = load_style_guide(config)
    recent = list_recent_topics(channel_root, days=30)
    recent_block = format_for_prompt(recent)
    trend_block = _format_trend_candidates(trends_path)

    template = load_prompt("02_idea_generation", config)
    prompt = (
        template
        .replace("{NICHE_STYLE_GUIDE}", style_guide)
        .replace("{TREND_CANDIDATES}", trend_block)
        .replace("{RECENT_TOPICS}", recent_block)
        .replace("{N_TARGET}", str(n_target))
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = Path(config["llm"]["manual_io_dir"]) / f"_daily_{today}"
    base.mkdir(parents=True, exist_ok=True)
    prompt_path = base / "idea_generation_PROMPT.txt"
    response_path = base / "idea_generation_RESPONSE.txt"

    existing_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    if existing_prompt != prompt:
        prompt_path.write_text(prompt, encoding="utf-8")
        log.info("wrote idea-gen prompt: %s (%d chars)", prompt_path, len(prompt))
        if existing_prompt and response_path.exists():
            log.warning(
                "idea-gen prompt changed since last run; existing %s may be stale",
                response_path.name,
            )

    if not response_path.exists() or not response_path.read_text(encoding="utf-8").strip():
        raise ManualLLMHalt(prompt_path, response_path, "idea_generation")

    raw = response_path.read_text(encoding="utf-8").strip()
    log.info("loaded idea-gen response: %s (%d chars)", response_path, len(raw))

    candidates = _parse_idea_response(raw)
    ranked = rank_candidates(candidates)
    if not ranked:
        raise ValueError("idea-gen response parsed but produced 0 ranked candidates")

    log.info(
        "ranked %d candidates; top=%.3f bottom=%.3f",
        len(ranked), ranked[0].weighted_total, ranked[-1].weighted_total,
    )

    picks = ranked[:n_picks]
    # Persist a sidecar with the full ranked list (audit + reuse if we want top N+1 fallback)
    audit_path = base / "idea_generation_RANKED.json"
    audit_payload = _build_audit_payload(
        ranked, n_target=n_target, n_picks=n_picks, trends_path=trends_path,
    )
    audit_path.write_text(json.dumps(audit_payload, indent=2), encoding="utf-8")
    log.info("wrote ranked audit: %s", audit_path)

    return picks
