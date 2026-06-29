"""ShadowVerse production pipeline orchestrator.

Plain Python. No agent framework. Each stage is an explicit function call.
Every stub raises NotImplementedError with phase guidance so silent skips are impossible.

Three sacred human gates are enforced here:
    1. Idea selection — happens BEFORE this module is invoked (Notion/Sheet manual step).
    2. Fact-check resolution — `await_fact_check_resolution()` halts the pipeline.
    3. Final video QA — `await_final_qa()` halts the pipeline.

Run as a module:
    python -m pipeline --topic-id 2026-05-05_001
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, TypeVar

import requests
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

PIPELINE_ROOT = Path(__file__).resolve().parent
log = logging.getLogger("pipeline")

JobStatus = Literal[
    "topic_approved",
    "script_drafted",
    "fact_check_pending",
    "fact_check_resolved",
    "assets_fetched",
    "voiceover_done",
    "captions_done",
    "render_done",
    "qa_approved",
    "variants_done",
    "metadata_done",
    "scheduled",
    "published",
    "failed",
]

Platform = Literal["youtube", "tiktok", "instagram"]


class PipelineHalted(Exception):
    """Base class for intentional pipeline pauses — manual LLM work or sacred gates.

    Distinct from NotImplementedError (which means a stage is unimplemented) and from
    arbitrary exceptions (which mean something went wrong). Halts are idempotent: the
    pipeline can be re-run and will resume past the halt once the operator has done
    their part.
    """


class ManualLLMHalt(PipelineHalted):
    """Raised when a manual-mode LLM stage is waiting for the operator's response file.

    Re-running the pipeline after writing the response file resumes from the same stage.
    """

    def __init__(self, prompt_path: Path, response_path: Path, stage_name: str):
        self.prompt_path = prompt_path
        self.response_path = response_path
        self.stage_name = stage_name
        super().__init__(
            f"\n\n  [HALT] {stage_name} requires manual LLM response.\n"
            f"  1. Open the prompt:   {prompt_path}\n"
            f"  2. Paste it into your chat LLM (Claude Code / Claude.ai / ChatGPT).\n"
            f"  3. Save the response: {response_path}\n"
            f"  4. Re-run the pipeline; it will resume from this stage.\n"
        )


class HumanReviewRequired(PipelineHalted):
    """Raised when a sacred manual gate is waiting for the operator's review / approval.

    Operator's job is to read the review doc and edit / accept the action file.
    Re-running the pipeline after the action file is in the desired state continues past
    the gate.
    """

    def __init__(self, gate_name: str, review_path: Path, action_path: Path, summary: str):
        self.gate_name = gate_name
        self.review_path = review_path
        self.action_path = action_path
        super().__init__(
            f"\n\n  [GATE] {gate_name} requires human review.\n"
            f"  {summary}\n"
            f"  1. Read the review:    {review_path}\n"
            f"  2. Edit or accept:     {action_path}\n"
            f"  3. Re-run the pipeline; it will continue past this gate.\n"
        )


class QualityCheckFailed(PipelineHalted):
    """Raised when a script's self-scored quality is below the configured threshold.

    Distinct from HumanReviewRequired in that the operator's action is to REWRITE
    upstream (script_RESPONSE.txt), not to edit a downstream script_FINAL. Idempotent:
    once the response has improved scores, re-running resumes past the gate.
    """

    def __init__(self, topic_id: str, scores: dict[str, float], total: float, threshold: float):
        self.topic_id = topic_id
        self.scores = scores
        self.total = total
        self.threshold = threshold
        score_lines = "\n".join(f"    {k}: {v:.2f}" for k, v in sorted(scores.items()))
        super().__init__(
            f"\n\n  [QUALITY-GATE] script for topic {topic_id} scored {total:.2f} "
            f"(threshold {threshold:.2f}).\n"
            f"  Per-dimension scores:\n{score_lines}\n"
            f"  1. Rewrite the response file with stronger hooks / specifics / opinion / sources / B-roll.\n"
            f"  2. Re-run the pipeline; it will resume from script generation.\n"
            f"  (Or lower script_quality.min_score in config.yaml if the threshold is too strict.)\n"
        )


class ScriptRuleViolation(PipelineHalted):
    """Raised when a script breaks a Stage 1.5 hard rule (2026-06-09 review).

    Covers the discovery-floor checks (PU-3 anchor gate / PU-4 modal ban, R2
    H1/H2) and the word-count bounds halt (PU-11, R2 H4). Semantics per Manager
    C3: capped regenerate-with-feedback — script generation is a manual-halt
    LLM stage, so this halt IS the regenerate request. The message names the
    violated rule(s) so the rewrite is targeted. Attempts are tracked in
    `<manual_io_dir>/<topic_id>/stage15_regen_attempts.txt`; past
    ``max_attempts`` the message escalates to the operator instead of asking
    for another rewrite. The topic is NEVER auto-killed.
    """

    def __init__(self, topic_id: str, violations: list[str], attempt: int, max_attempts: int):
        self.topic_id = topic_id
        self.violations = violations
        self.attempt = attempt
        self.max_attempts = max_attempts
        rules = "\n".join(f"    - {v}" for v in violations)
        if attempt <= max_attempts:
            action = (
                f"  Regenerate attempt {attempt} of {max_attempts}: rewrite "
                f"script_RESPONSE.txt addressing the feedback above, then re-run the "
                f"pipeline; it will resume from script generation and re-check.\n"
            )
        else:
            action = (
                f"  Max regenerate attempts ({max_attempts}) exhausted — surface to the "
                f"OPERATOR for a manual decision (ship-with-waiver, hand-fix, or "
                f"reschedule). Do NOT auto-kill the topic.\n"
            )
        super().__init__(
            f"\n\n  [SCRIPT-RULE-GATE] Stage 1.5 hard-rule violation(s) for topic "
            f"{topic_id}:\n{rules}\n"
            f"{action}"
            f"  (Rollback: each check is config-flagged under script_quality in "
            f"config.yaml — anchor_gate_enabled / modal_ban_enabled / "
            f"word_count_halt_enabled.)\n"
        )


class MetadataRuleViolation(PipelineHalted):
    """Raised when generated metadata breaks a config-flagged hard rule (the
    title-anchor gate, PU-3T). Metadata is a manual-halt LLM stage, so this halt
    IS the regenerate request: fix the title in metadata_RESPONSE.txt and re-run.
    The topic is NEVER auto-killed. One-flip rollback via
    ``script_quality.title_anchor_gate_enabled``.
    """

    def __init__(self, topic_id: str, violations: list[str]):
        self.topic_id = topic_id
        self.violations = violations
        rules = "\n".join(f"    - {v}" for v in violations)
        super().__init__(
            f"\n\n  [METADATA-RULE-GATE] metadata hard-rule violation(s) for topic "
            f"{topic_id}:\n{rules}\n"
            f"  Rewrite the YOUTUBE SHORTS Title in metadata_RESPONSE.txt so its "
            f"first 3 words carry a recognizable anchor, then re-run; the pipeline "
            f"resumes from metadata generation and re-checks.\n"
            f"  (Rollback: set script_quality.title_anchor_gate_enabled: false in "
            f"config.yaml.)\n"
        )


class IntegrityCheckFailed(PipelineHalted):
    """Raised when `tools.media_integrity.check_integrity` rejects a master or variant.

    Distinct from `PipelineQAFailed` (which aggregates the 11-check prepublish gate):
    this fires for the structural-soundness gate that runs immediately after each
    render. Operator's action is to investigate the broken file (typically a
    truncated MP4 missing its `moov` atom) and re-render. Idempotent: once a clean
    file is on disk, re-running the pipeline resumes past the gate.
    """

    def __init__(self, video_path: Path, reason: str, *, stage: str = "post-render"):
        self.video_path = video_path
        self.reason = reason
        self.stage = stage
        super().__init__(
            f"\n\n  [INTEGRITY-GATE] {stage} integrity check FAILED for "
            f"{video_path.name}.\n"
            f"  Reason: {reason}\n"
            f"  1. Inspect / re-render the file at {video_path}.\n"
            f"  2. Re-run the pipeline; the gate will re-check.\n"
        )


# ---------------------------------------------------------------------------
# Typed data models — the contracts between stages
# ---------------------------------------------------------------------------


class TopicJob(BaseModel):
    """A topic the human picked from the trend-pull inbox; entry point to the pipeline."""

    id: str = Field(..., description="YYYY-MM-DD_NNN sequence — joins to the tracker")
    topic: str
    angle: str
    hook_concept: str
    status: JobStatus = "topic_approved"


class ScriptDraft(BaseModel):
    """Output of generate_script(); input to fact_check_script()."""

    topic_id: str
    hook_variants: list[str]                 # exactly 3 per the script-gen prompt
    hook_formulas: list[str] = []            # optional formula names per hook (Contradiction, etc.)
    chosen_hook_index: int | None = None     # filled in during human review
    body: str                                # 80–95 spoken words (aim ~88) with [B-ROLL: ...] cues inline
    broll_cues: list[str]                    # parsed from body
    fact_check_queue: list[str]              # claims the LLM flagged for verification
    word_count: int
    quality_scores: dict[str, float] = {}    # parsed from QUALITY_SCORES section if present
    quality_rationale: str = ""              # optional one-line rationale from the LLM


class FactClaim(BaseModel):
    claim_text: str
    status: Literal["VERIFIED", "UNCLEAR", "LIKELY_WRONG", "UNVERIFIABLE"]
    source_url: str | None = None
    source_quote: str | None = None
    suggested_fix: str | None = None         # populated when status == LIKELY_WRONG
    # tool_used captures which verification tool produced the row (audit M3).
    # `tavily` is the preferred path per `feedback_tavily_mcp.md`. `web` means
    # the fact-checker fell back to WebSearch/WebFetch. `none` means the claim
    # was verified from internal knowledge alone. `unknown` is the default for
    # legacy 5-column responses written before the Tool column existed — the
    # parser logs a WARNING when it sees that path.
    tool_used: Literal["tavily", "web", "none", "unknown"] = "unknown"


class FactCheckReport(BaseModel):
    topic_id: str
    claims: list[FactClaim]

    @property
    def unresolved_count(self) -> int:
        return sum(1 for c in self.claims if c.status in {"UNCLEAR", "LIKELY_WRONG"})


class AssetBundle(BaseModel):
    topic_id: str
    clips: list[Path]                        # b-roll video clips on disk
    images: list[Path]                       # generated/stock images
    licenses: list[dict]                     # one entry per asset; {source, license, url}


class MetadataBundle(BaseModel):
    topic_id: str
    youtube_title: str
    youtube_description: str
    youtube_tags: list[str]
    youtube_hashtags: list[str]
    tiktok_caption: str
    tiktok_hashtags: list[str]
    instagram_caption: str
    instagram_hashtags: list[str]
    cover_text: str
    cover_background_desc: str
    cover_color_accent: str
    # Pattern name from prompts/library/thumbnail_patterns.md, drives the renderer.
    # Defaults to "big_text_claim" when the metadata LLM omits the field (legacy responses).
    cover_pattern_name: str = "big_text_claim"
    # Operator pastes-and-pins this at upload (PU-2, 2026-05-29). One short non-URL
    # line in the friend voice posing the script's stakes-tied closing question.
    # Not consumed by any render/upload stage — surfaced for the manual Saturday
    # engagement SOP. Empty when the metadata LLM omits the section (legacy responses).
    pinned_comment: str = ""


# ---------------------------------------------------------------------------
# Config + logging bootstrap
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None = None) -> dict:
    """Load config.yaml and overlay .env. Fails loud if either is missing."""
    config_path = config_path or PIPELINE_ROOT / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing {config_path}. Copy config.yaml.template to config.yaml and edit."
        )

    env_path = PIPELINE_ROOT / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f"Missing {env_path}. Copy .env.template to .env and fill in your keys."
        )
    load_dotenv(env_path)

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


# WORKFLOW_AUDIT_2026-05-31 M4: the keys every CLI entrypoint needs before it can
# run a stage. Missing keys previously surfaced as a bare KeyError deep inside a
# stage (e.g. config["assets"]["preferred_stock_provider"] at fetch_assets); this
# lets the CLI fail fast at startup with ALL the missing paths named at once.
# NOTE: validate_config only ASSERTS PRESENCE — it never injects defaults or
# mutates values, so the sacred keys (fact_check.require_human_resolution /
# auto_resolve_gate_2, publishing.human_qa_required / kill_switch) are read-only here.
_REQUIRED_CONFIG_KEYS: dict[str, list[str]] = {
    "paths": ["channel_root", "logs", "prompts", "project_root"],
    "channel": ["style_guide_path"],
    "llm": ["primary_provider", "manual_io_dir"],
    "tts": ["provider"],
    "assets": ["preferred_stock_provider"],
    "fact_check": ["require_human_resolution", "auto_resolve_gate_2"],
    "publishing": ["human_qa_required", "kill_switch"],
    "logging": ["level"],
}


def validate_config(config: dict) -> None:
    """Assert every required `section.key` is present in `config`.

    Raises a single aggregated ``ValueError`` listing ALL missing paths, so the
    operator fixes config.yaml in one pass instead of chasing one KeyError per
    run. Called from the CLI entrypoints (NOT from bare ``load_config``, which
    stays permissive so the dual-agent isolation contract test — which loads a
    deliberately minimal config — is unaffected).

    Presence-only: this reads keys to confirm they exist; it never writes or
    defaults them, so the sacred fact_check/publishing values are untouched.
    """
    missing: list[str] = []
    for section, keys in _REQUIRED_CONFIG_KEYS.items():
        block = config.get(section)
        if not isinstance(block, dict):
            # The whole section is absent (or not a mapping) — every key under it
            # is missing; name them all so the message is actionable.
            missing.extend(f"{section}.{k}" for k in keys)
            continue
        for k in keys:
            if k not in block:
                missing.append(f"{section}.{k}")
    if missing:
        raise ValueError(
            "config.yaml is missing required key(s): "
            + ", ".join(missing)
            + ". Edit config.yaml (see config.yaml.template) and re-run."
        )

    # Optional dual-track block (added 2026-06-21). NOT in _REQUIRED_CONFIG_KEYS —
    # absent means single-track mode. If present, validate its types so a malformed
    # block fails loud at startup rather than mid-run. (Presence-only otherwise.)
    tracks = config.get("tracks")
    if tracks is not None:
        track_errors: list[str] = []
        if not isinstance(tracks, dict):
            track_errors.append("tracks must be a mapping")
        else:
            dte = tracks.get("dual_track_enabled")
            if "dual_track_enabled" in tracks and not isinstance(dte, bool):
                track_errors.append("tracks.dual_track_enabled must be true/false")
            gt = tracks.get("general_tech")
            if gt is not None:
                if not isinstance(gt, dict):
                    track_errors.append("tracks.general_tech must be a mapping")
                else:
                    sb = gt.get("suppress_ai_vendor_bonus")
                    if "suppress_ai_vendor_bonus" in gt and not isinstance(sb, bool):
                        track_errors.append(
                            "tracks.general_tech.suppress_ai_vendor_bonus must be true/false"
                        )
                    slot = gt.get("slot")
                    if "slot" in gt and (not isinstance(slot, int) or isinstance(slot, bool)):
                        track_errors.append("tracks.general_tech.slot must be an integer")
        if track_errors:
            raise ValueError(
                "config.yaml has a malformed [tracks] block: "
                + "; ".join(track_errors)
                + ". See config.yaml.template."
            )


def setup_logging(config: dict, run_id: str) -> Path:
    """Configure logging. Returns the per-run log directory.

    Per-run subdir means each pipeline invocation gets isolated logs — easy to debug.
    """
    log_root = Path(config["paths"]["logs"])
    run_dir = log_root / run_id if config["logging"].get("per_run_subdir", True) else log_root
    run_dir.mkdir(parents=True, exist_ok=True)

    log_file = run_dir / "pipeline.log"
    fmt = config["logging"].get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    logging.basicConfig(
        level=config["logging"].get("level", "INFO"),
        format=fmt,
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
        force=True,
    )
    log.info("logging initialized — run_id=%s log_file=%s", run_id, log_file)
    return run_dir


_PROMPT_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)


def load_prompt(name: str, config: dict) -> str:
    """Read a prompt template from prompts/<name>.md.

    Each prompt file in `prompts/` has a small markdown header (purpose, substitution map)
    followed by the actual prompt inside a fenced code block. This loader returns only the
    content of the FIRST fenced block — header text is documentation, not part of the prompt.
    Files with no fence are returned whole (back-compat).
    """
    prompts_dir = Path(config["paths"]["prompts"])
    path = prompts_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    raw = path.read_text(encoding="utf-8")
    fence = _PROMPT_FENCE_RE.search(raw)
    return fence.group(1) if fence else raw


def load_style_guide(config: dict) -> str:
    """Read the channel's style guide verbatim — injected as {NICHE_STYLE_GUIDE} in prompts."""
    path = Path(config["channel"]["style_guide_path"])
    if not path.exists():
        raise FileNotFoundError(f"Style guide not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Manual-mode LLM dispatch — file-IO halt pattern
# ---------------------------------------------------------------------------


def _manual_io_paths(stage_name: str, topic_id: str, config: dict) -> tuple[Path, Path]:
    """Return (prompt_path, response_path) for a manual-mode LLM stage.

    Per-topic subdir is created on demand under config.llm.manual_io_dir.
    """
    base = Path(config["llm"]["manual_io_dir"]) / topic_id
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{stage_name}_PROMPT.txt", base / f"{stage_name}_RESPONSE.txt"


def _await_manual_response(
    prompt_text: str, stage_name: str, topic_id: str, config: dict
) -> str:
    """Manual LLM dispatch: write prompt, return response if it exists, else halt.

    Idempotent — re-running the pipeline after writing the response file resumes here.
    The prompt file is always rewritten so re-substitutions (style guide updates etc.)
    propagate; the operator should regenerate the response if the prompt changed.
    """
    prompt_path, response_path = _manual_io_paths(stage_name, topic_id, config)

    existing_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    if existing_prompt != prompt_text:
        prompt_path.write_text(prompt_text, encoding="utf-8")
        log.info("wrote manual prompt: %s (%d chars)", prompt_path, len(prompt_text))
        if existing_prompt and response_path.exists():
            log.warning(
                "prompt changed since last run; existing %s may be stale",
                response_path.name,
            )

    if not response_path.exists() or not response_path.read_text(encoding="utf-8").strip():
        raise ManualLLMHalt(prompt_path, response_path, stage_name)

    response = response_path.read_text(encoding="utf-8").strip()
    log.info("loaded manual response: %s (%d chars)", response_path, len(response))
    return response


# ---------------------------------------------------------------------------
# Pipeline stages — each one is a real function with NotImplementedError stub
# ---------------------------------------------------------------------------


_HOOK_RE = re.compile(r"^HOOK_([ABC])\s*:\s*(.+?)$", re.MULTILINE)
_FACT_CHECK_MARKER_RE = re.compile(
    r"^[\s#*]*FACT[_\s]*CHECK[_\s]*QUEUE[\s:#*]*$",
    re.MULTILINE | re.IGNORECASE,
)
_QUALITY_SCORES_MARKER_RE = re.compile(
    r"^[\s#*]*QUALITY[_\s]*SCORES[\s:#*]*$",
    re.MULTILINE | re.IGNORECASE,
)
_BROLL_OPEN_RE = re.compile(r"\[B-?ROLL\s*:\s*", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)$", re.MULTILINE)
# Captures `[formula: Contradiction]` annotations on hook lines (case-insensitive)
_HOOK_FORMULA_RE = re.compile(r"\[\s*formula\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
# Parses one line of the QUALITY_SCORES section: `- name: value` where value is a float or a string (rationale)
_QUALITY_LINE_RE = re.compile(r"^\s*[-*]\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$", re.MULTILINE)

# Soft (advisory) set of the hook-formula short names the script-gen prompt
# examples emit (prompts/library/viral_hooks.md, prompts/03_script_generation.md).
# These are PROSE-headed in viral_hooks.md with no machine-readable list and the
# `[formula: X]` names are short forms, so this is WARN-ONLY: an unrecognized
# name (a typo, or a genuinely new formula) logs a warning but never rejects.
# Compared casefolded so capitalization/spacing in the annotation doesn't matter.
_KNOWN_HOOK_FORMULAS = {
    "contradiction",
    "specific-number promise",
    "result-first mid-action",
    "comparison frame",
    "anti-pattern setup",
    "specific-question",
    "measured-claim",
    "cited-observation lead",
    "format-branded",
    "you're doing it wrong",
    "result-first",
    "personal-breakthrough lead",   # #15 — general-tech / crazy-story LEAD formula (2026-06-21)
}


def _extract_broll(body: str) -> tuple[list[str], str]:
    """Extract `[B-ROLL: ...]` cues from body, returning (cues, body_without_cues).

    Bracket-depth-aware so a cue can contain nested `[...]` brackets, e.g.
        [B-ROLL: Cursor settings showing the "max session length" field]
    A regex that stops at the first `]` would truncate at any inner bracket.

    Note (2026-06-19 unification): on the `_parse_script_response` path, `[VERIFY: ...]`
    tags are already stripped upstream by `script_response_parser._clean_body`, so a
    nested VERIFY won't reach here from there. The depth-awareness still matters for
    OTHER callers that pass a not-yet-cleaned body (e.g. the operator-signed
    script_FINAL.txt in `await_fact_check_resolution`) and for any other nested bracket.
    """
    cues: list[str] = []
    out_parts: list[str] = []
    i = 0
    while i < len(body):
        m = _BROLL_OPEN_RE.search(body, i)
        if not m:
            out_parts.append(body[i:])
            break
        out_parts.append(body[i:m.start()])
        depth = 1
        j = m.end()
        while j < len(body) and depth > 0:
            ch = body[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            j += 1
        if depth == 0:
            cues.append(body[m.end():j - 1].strip())
            i = j
        else:
            # Unbalanced opener — preserve as-is and stop scanning.
            out_parts.append(body[m.start():])
            break
    return cues, "".join(out_parts)


def _parse_quality_scores(section: str) -> tuple[dict[str, float], str]:
    """Parse the QUALITY_SCORES section into a dict and an optional rationale string.

    Tolerant: lines that don't fit `- name: <float>` get treated as rationale candidates.
    Returns ({}, "") if the section is absent or unparseable — quality gating is opt-in.
    """
    scores: dict[str, float] = {}
    rationale = ""
    for m in _QUALITY_LINE_RE.finditer(section):
        name = m.group(1).strip().lower()
        raw = m.group(2).strip()
        if name == "rationale":
            rationale = raw
            continue
        # Strip optional comment / annotation in parens
        raw_num = re.sub(r"\s*\(.*\)\s*$", "", raw).strip()
        try:
            scores[name] = max(0.0, min(1.0, float(raw_num)))
        except ValueError:
            # Non-numeric value (e.g., "n/a") — skip this line silently
            continue
    return scores, rationale


def _strip_hook_formula(line: str) -> tuple[str, str]:
    """Pull the `[formula: <name>]` annotation off a hook line.

    Returns (clean_hook_text, formula_name_or_empty). Tolerant: missing annotation returns "".
    """
    fm = _HOOK_FORMULA_RE.search(line)
    if not fm:
        return line.strip(), ""
    formula = fm.group(1).strip()
    cleaned = _HOOK_FORMULA_RE.sub("", line).strip()
    return cleaned, formula


def _parse_script_response(
    response: str, topic_id: str, config: dict | None = None
) -> ScriptDraft:
    """Parse the LLM's script-gen response (per prompts/03_script_generation.md format) into ScriptDraft.

    Expected layout (newer format):
        HOOK_A: <first-sentence variant>   [formula: Contradiction]
        HOOK_B: <first-sentence variant>   [formula: Specific-Number Promise]
        HOOK_C: <first-sentence variant>   [formula: Cited-Observation Lead]

        <body prose with [B-ROLL: cue] tags inline>

        FACT_CHECK_QUEUE
        - <claim 1>
        - <claim 2>

        QUALITY_SCORES
        - hook_strength: 0.85
        - specificity: 0.70
        - opinion_density: 0.60
        - cited_observation_quality: 0.80
        - broll_cadence: 0.75
        - rationale: <one sentence>

    Tolerant of older responses without QUALITY_SCORES or [formula: ...] annotations —
    those fields parse to empty / {} and the quality gate becomes a no-op.

    Fails loud with a specific instruction on what to fix in the response file.
    """
    # M-1 unification (2026-06-19): body extraction + cleaning delegates to the
    # single shared, section-aware parser (tools.script_response_parser) so
    # generate_script's body (which manual gate-2 proposes) and the auto gate-2
    # body (extract_final_script) derive from ONE code path — no more dual-parser
    # drift. This wrapper keeps the manual-halt validation contract: the specific,
    # actionable ValueError messages the operator sees to fix script_RESPONSE.txt.
    from tools.script_response_parser import ScriptResponseParseError, parse_response

    # 1. Exactly 3 HOOK_A/B/C lines (the prompt asks for three variants).
    hook_matches = list(_HOOK_RE.finditer(response))
    if len(hook_matches) != 3:
        raise ValueError(
            f"Expected 3 lines of the form 'HOOK_A:' / 'HOOK_B:' / 'HOOK_C:', "
            f"found {len(hook_matches)}. Edit script_RESPONSE.txt and re-run."
        )

    # 2. FACT_CHECK_QUEUE section required.
    if not _FACT_CHECK_MARKER_RE.search(response):
        raise ValueError(
            "Missing 'FACT_CHECK_QUEUE' section. The LLM must list every claim to be "
            "fact-checked as a bulleted list under that header. Edit script_RESPONSE.txt "
            "and re-run."
        )

    # 3. Section-aware parse: handles SCRIPT_BODY headers, strips [VERIFY] tags +
    #    ALL-CAPS template placeholders + CHOSEN HOOK:/SCRIPT: dividers + a
    #    trailing `---` rule, preserves [B-ROLL: ...] cues inline.
    try:
        parsed = parse_response(response)
    except ScriptResponseParseError as exc:
        raise ValueError(
            "Body is empty. The LLM must write the script between the hooks and the "
            "FACT_CHECK_QUEUE section. Edit script_RESPONSE.txt and re-run."
        ) from exc

    hooks = [parsed.hook_a_text, parsed.hook_b_text, parsed.hook_c_text]
    if any(h is None for h in hooks):
        missing = [f"HOOK_{ltr}" for ltr, h in zip(("A", "B", "C"), hooks) if h is None]
        raise ValueError(
            f"Missing {', '.join(missing)} line(s) — provide exactly one each of "
            f"HOOK_A / HOOK_B / HOOK_C. Edit script_RESPONSE.txt and re-run."
        )
    formulas = [
        parsed.hook_a_formula or "",
        parsed.hook_b_formula or "",
        parsed.hook_c_formula or "",
    ]
    for formula, letter in zip(formulas, ("A", "B", "C")):
        # WORKFLOW_AUDIT_2026-05-31 L1: advisory only — viral_hooks.md has no
        # machine-readable formula list and new formulas are expected, so a name
        # we don't recognize logs a WARNING but never rejects the draft.
        if formula and formula.casefold() not in _KNOWN_HOOK_FORMULAS:
            log.warning(
                "unknown hook formula %r on HOOK_%s — not in viral_hooks.md; "
                "keeping (tolerant for new formulas)",
                formula, letter,
            )

    body = parsed.script_body_text
    broll_cues, body_no_broll = _extract_broll(body)

    # WORKFLOW_AUDIT_2026-05-31 L1: word-count outlier rails from NON-SACRED config
    # keys read with .get() defaults (so a config without them still parses — the
    # dual-agent isolation fixtures rely on load_config staying permissive).
    sq = (config or {}).get("script_quality", {}) if config is not None else {}
    word_count_min = int(sq.get("word_count_min", 80))
    word_count_max = int(sq.get("word_count_max", 200))
    word_count = len(body_no_broll.split())
    if word_count < word_count_min or word_count > word_count_max:
        log.warning(
            "script body is %d words; expected %d–%d. Operator should review.",
            word_count, word_count_min, word_count_max,
        )

    return ScriptDraft(
        topic_id=topic_id,
        hook_variants=[h for h in hooks if h is not None],
        hook_formulas=formulas,
        body=body,
        broll_cues=broll_cues,
        fact_check_queue=parsed.fact_check_queue,
        word_count=word_count,
        quality_scores=parsed.quality_scores,
        quality_rationale=parsed.quality_rationale or "",
    )


def generate_script(topic: TopicJob, config: dict) -> ScriptDraft:
    """Stage 1: LLM produces 3 hook variants + an 80–95 word script with [B-ROLL] cues.

    Dispatches on `config.llm.primary_provider`:
      - "manual" (default): write filled prompt to <manual_io_dir>/<topic_id>/script_PROMPT.txt,
        halt with ManualLLMHalt; on resume, parse script_RESPONSE.txt into a ScriptDraft.
      - "anthropic"/"openai": not implemented today — pipeline runs in manual mode by design.
    """
    provider = config["llm"]["primary_provider"]
    if provider != "manual":
        raise NotImplementedError(
            f"llm.primary_provider={provider!r} is not implemented. "
            f"Only 'manual' is supported today; flip the value in config.yaml or implement "
            f"the API branch and re-add the matching package to requirements.txt."
        )

    template = load_prompt((config.get("prompts") or {}).get("script", "03_script_generation"), config)
    style_guide = load_style_guide(config)

    # CTA rotation is the operator's call — inject a directive that points the LLM at the
    # CTA section of the style guide rather than hardcoding one of the variants here.
    cta_directive = (
        "Use one of the CTAs from the 'Format constraints / CTA style' section in the "
        "style guide above. Pick one not used in the last 2 videos."
    )

    prompt = (
        template
        .replace("{NICHE_STYLE_GUIDE}", style_guide)
        .replace("{TOPIC}", topic.topic)
        .replace("{ANGLE}", topic.angle)
        .replace("{HOOK_CONCEPT}", topic.hook_concept)
        .replace("{CTA_STYLE}", cta_directive)
    )

    response = _await_manual_response(prompt, "script", topic.id, config)
    return _parse_script_response(response, topic.id, config)


# ---------------------------------------------------------------------------
# Stage 1.5 — script quality gate (no LLM call, reads self-scored fields)
# ---------------------------------------------------------------------------


# The 5 dimensions the script-gen prompt asks for, each weighted equally.
# Keep in sync with prompts/03_script_generation.md QUALITY_SCORES section
# and prompts/11_script_quality_review.md.
SCRIPT_QUALITY_DIMENSIONS: tuple[str, ...] = (
    "hook_strength",
    "second_hook_strength",
    "specificity",
    "opinion_density",
    "cited_observation_quality",
    "broll_cadence",
)


# ---------------------------------------------------------------------------
# Stage 1.5 hard hook rules — 2026-06-09 weekly review, PU-3 + PU-4 (R2 H1/H2)
# discovery-floor change-set, plus the PU-11 word-count halt (R2 H4).
# All config-flagged under `script_quality` for one-flip rollback.
# ---------------------------------------------------------------------------

_STAGE15_BROLL_RE = re.compile(r"\[B-ROLL:[^\]]*\]", re.IGNORECASE)

# Modal / hypothetical openers banned in sentence 1 + title (PU-4, R2 H2: the
# 10-view-floor autopsy). The spoken-body gate can't see the title (it doesn't
# exist yet at Stage 1.5). The matching first-3-words TITLE gate (PU-3T) runs
# later at the metadata stage — see `title_anchor_violation` +
# `_enforce_metadata_hard_rules`; the prompt rule lives in 06_metadata_generation.md.
_MODAL_OPENER_RE = re.compile(
    r"\b(?:could|might)\b|\bimagine\s+if\b|\bwhat\s+if\b", re.IGNORECASE
)

# Curated vendor/brand names a layman recognizes on sight (lowercase). Part of
# the PU-3 anchor heuristic — extend freely, it only ever ADMITS scripts.
_KNOWN_VENDOR_NAMES = frozenset({
    "chatgpt", "openai", "claude", "anthropic", "gemini", "google", "deepmind",
    "apple", "siri", "iphone", "microsoft", "copilot", "windows", "meta",
    "facebook", "instagram", "whatsapp", "llama", "tesla", "elon", "musk",
    "grok", "xai", "nvidia", "amazon", "alexa", "android", "samsung", "tiktok",
    "youtube", "netflix", "spotify", "reddit", "midjourney", "deepseek",
    "perplexity", "bing", "sora", "gpt", "disney", "uber", "walmart",
    # Extended 2026-06-24 (title-anchor gate): brands/people the channel covers
    # whose names were otherwise unrecognized. Admit-only — safe for PU-3 too.
    "neuralink", "altman", "zuckerberg", "nadella", "pichai", "hassabis",
    "murati", "cursor", "suno", "runway", "elevenlabs", "mistral", "spacex",
    "waymo", "figma", "canva", "notion", "slack", "discord", "zoom", "github",
    "paypal", "snapchat", "pinterest", "linkedin", "karpathy",
})

# Curated universal consumer concepts (lowercase) — the non-name anchors a
# general viewer parses in under a second (PU-3, R2 H1).
_UNIVERSAL_CONSUMER_CONCEPTS = frozenset({
    "ai", "a.i.", "robot", "robots", "chatbot", "chatbots", "phone", "phones",
    "smartphone", "internet", "lawsuit", "court", "judge", "police", "fbi",
    "government", "congress", "billion", "billions", "million", "millions",
    "money", "scam", "scams", "hacked", "hacker", "hackers", "banned",
    "doctor", "doctors", "hospital", "teacher", "teachers", "school",
    "schools", "job", "jobs", "boss", "kids", "teens", "grandma", "mom",
    "dad", "election", "president", "deepfake", "deepfakes",
})


def _spoken_text(body: str) -> str:
    """Script body with [B-ROLL: ...] cues stripped — what the TTS will speak."""
    return re.sub(r"\s+", " ", _STAGE15_BROLL_RE.sub(" ", body)).strip()


def _first_spoken_sentence(body: str) -> str:
    """First sentence of the spoken body (up to the first . ! or ?)."""
    spoken = _spoken_text(body)
    m = re.match(r"^(.+?[.!?])(?:\s|$)", spoken)
    return m.group(1).strip() if m else spoken


def _normalize_word(word: str) -> str:
    """Strip surrounding punctuation + a possessive 's for anchor matching."""
    w = word.strip(".,!?;:'\"“”‘’()[]…—–-")
    w = re.sub(r"[’']s$", "", w)
    return w


def anchor_gate_violation(body: str) -> str | None:
    """PU-3 (R2 H1) first-4-words anchor heuristic. None = OK, str = feedback.

    The first 4 spoken words must contain at least one of:
      - a concrete number (any digit),
      - a known vendor/brand name (curated list),
      - a universal consumer concept (curated list),
      - a capitalized proper noun — any word past position 1 starting uppercase,
        or any word with an interior capital (camelCase brands like "iPhone").
        The very first word being capitalized is NOT evidence; every sentence
        starts that way.
    """
    words = _spoken_text(body).split()[:4]
    if not words:
        return "script body has no spoken words"
    cleaned = [_normalize_word(w) for w in words]
    for i, w in enumerate(cleaned):
        if not w:
            continue
        if any(ch.isdigit() for ch in w):
            return None  # concrete number
        if w.lower() in _KNOWN_VENDOR_NAMES:
            return None  # known brand/product
        if w.lower() in _UNIVERSAL_CONSUMER_CONCEPTS:
            return None  # universal consumer concept
        if len(w) >= 2 and any(ch.isupper() for ch in w[1:]):
            return None  # interior capital — camelCase brand / acronym
        if i >= 1 and w[0].isupper():
            return None  # capitalized proper noun past sentence start
    return (
        f"first 4 spoken words ({' '.join(words)!r}) contain no recognizable "
        f"named anchor — need a person, brand/product, concrete number, or "
        f"universal consumer concept in the opening 4 words"
    )


def _title_is_title_case(words: list[str]) -> bool:
    """Heuristic: is the title written in Title Case (most words capitalized)? If
    so, mid-title capitalization is NOT evidence of a proper noun. ShadowVerse
    writes sentence-case titles, so this is normally False. Judged over words of
    length >= 4 to ignore the short function words (of/the/to/a) that stay
    lowercase even in Title Case."""
    sig = [w for w in (_normalize_word(x) for x in words) if len(w) >= 4]
    if len(sig) < 3:
        return False
    capped = sum(1 for w in sig if w[0].isupper())
    return capped / len(sig) >= 0.8


def title_anchor_violation(title: str) -> str | None:
    """PU-3T first-3-words TITLE anchor gate. None = OK, str = feedback.

    The de-facto thumbnail: ShadowVerse uploads no custom thumbnails, so the
    title text is the cold-feed stop signal, and every video that has ever
    cleared the breakout ceiling carried a recognizable anchor in its title. The
    first 3 title words must contain at least one of:
      - a concrete number (any digit),
      - a known vendor/brand/household-name person (curated PU-3 list),
      - an interior-capital token (camelCase brand / acronym — iPhone, GPT-5, FBI),
      - a universal consumer concept, INCLUDING bare "AI" (operator decision
        2026-06-24: "AI" is an accepted anchor for this AI-focused channel),
      - a capitalized proper noun PAST the first word (Karpathy, Figma, Nobel) in
        a sentence-case title — names the curated lexicon doesn't list.

    Like the spoken-body gate (``anchor_gate_violation``), "AI" counts as an
    anchor. The title gate's only twist is that a title's always-capitalized
    first word is not proof of a proper noun, so capitalization is trusted only
    for words 2-3 of a sentence-case title (a Title-Cased title capitalizes
    everything, so it carries no signal — judged by ``_title_is_title_case``).
    ADMIT-biased: the lexicons + the cap rule only ever pass a title. What still
    fails: a no-anchor opener with no brand, person, number, vivid concept, or
    "AI" in the first 3 words (dev-infra like "uv replaced pip"; abstract like
    "We're more patient than...").
    """
    if not title or not title.strip():
        return "title is empty"
    all_words = title.split()
    trust_caps = not _title_is_title_case(all_words)
    words = all_words[:3]
    for i, raw in enumerate(words):
        w = _normalize_word(raw)
        if not w:
            continue
        lw = w.lower()
        if any(ch.isdigit() for ch in w):
            return None  # concrete number
        if lw in _KNOWN_VENDOR_NAMES:
            return None  # known brand / product / household-name person
        if len(w) >= 2 and any(ch.isupper() for ch in w[1:]):
            return None  # interior capital — camelCase brand / acronym
        if lw in _UNIVERSAL_CONSUMER_CONCEPTS:
            return None  # vivid consumer object (lawsuit, robot, billion...)
        if trust_caps and i >= 1 and w[0].isupper():
            return None  # proper noun past the always-capitalized first word
    return (
        f"title's first 3 words ({' '.join(words)!r}) carry no recognizable "
        f"anchor — lead with a known brand/person, a concrete number, or a vivid "
        f"consumer object in the first 3 words (a bare 'This AI'/'Your AI'/'A new "
        f"AI' opener fails; the title is the de-facto thumbnail)"
    )


def modal_opener_violation(sentence: str) -> str | None:
    """PU-4 (R2 H2) modal-framing ban. None = OK, str = feedback.

    Sentence 1 must state a dated factual event; modal/hypothetical framings
    ("could", "might", "imagine if", "what if") are banned in it.
    """
    m = _MODAL_OPENER_RE.search(sentence)
    if m is None:
        return None
    return (
        f"modal/hypothetical framing {m.group(0)!r} in the first sentence "
        f"({sentence!r}) — sentence 1 must state a dated factual event, not a "
        f"could/might/imagine-if/what-if hypothetical"
    )


def _stage15_attempts_path(config: dict, topic_id: str) -> Path | None:
    manual_io_dir = (config.get("llm") or {}).get("manual_io_dir")
    if not manual_io_dir:
        return None
    return Path(manual_io_dir) / topic_id / "stage15_regen_attempts.txt"


def _bump_stage15_attempts(config: dict, topic_id: str) -> int:
    """Increment + persist the per-topic regenerate counter. Fail-soft: the
    HALT is the load-bearing part; the counter is bookkeeping, so I/O issues
    log a warning and report attempt 1 rather than masking the gate."""
    path = _stage15_attempts_path(config, topic_id)
    if path is None:
        log.warning(
            "stage 1.5 regen counter: llm.manual_io_dir unset — cannot persist "
            "attempt count for %s; reporting attempt 1",
            topic_id,
        )
        return 1
    try:
        previous = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
    except (OSError, ValueError) as exc:
        log.warning("stage 1.5 regen counter unreadable at %s (%s); resetting", path, exc)
        previous = 0
    attempt = previous + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{attempt}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("stage 1.5 regen counter not persisted at %s (%s)", path, exc)
    return attempt


def _clear_stage15_attempts(config: dict, topic_id: str) -> None:
    """Remove the regen counter once the hard rules pass, so a future topic
    re-run starts its attempt budget fresh."""
    path = _stage15_attempts_path(config, topic_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("stage 1.5 regen counter not cleared at %s (%s)", path, exc)


# Manager C3 (2026-06-09): capped regenerate-with-feedback — after this many
# tracked attempts the halt message escalates to the operator. Never auto-kill.
STAGE15_MAX_REGEN_ATTEMPTS = 2


def _enforce_script_hard_rules(script: ScriptDraft, config: dict) -> None:
    """Run the config-flagged Stage 1.5 hard rules; raise ScriptRuleViolation
    (a halt) with rule-specific feedback when any are broken.

    Flags (all default OFF so legacy configs keep legacy behavior; production
    config.yaml + template set them true — one-flip rollback each):
      - script_quality.anchor_gate_enabled   (PU-3, R2 H1)
      - script_quality.modal_ban_enabled     (PU-4, R2 H2)
      - script_quality.word_count_halt_enabled (PU-11, R2 H4 — bounds come from
        the existing word_count_min/word_count_max keys; set word_count_max
        back to 200 to loosen)
    """
    qcfg = config.get("script_quality") or {}
    violations: list[str] = []

    if bool(qcfg.get("anchor_gate_enabled", False)):
        v = anchor_gate_violation(script.body)
        if v:
            violations.append(f"anchor gate (PU-3, R2 H1): {v}")

    if bool(qcfg.get("modal_ban_enabled", False)):
        v = modal_opener_violation(_first_spoken_sentence(script.body))
        if v:
            violations.append(f"modal ban (PU-4, R2 H2): {v}")

    if bool(qcfg.get("word_count_halt_enabled", False)):
        wc_min = int(qcfg.get("word_count_min", 80))
        wc_max = int(qcfg.get("word_count_max", 200))
        if not (wc_min <= script.word_count <= wc_max):
            violations.append(
                f"word count (PU-11, R2 H4): body is {script.word_count} spoken "
                f"words, outside [{wc_min}, {wc_max}] — cut the script to fit "
                f"(or raise script_quality.word_count_max back to 200 to loosen)"
            )

    if violations:
        attempt = _bump_stage15_attempts(config, script.topic_id)
        log.error(
            "Stage 1.5 hard-rule violation(s) on %s (attempt %d/%d): %s",
            script.topic_id, attempt, STAGE15_MAX_REGEN_ATTEMPTS,
            "; ".join(violations),
        )
        raise ScriptRuleViolation(
            topic_id=script.topic_id,
            violations=violations,
            attempt=attempt,
            max_attempts=STAGE15_MAX_REGEN_ATTEMPTS,
        )

    _clear_stage15_attempts(config, script.topic_id)


def evaluate_script_quality(script: ScriptDraft, config: dict) -> ScriptDraft:
    """Stage 1.5: read the self-scored quality fields and gate on them.

    Behavior controlled by config.script_quality:
      - min_score (float, default 0.50): the weighted-total threshold for "publish"
      - enforce_min_score (bool, default false): when true, halt below threshold;
        when false, log a warning but pass through

    Scoring denominator (H-3, 2026-06-19):
      - require_full_dimensions=true (production): the mean is over ALL canonical
        dimensions with a missing one counting as 0.0, so an LLM can't inflate
        its score by omitting weak dimensions and a response with no
        QUALITY_SCORES section scores 0.0 -> halts under enforce.
      - require_full_dimensions=false (legacy/code default): tolerant — a missing
        section or no recognized dimensions passes through with a warning; the
        mean is over only the present dimensions.

    Returns the same ScriptDraft unchanged on success — this is a pure gate, not a
    transform. The scores are already in script.quality_scores from the parser.
    """
    qcfg = config.get("script_quality") or {}
    min_score = float(qcfg.get("min_score", 0.50))
    enforce = bool(qcfg.get("enforce_min_score", False))

    # WORKFLOW_AUDIT_2026-05-16 H2 (mirror): same gate-bypass class as
    # prepublish_qa.enabled=false. /start -auto's gate-3 auto-approve is
    # conditional on Stage 1.5 passing, but "passing" by enforce_min_score=false
    # was a silent pass-through. Require `allow_disable_in_production=true`
    # to allow the unenforced mode. The default (no key set, enforce=False)
    # raises so the production config can't drift silently.
    if not enforce:
        if bool(qcfg.get("allow_disable_in_production", False)) is True:
            log.warning(
                "script_quality.enforce_min_score=false AND "
                "allow_disable_in_production=true — Stage 1.5 will pass-through "
                "below-threshold scores (unit-test / explicit-bypass path)"
            )
        else:
            log.error(
                "script_quality.enforce_min_score=false without script_quality."
                "allow_disable_in_production=true — refusing to run Stage 1.5 "
                "in unenforced mode. Set both flags explicitly to bypass the "
                "gate, or flip enforce_min_score back to true."
            )
            raise RuntimeError(
                "Stage 1.5 (script_quality) cannot run unenforced: "
                "script_quality.enforce_min_score=false requires "
                "script_quality.allow_disable_in_production=true. "
                "This guard exists because /start -auto's gate-3 auto-approve "
                "is conditional on Stage 1.5 passing."
            )

    # Hard rules first (2026-06-09 review: PU-3 anchor gate, PU-4 modal ban,
    # PU-11 word-count halt). Independent of the self-scored dimensions — they
    # run even when QUALITY_SCORES is absent. May raise ScriptRuleViolation.
    _enforce_script_hard_rules(script, config)

    # H-3 (2026-06-19 review): never take the mean over only the dimensions the
    # LLM chose to emit — that lets it inflate its score by dropping its weak
    # dimensions (scorer-controlled denominator), and a response with NO
    # QUALITY_SCORES section silently passed. When require_full_dimensions is
    # set, score over the FULL canonical set with a missing dimension counting
    # as 0.0, so omission can only HURT the mean and an absent section scores
    # 0.0 -> halts under enforce. Config-flagged for one-flip rollback; the code
    # default False preserves the legacy tolerant gate (keeps old configs/tests).
    require_full = bool(qcfg.get("require_full_dimensions", False))
    present = [d for d in SCRIPT_QUALITY_DIMENSIONS if d in script.quality_scores]

    if require_full:
        missing = [d for d in SCRIPT_QUALITY_DIMENSIONS if d not in script.quality_scores]
        if missing:
            log.warning(
                "script quality gate: %s missing dimension(s) %s — each scored 0.0 "
                "(require_full_dimensions). Re-issue script_RESPONSE.txt with all of: %s",
                script.topic_id, ", ".join(missing), ", ".join(SCRIPT_QUALITY_DIMENSIONS),
            )
        total = sum(
            script.quality_scores.get(d, 0.0) for d in SCRIPT_QUALITY_DIMENSIONS
        ) / len(SCRIPT_QUALITY_DIMENSIONS)
    else:
        # Legacy tolerant gate: a missing section / no recognized dims pass through.
        if not script.quality_scores:
            log.warning(
                "script quality gate: no QUALITY_SCORES section in response for %s — "
                "gate is a no-op. Re-issue script_RESPONSE.txt with the section to enable scoring.",
                script.topic_id,
            )
            return script
        if not present:
            log.warning(
                "script quality gate: QUALITY_SCORES section had no recognized dimensions for %s. "
                "Expected any of: %s",
                script.topic_id, ", ".join(SCRIPT_QUALITY_DIMENSIONS),
            )
            return script
        total = sum(script.quality_scores[d] for d in present) / len(present)

    log.info(
        "script quality: %s weighted_total=%.3f over %d/%d scored dimensions "
        "(min=%.2f, enforce=%s, require_full=%s)",
        script.topic_id, total, len(present), len(SCRIPT_QUALITY_DIMENSIONS),
        min_score, enforce, require_full,
    )
    if script.quality_rationale:
        log.info("script quality rationale: %s", script.quality_rationale)

    if total < min_score:
        if enforce:
            raise QualityCheckFailed(
                topic_id=script.topic_id,
                scores=script.quality_scores,
                total=total,
                threshold=min_score,
            )
        log.warning(
            "script %s scored %.3f, below threshold %.2f — pass-through because "
            "script_quality.enforce_min_score is false",
            script.topic_id, total, min_score,
        )
    else:
        # Canonical OK line — /start -auto greps the per-run log for this
        # exact prefix before dropping <topic_id>_master_QA_APPROVED.marker.
        log.info(
            "Stage 1.5 OK on %s (weighted_total=%.3f >= %.2f)",
            script.topic_id, total, min_score,
        )

    # Fail-soft learning telemetry (self-improving loop): persist the parsed
    # per-dimension quality scores so the loop can correlate craft with reach.
    # A telemetry error must NEVER break Stage 1.5 — log and swallow.
    try:
        from learning.telemetry import append_script_quality

        channel_root = (config.get("paths") or {}).get("channel_root")
        if channel_root:
            append_script_quality(
                channel_root,
                topic_id=script.topic_id,
                dims=dict(script.quality_scores),
                weighted_total=total,
            )
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the gate
        log.warning("learning telemetry append failed for %s: %s", script.topic_id, exc)

    return script


_TABLE_ROW_RE = re.compile(r"^\s*\|(.+?)\|\s*$", re.MULTILINE)
_SEPARATOR_CELL_RE = re.compile(r"^[\s:-]+$")
_STATUS_NORMALIZE = {
    "VERIFIED": "VERIFIED",
    "UNCLEAR": "UNCLEAR",
    "LIKELY WRONG": "LIKELY_WRONG",
    "LIKELY_WRONG": "LIKELY_WRONG",
    "UNVERIFIABLE": "UNVERIFIABLE",
}
_HEADER_INDEX_TOKENS = {"#", "no", "no.", "num", "number", "idx", "index"}

# Canonical fact-check tool-source values (audit M3, WORKFLOW_AUDIT_2026-05-16).
# `tavily` is the preferred path per `feedback_tavily_mcp.md`; `web` is the
# WebSearch/WebFetch fallback (logged WARNING so silent override of the durable
# rule is visible); `none` is internal-knowledge-only.
_CANONICAL_TOOL_VALUES = {"tavily", "web", "none"}
_TOOL_DEFAULT_FOR_LEGACY = "unknown"


def _classify_header_cell(cell: str) -> str | None:
    """Classify a fact-check table header cell to one of {claim, status, url, quote, fix, tool}.

    Returns the col_map key or None for unrecognized cells (which are silently dropped —
    e.g. a leading "#" index column). Tolerant to bold markdown (`**Claim**`),
    parenthesized notes (`Claim (verbatim)`), "if X" suffixes (`Fix if LIKELY_WRONG`),
    and "/X" suffixes (`Source quote / note`).

    The "tool" key is the 6th column added by audit M3 (WORKFLOW_AUDIT_2026-05-16)
    so the fact-check parser can surface tavily-mcp-vs-WebSearch tool-mix.
    """
    c = cell.lower().strip("*").strip()
    c = re.sub(r"\s*\([^)]*\)", "", c).strip()  # strip "(verbatim)"
    c = re.sub(r"\s+if\s+.*$", "", c).strip()    # strip "if LIKELY_WRONG"
    c = c.split("/")[0].strip()                  # strip "/note"
    if not c or c in _HEADER_INDEX_TOKENS:
        return None
    if "claim" in c:
        return "claim"
    if c == "status":
        return "status"
    if "quote" in c or "wording" in c:
        return "quote"
    if "fix" in c or "suggest" in c:
        return "fix"
    if c == "url" or "source url" in c or "link" in c or c == "source":
        return "url"
    if c == "tool" or "tool used" in c or "tool source" in c:
        return "tool"
    return None


def _parse_factcheck_response(response: str, topic_id: str) -> FactCheckReport:
    """Parse the fact-checker's markdown table into a FactCheckReport.

    Expected format (per prompts/05_fact_check.md, 6 columns since audit M3
    WORKFLOW_AUDIT_2026-05-16; 5-column legacy still accepted):
        | Claim | Status | Source URL | Source quote | Suggested fix | Tool |
        |-------|--------|------------|--------------|----------------|------|
        | "..." | VERIFIED | https://... | "..." | (n/a) | tavily |
        ...

    Tolerant of:
      - Extra columns (e.g. a leading "#" index column) — column positions
        are looked up from the header row by name when a header is present
      - Markdown bold around header cells (`**Claim**`)
      - Common header aliases ("Claim (verbatim)", "Source", "Fix if LIKELY_WRONG",
        "Source quote / note", "Tool used", "Tool source")
      - Missing Tool column (legacy 5-column responses): tool_used defaults to
        `"unknown"` and the parser logs a WARNING per row missing the value

    Header detection: a row immediately preceding a separator row is the header.
    Markdown table syntax mandates this structure. Falls back to a strict 5-column
    positional layout if no header is detected (legacy behavior preserved for
    edge cases).

    Emits a summary `fact-check tool mix: tavily=N web=M none=P unknown=Q`
    log line after parsing. If `web` count is non-zero, also emits a WARNING
    reminding about the tavily-mcp durable rule (`feedback_tavily_mcp.md`).
    """
    raw_rows = _TABLE_ROW_RE.findall(response)

    def _cells(row_text: str) -> list[str]:
        return [c.strip() for c in row_text.split("|")]

    def _is_separator(cells: list[str]) -> bool:
        return bool(cells) and all(_SEPARATOR_CELL_RE.fullmatch(c) for c in cells if c)

    header_idx: int | None = None
    for i in range(len(raw_rows) - 1):
        if _is_separator(_cells(raw_rows[i + 1])):
            header_idx = i
            break

    # Default to 5-column positional layout (legacy behavior). The Tool column
    # has no positional default — its absence is the signal to fall back to
    # "unknown" with a WARNING. -1 is the sentinel for "not present".
    col_map: dict[str, int] = {
        "claim": 0,
        "status": 1,
        "url": 2,
        "quote": 3,
        "fix": 4,
        "tool": -1,
    }
    if header_idx is not None:
        header_cells = _cells(raw_rows[header_idx])
        mapped_from_header: set[str] = set()
        for j, cell in enumerate(header_cells):
            key = _classify_header_cell(cell)
            if key is not None:
                col_map[key] = j
                mapped_from_header.add(key)

        # WORKFLOW_AUDIT_2026-05-31 L2: a header row WAS detected, but only keys
        # that classified got their column overwritten — any required column
        # (claim/status/url) that failed to classify SILENTLY retains its
        # positional default, which under a reordered table points at the WRONG
        # cell. Fail loud instead of parsing the wrong column. ADDITIVE: this runs
        # only when a header is present (header_idx is not None) and BEFORE the
        # row loop, so the no-header positional path and the downstream H2/status/
        # tool raises are untouched. Tool is optional → excluded from the set.
        required_cols = ("claim", "status", "url")
        unmapped = [c for c in required_cols if c not in mapped_from_header]
        if unmapped:
            raise ValueError(
                "Fact-check table header detected but could not map required "
                f"column(s): {{{', '.join(unmapped)}}}. Saw header cells: "
                f"{header_cells}. Rename them to Claim / Status / Source URL "
                "(aliases allowed) and re-run factcheck_RESPONSE.txt."
            )

    tool_column_present = col_map["tool"] >= 0
    if not tool_column_present:
        # Legacy 5-column input. Emit a single header-level WARNING (not
        # per-row) so the noise stays readable but the operator gets the
        # signal that the file pre-dates the M3 fix.
        log.warning(
            "fact-check response missing the 'Tool' column (audit M3): "
            "defaulting tool_used='unknown' for all rows. "
            "Producer prompt expects 6 columns since 2026-05-16; legacy "
            "responses still parse but lose tavily-vs-WebSearch visibility."
        )

    claims: list[FactClaim] = []
    for i, row_text in enumerate(raw_rows):
        if i == header_idx:
            continue
        cells = _cells(row_text)
        if not cells or all(c == "" for c in cells):
            continue
        if _is_separator(cells):
            continue
        # Back-compat: skip a literal "Claim" header when no separator was detected.
        if cells[0].lower() in {"claim", "claim text"}:
            continue

        def _get(key: str) -> str:
            j = col_map[key]
            return cells[j] if 0 <= j < len(cells) else ""

        claim_text = _get("claim")
        status_raw = _get("status")
        source_url = _get("url")
        source_quote = _get("quote")
        suggested_fix = _get("fix")
        tool_raw = _get("tool") if tool_column_present else ""

        # WORKFLOW_AUDIT_2026-05-31 H2: reject a row whose Claim cell is empty.
        # The full-empty-row (836) and literal-"claim"-header (841) skips above
        # already short-circuit legitimately-blank rows, so reaching here with an
        # empty claim means a phantom row (e.g. `|  | VERIFIED | url | "q" | | tavily |`)
        # that would otherwise append a FactClaim with claim_text="" — inflating
        # unresolved_count and polluting the gate-2 report. Mirror the status/Tool
        # strict-or-raise pattern. Normalize the same way the FactClaim does so a
        # whitespace- or quote-only cell is also caught.
        if not claim_text.strip().strip('"').strip():
            raise ValueError(
                f"Empty Claim cell for a fact-check row (status={status_raw!r}). "
                f"Every row MUST name the claim verbatim. Edit "
                f"factcheck_RESPONSE.txt and re-run."
            )

        status_key = re.sub(r"[\s_]+", " ", status_raw.upper()).strip()
        status = _STATUS_NORMALIZE.get(status_key)
        if not status:
            # WORKFLOW_AUDIT_2026-05-16 H1: do NOT silently downgrade. A
            # non-canonical status (`VERIFIED (paraphrase)`, `LIKELY_WRONG
            # (minor)`, `UNCLEAR — needs source`, leading bullet `- VERIFIED`,
            # leading index `1. VERIFIED`) is a producer-prompt violation —
            # raising forces a re-write of factcheck_RESPONSE.txt so auto-
            # resolve mode never silently classifies a `VERIFIED (paraphrase)`
            # as UNVERIFIABLE and ships the claim untouched.
            raise ValueError(
                f"Non-canonical fact-check status {status_raw!r} for claim "
                f"{claim_text[:80]!r}. Status MUST be exactly one of: "
                f"VERIFIED, UNCLEAR, LIKELY WRONG, UNVERIFIABLE — no "
                f"parentheticals, no qualifiers, no inline notes, no leading "
                f"bullet or index column. Put nuance in Source quote / "
                f"Suggested fix. Edit factcheck_RESPONSE.txt and re-run the "
                f"fact-check stage."
            )

        # Tool column (audit M3). Validate strictly when present; default to
        # "unknown" when the column was absent in the header (legacy 5-col).
        if tool_column_present:
            tool_value = tool_raw.lower().strip().strip("`").strip()
            if not tool_value:
                # Cell present in column but empty — treat as malformed.
                raise ValueError(
                    f"Empty Tool cell for claim {claim_text[:80]!r}. Tool MUST "
                    f"be exactly one of: tavily, web, none. Edit "
                    f"factcheck_RESPONSE.txt and re-run."
                )
            if tool_value not in _CANONICAL_TOOL_VALUES:
                # Mirror H1 strict-or-raise pattern for the Tool column.
                raise ValueError(
                    f"Non-canonical fact-check Tool {tool_raw!r} for claim "
                    f"{claim_text[:80]!r}. Tool MUST be exactly one of: "
                    f"tavily, web, none. Edit factcheck_RESPONSE.txt and re-run."
                )
        else:
            tool_value = _TOOL_DEFAULT_FOR_LEGACY

        claims.append(FactClaim(
            claim_text=claim_text.strip().strip('"').strip(),
            status=status,
            source_url=source_url or None,
            source_quote=(source_quote.strip().strip('"') or None),
            suggested_fix=(suggested_fix or None) if status == "LIKELY_WRONG" else None,
            tool_used=tool_value,
        ))

    if not claims:
        raise ValueError(
            "No fact-check claims parsed. Expected a markdown table with rows of the form "
            "'| claim | STATUS | URL | source quote | suggested fix | tool |'. "
            "Edit factcheck_RESPONSE.txt and re-run."
        )

    # Audit M3: emit a mix-summary so tavily-mcp-vs-WebSearch fallback is
    # observable in the per-run log. WARNING when web > 0 (silent override of
    # the durable rule in feedback_tavily_mcp.md).
    tally: dict[str, int] = {"tavily": 0, "web": 0, "none": 0, "unknown": 0}
    for c in claims:
        tally[c.tool_used] = tally.get(c.tool_used, 0) + 1
    log.info(
        "fact-check tool mix: tavily=%d web=%d none=%d unknown=%d",
        tally["tavily"],
        tally["web"],
        tally["none"],
        tally["unknown"],
    )
    if tally["web"] > 0:
        log.warning(
            "fact-check fell back to WebSearch/WebFetch for %d of %d claims. "
            "Durable rule (feedback_tavily_mcp.md) prefers tavily-mcp; check "
            "whether the tavily-mcp connection dropped this cycle.",
            tally["web"],
            len(claims),
        )

    return FactCheckReport(topic_id=topic_id, claims=claims)


def fact_check_script(script: ScriptDraft, config: dict) -> FactCheckReport:
    """Stage 2: a second model verifies every factual claim against authoritative sources.

    Dispatches on `config.fact_check.provider`:
      - "manual" / "claude_with_websearch": file-IO halt pattern. Prompt lands at
        <manual_io_dir>/<topic_id>/factcheck_PROMPT.txt; on resume, response is parsed
        into a FactCheckReport.
      - "perplexity": not yet implemented — flip provider to manual / claude_with_websearch.
    """
    provider = config["fact_check"]["provider"]
    if provider not in {"manual", "claude_with_websearch"}:
        raise NotImplementedError(
            f"fact_check.provider={provider!r} is not implemented today. "
            f"Set it to 'manual' or 'claude_with_websearch' in config.yaml."
        )

    # Prompt is config-selectable so the general-tech track can point at the
    # stricter crazy-story variant (05_fact_check_general_tech) via its isolated
    # config; ai-vendor defaults to the standard 6-column prompt (unchanged).
    prompt_name = config["fact_check"].get("prompt", "05_fact_check")
    template = load_prompt(prompt_name, config)

    # Compose the script content the fact-checker sees: all 3 hook variants + the body
    # (the operator hasn't picked a hook yet, so all candidates are fair game for verification).
    hooks_block = "\n".join(
        f"HOOK_{chr(65 + i)}: {h}" for i, h in enumerate(script.hook_variants)
    )
    script_text = f"{hooks_block}\n\n{script.body}"
    prompt = template.replace("{SCRIPT}", script_text)

    response = _await_manual_response(prompt, "factcheck", script.topic_id, config)
    return _parse_factcheck_response(response, script.topic_id)


_REPLACE_PATTERN_RE = re.compile(
    r"[Rr]eplace\s+[\"'](.+?)[\"']\s+with\s+[\"'](.+?)[\"']",
    re.DOTALL,
)


def _try_apply_fix(body: str, claim: FactClaim) -> tuple[str, str]:
    """Attempt to apply a claim's suggested_fix to the script body.

    Recognized format: `Replace "X" with "Y"` (single OR double quotes; multi-line OK).
    Returns (possibly_updated_body, status_note). Status note is one of:
      - "applied"           : substitution succeeded
      - "no_match"          : the X text was not found verbatim in the body
      - "unparseable"       : suggested_fix doesn't follow the Replace X with Y form
      - "skipped_no_fix"    : claim has no suggested_fix

    The status check is intentionally NOT gated to LIKELY_WRONG — auto-resolve mode
    (config.fact_check.auto_resolve_gate_2 = true) calls this on UNCLEAR claims as
    well. Callers decide which statuses to attempt; this function only acts when a
    suggested_fix is present and parseable.
    """
    if not claim.suggested_fix:
        return body, "skipped_no_fix"

    m = _REPLACE_PATTERN_RE.search(claim.suggested_fix)
    if not m:
        return body, "unparseable"

    old, new = m.group(1), m.group(2)
    if old not in body:
        return body, "no_match"

    return body.replace(old, new, 1), "applied"


def _write_factcheck_review(
    script: ScriptDraft,
    report: FactCheckReport,
    fixes_applied: list[tuple[FactClaim, str]],
    review_path: Path,
) -> None:
    """Write a human-readable review doc to factcheck_REVIEW.md.

    Lists every claim by status, surfaces the LIKELY_WRONG / UNCLEAR rows that need
    operator judgment, and shows which fixes were auto-applied to script_FINAL.txt.
    """
    by_status: dict[str, list[FactClaim]] = {}
    for c in report.claims:
        by_status.setdefault(c.status, []).append(c)

    lines: list[str] = []
    lines.append(f"# Fact-check review: {script.topic_id}")
    lines.append("")
    lines.append(
        f"**Summary:** {len(report.claims)} claims | "
        + " | ".join(f"{n} {s}" for s, n in sorted(((k, len(v)) for k, v in by_status.items())))
        + f" | unresolved: {report.unresolved_count}"
    )
    lines.append("")
    lines.append("## Sign-off instructions")
    lines.append("")
    lines.append("1. Read this file end-to-end.")
    lines.append("2. Open `script_FINAL.txt` (already pre-populated with proposed fixes).")
    lines.append("3. Edit `script_FINAL.txt` if you want different wording, or leave as-is to accept.")
    lines.append("4. Re-run the pipeline. The fact-check gate will pass once `script_FINAL.txt` exists.")
    lines.append("")

    if by_status.get("LIKELY_WRONG"):
        lines.append("## ❌ LIKELY_WRONG (auto-fix attempted)")
        lines.append("")
        applied_map = {id(c): note for c, note in fixes_applied}
        for c in by_status["LIKELY_WRONG"]:
            note = applied_map.get(id(c), "not attempted")
            badge = {
                "applied": "✅ AUTO-APPLIED to script_FINAL.txt",
                "no_match": "⚠ FIX NOT APPLIED (text not found verbatim — review manually)",
                "unparseable": "⚠ FIX NOT APPLIED (couldn't parse suggested fix — review manually)",
            }.get(note, f"⚠ {note}")
            lines.append(f"### Claim: {c.claim_text}")
            lines.append(f"- **Source:** {c.source_url or '(none)'}")
            if c.source_quote:
                lines.append(f"- **What the source says:** {c.source_quote}")
            if c.suggested_fix:
                lines.append(f"- **Suggested fix:** {c.suggested_fix}")
            lines.append(f"- **Status:** {badge}")
            lines.append("")

    if by_status.get("UNCLEAR"):
        lines.append("## ⚠ UNCLEAR (operator judgment required)")
        lines.append("")
        lines.append(
            "_These rows are not auto-fixed — they need a human call. Edit `script_FINAL.txt` "
            "to address each, or accept the original wording._"
        )
        lines.append("")
        for c in by_status["UNCLEAR"]:
            lines.append(f"### Claim: {c.claim_text}")
            lines.append(f"- **Source:** {c.source_url or '(none)'}")
            if c.source_quote:
                lines.append(f"- **What the source says:** {c.source_quote}")
            if c.suggested_fix:
                lines.append(f"- **Suggested wording:** {c.suggested_fix}")
            lines.append("")

    if by_status.get("UNVERIFIABLE"):
        lines.append("## ❓ UNVERIFIABLE")
        lines.append("")
        for c in by_status["UNVERIFIABLE"]:
            lines.append(f"- {c.claim_text}")
            if c.source_quote:
                lines.append(f"  - {c.source_quote}")
        lines.append("")

    if by_status.get("VERIFIED"):
        lines.append(f"## ✅ VERIFIED ({len(by_status['VERIFIED'])} claims, no action)")
        lines.append("")
        for c in by_status["VERIFIED"]:
            lines.append(f"- {c.claim_text}")
            if c.source_url:
                lines.append(f"  - {c.source_url}")
        lines.append("")

    review_path.write_text("\n".join(lines), encoding="utf-8")


def _build_diff(original: str, updated: str) -> str:
    """Build a minimal unified diff between original and updated body strings.

    Inline rather than `difflib.unified_diff` because (a) we want it in the review doc
    in markdown-fenced form and (b) the two strings are usually small.
    """
    if original == updated:
        return "(no changes applied)\n"
    import difflib
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=False),
        updated.splitlines(keepends=False),
        fromfile="script_RESPONSE.txt (body)",
        tofile="script_FINAL.txt",
        lineterm="",
        n=2,
    )
    return "\n".join(diff_lines) + "\n"


_VERIFY_TAG_RE = re.compile(r"\s*\[VERIFY:[^\]]*\]\s*", re.IGNORECASE)


def _strip_verify_tags(body: str) -> tuple[str, list[str]]:
    """Remove `[VERIFY: ...]` tags from a script body and return (cleaned_body, removed_tags).

    Used by auto-gate-2: when the operator is delegating gate 2 review, any [VERIFY] tag
    that survived fact-check would otherwise be spoken by the TTS verbatim. Removing them
    turns "Cursor 0.46 [VERIFY: confirm version]" into "Cursor 0.46". Operator catches
    semantic errors at gate 3.
    """
    removed: list[str] = []
    def _capture(m: re.Match) -> str:
        removed.append(m.group(0).strip())
        return " "
    cleaned = _VERIFY_TAG_RE.sub(_capture, body)
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned, removed


def _write_auto_resolution_audit(
    script: ScriptDraft,
    report: FactCheckReport,
    fixes_log: list[tuple[FactClaim, str]],
    stripped_tags: list[str],
    audit_path: Path,
) -> None:
    """Write an audit doc the operator reads at gate 3 to spot-check auto-resolution.

    Lists every fix that was attempted (applied / no_match / unparseable / skipped),
    every [VERIFY] tag silently removed, and every UNVERIFIABLE row left as-is.
    """
    lines: list[str] = []
    lines.append(f"# Auto-resolution audit: {script.topic_id}")
    lines.append("")
    lines.append(
        "Gate 2 was auto-resolved (config.fact_check.auto_resolve_gate_2 = true). This file "
        "records every change. Spot-check at gate 3 — if a line below sounds wrong in the "
        "rendered master, reject and tell Claude what to fix upstream."
    )
    lines.append("")

    applied_map = {id(c): note for c, note in fixes_log}

    by_status: dict[str, list[FactClaim]] = {}
    for c in report.claims:
        by_status.setdefault(c.status, []).append(c)

    if by_status.get("LIKELY_WRONG"):
        lines.append("## Auto-applied fixes (LIKELY_WRONG)")
        lines.append("")
        for c in by_status["LIKELY_WRONG"]:
            note = applied_map.get(id(c), "not_attempted")
            lines.append(f"- **{c.claim_text}** — {note}")
            if c.suggested_fix:
                lines.append(f"  - fix: {c.suggested_fix}")
            if c.source_url:
                lines.append(f"  - source: {c.source_url}")
        lines.append("")

    if by_status.get("UNCLEAR"):
        lines.append("## Auto-attempted fixes (UNCLEAR)")
        lines.append("")
        for c in by_status["UNCLEAR"]:
            note = applied_map.get(id(c), "not_attempted")
            lines.append(f"- **{c.claim_text}** — {note}")
            if c.suggested_fix:
                lines.append(f"  - fix: {c.suggested_fix}")
            if c.source_url:
                lines.append(f"  - source: {c.source_url}")
        lines.append("")

    if by_status.get("UNVERIFIABLE"):
        lines.append("## Left as-is (UNVERIFIABLE)")
        lines.append("")
        lines.append(
            "_These are personal observations or claims with no external source. Listen "
            "for them in the master — if a cited observation feels off-brand or wrong, "
            "reject at gate 3._"
        )
        lines.append("")
        for c in by_status["UNVERIFIABLE"]:
            lines.append(f"- {c.claim_text}")
        lines.append("")

    if stripped_tags:
        lines.append("## [VERIFY] tags silently removed")
        lines.append("")
        for tag in stripped_tags:
            lines.append(f"- `{tag}`")
        lines.append("")

    if by_status.get("VERIFIED"):
        lines.append(f"## Verified ({len(by_status['VERIFIED'])} claims, no action)")
        lines.append("")
        for c in by_status["VERIFIED"]:
            lines.append(f"- {c.claim_text}")
        lines.append("")

    audit_path.write_text("\n".join(lines), encoding="utf-8")


def _scan_script_for_artifacts_or_halt(
    script_path: Path,
    topic_id: str | None = None,
    *,
    style_guide_path: str | Path | None = None,
) -> None:
    """Pre-TTS, once-per-topic script-hygiene gate: halt if ``script_FINAL.txt``
    carries template artifacts (#14), sourcing-hygiene issues (#15), or a
    pre-render lint failure (#16).

    Three checks, all run against the SCRIPT body before it reaches TTS (NOT
    per-variant — that is Stage 11 / `check_variant`):

      * **#14** (Sprint 5 Layer-2): template / internal-name / stage-instruction
        artifacts (`_12_002` prevention).
      * **#15** (PU-7b, weekly-review 2026-05-29): residual ``[VERIFY: …]``
        placeholder tags or anonymous "a Reddit/X user" citations
        (`_08_001` prevention — R2 §4 + §7).
      * **#16** (pre-render lint): unresolved placeholder tokens
        (``[VERIFY`` / ``[NEEDS`` / ``[TODO`` / ``[FIXME``) or a retired/forbidden
        CTA. The banned-CTA list is pulled from the style guide's
        retired/forbidden sections (`style_guide_path`) so it stays in sync.
        Forward-looking guard for `_08_001` ([VERIFY] spoken aloud) and `_11_002`
        (the retired ``Comment "deploy" and I will send you the link.`` bait).

    All checks aggregate into a single `PipelineQAFailed` so the operator sees
    every hygiene failure for the script in one halt instead of fixing one,
    re-running, then tripping the next.

    Imports `tools.prepublish_qa` lazily because that module is itself
    `__init__`-importable but has a heavier transitive-import footprint
    (ffprobe helpers, etc.) and pipeline.py is loaded by every CLI entry.

    Args:
        script_path: the FINAL script to scan.
        topic_id: surfaced in the halt header.
        style_guide_path: style guide whose retired/forbidden CTA phrases
            augment the #16 baseline. When None, #16 falls back to its module
            default path (and to the built-in baseline if that is unreadable).

    Raises:
        PipelineQAFailed: any #14 / #15 / #16 match, missing file, or unreadable
            script. Inherits from `PipelineHalted` so the existing halt /
            resume plumbing treats this like any other sacred-gate stop.
    """
    from tools.prepublish_qa import (  # local import to avoid CLI startup cost
        PipelineQAFailed,
        check_script,
        check_script_prerender,
        check_script_sourcing,
    )

    failures: dict[int, dict[str, str]] = {}
    for result in (
        check_script(script_path),
        check_script_sourcing(script_path),
        check_script_prerender(script_path, style_guide_path=style_guide_path),
    ):
        if not result.ok:
            failures[result.check_id] = {
                "name": result.name,
                "expected": result.expected,
                "actual": result.actual,
                "message": result.message,
            }

    if failures:
        raise PipelineQAFailed(
            failures=failures,
            video_path=None,
            topic_id=topic_id,
        )


def await_fact_check_resolution(
    script: ScriptDraft, report: FactCheckReport, config: dict
) -> ScriptDraft:
    """SACRED GATE 2: handle fact-check resolution.

    Two modes:
      - **Manual gate (default).** Apply LIKELY_WRONG fixes to a proposed
        `script_FINAL.txt`, write `factcheck_REVIEW.md`, halt with HumanReviewRequired.
      - **Auto-resolve mode.** When `config.fact_check.auto_resolve_gate_2 = true`,
        apply fixes for every claim that has a suggested_fix (LIKELY_WRONG + UNCLEAR),
        strip any surviving `[VERIFY: ...]` tags from the body, write
        `factcheck_AUTO_RESOLUTION.md`, return the updated script, and continue.
        Operator's gate-3 final-QA review is the only remaining safety net in this mode.

    `config.fact_check.require_human_resolution` (sacred-gate kill-switch) must remain
    true in both modes — it's the safety guard against accidentally disabling gates 2+3
    together.

    If `script_FINAL.txt` already exists, treat it as the operator's signed-off body
    regardless of mode and use it as-is.
    """
    if not config["fact_check"]["require_human_resolution"]:
        raise RuntimeError(
            "Refusing to run: config.fact_check.require_human_resolution must be true. "
            "This is a SACRED GATE per the operating guide."
        )

    auto_resolve = bool(config["fact_check"].get("auto_resolve_gate_2", False))

    base = Path(config["llm"]["manual_io_dir"]) / script.topic_id
    base.mkdir(parents=True, exist_ok=True)
    final_path = base / "script_FINAL.txt"
    review_path = base / "factcheck_REVIEW.md"
    diff_path = base / "factcheck_DIFF.md"
    audit_path = base / "factcheck_AUTO_RESOLUTION.md"

    if final_path.exists():
        # Sprint 5 L2 (check #14): scan operator-signed script_FINAL.txt for
        # template / internal / stage artifacts BEFORE we hand the body down
        # to TTS. Prevents the `_12_002` failure mode where edge-TTS spoke
        # "SCRIPT_BODY (uses HOOK_A as the verbal opener):" out loud because
        # the artifact had survived gate-2 resolution into the signed-off file.
        _scan_script_for_artifacts_or_halt(
            final_path,
            script.topic_id,
            style_guide_path=config.get("channel", {}).get("style_guide_path"),
        )
        new_body = final_path.read_text(encoding="utf-8").strip()
        broll_cues, _ = _extract_broll(new_body)
        word_count = len(_extract_broll(new_body)[1].split())
        log.info(
            "fact-check resolved: loaded script_FINAL.txt (%d words, %d b-roll cues)",
            word_count, len(broll_cues),
        )
        return script.model_copy(update={
            "body": new_body,
            "broll_cues": broll_cues,
            "word_count": word_count,
        })

    # Build proposed final body by applying suggested_fix inline.
    # Manual mode: only LIKELY_WRONG. Auto mode: also UNCLEAR.
    #
    # Auto-resolve mode (2026-05-14 fix for the `_12_002` regression): re-derive
    # `proposed_body` from a section-aware parse of `script_RESPONSE.txt` so we
    # NEVER carry a `SCRIPT_BODY (uses HOOK_X ...)` header — or any other
    # cross-section LLM artifact — into `script_FINAL.txt`. The Sprint 5 Layer-2
    # scan (`_scan_script_for_artifacts_or_halt`, below) stays as
    # defense-in-depth. Manual mode keeps the legacy `script.body` source so
    # operator-visible diffs remain unchanged.
    if auto_resolve:
        from tools.script_response_parser import (
            extract_final_script,
            parse_response,
        )

        response_path = base / "script_RESPONSE.txt"
        parsed = parse_response(response_path.read_text(encoding="utf-8"))
        chosen = parsed.chosen_hook_letter or "A"
        proposed_body = extract_final_script(parsed, chosen=chosen)
    else:
        proposed_body = script.body
    fixes_log: list[tuple[FactClaim, str]] = []
    eligible_statuses = {"LIKELY_WRONG", "UNCLEAR"} if auto_resolve else {"LIKELY_WRONG"}
    for claim in report.claims:
        if claim.status not in eligible_statuses:
            continue
        proposed_body, note = _try_apply_fix(proposed_body, claim)
        fixes_log.append((claim, note))

    stripped_tags: list[str] = []
    if auto_resolve:
        proposed_body, stripped_tags = _strip_verify_tags(proposed_body)

    final_path.write_text(proposed_body, encoding="utf-8")
    _write_factcheck_review(script, report, fixes_log, review_path)
    diff_path.write_text(
        f"# Diff applied to script_FINAL.txt\n\n```diff\n{_build_diff(script.body, proposed_body)}```\n",
        encoding="utf-8",
    )

    applied_count = sum(1 for _, n in fixes_log if n == "applied")

    if auto_resolve:
        _write_auto_resolution_audit(script, report, fixes_log, stripped_tags, audit_path)
        log.info(
            "gate 2 auto-resolved: %d/%d fixes applied, %d [VERIFY] tags stripped (audit: %s)",
            applied_count, len(fixes_log), len(stripped_tags), audit_path.name,
        )
        # Sprint 5 L2 (check #14): scan auto-resolved script_FINAL.txt for
        # template artifacts before returning. If the gate-2 LLM hallucinated
        # a SCRIPT_BODY-style header, this halts before TTS instead of letting
        # edge-TTS speak the artifact aloud (the _12_002 failure mode).
        _scan_script_for_artifacts_or_halt(
            final_path,
            script.topic_id,
            style_guide_path=config.get("channel", {}).get("style_guide_path"),
        )
        broll_cues, body_no_broll = _extract_broll(proposed_body)
        return script.model_copy(update={
            "body": proposed_body,
            "broll_cues": broll_cues,
            "word_count": len(body_no_broll.split()),
        })

    summary = (
        f"Report: {len(report.claims)} claims, {report.unresolved_count} unresolved. "
        f"Auto-applied {applied_count} fix(es) to script_FINAL.txt."
    )
    raise HumanReviewRequired(
        gate_name="fact_check_resolution",
        review_path=review_path,
        action_path=final_path,
        summary=summary,
    )


_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "with", "and", "or",
    "but", "as", "is", "are", "was", "were", "be", "been", "being", "this", "that",
    "showing", "running", "active", "open", "side", "left", "right", "then",
    "while", "during", "task", "complete", "completing",
}


def _cue_to_query(cue: str) -> str:
    """Reduce a B-ROLL cue to a stock-search-friendly query.

    Takes the first 3–5 meaningful words after stop-word removal. LLM-written cues
    tend to be specific tool descriptions; stock libraries have generic equivalents,
    so a simplified query usually returns better-than-nothing matches.
    """
    cleaned = re.sub(r"[^\w\s]", " ", cue.lower())
    words = [w for w in cleaned.split() if len(w) > 2 and w not in _STOPWORDS]
    return " ".join(words[:4]) if words else "developer computer"


_T = TypeVar("_T")


def _retry_with_backoff(
    fn: Callable[[], _T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (requests.RequestException,),
) -> _T:
    """Call ``fn()``; retry on ``retry_on`` with exponential backoff, re-raise last.

    R1 (WORKFLOW_AUDIT_2026-05-31) — the unified, dependency-free retry primitive
    for the pipeline's external-API touchpoints. Plain Python (for / try / except +
    ``time.sleep(base_delay * 2**i)``), NO tenacity/backoff (that is the rejected
    NEW-DEP path; see feedback_engineering_principles.md — plain Python, explicit
    calls).

    Sleeps base_delay, 2*base_delay, 4*base_delay … between attempts; the LAST
    attempt does NOT sleep. On exhaustion the most-recent exception is re-raised so
    the caller's own handler still runs (e.g. the search functions' ``except
    requests.RequestException: return None`` preserves the provider-fallback
    contract). Anything NOT in ``retry_on`` propagates immediately (fail loud).

    Caution: under the dual-agent /start -auto shape retries multiply wall-clock —
    keep ``attempts`` low and ``base_delay`` small so a stuck provider can't blow
    the publish-slot timing.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            log.warning(
                "transient failure (attempt %d/%d), retrying in %.2fs: %s",
                i + 1, attempts, delay, exc,
            )
            time.sleep(delay)
    assert last_exc is not None  # only reachable after at least one caught exc
    raise last_exc


def _search_pexels_video(query: str, api_key: str) -> dict | None:
    """Search Pexels videos (portrait, HD+). Returns dict with url/source/license/page or None."""
    def _do_request() -> requests.Response:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "portrait"},
            headers={"Authorization": api_key},
            timeout=20,
        )
        r.raise_for_status()
        return r

    try:
        # R1: a transient blip (timeout / 5xx / dropped socket) now retries with
        # backoff before giving up, instead of failing on first error. M1's
        # None-on-final-failure contract is preserved — _retry_with_backoff
        # re-raises the last exception into this except, which returns None so
        # fetch_assets still falls back to the other provider.
        r = _retry_with_backoff(_do_request)
    except requests.RequestException as e:
        # M1: narrow from bare Exception to requests.RequestException so a genuinely
        # unexpected (non-network) error fails loud instead of being swallowed. Keep
        # returning None — fetch_assets relies on None to fall back to the other
        # provider; re-raising a 4xx here would break the Pexels→Pixabay chain.
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("pexels search failed for %r (HTTP %s): %s", query, status, e)
        return None

    data = r.json()
    videos = data.get("videos") or []
    if not videos:
        return None

    video = videos[0]
    files = sorted(video.get("video_files", []), key=lambda f: -f.get("height", 0))
    for f in files:
        if f.get("height", 0) >= 1080 and f.get("width", 0) < f.get("height", 0):
            return {
                "url": f["link"],
                "source": "pexels",
                "license": "Pexels License (free for commercial use, no attribution required)",
                "page_url": video.get("url"),
                "query": query,
            }
    if files:
        return {
            "url": files[0]["link"],
            "source": "pexels",
            "license": "Pexels License (free for commercial use, no attribution required)",
            "page_url": video.get("url"),
            "query": query,
        }
    return None


def _search_pixabay_video(query: str, api_key: str) -> dict | None:
    """Search Pixabay videos (vertical). Returns dict or None."""
    def _do_request() -> requests.Response:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": query, "per_page": 5, "video_type": "all"},
            timeout=20,
        )
        r.raise_for_status()
        return r

    try:
        # R1: retry transient blips with backoff before giving up (see
        # _search_pexels_video). None-on-final-failure preserved for the fallback.
        r = _retry_with_backoff(_do_request)
    except requests.RequestException as e:
        # M1: narrowed to requests.RequestException (see _search_pexels_video). Keep
        # returning None so fetch_assets' provider fallback still triggers.
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("pixabay search failed for %r (HTTP %s): %s", query, status, e)
        return None

    data = r.json()
    hits = data.get("hits") or []
    # Prefer vertical-aspect hits when available
    def is_portrait(h: dict) -> bool:
        v = h.get("videos", {}).get("large") or h.get("videos", {}).get("medium") or {}
        w, ht = v.get("width", 0), v.get("height", 0)
        return ht > w
    hits.sort(key=lambda h: 0 if is_portrait(h) else 1)
    if not hits:
        return None

    hit = hits[0]
    videos = hit.get("videos", {})
    for q in ("large", "medium", "small", "tiny"):
        v = videos.get(q) or {}
        if v.get("url"):
            return {
                "url": v["url"],
                "source": "pixabay",
                "license": "Pixabay License (free for commercial use, no attribution required)",
                "page_url": hit.get("pageURL"),
                "query": query,
            }
    return None


def _download_clip(url: str, dest: Path, timeout: int = 90) -> bool:
    """Download a video clip to `dest`. Skip if already present. Returns True on success.

    M7: streams to a sibling ``<dest>.part`` temp and only ``os.replace()``s it onto
    ``dest`` after a fully-successful download. A crash / kill / power-loss therefore
    leaves a ``.part`` (which the cache check below ignores), never a truncated-but-
    non-zero ``dest`` that the ``st_size > 0`` fast-path would wrongly treat as a
    complete cache hit. ``os.replace`` is atomic on the same filesystem.
    """
    import requests
    if dest.exists() and dest.stat().st_size > 0:
        log.info("clip already cached: %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        # Only now — full body read with no exception — promote the temp to final.
        os.replace(tmp, dest)
        log.info("downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return True
    except requests.RequestException as e:
        # M1: narrowed to requests.RequestException. Keep returning False (the
        # clip loop treats a missed cue as skippable); a non-network error now
        # propagates loud instead of being silently swallowed as a failed download.
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("download failed for %s → %s (HTTP %s): %s", url, dest.name, status, e)
        # M7: clean up the partial temp so it cannot accumulate; never leave a
        # truncated `dest` behind (os.replace only runs on full success above).
        tmp.unlink(missing_ok=True)
        return False


def fetch_assets(script: ScriptDraft, config: dict) -> AssetBundle:
    """Stage 3: parse B-ROLL cues, search Pexels then Pixabay (per `assets.preferred_stock_provider`),
    download matching portrait clips. Flux fallback is deferred (config.assets.flux_fallback_enabled
    is honored only after ComfyUI is set up — for now, missed cues are logged and skipped).

    Long-form: when is_longform(config), stock fetch is skipped entirely — the visual
    beats are generated (SDXL + diagrams) at render time by tools.longform_render, so
    this returns an empty bundle (render_master's long-form path ignores `assets`).
    """
    from tools.longform_config import is_longform
    if is_longform(config):
        log.info("assets: long-form mode — beats built at render time; skipping stock fetch")
        return AssetBundle(topic_id=script.topic_id, clips=[], images=[], licenses=[])

    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    pixabay_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not pexels_key and not pixabay_key:
        raise RuntimeError(
            "fetch_assets requires PEXELS_API_KEY and/or PIXABAY_API_KEY in .env. "
            "Currently both are empty."
        )

    preferred = config["assets"]["preferred_stock_provider"]
    providers = ["pexels", "pixabay"] if preferred == "pexels" else ["pixabay", "pexels"]

    out_dir = Path(config["paths"]["channel_root"]) / "03_assets" / "stock" / script.topic_id
    out_dir.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    licenses: list[dict] = []
    # Track which cues produced no clip (no stock match OR download failure) so
    # the end-of-loop summary names them — otherwise a partial b-roll miss only
    # shows up as scattered per-cue WARNINGs an operator must grep out of the log.
    failed: list[tuple[int, str]] = []

    for idx, cue in enumerate(script.broll_cues):
        query = _cue_to_query(cue)
        log.info("[cue %d/%d] %r → query %r", idx + 1, len(script.broll_cues), cue, query)

        match: dict | None = None
        for prov in providers:
            if prov == "pexels" and pexels_key:
                match = _search_pexels_video(query, pexels_key)
            elif prov == "pixabay" and pixabay_key:
                match = _search_pixabay_video(query, pixabay_key)
            if match:
                break

        if not match:
            log.warning("[cue %d] no stock match for query %r — skipping (Flux fallback deferred)",
                        idx + 1, query)
            failed.append((idx, query))
            continue

        # Filename: <topic_id>_<idx>_<source>.mp4
        ext = ".mp4"
        dest = out_dir / f"{script.topic_id}_cue{idx:02d}_{match['source']}{ext}"
        if _download_clip(match["url"], dest):
            clips.append(dest)
            licenses.append({
                "cue_index": idx,
                "cue_text": cue,
                "query": match["query"],
                "source": match["source"],
                "license": match["license"],
                "page_url": match.get("page_url"),
                "file": str(dest),
            })
        else:
            failed.append((idx, query))

    if not clips:
        raise RuntimeError(
            "No b-roll clips successfully fetched for any cue. Check API keys and "
            "network access. Cues attempted: " + str(len(script.broll_cues))
        )

    log.info("fetched %d/%d cues (%.1f MB total)",
             len(clips), len(script.broll_cues),
             sum(p.stat().st_size for p in clips) / 1e6)
    if failed:
        log.warning(
            "fetch_assets: %d/%d cues failed: %s",
            len(failed), len(script.broll_cues),
            ", ".join(f"#{i}:{q!r}" for i, q in failed),
        )

    # Write a manifest for the renderer to consume
    manifest_path = out_dir / "manifest.json"
    import json
    manifest_path.write_text(json.dumps(licenses, indent=2), encoding="utf-8")

    return AssetBundle(topic_id=script.topic_id, clips=clips, images=[], licenses=licenses)


def _strip_visual_directions(text: str) -> str:
    """Remove [B-ROLL: ...] and [VERIFY: ...] tags from text before TTS — they are visual
    direction or fact-check markers, not spoken content."""
    cues, cleaned = _extract_broll(text)  # also strips [B-ROLL: ...]
    cleaned = re.sub(r"\[VERIFY[^\]]*\]", "", cleaned)
    # Long-form chapter markers are visual/timestamp directions, never spoken. Shorts
    # scripts contain no [CHAPTER: ...] tags, so this is a no-op for them.
    cleaned = re.sub(r"\[CHAPTER[^\]]*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# edge-tts requires a signed-percent speaking rate ("+10%", "-5%", "+0%"). A
# malformed value would otherwise sail past here and blow up deep in the synth
# coroutine with a cryptic error; validate it at the load site instead.
_TTS_RATE_RE = re.compile(r"^[+-]\d+%$")


def _vo_edge_tts(topic_id: str, text: str, audio_dir: Path, config: dict) -> Path:
    """Synthesize VO via Microsoft Edge's public neural TTS endpoint.

    Output: 48kHz 16-bit mono WAV (raw conversion). Caches MP3 alongside.

    Loudness normalization moved to Stage 7.5 (`_normalize_vo_loudness`) so the
    inline filter no longer applies the single-pass `loudnorm=...:TP=-1.5` here.
    """
    import asyncio
    import edge_tts

    voice = config["tts"].get("edge_tts_voice", "en-US-AndrewMultilingualNeural")
    rate = str(config["tts"].get("rate", "+0%"))  # edge-tts format: "+10%", "-5%", "+0%"
    if not _TTS_RATE_RE.fullmatch(rate):
        raise ValueError(
            f"config.tts.rate={rate!r} is malformed; edge-tts requires a signed percent "
            f"like '+10%', '-5%', or '+0%'. Edit config.yaml tts.rate and re-run."
        )
    mp3_path = audio_dir / f"{topic_id}_vo.mp3"
    wav_path = audio_dir / f"{topic_id}_vo.wav"

    async def synth() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(mp3_path))

    log.info("edge-tts: synthesizing %d chars with voice=%s rate=%s", len(text), voice, rate)

    # M6 (WORKFLOW_AUDIT_2026-05-31): edge-tts hits Microsoft's PUBLIC endpoint
    # (no SLA), so a single attempt fails the whole VO stage on a transient 429 /
    # socket drop. Wrap in a bounded plain-Python retry with exponential backoff;
    # re-raise the last exception on final failure so a truly-down endpoint still
    # fails LOUD. RETRY-ONLY — no provider fallback (that's a separate money
    # decision). tts.* is not a sacred key, so retry count is config-tunable.
    import time

    import aiohttp  # transitive dep of edge-tts; no new pip line

    retries = int(config["tts"].get("edge_tts_retries", 3))
    retries = max(1, retries)  # always at least one attempt
    transient = (aiohttp.ClientError, asyncio.TimeoutError)
    for attempt in range(1, retries + 1):
        try:
            asyncio.run(synth())
            break
        except transient as e:
            if attempt >= retries:
                log.error(
                    "edge-tts failed after %d attempt(s): %s — endpoint may be down",
                    attempt, e,
                )
                raise
            delay = 0.5 * (2 ** (attempt - 1))  # 0.5s, 1s, 2s, ...
            log.warning(
                "edge-tts attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, retries, e, delay,
            )
            time.sleep(delay)
    log.info("edge-tts wrote %s (%.1f KB)", mp3_path.name, mp3_path.stat().st_size / 1024)

    # Convert MP3 -> WAV mono at target sample rate. Loudness is handled by
    # Stage 7.5 (two-pass loudnorm) downstream, so no `af` filter here.
    import ffmpeg
    sr = int(config["tts"].get("sample_rate_hz", 48000))
    (
        ffmpeg
        .input(str(mp3_path))
        .output(
            str(wav_path),
            ar=sr,
            ac=1,
            acodec="pcm_s16le",
        )
        .overwrite_output()
        .run(quiet=True)
    )
    log.info("edge-tts wrote raw WAV %s (%d Hz mono, %.1f KB) — loudnorm runs as Stage 7.5",
             wav_path.name, sr, wav_path.stat().st_size / 1024)
    return wav_path


def generate_voiceover(script: ScriptDraft, config: dict) -> Path:
    """Stage 4: TTS dispatch on `config.tts.provider`. Returns the WAV path.

    Providers:
      - "edge-tts" (default): Microsoft Edge neural TTS via public endpoint. No key, no
        model weights install. Decent quality for drafts and most publishing use cases.
      - "elevenlabs": ElevenLabs HTTP API (Brian voice). Code ships dormant — operator
        must subscribe + add `ELEVENLABS_API_KEY` to `.env` + add `elevenlabs` to
        `requirements.txt` before activating. SDK is imported lazily inside this
        branch so the rest of the pipeline runs without `elevenlabs` installed.
      - "f5-tts-local" / "xtts-local": local-GPU voice-cloning. Requires separate install
        and a voice-reference WAV. Not implemented today — see operator note in
        feedback_no_overpromise_videos.md memory.

    The spoken text is the script body with [B-ROLL] and [VERIFY] tags stripped.
    """
    provider = config["tts"]["provider"]

    audio_dir = Path(config["paths"]["channel_root"]) / "03_assets" / "audio_vo" / script.topic_id
    audio_dir.mkdir(parents=True, exist_ok=True)

    spoken_text = _strip_visual_directions(script.body)
    if not spoken_text:
        raise ValueError(
            f"After stripping [B-ROLL] / [VERIFY] tags, no spoken text remains for "
            f"topic_id={script.topic_id}. Check script_FINAL.txt has actual prose."
        )

    if provider == "edge-tts":
        return _vo_edge_tts(script.topic_id, spoken_text, audio_dir, config)

    if provider == "elevenlabs":
        # Lazy-import — the elevenlabs SDK is NOT in requirements.txt by default.
        # Pipeline must remain importable without it; only this branch should pull it in.
        from tools.tts_elevenlabs import synthesize as _elevenlabs_synthesize

        wav_path = audio_dir / f"{script.topic_id}_vo.wav"
        eleven_cfg = config["tts"].get("elevenlabs", {}) or {}
        log.info("elevenlabs: synthesizing %d chars -> %s", len(spoken_text), wav_path.name)
        kwargs: dict = {
            "sample_rate_hz": int(config["tts"].get("sample_rate_hz", 48000)),
        }
        # Operator-tunable knobs map to the synthesize() kwargs by name.
        for key in ("voice_id", "model_id", "stability", "similarity_boost",
                    "style", "speed", "output_format", "max_retries"):
            if key in eleven_cfg:
                kwargs[key] = eleven_cfg[key]
        return _elevenlabs_synthesize(spoken_text, wav_path, **kwargs)

    raise NotImplementedError(
        f"tts.provider={provider!r} not yet implemented. Set to 'edge-tts' in config.yaml "
        f"for the default unattended path. F5-TTS / XTTS require a focused install session — "
        f"see feedback_no_overpromise_videos.md memory."
    )


def _normalize_vo_loudness(vo_path: Path, config: dict) -> Path:
    """Stage 7.5: two-pass EBU R128 loudnorm on the raw VO WAV (in-place).

    Replaces the single-pass `loudnorm=I=-14:LRA=11:TP=-1.5` filter that used to
    live in `_vo_edge_tts`. Single-pass loudnorm is dynamic-range-compressed and
    only approximates the target — the previous 9 masters all drifted to
    -15.1..-15.6 LUFS instead of -14.0. The two-pass approach measures first,
    then applies the linear correction, landing within +/- 0.5 LU of target.

    Reads `config.loudnorm.{target_lufs, target_tp, target_lra}` for the targets
    (defaults: -14.0 / -1.0 / 11.0 per audio_loudnorm.DEFAULT_*).
    Writes back to the same path; returns it.
    """
    from tools.audio_loudnorm import normalize_vo

    loudnorm_cfg = config.get("loudnorm", {}) or {}
    target_lufs = float(loudnorm_cfg.get("target_lufs", -14.0))
    target_tp = float(loudnorm_cfg.get("target_tp", -1.0))
    target_lra = float(loudnorm_cfg.get("target_lra", 11.0))

    log.info("Stage 7.5: loudnorm two-pass on %s (I=%.1f TP=%.1f LRA=%.1f)",
             vo_path.name, target_lufs, target_tp, target_lra)
    # FFmpeg refuses to write output to its input path. Stage to a sibling
    # `.normalized.wav` then os.replace() atomically over the original — that
    # gives callers the same vo_path they passed in, avoiding a churn through
    # generate_captions / render_master path-tracking.
    tmp_path = vo_path.with_suffix(".normalized.wav")
    measurements = normalize_vo(
        vo_path,
        tmp_path,
        target_lufs=target_lufs,
        target_tp=target_tp,
        target_lra=target_lra,
    )
    import os
    import time
    # Windows flake: a freshly-written WAV is briefly held by AV real-time scan /
    # the search indexer, so a bare os.replace() can raise PermissionError
    # [WinError 5] (Access denied) even though nothing of ours holds a handle.
    # Left unretried this silently leaves the RAW vo.wav in place, the render
    # muxes un-normalized audio, and Stage 11's LUFS gate fails downstream.
    # Bounded retry-with-backoff makes the atomic swap resilient; behavior is
    # otherwise unchanged.
    last_exc: OSError | None = None
    for attempt in range(1, 9):
        try:
            os.replace(tmp_path, vo_path)
            last_exc = None
            break
        except PermissionError as exc:
            last_exc = exc
            log.warning(
                "Stage 7.5 replace attempt %d/8 hit a lock on %s (%s); backing off",
                attempt, vo_path.name, exc,
            )
            time.sleep(0.5 * attempt)
    if last_exc is not None:
        raise last_exc
    log.info(
        "Stage 7.5 done: input_i=%.2f LUFS input_tp=%.2f dBTP target_offset=%.2f",
        measurements.get("input_i", 0.0),
        measurements.get("input_tp", 0.0),
        measurements.get("target_offset", 0.0),
    )
    return vo_path


def _probe_wav_duration_seconds(wav_path: Path) -> float:
    """Return the duration of a WAV file in seconds.

    edge-tts and Stage 7.5 (`audio_loudnorm.normalize_vo`) both emit PCM
    (`pcm_s16le`) WAV, which stdlib `wave` reads with no external process. If
    the file is not PCM-decodable by `wave` (compressed payload in a .wav
    container, header quirk), fall back to `ffprobe` via the same subprocess
    pattern the rest of the pipeline uses for ffmpeg/ffprobe.

    Raises:
        RuntimeError: neither `wave` nor `ffprobe` could read a duration.
    """
    import wave

    wav_path = Path(wav_path)
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        if rate > 0:
            return frames / float(rate)
        log.warning(
            "wave reported framerate 0 for %s; falling back to ffprobe", wav_path.name
        )
    except (wave.Error, OSError, EOFError) as exc:
        log.debug(
            "wave could not read %s (%s); falling back to ffprobe", wav_path.name, exc
        )

    # Fallback: ffprobe the container for the format-level duration.
    import subprocess

    args = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(wav_path),
    ]
    try:
        proc = subprocess.run(
            args, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"could not measure VO duration: wave failed and ffprobe not on PATH "
            f"({wav_path})"
        ) from exc
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            f"ffprobe could not read a duration for {wav_path} "
            f"(exit {proc.returncode}, stderr tail: {proc.stderr[-200:]!r})"
        )
    try:
        return float(out)
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe returned a non-numeric duration {out!r} for {wav_path}"
        ) from exc


def _warn_if_vo_over_duration(vo_path: Path, script: "ScriptDraft", config: dict) -> float | None:
    """WARN (never halt) if the normalized VO is longer than the breakout target.

    Runs right after Stage 7.5 loudnorm, when the normalized VO .wav is the final
    spoken artifact. Reads `script_quality.duration_warn_s` (default 38.0); a value
    of 0 (or negative) disables the check. This is deliberately warn-only: an
    unattended `/start -auto` run must not deadlock on a fresh duration gate —
    B1's word_count_max (98) bounds the tail, and this surfaces the real measured
    overrun for the next cycle to tighten the script.

    Returns the measured duration in seconds, or None when the check is disabled.
    Never raises on a measurement failure — a duration probe must not break the
    render path; it logs and returns None instead.
    """
    qcfg = config.get("script_quality", {}) or {}
    warn_s = float(qcfg.get("duration_warn_s", 38.0))
    if warn_s <= 0:
        return None
    try:
        duration = _probe_wav_duration_seconds(vo_path)
    except RuntimeError as exc:  # noqa: BLE001 — duration probe is advisory; never break render
        log.warning("could not measure VO duration for %s (%s); skipping duration WARN",
                    vo_path.name, exc)
        return None
    if duration > warn_s:
        log.warning(
            "VO duration %.1fs exceeds the %.1fs breakout target (script ~%d words) "
            "— tighten the script next cycle",
            duration, warn_s, script.word_count,
        )
    else:
        log.info("VO duration %.1fs within the %.1fs breakout target", duration, warn_s)
    return duration


def _format_ass_time(seconds: float) -> str:
    """ASS subtitle time format: H:MM:SS.cc (centiseconds)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _chunk_words_for_captions(
    word_segments: list[tuple[str, float, float]],
    max_words: int = 3,
    max_gap_s: float = 0.6,
) -> list[tuple[str, float, float]]:
    """Group whisper word-timestamps into 1–3 word caption chunks.

    Breaks chunks on (a) sentence-ending punctuation, (b) timing gaps over max_gap_s,
    (c) reaching max_words. Punctuation stays attached to the last word.
    """
    if not word_segments:
        return []

    chunks: list[tuple[str, float, float]] = []
    cur: list[tuple[str, float, float]] = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(w for w, _, _ in cur).strip()
        chunks.append((text, cur[0][1], cur[-1][2]))
        cur.clear()

    for word, start, end in word_segments:
        if cur and (start - cur[-1][2]) > max_gap_s:
            flush()
        cur.append((word, start, end))
        ends_sentence = word.rstrip().endswith((".", "!", "?"))
        if len(cur) >= max_words or ends_sentence:
            flush()
    flush()
    return chunks


_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Inter,96,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,1,5,40,40,800,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def generate_captions(vo_path: Path, script: ScriptDraft, config: dict) -> Path:
    """Stage 9: caption-style dispatch on `config.captions.style`.

    Two styles supported:
      - `"word_pop"` (default, R5 retention lever): word-by-word "popping" captions
        via `tools.caption_word_pop` — yellow active word with a 125%->100%
        scale-bounce, white inactive words. Per Day 3.3 of the 30/60/90 plan.
      - `"legacy"`: the original 1-3 word static-block emitter, kept for one-flip
        rollback if word-pop misbehaves on a particular topic.

    Returns the path to the written `.ass` file.
    """
    style = (config.get("captions", {}) or {}).get("style", "word_pop")
    if style == "legacy":
        log.info("captions: dispatching to legacy 1-3 word block style")
        return _generate_captions_legacy(vo_path, script, config)
    if style == "lower_third":
        log.info("captions: dispatching to long-form lower-third style")
        from tools.longform_captions import generate_lower_third_captions
        return generate_lower_third_captions(vo_path, script, config)
    if style != "word_pop":
        raise ValueError(
            f"captions.style={style!r} is not a valid value; expected "
            f"'word_pop', 'lower_third', or 'legacy'."
        )

    from tools.caption_word_pop import (
        pack_into_lines,
        render_ass,
        transcribe_words,
        warn_if_fonts_missing,
    )

    if not vo_path.exists():
        raise FileNotFoundError(f"Voiceover audio not found: {vo_path}")

    captions_cfg = config.get("captions", {}) or {}
    model_dir = Path(config["paths"]["models"]) / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_name = captions_cfg["whisper_model"]
    compute_type = captions_cfg["whisper_compute_type"]

    log.info("captions: word_pop style — transcribing %s with model=%s",
             vo_path.name, model_name)
    warn_if_fonts_missing()
    words = transcribe_words(
        vo_path,
        model_name=model_name,
        compute_type=compute_type,
        download_root=model_dir,
    )

    lines = pack_into_lines(words)
    log.info("captions: packed %d words into %d lines", len(words), len(lines))

    wip_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_wip" / script.topic_id
    wip_dir.mkdir(parents=True, exist_ok=True)
    captions_path = wip_dir / f"{script.topic_id}_captions.ass"

    # Operator-tunable visual knobs read from config; otherwise the spec defaults
    # in caption_word_pop.render_ass (Montserrat Black, 84/96 px, MarginV=540).
    render_kwargs: dict = {}
    font_name = captions_cfg.get("font_name")
    if font_name:
        render_kwargs["font_primary"] = font_name
    res = config.get("render", {}).get("resolution")
    if res and len(res) == 2:
        render_kwargs["video_width"] = int(res[0])
        render_kwargs["video_height"] = int(res[1])

    render_ass(lines, captions_path, **render_kwargs)
    log.info("captions: wrote %s", captions_path)
    return captions_path


def _generate_captions_legacy(vo_path: Path, script: ScriptDraft, config: dict) -> Path:
    """Stage 9 (legacy): static-block 1-3 word ASS captions via faster-whisper.

    Original implementation kept verbatim so `captions.style: legacy` is a clean
    one-flip rollback if word_pop misbehaves on a particular topic.

    Word-level timestamps are required (config.captions.word_level_timestamps must be true).
    Output is a styled ASS file ready for FFmpeg's `subtitles` filter to burn in. Style:
    centered, bold sans-serif, large white text with black outline + drop shadow.
    Margins keep captions clear of the top 14% / bottom 18% UI safe-zones.
    """
    from faster_whisper import WhisperModel

    if not vo_path.exists():
        raise FileNotFoundError(f"Voiceover audio not found: {vo_path}")

    model_dir = Path(config["paths"]["models"]) / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)

    model_name = config["captions"]["whisper_model"]
    compute_type = config["captions"]["whisper_compute_type"]
    log.info("loading faster-whisper model=%s compute_type=%s (download_root=%s)",
             model_name, compute_type, model_dir)
    model = WhisperModel(model_name, device="cuda", compute_type=compute_type,
                         download_root=str(model_dir))

    log.info("transcribing %s (word_timestamps=True)", vo_path.name)
    segments, info = model.transcribe(
        str(vo_path),
        word_timestamps=True,
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
    )

    # Flatten word-level segments
    words: list[tuple[str, float, float]] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            words.append((w.word.strip(), float(w.start), float(w.end)))

    if not words:
        raise RuntimeError(
            f"Whisper transcription produced no word-level segments for {vo_path}. "
            f"Check the audio is non-silent and word_timestamps is supported."
        )

    chunks = _chunk_words_for_captions(words, max_words=3, max_gap_s=0.6)
    log.info("transcription: %d words → %d caption chunks (lang=%s, duration=%.1fs)",
             len(words), len(chunks), info.language, info.duration)

    # Output to per-topic working dir under 04_renders\_wip\
    wip_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_wip" / script.topic_id
    wip_dir.mkdir(parents=True, exist_ok=True)
    captions_path = wip_dir / f"{script.topic_id}_captions.ass"

    lines = [_ASS_HEADER]
    for text, start, end in chunks:
        # Escape ASS special chars in dialogue text
        safe = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},Default,,0,0,0,,{safe}"
        )
    captions_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote captions: %s", captions_path)
    return captions_path


def _master_output_path(config: dict, topic_id: str) -> Path:
    """Canonical master path: ``<channel_root>/04_renders/_final_master/<topic_id>_master.mp4``.

    Shared by ``render_master`` (the encode target) and the ``RenderLock``
    orphan guard in ``run_for_topic`` so both agree on the file name without
    duplicating the path formula.
    """
    out_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_final_master"
    return out_dir / f"{topic_id}_master.mp4"


def render_master(
    script: ScriptDraft,
    assets: AssetBundle,
    vo_path: Path,
    captions_path: Path,
    config: dict,
    *,
    force_encoder: str | None = None,
) -> Path:
    """Stage 6: FFmpeg assembly into a 1080×1920 H.264 master.

    Strategy:
      - VO audio is the timing spine — total master duration = VO duration.
      - B-roll clips are cycled in order, each segment = VO_duration / N_clips.
      - Each clip is `stream_loop`-ed and trimmed to its segment so short clips repeat.
      - Each clip is scaled (cover) and center-cropped to 1080×1920.
      - The concat'd video gets the ASS subtitles filter applied for burned-in captions.
      - Final mux: video + VO audio, encoded H.264 (NVENC if config.render.hardware_accel='nvenc').

    Args:
      force_encoder: when set (e.g. "libx264"), override the encoder choice from
        config.render.hardware_accel. Used by _render_with_integrity_retry() to
        force a libx264 re-render after a suspected NVENC silent-corruption
        event. None (default) preserves the config-driven choice.

    Windows path note: the ASS/subtitles filter struggles with drive-letter colons in
    its filename argument. We work around it by running ffmpeg with cwd set to the
    captions file's directory and passing a relative basename to the `ass` filter.

    Long-form: when is_longform(config), delegate to tools.longform_render — beats
    are built at render time from the VO duration and `assets` is ignored. Returns
    the same _master_output_path so the lock/integrity/skip-guard wrapping is intact.
    """
    from tools.longform_config import is_longform
    if is_longform(config):
        from tools.longform_render import render_master_longform
        return render_master_longform(
            script, vo_path, captions_path, config,
            _master_output_path(config, script.topic_id),
            force_encoder=force_encoder,
        )

    import ffmpeg
    import subprocess

    if not assets.clips:
        raise RuntimeError("render_master: no b-roll clips in AssetBundle")
    if not vo_path.exists():
        raise FileNotFoundError(f"VO audio not found: {vo_path}")
    if not captions_path.exists():
        raise FileNotFoundError(f"Captions ASS not found: {captions_path}")

    # 1. Probe VO duration — that's the master's total length.
    probe = ffmpeg.probe(str(vo_path))
    audio_duration = float(probe["format"]["duration"])
    n_clips = len(assets.clips)
    seg_duration = audio_duration / n_clips
    log.info("render: vo=%.1fs across %d clips → %.2fs per segment",
             audio_duration, n_clips, seg_duration)

    res = config["render"]["resolution"]  # [w, h] = [1080, 1920]
    w, h = int(res[0]), int(res[1])
    fps = int(config["render"]["framerate"])
    bitrate_k = int(config["render"]["bitrate_kbps"])

    # 2. Build per-clip processed video streams.
    video_segments = []
    for clip_path in assets.clips:
        seg = (
            ffmpeg
            .input(str(clip_path), stream_loop=-1, t=seg_duration)
            .video
            .filter("scale", w, h, force_original_aspect_ratio="increase")
            .filter("crop", w, h)
            .filter("setsar", 1)
            .filter("fps", fps=fps)
        )
        video_segments.append(seg)

    # 3. Concat segments. Apply ASS subtitles filter using BASENAME — ffmpeg cwd is set
    #    to the captions file's parent so the filter avoids the Windows colon-escape mess.
    concat_v = ffmpeg.concat(*video_segments, v=1, a=0)
    captions_basename = captions_path.name
    concat_v = concat_v.filter("ass", captions_basename)

    # 4. Audio is just the VO at its original sample rate.
    audio = ffmpeg.input(str(vo_path)).audio

    # 5. Output path under 04_renders\_final_master\. Encode to a sibling
    #    `.part` file and atomically promote to the canonical master name ONLY
    #    after the smoke-decode check passes. Crash-safety: a killed/detached
    #    render never leaves a half-written file at the master path, so resume
    #    always re-renders cleanly and no partial can masquerade as "done".
    master_path = _master_output_path(config, script.topic_id)
    master_path.parent.mkdir(parents=True, exist_ok=True)
    from tools.render_lock import part_path_for

    part_path = part_path_for(master_path)

    # `force_encoder` (used by _render_with_integrity_retry on integrity failure)
    # overrides the config-driven NVENC choice. "libx264" forces software encode.
    if force_encoder:
        use_nvenc = force_encoder == "h264_nvenc"
        vcodec = force_encoder
        if force_encoder == "libx264":
            log.warning("render: force_encoder=libx264 (overriding config); software encode")
    else:
        use_nvenc = config["render"].get("hardware_accel") == "nvenc"
        vcodec = "h264_nvenc" if use_nvenc else "libx264"

    out_kwargs: dict = {
        "vcodec": vcodec,
        "acodec": config["render"].get("audio_codec", "aac"),
        "pix_fmt": "yuv420p",
        "r": fps,
        "ar": 48000,
        "ac": 2,                # R7: stereo upmix from mono VO; prepublish_qa #7 expects 2.
        "movflags": "+faststart",
        # Encode target is the lock's `.part` sidecar (see part_path_for), whose
        # `.mp4.part` extension ffmpeg cannot map to a muxer — it errors out at
        # AVFormatContext init ("Unable to choose an output format ... use a
        # standard extension or specify the format manually"). Pin the muxer
        # explicitly so output-container selection never depends on the
        # filename. Propagates to all three .output() calls below (primary +
        # both libx264 retries) via **out_kwargs. The atomic promote to the
        # canonical `.mp4` happens after a clean encode.
        "f": "mp4",
        "t": audio_duration,
        "shortest": None,
    }
    if use_nvenc:
        out_kwargs["preset"] = "p5"
        out_kwargs["b:v"] = f"{bitrate_k}k"
    else:
        out_kwargs["preset"] = "fast"
        out_kwargs["b:v"] = f"{bitrate_k}k"
    out_kwargs["b:a"] = "128k"

    log.info("render: encoding to %s (vcodec=%s, %dkbps)", master_path.name, vcodec, bitrate_k)
    cmd_args = (
        ffmpeg
        .output(concat_v, audio, str(part_path), **out_kwargs)
        .overwrite_output()
        .compile()
    )
    # Run via subprocess so we can set cwd to the captions dir (path-escape workaround).
    result = subprocess.run(
        cmd_args,
        cwd=str(captions_path.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("ffmpeg failed (returncode=%d):\n%s", result.returncode, (result.stderr or "")[-3000:])
        # Retry once with libx264 whenever the NVENC attempt failed. The prior
        # gate keyed on stderr literally containing "nvenc", which silently
        # skipped the fallback for encoder failures that don't name the encoder
        # (driver/session-init/OOM errors — and the format-selection error that
        # used to fail here). libx264 is the safe software fallback, so fall back
        # on ANY non-zero NVENC encode; a genuine non-encoder problem just fails
        # again and raises below with the libx264 stderr.
        if use_nvenc:
            log.warning("NVENC encode failed (rc=%d); retrying with libx264", result.returncode)
            out_kwargs["vcodec"] = "libx264"
            out_kwargs["preset"] = "fast"
            cmd_args = (
                ffmpeg
                .output(concat_v, audio, str(part_path), **out_kwargs)
                .overwrite_output()
                .compile()
            )
            result = subprocess.run(cmd_args, cwd=str(captions_path.parent),
                                    capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg render failed (returncode={result.returncode}). "
                f"Stderr tail:\n{(result.stderr or '')[-2000:]}"
            )

    size_mb = part_path.stat().st_size / 1e6
    log.info("render: encoded %s (%.1f MB, %.1fs); verifying before promote",
             master_path.name, size_mb, audio_duration)

    # Stage 6.1 (belt-and-suspenders): a 3-frame decode probe immediately after
    # render. Catches NVENC silent-corruption (rc=0 + NAL-corrupt master) before
    # Stage 10.1's deeper check. If it fails AND we just used NVENC, swap to
    # libx264 inline using the same retry shape as the rc-fail branch above.
    if not _decode_smoke_check(part_path):
        log.error(
            "render: smoke-decode-check FAILED on %s (encoder=%s); attempting libx264 retry",
            master_path.name, vcodec,
        )
        if use_nvenc:
            log.warning(
                "render: NVENC produced rc=0 but NAL-corrupt output; re-encoding with libx264"
            )
            out_kwargs["vcodec"] = "libx264"
            out_kwargs["preset"] = "fast"
            cmd_args = (
                ffmpeg
                .output(concat_v, audio, str(part_path), **out_kwargs)
                .overwrite_output()
                .compile()
            )
            retry_result = subprocess.run(
                cmd_args, cwd=str(captions_path.parent),
                capture_output=True, text=True,
            )
            if retry_result.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg libx264 retry after smoke-check fail returned "
                    f"rc={retry_result.returncode}. "
                    f"Stderr tail:\n{(retry_result.stderr or '')[-2000:]}"
                )
            if not _decode_smoke_check(part_path):
                raise RuntimeError(
                    f"render: smoke-decode-check still FAILED after libx264 retry "
                    f"on {part_path}"
                )
            size_mb = part_path.stat().st_size / 1e6
            log.info(
                "render: re-encoded (libx264 retry) %s (%.1f MB, %.1fs); verifying before promote",
                master_path.name, size_mb, audio_duration,
            )
        else:
            raise RuntimeError(
                f"render: smoke-decode-check FAILED on {part_path} (encoder={vcodec}; "
                f"no NVENC retry path available)"
            )

    # Atomic promote: the `.part` passed the smoke-decode check, so swap it into
    # the canonical master name in a single os.replace (atomic on the same
    # volume). Only now does a file exist at master_path — a render killed at any
    # point before this line leaves only the `.part`, never a half-written master.
    os.replace(part_path, master_path)
    final_mb = master_path.stat().st_size / 1e6
    log.info("master rendered: %s (%.1f MB, %.1fs)", master_path.name, final_mb, audio_duration)

    return master_path


def _decode_smoke_check(master_path: Path) -> bool:
    """Stage 6.1 belt-and-suspenders: 3-frame decode probe on a freshly rendered master.

    Returns True if ffmpeg can decode the first 3 video frames cleanly (rc=0),
    False otherwise. Designed to catch NVENC silent corruption (rc=0 + NAL-
    corrupt output) before Stage 10.1's deeper decode probe runs. This is a
    cheap O(100ms) probe that complements `_check_media_integrity()` rather
    than replacing it.

    Implementation: `ffmpeg -i <master> -frames:v 3 -f null -`. Any non-zero
    exit code is treated as corruption. Missing-file is also False (caller
    decides whether that warrants a retry vs an immediate halt).
    """
    import subprocess

    if not master_path.exists():
        log.error("smoke-decode-check: master does not exist: %s", master_path)
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(master_path),
             "-frames:v", "3", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        log.error("smoke-decode-check: ffmpeg timed out on %s", master_path)
        return False
    except FileNotFoundError:
        # ffmpeg not on PATH; can't probe. Fail-safe: return True so we don't
        # block renders in environments without ffmpeg (caller's render would
        # have already failed earlier).
        log.warning("smoke-decode-check: ffmpeg not on PATH; skipping probe")
        return True
    if proc.returncode != 0:
        log.error(
            "smoke-decode-check FAIL on %s (rc=%d): %s",
            master_path.name, proc.returncode, (proc.stderr or "")[-500:],
        )
        return False
    return True


def _render_with_integrity_retry(
    script: "ScriptDraft",
    assets: "AssetBundle",
    vo_path: Path,
    captions_path: Path,
    config: dict,
    *,
    max_retries: int = 1,
) -> Path:
    """Wrap render_master() + Stage 10.1 integrity check with a single libx264 retry.

    Defense against NVENC silent corruption (rc=0 + NAL-corrupt master):
      1. Run render_master() with config-driven encoder.
      2. Run _check_media_integrity(stage="post-master").
      3. If IntegrityCheckFailed fires AND we have retries left, rename the
         broken master to <master>.nvenc-corrupt.mp4 for postmortem evidence
         and re-invoke render_master(force_encoder="libx264"). Re-check.
      4. On second failure, re-raise the IntegrityCheckFailed.

    Returns the path to the integrity-verified master.
    """
    attempt = 0
    last_exc: IntegrityCheckFailed | None = None
    force_encoder: str | None = None

    while attempt <= max_retries:
        master_path = render_master(
            script, assets, vo_path, captions_path, config,
            force_encoder=force_encoder,
        )
        log.info("master rendered: %s", master_path)
        try:
            _check_media_integrity(master_path, stage="post-master")
            # Canonical OK line — /start -auto greps for this exact prefix
            # before dropping <topic_id>_master_QA_APPROVED.marker.
            log.info("Stage 10.1 OK on %s", master_path.name)
            return master_path
        except IntegrityCheckFailed as exc:
            last_exc = exc
            attempt += 1
            if attempt > max_retries:
                log.error(
                    "render+integrity: %d retries exhausted on %s; re-raising",
                    max_retries, master_path.name,
                )
                raise
            # Rename the corrupt master so it's preserved for postmortem.
            corrupt_path = master_path.with_suffix(
                master_path.suffix + ".nvenc-corrupt.mp4"
            )
            try:
                if corrupt_path.exists():
                    corrupt_path.unlink()
                master_path.rename(corrupt_path)
                log.warning(
                    "render+integrity: preserved corrupt master at %s",
                    corrupt_path,
                )
            except OSError as rename_exc:
                log.error(
                    "render+integrity: failed to rename corrupt master %s -> %s: %s",
                    master_path, corrupt_path, rename_exc,
                )
            log.warning(
                "render+integrity: integrity check failed on attempt %d (%s); "
                "retrying with force_encoder=libx264",
                attempt, exc.reason,
            )
            force_encoder = "libx264"

    # Defensive — loop always returns or raises above, but mypy/lint clarity.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("_render_with_integrity_retry: unreachable")


def await_final_qa(master_path: Path, config: dict) -> bool:
    """SACRED GATE 3: halt until a human has watched the master end-to-end on a phone-sized window.

    Approval mechanism: operator creates a marker file `<master>_QA_APPROVED.marker`
    (empty file) alongside the master. Once present, gate passes. To withdraw approval,
    delete the marker and re-run.
    """
    if not config["publishing"]["human_qa_required"]:
        raise RuntimeError(
            "Refusing to run: config.publishing.human_qa_required must be true. "
            "This is a SACRED GATE per the operating guide."
        )
    if not master_path.exists():
        raise FileNotFoundError(f"Master not found for QA: {master_path}")

    marker = master_path.parent / f"{master_path.stem}_QA_APPROVED.marker"
    if marker.exists():
        log.info("final QA approved (marker exists): %s", marker.name)
        return True

    raise HumanReviewRequired(
        gate_name="final_video_qa",
        review_path=master_path,
        action_path=marker,
        summary=(
            f"Watch the master end-to-end on a phone-sized window. If approved, create "
            f"the marker file (`type nul > {marker.name}` in cmd, or `New-Item {marker.name}` "
            f"in PowerShell). To reject, leave the marker absent and edit upstream artifacts."
        ),
    )


def generate_variants(master_path: Path, config: dict) -> dict[Platform, Path]:
    """Stage 7: produce YT/TT/IG variants from the approved master.

    Per-platform differences (operating guide §4 Phase 4) are deliberately small:
      - YouTube Shorts: clean stream-copy (no re-encode, no audio bed). Manual upload.
      - TikTok: 0.3s video + audio fade-in (different first 2 frames vs YT).
      - Instagram Reels: 0.5s video + audio fade-in (different first 2 frames vs both).
    All three are ready for upload; YouTube goes via Studio manually, TT/IG via Metricool.
    """
    import ffmpeg
    import subprocess

    if not master_path.exists():
        raise FileNotFoundError(f"Master not found: {master_path}")

    base = Path(config["paths"]["channel_root"]) / "05_exports"
    yt_dir = base / "youtube"
    tt_dir = base / "tiktok"
    ig_dir = base / "instagram"
    for d in (yt_dir, tt_dir, ig_dir):
        d.mkdir(parents=True, exist_ok=True)

    stem = master_path.stem.replace("_master", "")
    yt_path = yt_dir / f"{stem}_yt.mp4"
    tt_path = tt_dir / f"{stem}_tt.mp4"
    ig_path = ig_dir / f"{stem}_ig.mp4"

    # YT: stream copy (zero-loss, fast)
    log.info("variants: YT (stream copy)")
    yt_args = (
        ffmpeg
        .input(str(master_path))
        .output(str(yt_path), c="copy", movflags="+faststart")
        .overwrite_output()
        .compile()
    )
    r = subprocess.run(yt_args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"YT variant encode failed:\n{(r.stderr or '')[-1500:]}")

    bitrate_k = int(config["render"]["bitrate_kbps"])

    def _fade_variant(out_path: Path, fade_seconds: float) -> None:
        # Audio-only fade-in. Video starts from the source first frame (no
        # fade-from-black). Phase A integration smoke (2026-05-08) caught
        # prepublish_qa #12 failing on TT/IG variants because the prior
        # video fade-from-black produced a 70-100ms black region at t=0,
        # which the gate correctly flagged as a no-black-frame violation.
        inp = ffmpeg.input(str(master_path))
        v = inp.video
        a = inp.audio.filter("afade", type="in", start_time=0, duration=fade_seconds)
        args = (
            ffmpeg
            .output(
                v, a, str(out_path),
                vcodec="libx264",
                acodec="aac",
                preset="fast",
                pix_fmt="yuv420p",
                ac=2,                # R7: keep stereo across variants (mirrors render_master).
                movflags="+faststart",
                **{"b:v": f"{bitrate_k}k", "b:a": "128k"},
            )
            .overwrite_output()
            .compile()
        )
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"variant encode failed for {out_path.name}:\n{(r.stderr or '')[-1500:]}")

    # Variant platform set is config-gated (long-form = YouTube-only). Absent =>
    # all three (Shorts byte-identical). YouTube is always built (the primary).
    platforms = (config.get("variants", {}) or {}).get("platforms") or [
        "youtube", "tiktok", "instagram"]
    out = {"youtube": yt_path}
    if "tiktok" in platforms:
        log.info("variants: TT (0.3s fade-in)")
        _fade_variant(tt_path, 0.3)
        out["tiktok"] = tt_path
    if "instagram" in platforms:
        log.info("variants: IG (0.5s fade-in)")
        _fade_variant(ig_path, 0.5)
        out["instagram"] = ig_path

    log.info("variants done: %s",
             ", ".join(f"{k}={v.stat().st_size / 1e6:.1f}MB" for k, v in out.items()))
    return out


_METADATA_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:##\s*)?(YOUTUBE(?:\s*SHORTS)?|TIKTOK|INSTAGRAM\s*REELS|COVER(?:\s*/\s*THUMBNAIL(?:\s+CONCEPT)?)?|PINNED\s*COMMENT)"
    r"\s*:?\s*\n",
    re.IGNORECASE,
)
_METADATA_FIELD_RE_TPL = (
    r"^\s*[-*]?\s*\*?\*?{label}\*?\*?\s*:\s*(.+?)(?=^\s*[-*]?\s*\*?\*?(?:Title|Description|Tags|Hashtags|Caption|"
    r"Pattern|Text(?:\s+overlay)?|Background(?:\s+\w+)*|Accent(?:\s+color)?|Color(?:\s+accent)?)"
    r"\*?\*?\s*:|\Z)"
)


def _md_section_field(section_text: str, label: str) -> str:
    pattern = _METADATA_FIELD_RE_TPL.format(label=re.escape(label))
    m = re.search(pattern, section_text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _md_extract_hashtags(text: str) -> list[str]:
    return re.findall(r"#[\w\d_]+", text)


def _md_extract_tags(text: str) -> list[str]:
    """Tags can be comma-separated, bulleted, or both. Normalize to a flat list."""
    return [t.strip("-• ").strip() for t in re.split(r"[,\n]+", text) if t.strip("-• ").strip()]


def _parse_metadata_response(response: str, topic_id: str) -> MetadataBundle:
    """Parse the metadata-gen LLM response into a MetadataBundle.

    Expected layout (per prompts/06_metadata_generation.md):
        ## YOUTUBE SHORTS
        Title: ...
        Description: ...  (may span multiple lines)
        Tags: tag1, tag2, ...
        Hashtags: #tag1 #tag2 #tag3

        ## TIKTOK
        Caption: ...
        Hashtags: #...

        ## INSTAGRAM REELS
        Caption: ...
        Hashtags: #...

        ## COVER
        Text overlay: ...
        Background: ...
        Accent color: ...

    Section headers can be `## NAME` or `NAME:`. Fields can be `Label:` or `**Label:**`.
    """
    # Build {section_name: section_text} by walking matches and slicing the response.
    matches = list(_METADATA_SECTION_RE.finditer(response))
    if not matches:
        raise ValueError(
            "No metadata sections found. Expected sections labeled YOUTUBE SHORTS, TIKTOK, "
            "INSTAGRAM REELS, COVER. Edit metadata_RESPONSE.txt and re-run."
        )
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        key = re.sub(r"\s+", " ", m.group(1).upper()).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        sections[key] = response[m.end():end]

    yt = sections.get("YOUTUBE SHORTS", "") or sections.get("YOUTUBE", "")
    tt = sections.get("TIKTOK", "")
    ig = sections.get("INSTAGRAM REELS", "")
    cv = (
        sections.get("COVER", "")
        or sections.get("COVER / THUMBNAIL", "")
        or sections.get("COVER / THUMBNAIL CONCEPT", "")
    )
    # PINNED COMMENT (PU-2, optional). Free-form single line — no Label: sub-field,
    # so take the whole section body trimmed. Recognized as a section boundary by
    # _METADATA_SECTION_RE so it can no longer bleed into the COVER accent field.
    # Strip a leading "- " bullet the LLM may emit.
    pc = sections.get("PINNED COMMENT", "").strip()
    pc = re.sub(r"^[-*]\s+", "", pc).strip()

    # Long-form (YouTube-only) metadata uses a "YOUTUBE" section and omits TikTok/
    # Instagram by design (that track does not cross-post). Relax the cross-platform
    # requirement for it; the empty tiktok/instagram fields are valid on MetadataBundle.
    youtube_only = "YOUTUBE SHORTS" not in sections and bool(yt)
    required = ([("YOUTUBE", yt), ("COVER", cv)] if youtube_only
                else [("YOUTUBE SHORTS", yt), ("TIKTOK", tt),
                      ("INSTAGRAM REELS", ig), ("COVER", cv)])
    missing = [name for name, text in required if not text]
    if missing:
        raise ValueError(
            f"Metadata response missing required section(s): {', '.join(missing)}. "
            f"Edit metadata_RESPONSE.txt and re-run."
        )

    pattern_raw = _md_section_field(cv, "Pattern")
    # Normalize: spaces / dashes / mixed case → underscores + lower
    pattern_name = re.sub(r"[\s\-]+", "_", pattern_raw.lower()).strip("_") if pattern_raw else "big_text_claim"
    if not pattern_name:
        pattern_name = "big_text_claim"

    return MetadataBundle(
        topic_id=topic_id,
        youtube_title=_md_section_field(yt, "Title"),
        youtube_description=_md_section_field(yt, "Description"),
        youtube_tags=_md_extract_tags(_md_section_field(yt, "Tags")),
        youtube_hashtags=_md_extract_hashtags(_md_section_field(yt, "Hashtags")),
        tiktok_caption=_md_section_field(tt, "Caption"),
        tiktok_hashtags=_md_extract_hashtags(_md_section_field(tt, "Hashtags")),
        instagram_caption=_md_section_field(ig, "Caption"),
        instagram_hashtags=_md_extract_hashtags(_md_section_field(ig, "Hashtags")),
        cover_text=(
            _md_section_field(cv, "Text overlay")
            or _md_section_field(cv, "Text")
        ),
        cover_background_desc=_md_section_field(cv, "Background"),
        cover_color_accent=(
            _md_section_field(cv, "Accent color")
            or _md_section_field(cv, "Accent")
        ),
        cover_pattern_name=pattern_name,
        pinned_comment=pc,
    )


def _enforce_metadata_hard_rules(bundle: MetadataBundle, config: dict) -> None:
    """Run the config-flagged metadata hard rules; raise MetadataRuleViolation
    (a halt) with feedback when any are broken.

    Flag (default OFF so legacy configs keep legacy behavior; production
    config.yaml + template set it true — one-flip rollback):
      - script_quality.title_anchor_gate_enabled (PU-3T) — the de-facto-thumbnail
        TITLE anchor (first 3 words). Complements the PU-3 spoken-body anchor
        gate, which by design cannot see the title (it doesn't exist at Stage 1.5).
    """
    qcfg = config.get("script_quality") or {}
    violations: list[str] = []

    if bool(qcfg.get("title_anchor_gate_enabled", False)):
        v = title_anchor_violation(bundle.youtube_title)
        if v:
            violations.append(f"title anchor (PU-3T): {v}")

    if violations:
        log.error(
            "metadata hard-rule violation(s) on %s: %s",
            bundle.topic_id, "; ".join(violations),
        )
        raise MetadataRuleViolation(topic_id=bundle.topic_id, violations=violations)


def generate_metadata(script: ScriptDraft, config: dict) -> MetadataBundle:
    """Stage 8: LLM produces titles, descriptions, hashtags, cover concept per platform.

    Dispatches on `config.llm.primary_provider` — same pattern as `generate_script`.
    """
    provider = config["llm"]["primary_provider"]
    if provider != "manual":
        raise NotImplementedError(
            f"llm.primary_provider={provider!r} not yet implemented for metadata. "
            f"Set to 'manual' in config.yaml."
        )

    template = load_prompt((config.get("prompts") or {}).get("metadata", "06_metadata_generation"), config)
    style_guide = load_style_guide(config)

    # The script content the metadata LLM sees: hooks + final body (post fact-check resolution).
    hooks_block = "\n".join(
        f"HOOK_{chr(65 + i)}: {h}" for i, h in enumerate(script.hook_variants)
    )
    script_text = f"{hooks_block}\n\n{script.body}"
    prompt = template.replace("{NICHE_STYLE_GUIDE}", style_guide).replace("{SCRIPT}", script_text)

    response = _await_manual_response(prompt, "metadata", script.topic_id, config)
    bundle = _parse_metadata_response(response, script.topic_id)
    _enforce_metadata_hard_rules(bundle, config)
    return bundle


def generate_thumbnail(metadata: MetadataBundle, config: dict) -> Path:
    """Stage 8.5: render a custom 1080×1920 PNG thumbnail using a named pattern.

    Reads `metadata.cover_pattern_name` (one of `prompts/library/thumbnail_patterns.md`'s
    8 names; defaults to big_text_claim). Falls back to big_text_claim with a warning
    when the named pattern isn't yet implemented in `tools/make_thumbnail.py`.

    Output: `<channel_root>/04_renders/_thumbnails/<topic_id>_thumbnail.png`. The
    operator uploads this to YouTube Studio manually as part of the Shorts upload
    flow (since manual_youtube_upload is true).
    """
    # Local import keeps the dependency optional — Pillow is already in requirements.
    from tools import make_thumbnail

    out_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_thumbnails"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{metadata.topic_id}_thumbnail.png"

    text_overlay = metadata.cover_text.strip()
    if not text_overlay:
        log.warning(
            "metadata.cover_text is empty for %s — thumbnail will render with topic_id as a fallback",
            metadata.topic_id,
        )
        text_overlay = metadata.topic_id.upper()

    log.info(
        "thumbnail: rendering %s pattern with overlay %r",
        metadata.cover_pattern_name, text_overlay,
    )
    make_thumbnail.render(metadata.cover_pattern_name, text_overlay, out_path)
    return out_path


def schedule_publishing(
    variants: dict[Platform, Path],
    metadata: MetadataBundle,
    config: dict,
) -> None:
    """Stage 9: push to Metricool/Buffer for IG+TT. YouTube Shorts upload stays manual."""
    if config["publishing"].get("kill_switch", False):
        log.warning("KILL SWITCH ENGAGED — refusing to schedule publishing.")
        raise RuntimeError("publishing.kill_switch is true; refusing to schedule.")
    raise NotImplementedError(
        "Phase 2: POST to Metricool/Buffer API for tiktok+instagram. "
        "YouTube remains manual — operator uploads via Studio web/mobile."
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_for_topic(topic: TopicJob, config: dict) -> None:
    """Run the full pipeline for one topic. Halts at each sacred gate.

    Each stage is independently callable; this function exists to document the canonical order.
    Failures bubble up — no silent except-pass. Re-run from a later stage by invoking that
    stage's function directly with cached inputs.
    """
    log.info("starting pipeline for topic_id=%s", topic.id)

    script = generate_script(topic, config)
    log.info("script drafted: %d words, %d hooks", script.word_count, len(script.hook_variants))

    script = evaluate_script_quality(script, config)  # may raise QualityCheckFailed (a halt)

    report = fact_check_script(script, config)
    log.info("fact-check: %d claims, %d unresolved", len(report.claims), report.unresolved_count)

    script = await_fact_check_resolution(script, report, config)  # halts pipeline

    assets = fetch_assets(script, config)
    log.info("assets: %d clips, %d images", len(assets.clips), len(assets.images))

    vo_path = generate_voiceover(script, config)

    # Stage 7.5: two-pass loudnorm so the VO lands at the configured target
    # before captions and render. In-place rewrite of vo_path.
    vo_path = _normalize_vo_loudness(vo_path, config)

    # Post-Stage-7.5: the normalized VO is the final spoken artifact, so measure
    # its real duration and WARN (never halt) if it overruns the <=38s breakout
    # target. Warn-only by design — unattended /start -auto must not deadlock.
    _warn_if_vo_over_duration(vo_path, script, config)

    from tools.gpu_lock import gpu_lock_from_config

    # GPU semaphore (H-4, 2026-06-19 review): the two parallel /start -auto
    # topics each run whisper-large-v3 (captions) + NVENC (render). On the 6 GB
    # card those can collide and OOM/thrash. This machine-wide lock serializes
    # the GPU span (captions through render) across topics; the non-GPU stages
    # still overlap. No-op when render.gpu_lock_enabled is false (default) or
    # uncontended (a single video acquires it instantly).
    with gpu_lock_from_config(config, script.topic_id):
        captions_path = generate_captions(vo_path, script, config)

        # Stage 8.5 (Sprint 5 Layer 3): caption-side template-artifact double-check.
        # Catches the _12_002 failure class: edge-TTS spoke a literal template
        # annotation aloud, word-pop transcribed it back into the .ass file, and
        # without this gate the artifact would have been burned into the render.
        # Halts via PipelineQAFailed — same semantics as Stage 11.
        _check_captions_for_template_artifacts(captions_path, script.topic_id)

        # Stage 6 + 10.1: render, then deep-decode integrity check, with a single
        # libx264 retry on integrity failure (NVENC silent-corruption defense).
        #
        # The render is held under a RenderLock (heartbeat + atomic `.part` write).
        # If a previous invocation's render was detached/killed mid-encode — the
        # recurring cycle-24 / 2026-06-05 sub-agent-backgrounding footgun — its lock
        # is detected here as orphaned (stale heartbeat or dead PID), logged loudly,
        # its partial `.part` cleaned, and the render resumes cleanly with no manual
        # apex rescue and no silent freeze. A genuinely-live concurrent render is
        # waited on (then stolen if it goes stale) rather than double-encoded. The
        # lock records the launching argv so `tools/render_reaper.py` can replay an
        # orphaned render foreground. Keeps idempotent-resume + config isolation +
        # the NVENC→libx264 fallback intact.
        from tools.render_lock import RenderLock

        master_out = _master_output_path(config, script.topic_id)

        # Re-render skip-guard (CODE_AUDIT item (e)): if an approved, structurally
        # intact master already exists, skip the encode entirely. Pure early-exit
        # ABOVE the RenderLock so a no-op re-run doesn't even contend the render
        # lock. Downstream Stage 10.1 / 11 still re-verify the master/variants, so
        # this only ever avoids redundant work — it never promotes a file. Delete
        # the marker or the master to force a re-render.
        if _approved_master_intact(master_out, config):
            log.info(
                "render skipped: approved master intact (%s); delete the marker or master "
                "to force re-render",
                master_out,
            )
            master_path = master_out
        else:
            with RenderLock(
                master_out,
                topic_id=script.topic_id,
                argv=list(sys.argv),
                cwd=os.getcwd(),
                executable=sys.executable,
            ):
                master_path = _render_with_integrity_retry(
                    script, assets, vo_path, captions_path, config,
                )

    await_final_qa(master_path, config)  # halts pipeline

    variants = generate_variants(master_path, config)

    # Stage 11: per-variant integrity + prepublish QA (R7 / T7). Aggregates
    # failures across all variants into a single PipelineQAFailed so the
    # operator sees every problem at once.
    _run_prepublish_qa(script.topic_id, variants, captions_path, config)

    metadata = generate_metadata(script, config)
    thumbnail_path = generate_thumbnail(metadata, config)
    log.info("thumbnail rendered: %s", thumbnail_path)
    schedule_publishing(variants, metadata, config)
    log.info("topic_id=%s scheduled successfully", topic.id)


# ---------------------------------------------------------------------------
# Stage 10.1 / 11 helpers — integrity + prepublish QA
# ---------------------------------------------------------------------------


def _approved_master_intact(master_out: Path, config: dict) -> bool:
    """Return True iff an approved, structurally-intact master already exists.

    Used as a pure, read-only pre-check ABOVE the RenderLock so a re-run of an
    already-finished topic skips the ~1-2 min re-encode. All three must hold:
      1. the final master file exists,
      2. its gate-3 marker `<stem>_QA_APPROVED.marker` exists (same construction
         as `await_final_qa`), and
      3. `tools.media_integrity.check_integrity` passes on the master (so a
         truncated/bit-rotten cold-archive master — the integrity_sweep failure
         class — is NOT silently accepted as a skip).

    Gated by `config.render.skip_if_approved_master` (default False here for
    legacy safety; production config.yaml(.template) turns it ON). Never raises:
    an integrity failure (or any probe error) returns False so the caller falls
    through to a normal re-render rather than halting. This function ONLY ever
    AVOIDS work — it never promotes a file or mutates state.
    """
    if not bool(config.get("render", {}).get("skip_if_approved_master", False)):
        return False
    if not master_out.exists():
        return False
    marker = master_out.parent / f"{master_out.stem}_QA_APPROVED.marker"
    if not marker.exists():
        return False
    from tools.media_integrity import MediaIntegrityError, check_integrity

    try:
        check_integrity(master_out)
    except (FileNotFoundError, MediaIntegrityError) as exc:
        log.warning(
            "render skip-guard: master %s has gate-3 marker but FAILED integrity (%s) "
            "— will re-render",
            master_out.name, exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — skip-guard must never break the render path
        log.warning(
            "render skip-guard: integrity probe errored on %s (%s) — will re-render",
            master_out.name, exc,
        )
        return False
    return True


def _check_media_integrity(video_path: Path, *, stage: str) -> dict:
    """Stage 10.1 / 11.2: structural-integrity gate on a rendered video.

    Wraps `tools.media_integrity.check_integrity` and translates
    `MediaIntegrityError` / `FileNotFoundError` into `IntegrityCheckFailed`
    (a `PipelineHalted` subclass) so the existing `main()` halt handler picks
    it up cleanly. Returns the diagnostic dict on success.
    """
    from tools.media_integrity import MediaIntegrityError, check_integrity

    try:
        info = check_integrity(video_path)
    except FileNotFoundError as exc:
        raise IntegrityCheckFailed(video_path, str(exc), stage=stage) from exc
    except MediaIntegrityError as exc:
        raise IntegrityCheckFailed(video_path, str(exc), stage=stage) from exc
    log.info(
        "%s integrity OK: %s (size=%dB, duration=%.2fs, codec=%s, channels=%s)",
        stage,
        video_path.name,
        int(info.get("size_bytes", 0)),
        float(info.get("duration_s", 0.0)),
        info.get("video_codec"),
        info.get("audio_channels"),
    )
    return info


def _check_captions_for_template_artifacts(captions_path: Path, topic_id: str) -> None:
    """Stage 8.5 (Sprint 5 Layer 3): caption-side template-artifact gate.

    Runs `tools.caption_artifact_check.check_captions_for_artifacts` on the
    rendered .ass file BEFORE the render step burns it into the master. On
    FAIL raises `PipelineQAFailed` — same halt semantics as Stage 11 — so the
    operator sees one consistent failure shape regardless of which layer
    caught the regression.

    See `tools/caption_artifact_check.py` for the _12_002 regression context.
    """
    from tools.caption_artifact_check import check_captions_for_artifacts
    from tools.prepublish_qa import PipelineQAFailed

    log.info("Stage 8.5: caption artifact check on %s", captions_path.name)
    result = check_captions_for_artifacts(captions_path)
    if result.ok:
        log.info("Stage 8.5 OK on %s (%s)", captions_path.name, result.actual)
        return

    log.error(
        "Stage 8.5 FAIL on %s: %s", captions_path.name, result.message,
    )
    raise PipelineQAFailed(
        failures={
            result.check_id: {
                "name": result.name,
                "expected": result.expected,
                "actual": result.actual,
                "message": result.message,
            }
        },
        video_path=captions_path,
        topic_id=topic_id,
    )


def _run_prepublish_qa(
    topic_id: str,
    variants: dict[Platform, Path],
    captions_path: Path,
    config: dict,
) -> None:
    """Stage 11: per-variant `media_integrity` + `prepublish_qa.check_variant`.

    Honors `config.prepublish_qa.enabled` (default True) and
    `config.prepublish_qa.check_cited_observation` (default False — narrow R6
    scope). Aggregates every failure across all variants and raises a single
    `PipelineQAFailed` carrying the merged failures table so the operator
    sees every problem at once.
    """
    qa_cfg = config.get("prepublish_qa", {}) or {}
    if qa_cfg.get("enabled", True) is False:
        # WORKFLOW_AUDIT_2026-05-16 H2: skipping Stage 11 is a gate-bypass. In
        # /start -auto, gate-3 auto-approve is conditional on Stage 11 passing;
        # a skip + return previously satisfied that condition silently. Require
        # an explicit second flag `allow_disable_in_production: true` so the
        # disable path cannot be enabled by a one-line config edit during
        # debugging that's forgotten before the next auto-run.
        if qa_cfg.get("allow_disable_in_production", False) is True:
            log.warning(
                "prepublish_qa.enabled=false AND allow_disable_in_production=true "
                "— SKIPPING Stage 11 QA gate (unit-test / explicit-bypass path)"
            )
            return
        log.error(
            "prepublish_qa.enabled=false without prepublish_qa."
            "allow_disable_in_production=true — refusing to skip Stage 11. "
            "Set both flags explicitly to bypass the gate, or flip enabled "
            "back to true."
        )
        raise RuntimeError(
            "Stage 11 (prepublish_qa) cannot be skipped: "
            "prepublish_qa.enabled=false requires "
            "prepublish_qa.allow_disable_in_production=true. "
            "This guard exists because /start -auto's gate-3 auto-approve "
            "is conditional on Stage 11 passing."
        )

    from tools.prepublish_qa import PipelineQAFailed, check_variant

    check_cited_obs = bool(qa_cfg.get("check_cited_observation", False))
    channel_root = Path(config["paths"]["channel_root"])

    # Config-overridable QA thresholds. Long-form sets landscape resolution, a long
    # duration ceiling, and a lower caption-density floor (sparse lower-thirds vs the
    # dense Shorts word-pop). Absent => check_variant's Shorts defaults (1080x1920 /
    # 180s / word-pop density) apply, so the Shorts gate stays byte-identical.
    threshold_kwargs: dict = {}
    if qa_cfg.get("expected_resolution") is not None:
        er = qa_cfg["expected_resolution"]
        threshold_kwargs["expected_resolution"] = (int(er[0]), int(er[1]))
    if qa_cfg.get("max_duration_s") is not None:
        threshold_kwargs["max_duration_s"] = float(qa_cfg["max_duration_s"])
    if qa_cfg.get("min_caption_density") is not None:
        threshold_kwargs["min_caption_density"] = float(qa_cfg["min_caption_density"])

    aggregated_failures: dict[str, dict[int, dict[str, str]]] = {}
    for platform, video_path in variants.items():
        log.info("Stage 11: prepublish QA for %s -> %s", platform, video_path.name)
        # 11.2: integrity first — the prepublish gate also runs T2 internally,
        # but a fast pre-check makes the pipeline-side error message clearer
        # for truncated-file cases.
        _check_media_integrity(video_path, stage=f"variant:{platform}")

        report = check_variant(
            video_path,
            captions_path=captions_path,
            channel_root=channel_root,
            check_cited_observation=check_cited_obs,
            **threshold_kwargs,
        )
        if not report.ok:
            failures = report.failures_dict()
            aggregated_failures[platform] = failures
            log.error(
                "Stage 11 FAIL on %s: %d check(s) failed -> %s",
                platform, len(failures), sorted(failures.keys()),
            )
        else:
            log.info("Stage 11 OK on %s for %s (%d checks ran)", platform, topic_id, report.checks_run)

    if aggregated_failures:
        # Flatten: prefix each check_id with the platform so the operator can
        # tell apart yt:#7 from tt:#7 in the halt message. PipelineQAFailed's
        # failures dict is keyed by int — encode platform into the dict via
        # a synthetic offset (1000 * platform_idx) so int keys stay unique.
        merged: dict[int, dict[str, str]] = {}
        for idx, (platform, fails) in enumerate(sorted(aggregated_failures.items())):
            for cid, info in fails.items():
                tagged = dict(info)
                tagged["platform"] = platform
                merged[idx * 1000 + cid] = tagged
        raise PipelineQAFailed(
            failures=merged,
            topic_id=topic_id,
        )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _build_run_id(topic_id: str | None) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{topic_id}" if topic_id else stamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShadowVerse pipeline orchestrator")
    parser.add_argument("--topic-id", required=True, help="ID of the approved topic, e.g. 2026-05-05_001")
    parser.add_argument("--topic", required=True, help="One-sentence topic")
    parser.add_argument("--angle", required=True, help="One-sentence angle")
    parser.add_argument("--hook", required=True, help="Hook concept")
    parser.add_argument("--config", default=None, help="Path to config.yaml (default: alongside this file)")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    validate_config(config)  # M4: fail fast on missing keys before any stage runs
    setup_logging(config, _build_run_id(args.topic_id))

    topic = TopicJob(id=args.topic_id, topic=args.topic, angle=args.angle, hook_concept=args.hook)
    try:
        run_for_topic(topic, config)
    except PipelineHalted as halt:
        # Either ManualLLMHalt (LLM stage waiting for response file) or HumanReviewRequired
        # (sacred gate waiting for review). Both are intentional pauses, exit code 3.
        log.info("pipeline halted: %s", type(halt).__name__)
        print(str(halt), file=sys.stderr)
        return 3
    except NotImplementedError as e:
        log.error("pipeline halted at unimplemented stage: %s", e)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
