"""Tests for dual-track idea generation (2026-06-21).

Covers the shared daily-dir name helper and the track-aware generate_ideas:
general-tech loads the general-tech prompt, isolates IO under a track-suffixed
dir, injects the apex-supplied discovered-stories slot, scores with the
general-tech weights, and suppresses the AI-vendor bonus. ai-vendor stays on the
unsuffixed dir + default prompt. Heavy deps (style guide, prompt loading, trend
artifact, recent-topics scan) are monkeypatched so the test is hermetic.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import idea_generation as ig  # noqa: E402
from idea_generation import daily_io_dirname, generate_ideas  # noqa: E402
from pipeline import ManualLLMHalt  # noqa: E402


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _candidate(topic: str = "ChatGPT now runs on the new iPhone") -> dict:
    return {
        "topic": topic,
        "angle": "A surprising consumer-tech outcome",
        "hook_concept": "Your phone just changed",
        "why_now": "today",
        "audience": "general consumer",
        "source_indexes": [0],
        "cited_observation_candidate": {
            "summary": "A reviewer tried it.",
            "source_url": "https://example.com/x",
            "source_handle": "the-verge-byline",
            "retrievable_quote": "it just works",
        },
        "scores": {k: 0.5 for k in (
            "niche_fit", "hook_strength", "specificity", "trend_signal",
            "verifiability", "broll_feasibility", "observation_availability",
            "anti_cannibalization",
        )},
        "rationale": "baseline",
    }


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Config + monkeypatched heavy deps; returns (config, research_dir)."""
    research = tmp_path / "01_research"
    research.mkdir(parents=True)
    io_dir = tmp_path / "io"
    config = {
        "paths": {"channel_root": str(tmp_path), "prompts": str(tmp_path / "prompts")},
        "llm": {"manual_io_dir": str(io_dir)},
    }

    monkeypatch.setattr(ig, "load_style_guide", lambda c: "STYLE GUIDE")
    monkeypatch.setattr(ig, "list_recent_topics", lambda root, days=30: [])
    monkeypatch.setattr(ig, "format_for_prompt", lambda recs: "(none)")

    def fake_load_prompt(name, c):
        # ai-vendor template deliberately has NO {DISCOVERED_STORIES} token.
        base = (
            f"PROMPT={name}\nSG={{NICHE_STYLE_GUIDE}}\n"
            f"TREND={{TREND_CANDIDATES}}\nRECENT={{RECENT_TOPICS}}\nN={{N_TARGET}}"
        )
        if "general_tech" in name:
            base += "\nSTORIES={DISCOVERED_STORIES}"
        return base

    monkeypatch.setattr(ig, "load_prompt", fake_load_prompt)

    def fake_ensure(channel_root, *, force_refresh=False, track="ai-vendor"):
        p = research / (f"trends_{track}.json")
        p.write_text(json.dumps({"candidates": [
            {"title": "t", "url": "u", "source": "s", "tag": "x", "summary": "y"},
        ]}), encoding="utf-8")
        return p

    monkeypatch.setattr(ig, "_ensure_trends_artifact", fake_ensure)
    return config, tmp_path


# ---------------------------------------------------------------------------
# daily_io_dirname
# ---------------------------------------------------------------------------


def test_daily_io_dirname_ai_vendor_unsuffixed():
    assert daily_io_dirname("ai-vendor", date="2026-06-21") == "_daily_2026-06-21"


def test_daily_io_dirname_general_tech_suffixed():
    assert daily_io_dirname("general-tech", date="2026-06-21") == "_daily_2026-06-21_general-tech"


# ---------------------------------------------------------------------------
# generate_ideas — halt + prompt routing
# ---------------------------------------------------------------------------


def test_general_tech_halts_and_uses_suffixed_dir_and_prompt(patched):
    config, root = patched
    base = Path(config["llm"]["manual_io_dir"]) / f"_daily_{_today()}_general-tech"
    base.mkdir(parents=True)
    (base / "discovered_stories.txt").write_text("Nurse caught a misdiagnosis", encoding="utf-8")

    with pytest.raises(ManualLLMHalt):
        generate_ideas(config, track="general-tech")

    prompt = (base / "idea_generation_PROMPT.txt").read_text(encoding="utf-8")
    assert "PROMPT=02_idea_generation_general_tech" in prompt
    assert "Nurse caught a misdiagnosis" in prompt   # discovered-stories slot injected


def test_ai_vendor_halts_on_unsuffixed_dir_with_default_prompt(patched):
    config, root = patched
    with pytest.raises(ManualLLMHalt):
        generate_ideas(config, track="ai-vendor")

    base = Path(config["llm"]["manual_io_dir"]) / f"_daily_{_today()}"
    prompt = (base / "idea_generation_PROMPT.txt").read_text(encoding="utf-8")
    assert "PROMPT=02_idea_generation" in prompt
    assert "general_tech" not in prompt
    # ai-vendor template has no DISCOVERED_STORIES token → none should leak in.
    assert "STORIES=" not in prompt


# ---------------------------------------------------------------------------
# generate_ideas — scoring path (resume with a RESPONSE present)
# ---------------------------------------------------------------------------


def _resume(config, track):
    """Drive generate_ideas to a halt, write a RESPONSE, then resume → picks."""
    try:
        generate_ideas(config, track=track)
    except ManualLLMHalt as halt:
        halt.response_path.write_text(json.dumps([_candidate()]), encoding="utf-8")
    return generate_ideas(config, track=track, n_picks=1)


def test_general_tech_suppresses_ai_vendor_bonus_and_tags_audit(patched):
    config, root = patched
    picks = _resume(config, "general-tech")
    assert len(picks) == 1
    # Candidate mentions "ChatGPT" → would earn the AI-vendor bonus on ai-vendor,
    # but the general-tech track suppresses it.
    assert picks[0].ai_vendor_bonus == 0.0

    base = Path(config["llm"]["manual_io_dir"]) / f"_daily_{_today()}_general-tech"
    audit = json.loads((base / "idea_generation_RANKED.json").read_text(encoding="utf-8"))
    assert audit["track"] == "general-tech"


def test_ai_vendor_keeps_bonus_and_default_track(patched):
    config, root = patched
    picks = _resume(config, "ai-vendor")
    assert len(picks) == 1
    assert picks[0].ai_vendor_bonus == 0.05   # bonus fires on the ai-vendor track

    base = Path(config["llm"]["manual_io_dir"]) / f"_daily_{_today()}"
    audit = json.loads((base / "idea_generation_RANKED.json").read_text(encoding="utf-8"))
    assert audit["track"] == "ai-vendor"
