"""Unit tests for tools.longform_assets pure helpers + the [CHAPTER:] strip."""
from pipeline import _strip_visual_directions
from tools.longform_assets import _distribute_durations, _enhance_prompt, _route_source


def test_route_source_diagram_vs_sdxl():
    assert _route_source("diagram — timeline of AI milestones") == "diagram"
    assert _route_source("diagram - before vs after") == "diagram"
    assert _route_source("Diagram — anything") == "diagram"
    assert _route_source("a person at a laptop, surprised") == "sdxl"
    assert _route_source("abstract neural network of light") == "sdxl"


def test_distribute_durations_sums_to_total():
    durs = _distribute_durations(10, 50.0)
    assert len(durs) == 10
    assert abs(sum(durs) - 50.0) < 0.01
    assert all(d > 0 for d in durs)


def test_distribute_durations_over_cued_still_sums_exactly():
    # over-cued: 40 beats over 50s -> 1.25s each, must STILL sum to 50 (no clamp overflow)
    durs = _distribute_durations(40, 50.0)
    assert len(durs) == 40
    assert abs(sum(durs) - 50.0) < 0.01
    assert all(0 < d < 2.0 for d in durs)  # genuinely short, not clamped up to 2.5


def test_distribute_durations_typical_longform():
    # ~130 cues over ~11 min -> ~5s/beat, sums exactly
    durs = _distribute_durations(130, 660.0)
    assert abs(sum(durs) - 660.0) < 0.01
    assert 4.0 < (sum(durs) / len(durs)) < 6.0


def test_distribute_durations_empty():
    assert _distribute_durations(0, 10.0) == []


def test_enhance_prompt_appends_style():
    p = _enhance_prompt("a person at a laptop")
    assert p.startswith("a person at a laptop")
    assert "cinematic" in p and "photorealistic" in p


def test_strip_chapter_and_broll_markers():
    text = "Open here. [CHAPTER: The first sign] The agent [B-ROLL: a laptop] kept going."
    out = _strip_visual_directions(text)
    assert "CHAPTER" not in out
    assert "B-ROLL" not in out
    assert "Open here." in out and "kept going." in out


def test_strip_chapter_is_noop_without_markers():
    # Shorts text (no [CHAPTER:]) is unaffected beyond the existing B-ROLL/VERIFY strip.
    text = "Claude just shipped. [B-ROLL: phone screen] It surprised everyone."
    out = _strip_visual_directions(text)
    assert out == "Claude just shipped. It surprised everyone."
