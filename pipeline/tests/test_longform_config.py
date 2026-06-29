"""Unit tests for tools.longform_config.build_longform_config + is_longform."""
import copy

from tools.longform_config import build_longform_config, is_longform

BASE = {
    "longform": {"enabled": False, "word_count_min": 1400,
                 "word_count_max": 2000, "max_duration_s": 900},
    "render": {"resolution": [1080, 1920], "framerate": 30, "video_codec": "libx264"},
    "script_quality": {"word_count_min": 80, "word_count_max": 98, "duration_warn_s": 38.0,
                       "anchor_gate_enabled": True, "modal_ban_enabled": True},
    "captions": {"style": "word_pop", "whisper_model": "large-v3"},
    "prepublish_qa": {"enabled": True},
    "variants": {},
}


def test_base_config_never_mutated():
    base = copy.deepcopy(BASE)
    build_longform_config(base)
    assert base == BASE, "build_longform_config must deep-copy, never mutate the base"
    # the Shorts-critical values are intact
    assert base["render"]["resolution"] == [1080, 1920]
    assert base["captions"]["style"] == "word_pop"
    assert base["script_quality"]["word_count_max"] == 98


def test_longform_overrides_applied():
    cfg = build_longform_config(BASE)
    assert is_longform(cfg)
    assert cfg["render"]["resolution"] == [1920, 1080]
    assert cfg["script_quality"]["word_count_min"] == 1400
    assert cfg["script_quality"]["word_count_max"] == 2000
    assert cfg["script_quality"]["duration_warn_s"] == 720.0
    # the quality-discipline gates are KEPT, not removed
    assert cfg["script_quality"]["anchor_gate_enabled"] is True
    assert cfg["script_quality"]["modal_ban_enabled"] is True
    assert cfg["captions"]["style"] == "lower_third"
    assert cfg["prompts"]["script"] == "03_script_generation_longform"
    assert cfg["prompts"]["metadata"] == "06_metadata_generation_longform"
    assert cfg["prompts"]["idea"] == "02_idea_generation_longform"
    assert cfg["tracks"]["longform"]["idea_prompt"] == "02_idea_generation_longform"
    assert cfg["prepublish_qa"]["expected_resolution"] == [1920, 1080]
    assert cfg["prepublish_qa"]["max_duration_s"] == 900.0
    assert cfg["prepublish_qa"]["min_caption_density"] == 0.12
    assert cfg["variants"]["platforms"] == ["youtube"]


def test_is_longform_false_for_shorts():
    assert not is_longform(BASE)
    assert not is_longform({})
    assert not is_longform(None)
    assert not is_longform({"longform": {"enabled": False}})


def test_build_is_idempotent():
    once = build_longform_config(BASE)
    twice = build_longform_config(once)
    assert once == twice
