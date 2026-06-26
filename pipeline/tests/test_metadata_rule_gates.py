"""Metadata-stage hard-rule gates — title-anchor gate (PU-3T, 2026-06-24).

The first 3 words of the YouTube title (the de-facto thumbnail — ShadowVerse
uploads no custom thumbnails) must carry a recognizable anchor. Complements the
PU-3 spoken-body anchor gate, which by design cannot see the title (it doesn't
exist yet at Stage 1.5). Deliberately stricter than the body gate: a bare
"This AI"/"Your AI"/"A new AI" opener fails, and Title Case capitalization is
NOT treated as a proper-noun signal. One-flip rollback via
``script_quality.title_anchor_gate_enabled`` (default OFF in code).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import (  # noqa: E402
    MetadataBundle,
    MetadataRuleViolation,
    _enforce_metadata_hard_rules,
    anchor_gate_violation,
    title_anchor_violation,
)


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------


def _make_bundle(title: str, *, topic_id: str = "t1") -> MetadataBundle:
    """A MetadataBundle with the title under test and harmless placeholders for
    every other required field."""
    return MetadataBundle(
        topic_id=topic_id,
        youtube_title=title,
        youtube_description="desc",
        youtube_tags=["a"],
        youtube_hashtags=["#a"],
        tiktok_caption="cap",
        tiktok_hashtags=["#a"],
        instagram_caption="cap",
        instagram_hashtags=["#a"],
        cover_text="text",
        cover_background_desc="bg",
        cover_color_accent="#ffffff",
    )


# ---------------------------------------------------------------------------
# title_anchor_violation — pure heuristic
# ---------------------------------------------------------------------------


class TestTitleAnchorHeuristic:
    @pytest.mark.parametrize("title", [
        "Claude just rewrote its own code",             # known brand, word 1
        "ChatGPT 5.5 Pro shocked a Fields medalist",    # brand + number
        "Elon's brain chip let a paralyzed man play",   # person (possessive), word 1
        "Over 3 million people lost access today",      # digit in first 3
        "Gemini reads photos, text, and PDFs at once",  # brand
        "An iPhone feature got quietly deleted",        # interior capital
        "The lawsuit that could end OpenAI",            # vivid consumer concept "lawsuit"
        "Anthropic admits its model flatters you",      # brand, word 1
        "Neuralink let a paralyzed man raid Warcraft",  # extended-lexicon brand
        "Andrej Karpathy just defected to Anthropic",   # named human past word 1 (cap signal)
        "Simon Willison named the fatal flaw",          # named human past word 1
        "Figma just lost a designer to Claude",         # brand at word 1 (lexicon)
        "A Nobel winner just joined the makers of Claude",  # cap proper noun at word 2
        "AI quietly built itself a parallel internet",  # bare "AI" accepted (operator 2026-06-24)
        "This AI runs offline on your laptop",          # determiner + AI now admits
    ])
    def test_anchored_titles_pass(self, title: str) -> None:
        assert title_anchor_violation(title) is None

    @pytest.mark.parametrize("title", [
        "Something strange happened today",             # no anchor, no AI
        "These models are getting weird",               # generic plural, no anchor
        "uv shipped a faster installer today",          # dev-infra, no consumer anchor
        "We're more patient than ever before",          # abstract opener, no anchor
    ])
    def test_unanchored_titles_flagged(self, title: str) -> None:
        v = title_anchor_violation(title)
        assert v is not None
        assert "no recognizable" in v

    def test_empty_title_flagged(self) -> None:
        assert title_anchor_violation("") is not None
        assert title_anchor_violation("   ") is not None

    def test_bare_ai_is_an_accepted_title_anchor(self) -> None:
        # Operator decision 2026-06-24: "AI" counts as a title anchor (this is an
        # AI-focused channel), matching the spoken-body gate's treatment of "AI".
        assert anchor_gate_violation("Your AI just got downgraded overnight.") is None
        assert title_anchor_violation("Your AI just got downgraded overnight") is None

    def test_title_case_capitalization_is_not_an_anchor(self) -> None:
        # Title Case capitalizes every word; that alone must not pass the gate.
        assert title_anchor_violation("Something Strange Happened Over There") is not None

    def test_title_case_with_no_real_anchor_still_flagged(self) -> None:
        # A fully Title-Cased title carries no proper-noun signal from caps, so a
        # no-anchor Title Case opener must still fail (caps are not trusted here).
        assert title_anchor_violation("Some Strange Thing Happened Overnight") is not None

    def test_sentence_case_proper_noun_past_word_one_passes(self) -> None:
        # The deliberate fix: a capitalized name past the always-capitalized first
        # word IS trusted in a sentence-case title, even when not in the lexicon.
        assert title_anchor_violation("Yesterday Mira launched something wild") is None


# ---------------------------------------------------------------------------
# Gate wiring in _enforce_metadata_hard_rules
# ---------------------------------------------------------------------------


class TestMetadataGateWiring:
    def test_flag_off_is_legacy_passthrough(self) -> None:
        bundle = _make_bundle("This AI just changed everything")
        assert _enforce_metadata_hard_rules(bundle, {"script_quality": {}}) is None

    def test_flag_off_by_default_when_section_absent(self) -> None:
        bundle = _make_bundle("This AI just changed everything")
        assert _enforce_metadata_hard_rules(bundle, {}) is None

    def test_gate_halts_on_unanchored_title(self) -> None:
        bundle = _make_bundle("Something strange happened overnight", topic_id="2026-06-24_001")
        config = {"script_quality": {"title_anchor_gate_enabled": True}}
        with pytest.raises(MetadataRuleViolation) as excinfo:
            _enforce_metadata_hard_rules(bundle, config)
        assert "title anchor" in str(excinfo.value)
        assert excinfo.value.topic_id == "2026-06-24_001"

    def test_gate_passes_anchored_title(self) -> None:
        bundle = _make_bundle("Claude just rewrote its own code")
        config = {"script_quality": {"title_anchor_gate_enabled": True}}
        assert _enforce_metadata_hard_rules(bundle, config) is None

    def test_halt_message_names_the_rollback_flag(self) -> None:
        bundle = _make_bundle("We're more patient than ever")
        config = {"script_quality": {"title_anchor_gate_enabled": True}}
        with pytest.raises(MetadataRuleViolation) as excinfo:
            _enforce_metadata_hard_rules(bundle, config)
        assert "title_anchor_gate_enabled" in str(excinfo.value)
