"""Build the effective long-form config from the base (Shorts) config.

The /longform -auto command loads config.yaml, runs `build_longform_config()` on it,
writes the result to an isolated config path, and passes it to the pipeline via
--config. The pipeline stages then read long-form values (landscape resolution,
long word bands, lower-third captions, long-form prompts, YouTube-only variants,
landscape QA thresholds) without any change to how they read config.

The base config is NEVER mutated — `build_longform_config` deep-copies. With the
base config.yaml (longform.enabled: false) the Shorts pipeline is byte-identical.
"""
from __future__ import annotations

import copy

# Long-form prompt template names (the variants written for the deep-dive track).
LONGFORM_PROMPTS = {
    "idea": "02_idea_generation_longform",
    "script": "03_script_generation_longform",
    "metadata": "06_metadata_generation_longform",
}
LANDSCAPE_RES = [1920, 1080]


def is_longform(config: dict | None) -> bool:
    """True when this config is in long-form mode (the stage guards check this)."""
    return bool((config or {}).get("longform", {}).get("enabled"))


def build_longform_config(base: dict) -> dict:
    """Return a deep copy of `base` with the long-form overrides applied.

    Tunables (word bands, max_duration, asset paths) are read from base['longform']
    when present so the operator can adjust them in config.yaml; everything else is a
    long-form constant. Idempotent: building twice yields the same effective config.
    """
    cfg = copy.deepcopy(base)
    lf = dict(cfg.get("longform") or {})
    lf["enabled"] = True
    cfg["longform"] = lf

    # Landscape master.
    cfg.setdefault("render", {})["resolution"] = list(lf.get("resolution", LANDSCAPE_RES))

    # Long-form script length + duration warn (keeps the anchor/modal gates — discipline,
    # not removal — but lifts the Shorts word ceiling so a 1500-word body doesn't halt).
    sq = cfg.setdefault("script_quality", {})
    sq["word_count_min"] = int(lf.get("word_count_min", 1400))
    sq["word_count_max"] = int(lf.get("word_count_max", 2000))
    sq["duration_warn_s"] = float(lf.get("duration_warn_s", 720.0))

    # Long-form prompt variants (script + metadata selected via config.prompts;
    # idea selected via the longform track too — set both for belt-and-suspenders).
    pr = cfg.setdefault("prompts", {})
    pr["idea"] = LONGFORM_PROMPTS["idea"]
    pr["script"] = LONGFORM_PROMPTS["script"]
    pr["metadata"] = LONGFORM_PROMPTS["metadata"]
    cfg.setdefault("tracks", {}).setdefault("longform", {})["idea_prompt"] = LONGFORM_PROMPTS["idea"]

    # Lower-third captions (legible at 16:9, unlike the 96px Shorts word-pop).
    cfg.setdefault("captions", {})["style"] = "lower_third"

    # Landscape + long-duration prepublish-QA thresholds (plumbed into check_variant).
    qa = cfg.setdefault("prepublish_qa", {})
    qa["expected_resolution"] = list(LANDSCAPE_RES)
    qa["max_duration_s"] = float(lf.get("max_duration_s", 900.0))
    # Lower-thirds emit ~1 caption event per line (far sparser than the per-word
    # Shorts word-pop), so the density floor must drop or Stage-11 would fail it.
    qa["min_caption_density"] = float(lf.get("min_caption_density", 0.12))

    # YouTube-only — the long-form track does not cross-post.
    cfg.setdefault("variants", {})["platforms"] = ["youtube"]

    return cfg
