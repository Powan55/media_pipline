"""Policy engine: classify candidate changes and generate them from analysis.

Two responsibilities:
  1. ``classify(key)`` — is a config key SAFE_AUTO (reversible, non-sacred,
     bounded), PROPOSE (operator-gated via the weekly review), or LOCKED (sacred,
     never touched)? This is the authoritative table; the tuner trusts it.
  2. ``propose_changes(report, config)`` — turn reach-first findings into concrete
     candidate changes, each pre-classified.

v1 SAFE-AUTO set = the duration/word-budget knobs only (most mechanical, cheapest
rollback, directly tied to the <38s reach lever). Everything that touches scoring
structure, prompts, gates, visuals, or strategy is PROPOSE. tts.rate is
PROPOSE-only in v1 (retention-sensitive) until the loop has a track record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import AnalysisReport

SAFE_AUTO = "SAFE_AUTO"
PROPOSE = "PROPOSE"
LOCKED = "LOCKED"


@dataclass(frozen=True)
class KnobSpec:
    key: str
    klass: str
    value_type: type = float
    delta: float = 0.0            # signed step per cycle
    clamp: tuple[float, float] = (0.0, 0.0)
    min_sample: int = 5
    min_effect_pct: float = 5.0
    rollback_threshold_pct: float = -10.0


# Authoritative knob registry. Keys absent here default to PROPOSE (conservative).
KNOBS: dict[str, KnobSpec] = {
    # --- SAFE-AUTO (v1): duration / word-budget, tied to the <38s reach lever ---
    "script_quality.word_count_max": KnobSpec(
        "script_quality.word_count_max", SAFE_AUTO, int, delta=-3, clamp=(88, 110),
        min_sample=5, min_effect_pct=5.0, rollback_threshold_pct=-10.0),
    "script_quality.word_count_min": KnobSpec(
        "script_quality.word_count_min", SAFE_AUTO, int, delta=-3, clamp=(70, 85),
        min_sample=5, min_effect_pct=5.0, rollback_threshold_pct=-10.0),
    "script_quality.duration_warn_s": KnobSpec(
        "script_quality.duration_warn_s", SAFE_AUTO, float, delta=-1.0, clamp=(34.0, 42.0),
        min_sample=5, min_effect_pct=5.0, rollback_threshold_pct=-10.0),
    # --- PROPOSE (operator-gated) ---
    "tts.rate": KnobSpec("tts.rate", PROPOSE),                         # retention-sensitive
    "script_quality.anchor_gate_enabled": KnobSpec("script_quality.anchor_gate_enabled", PROPOSE),
    "script_quality.modal_ban_enabled": KnobSpec("script_quality.modal_ban_enabled", PROPOSE),
    "captions.style": KnobSpec("captions.style", PROPOSE),
    "tracks.dual_track_enabled": KnobSpec("tracks.dual_track_enabled", PROPOSE),
    # --- LOCKED (sacred — never applied or proposed) ---
    "fact_check.require_human_resolution": KnobSpec("fact_check.require_human_resolution", LOCKED),
    "fact_check.auto_resolve_gate_2": KnobSpec("fact_check.auto_resolve_gate_2", LOCKED),
    "publishing.human_qa_required": KnobSpec("publishing.human_qa_required", LOCKED),
    "publishing.kill_switch": KnobSpec("publishing.kill_switch", LOCKED),
}

# Scoring component weights are under a governance HOLD -> always PROPOSE.
_SCORING_WEIGHT_KEYS = frozenset({
    "niche_fit", "hook_strength", "specificity", "trend_signal", "verifiability",
    "broll_feasibility", "observation_availability", "anti_cannibalization",
})


def classify(key: str) -> str:
    """SAFE_AUTO / PROPOSE / LOCKED for a config key (defaults PROPOSE)."""
    if key in KNOBS:
        return KNOBS[key].klass
    if key in _SCORING_WEIGHT_KEYS or key.startswith("scoring_weights"):
        return PROPOSE
    return PROPOSE


@dataclass
class Candidate:
    key: str
    klass: str
    target_file: str
    current_value: object
    proposed_value: object
    category: str
    rationale: str
    evidence: str
    impact_effort: str
    sacred: bool
    traceability: str
    baseline_value: float | None = None
    baseline_n: int = 0
    spec: KnobSpec | None = field(default=None, repr=False)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get(config: dict, dotted: str):
    section, _, key = dotted.partition(".")
    return (config.get(section) or {}).get(key)


def _feature(report: AnalysisReport, dimension: str, value: str):
    for dr in report.dimensions:
        if dr.dimension == dimension:
            for fs in dr.features:
                if fs.value == value:
                    return fs
    return None


def propose_changes(report: AnalysisReport, config: dict) -> list[Candidate]:
    """Generate pre-classified candidate changes from a reach-first analysis."""
    candidates: list[Candidate] = []
    min_sample = report.min_sample
    baseline = report.cohort_median_views
    baseline_n = report.eligible_n

    # --- SAFE-AUTO: tighten word_count_max when <38s clearly out-reaches >=38s ---
    short = _feature(report, "duration_bucket", "<38s")
    longer = _feature(report, "duration_bucket", ">=38s")
    if (
        short and longer
        and short.n >= min_sample
        and short.median_views is not None
        and longer.median_views is not None
        and short.median_views >= longer.median_views * 1.15  # >=15% better reach
        and not short.retention_risk
    ):
        spec = KNOBS["script_quality.word_count_max"]
        current = _get(config, spec.key)
        if isinstance(current, (int, float)):
            proposed = int(_clamp(current + spec.delta, *spec.clamp))
            if proposed != current:
                candidates.append(Candidate(
                    key=spec.key, klass=SAFE_AUTO, target_file="config.yaml",
                    current_value=current, proposed_value=proposed,
                    category="Reach — duration",
                    rationale=(f"<38s videos reach {short.median_views:.0f} median views vs "
                               f"{longer.median_views:.0f} for >=38s (n={short.n}); tighten the "
                               f"spoken-word ceiling to push more videos under 38s."),
                    evidence=f"duration_bucket <38s median {short.median_views:.0f} > >=38s "
                             f"{longer.median_views:.0f} (evidence {short.evidence})",
                    impact_effort="high/low",
                    sacred=False,
                    traceability="analysis.duration_bucket",
                    baseline_value=baseline, baseline_n=baseline_n, spec=spec,
                ))

    # --- PROPOSE: top reach formula -> scoring weight nudge (HOLD, operator only) ---
    formula_dim = next((d for d in report.dimensions if d.dimension == "hook_formula"), None)
    if formula_dim and formula_dim.features:
        top = formula_dim.features[0]
        if top.n >= min_sample and baseline is not None and (top.median_views or 0) > baseline:
            candidates.append(Candidate(
                key="scoring_weights.observation_availability", klass=PROPOSE,
                target_file="scoring_weights.json",
                current_value=None, proposed_value="+0.02 (operator review)",
                category="Reach — topic scoring",
                rationale=(f"'{top.value}' is the top reach formula "
                           f"({top.median_views:.0f} median views, n={top.n}). Consider nudging the "
                           f"scoring weights that reward it. SCORING WEIGHTS ARE UNDER A GOVERNANCE "
                           f"HOLD — operator approval required."),
                evidence=f"hook_formula leader '{top.value}' median {top.median_views:.0f} > cohort {baseline:.0f}",
                impact_effort="high/med", sacred=True,
                traceability="analysis.hook_formula",
                baseline_value=baseline, baseline_n=baseline_n,
            ))

    # --- PROPOSE: anchor enforcement if anchored titles clearly out-reach ---
    anchor = _feature(report, "title_anchor", "anchor")
    no_anchor = _feature(report, "title_anchor", "no_anchor")
    if (anchor and no_anchor and anchor.n >= min_sample
            and anchor.median_views is not None and no_anchor.median_views is not None
            and anchor.median_views >= no_anchor.median_views * 1.25):
        candidates.append(Candidate(
            key="script_quality.anchor_gate_enabled", klass=PROPOSE, target_file="config.yaml",
            current_value=_get(config, "script_quality.anchor_gate_enabled"),
            proposed_value=True,
            category="Reach — title anchor",
            rationale=(f"Anchored titles reach {anchor.median_views:.0f} median views vs "
                       f"{no_anchor.median_views:.0f} unanchored (n={anchor.n}). Keep the anchor "
                       f"gate on / tighten the anchor rule in the script + metadata prompts."),
            evidence=f"title_anchor anchor {anchor.median_views:.0f} > no_anchor {no_anchor.median_views:.0f}",
            impact_effort="high/med", sacred=False,
            traceability="analysis.title_anchor",
            baseline_value=baseline, baseline_n=baseline_n,
        ))

    return candidates


__all__ = [
    "SAFE_AUTO", "PROPOSE", "LOCKED",
    "KnobSpec", "KNOBS", "Candidate", "classify", "propose_changes",
]
