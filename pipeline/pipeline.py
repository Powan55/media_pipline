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
from datetime import datetime
from pathlib import Path
from typing import Literal

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
    body: str                                # 100–150 words with [B-ROLL: ...] cues inline
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


def _extract_broll(body: str) -> tuple[list[str], str]:
    """Extract `[B-ROLL: ...]` cues from body, returning (cues, body_without_cues).

    Bracket-depth-aware so cues can contain nested `[VERIFY: ...]` markers, e.g.
        [B-ROLL: Cursor settings showing the "max session length" [VERIFY: name] field]
    A regex that stops at the first `]` would truncate at the inner VERIFY bracket.
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


def _parse_script_response(response: str, topic_id: str) -> ScriptDraft:
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
    hook_matches = list(_HOOK_RE.finditer(response))
    if len(hook_matches) != 3:
        raise ValueError(
            f"Expected 3 lines of the form 'HOOK_A:' / 'HOOK_B:' / 'HOOK_C:', "
            f"found {len(hook_matches)}. Edit script_RESPONSE.txt and re-run."
        )
    hooks: list[str] = []
    formulas: list[str] = []
    for m in hook_matches:
        cleaned, formula = _strip_hook_formula(m.group(2).strip())
        hooks.append(cleaned)
        formulas.append(formula)

    fc_marker = _FACT_CHECK_MARKER_RE.search(response)
    if not fc_marker:
        raise ValueError(
            "Missing 'FACT_CHECK_QUEUE' section. The LLM must list every claim to be "
            "fact-checked as a bulleted list under that header. Edit script_RESPONSE.txt "
            "and re-run."
        )

    # Body is everything between the end of the last hook line and the FACT_CHECK_QUEUE marker.
    body = response[hook_matches[-1].end():fc_marker.start()].strip()
    body = re.sub(r"\n+-{3,}\s*$", "", body).strip()  # strip trailing horizontal rule
    # Some LLMs emit `CHOSEN HOOK: HOOK_X` and/or a `SCRIPT:` divider between the
    # hooks block and the body. They are not in the prompt template, but if present
    # they leak into TTS as spoken text (caught at gate 3 on 2026-05-07_003 / _004).
    # Strip these meta-marker lines defensively.
    body = re.sub(
        r"^[ \t]*(?:CHOSEN[ \t]+HOOK[ \t]*:[^\n]*|SCRIPT[ \t]*:[ \t]*)\n",
        "",
        body,
        flags=re.IGNORECASE | re.MULTILINE,
    ).strip()
    if not body:
        raise ValueError(
            "Body is empty. The LLM must write a 100–150 word script between the hooks "
            "and the FACT_CHECK_QUEUE section. Edit script_RESPONSE.txt and re-run."
        )

    broll_cues, body_no_broll = _extract_broll(body)

    # If a QUALITY_SCORES marker exists, the fact-check section ends there.
    quality_marker = _QUALITY_SCORES_MARKER_RE.search(response, pos=fc_marker.end())
    if quality_marker:
        fc_section = response[fc_marker.end():quality_marker.start()]
        quality_section = response[quality_marker.end():]
    else:
        fc_section = response[fc_marker.end():]
        quality_section = ""
    fact_check_queue = [m.group(1).strip() for m in _BULLET_RE.finditer(fc_section)]
    quality_scores, quality_rationale = (
        _parse_quality_scores(quality_section) if quality_section else ({}, "")
    )

    word_count = len(body_no_broll.split())
    if word_count < 80 or word_count > 200:
        log.warning(
            "script body is %d words; prompt asked for 100–150. Operator should review.",
            word_count,
        )

    return ScriptDraft(
        topic_id=topic_id,
        hook_variants=hooks,
        hook_formulas=formulas,
        body=body,
        broll_cues=broll_cues,
        fact_check_queue=fact_check_queue,
        word_count=word_count,
        quality_scores=quality_scores,
        quality_rationale=quality_rationale,
    )


def generate_script(topic: TopicJob, config: dict) -> ScriptDraft:
    """Stage 1: LLM produces 3 hook variants + a 100–150 word script with [B-ROLL] cues.

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

    template = load_prompt("03_script_generation", config)
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
    return _parse_script_response(response, topic.id)


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


def evaluate_script_quality(script: ScriptDraft, config: dict) -> ScriptDraft:
    """Stage 1.5: read the self-scored quality fields and gate on them.

    Behavior controlled by config.script_quality:
      - min_score (float, default 0.50): the weighted-total threshold for "publish"
      - enforce_min_score (bool, default false): when true, halt below threshold;
        when false, log a warning but pass through

    Pass-through cases (no halt regardless of enforce flag):
      - Response was parsed but contained no QUALITY_SCORES section (legacy / older
        prompt). Logs a warning so the operator knows the gate is a no-op.
      - All dimensions present but the operator hasn't enabled enforcement yet.

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

    if not script.quality_scores:
        log.warning(
            "script quality gate: no QUALITY_SCORES section in response for %s — "
            "gate is a no-op. Re-issue script_RESPONSE.txt with the section to enable scoring.",
            script.topic_id,
        )
        return script

    # Equal-weighted mean across the canonical dimensions; ignore extras the LLM may add.
    present = [d for d in SCRIPT_QUALITY_DIMENSIONS if d in script.quality_scores]
    if not present:
        log.warning(
            "script quality gate: QUALITY_SCORES section had no recognized dimensions for %s. "
            "Expected any of: %s",
            script.topic_id, ", ".join(SCRIPT_QUALITY_DIMENSIONS),
        )
        return script

    total = sum(script.quality_scores[d] for d in present) / len(present)
    log.info(
        "script quality: %s weighted_total=%.3f over %d/%d dimensions (min=%.2f, enforce=%s)",
        script.topic_id, total, len(present), len(SCRIPT_QUALITY_DIMENSIONS), min_score, enforce,
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
        for j, cell in enumerate(_cells(raw_rows[header_idx])):
            key = _classify_header_cell(cell)
            if key is not None:
                col_map[key] = j

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

    template = load_prompt("05_fact_check", config)

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
    script_path: Path, topic_id: str | None = None
) -> None:
    """Sprint 5 Layer-2 safety net: halt the pipeline if ``script_FINAL.txt`` carries template artifacts.

    Imports `tools.prepublish_qa` lazily because that module is itself
    `__init__`-importable but has a heavier transitive-import footprint
    (ffprobe helpers, etc.) and pipeline.py is loaded by every CLI entry.

    Raises:
        PipelineQAFailed: any artifact match, missing file, or unreadable
            script. Inherits from `PipelineHalted` so the existing halt /
            resume plumbing treats this like any other sacred-gate stop.
    """
    from tools.prepublish_qa import (  # local import to avoid CLI startup cost
        PipelineQAFailed,
        SCRIPT_ARTIFACT_CHECK_ID,
        check_script,
    )

    result = check_script(script_path)
    if result.ok:
        return
    raise PipelineQAFailed(
        failures={
            SCRIPT_ARTIFACT_CHECK_ID: {
                "name": result.name,
                "expected": result.expected,
                "actual": result.actual,
                "message": result.message,
            }
        },
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
        _scan_script_for_artifacts_or_halt(final_path, script.topic_id)
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
        _scan_script_for_artifacts_or_halt(final_path, script.topic_id)
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


def _search_pexels_video(query: str, api_key: str) -> dict | None:
    """Search Pexels videos (portrait, HD+). Returns dict with url/source/license/page or None."""
    import requests
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 5, "orientation": "portrait"},
            headers={"Authorization": api_key},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("pexels search failed for %r: %s", query, e)
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
    import requests
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": query, "per_page": 5, "video_type": "all"},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("pixabay search failed for %r: %s", query, e)
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
    """Download a video clip to `dest`. Skip if already present. Returns True on success."""
    import requests
    if dest.exists() and dest.stat().st_size > 0:
        log.info("clip already cached: %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return True
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        log.info("downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return True
    except Exception as e:
        log.warning("download failed for %s → %s: %s", url, dest.name, e)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def fetch_assets(script: ScriptDraft, config: dict) -> AssetBundle:
    """Stage 3: parse B-ROLL cues, search Pexels then Pixabay (per `assets.preferred_stock_provider`),
    download matching portrait clips. Flux fallback is deferred (config.assets.flux_fallback_enabled
    is honored only after ComfyUI is set up — for now, missed cues are logged and skipped).
    """
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

    if not clips:
        raise RuntimeError(
            "No b-roll clips successfully fetched for any cue. Check API keys and "
            "network access. Cues attempted: " + str(len(script.broll_cues))
        )

    log.info("fetched %d/%d cues (%.1f MB total)",
             len(clips), len(script.broll_cues),
             sum(p.stat().st_size for p in clips) / 1e6)

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
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


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
    mp3_path = audio_dir / f"{topic_id}_vo.mp3"
    wav_path = audio_dir / f"{topic_id}_vo.wav"

    async def synth() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(mp3_path))

    log.info("edge-tts: synthesizing %d chars with voice=%s rate=%s", len(text), voice, rate)
    asyncio.run(synth())
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
    os.replace(tmp_path, vo_path)
    log.info(
        "Stage 7.5 done: input_i=%.2f LUFS input_tp=%.2f dBTP target_offset=%.2f",
        measurements.get("input_i", 0.0),
        measurements.get("input_tp", 0.0),
        measurements.get("target_offset", 0.0),
    )
    return vo_path


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
    if style != "word_pop":
        raise ValueError(
            f"captions.style={style!r} is not a valid value; expected 'word_pop' or 'legacy'."
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


def render_master(
    script: ScriptDraft,
    assets: AssetBundle,
    vo_path: Path,
    captions_path: Path,
    config: dict,
) -> Path:
    """Stage 6: FFmpeg assembly into a 1080×1920 H.264 master.

    Strategy:
      - VO audio is the timing spine — total master duration = VO duration.
      - B-roll clips are cycled in order, each segment = VO_duration / N_clips.
      - Each clip is `stream_loop`-ed and trimmed to its segment so short clips repeat.
      - Each clip is scaled (cover) and center-cropped to 1080×1920.
      - The concat'd video gets the ASS subtitles filter applied for burned-in captions.
      - Final mux: video + VO audio, encoded H.264 (NVENC if config.render.hardware_accel='nvenc').

    Windows path note: the ASS/subtitles filter struggles with drive-letter colons in
    its filename argument. We work around it by running ffmpeg with cwd set to the
    captions file's directory and passing a relative basename to the `ass` filter.
    """
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

    # 5. Output path under 04_renders\_final_master\
    out_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_final_master"
    out_dir.mkdir(parents=True, exist_ok=True)
    master_path = out_dir / f"{script.topic_id}_master.mp4"

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
        .output(concat_v, audio, str(master_path), **out_kwargs)
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
        # Retry once with libx264 if NVENC was the issue
        if use_nvenc and ("nvenc" in (result.stderr or "").lower() or "no nvenc" in (result.stderr or "").lower()):
            log.warning("NVENC failed; retrying with libx264")
            out_kwargs["vcodec"] = "libx264"
            out_kwargs["preset"] = "fast"
            cmd_args = (
                ffmpeg
                .output(concat_v, audio, str(master_path), **out_kwargs)
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

    size_mb = master_path.stat().st_size / 1e6
    log.info("master rendered: %s (%.1f MB, %.1fs)", master_path.name, size_mb, audio_duration)
    return master_path


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

    log.info("variants: TT (0.3s fade-in)")
    _fade_variant(tt_path, 0.3)
    log.info("variants: IG (0.5s fade-in)")
    _fade_variant(ig_path, 0.5)

    log.info(
        "variants done: YT=%.1fMB TT=%.1fMB IG=%.1fMB",
        yt_path.stat().st_size / 1e6,
        tt_path.stat().st_size / 1e6,
        ig_path.stat().st_size / 1e6,
    )
    return {"youtube": yt_path, "tiktok": tt_path, "instagram": ig_path}


_METADATA_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:##\s*)?(YOUTUBE\s*SHORTS|TIKTOK|INSTAGRAM\s*REELS|COVER(?:\s*/\s*THUMBNAIL(?:\s+CONCEPT)?)?)"
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

    yt = sections.get("YOUTUBE SHORTS", "")
    tt = sections.get("TIKTOK", "")
    ig = sections.get("INSTAGRAM REELS", "")
    cv = (
        sections.get("COVER", "")
        or sections.get("COVER / THUMBNAIL", "")
        or sections.get("COVER / THUMBNAIL CONCEPT", "")
    )

    missing = [name for name, text in [("YOUTUBE SHORTS", yt), ("TIKTOK", tt),
                                        ("INSTAGRAM REELS", ig), ("COVER", cv)] if not text]
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
    )


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

    template = load_prompt("06_metadata_generation", config)
    style_guide = load_style_guide(config)

    # The script content the metadata LLM sees: hooks + final body (post fact-check resolution).
    hooks_block = "\n".join(
        f"HOOK_{chr(65 + i)}: {h}" for i, h in enumerate(script.hook_variants)
    )
    script_text = f"{hooks_block}\n\n{script.body}"
    prompt = template.replace("{NICHE_STYLE_GUIDE}", style_guide).replace("{SCRIPT}", script_text)

    response = _await_manual_response(prompt, "metadata", script.topic_id, config)
    return _parse_metadata_response(response, script.topic_id)


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

    captions_path = generate_captions(vo_path, script, config)

    # Stage 8.5 (Sprint 5 Layer 3): caption-side template-artifact double-check.
    # Catches the _12_002 failure class: edge-TTS spoke a literal template
    # annotation aloud, word-pop transcribed it back into the .ass file, and
    # without this gate the artifact would have been burned into the render.
    # Halts via PipelineQAFailed — same semantics as Stage 11.
    _check_captions_for_template_artifacts(captions_path, script.topic_id)

    master_path = render_master(script, assets, vo_path, captions_path, config)
    log.info("master rendered: %s", master_path)

    # Stage 10.1: structural integrity gate on the master (catches truncated MP4s
    # and missing-moov failures before the operator's gate-3 review).
    _check_media_integrity(master_path, stage="post-master")
    # Canonical OK line — /start -auto greps the per-run log for this exact
    # prefix before dropping <topic_id>_master_QA_APPROVED.marker.
    log.info("Stage 10.1 OK on %s", master_path.name)

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
        )
        if not report.ok:
            failures = report.failures_dict()
            aggregated_failures[platform] = failures
            log.error(
                "Stage 11 FAIL on %s: %d check(s) failed -> %s",
                platform, len(failures), sorted(failures.keys()),
            )
        else:
            log.info("Stage 11 OK on %s (%d checks ran)", platform, report.checks_run)

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
