"""Tests for :mod:`tools.script_artifact_patterns`.

Phase-1 foundation for the Sprint 5 template-artifact prevention defense.
Layer 1, 2, 3 all import :func:`scan_for_artifacts` from the patterns module
so this test suite is the regression net for all three.

Coverage:
    * Every pattern matches at least one positive case
    * Every pattern is exercised by at least one negative case
    * Case-insensitive paths confirmed
    * Whitespace-vs-underscore variants both fire
    * Line-anchored patterns only match at line scope
    * Match metadata (line_no, category, pattern_name, matched_text) populated
    * Regression fixture for the literal ``_12_002`` failure string
"""

from __future__ import annotations

import pytest

from tools.script_artifact_patterns import (
    ArtifactMatch,
    INTERNAL_NAME_PATTERNS,
    STAGE_INSTRUCTION_PATTERNS,
    TEMPLATE_PLACEHOLDER_PATTERNS,
    format_matches,
    scan_for_artifacts,
)


# ---------------------------------------------------------------------------
# Template-placeholder patterns
# ---------------------------------------------------------------------------


class TestTemplatePlaceholderPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "{SCRIPT_BODY}",
            "<SCRIPT_BODY>",
            "[SCRIPT_BODY]",
            "{HOOK_A}",
            "leading text {SCRIPT_BODY} trailing",
            "<HOOK_A_VERBAL_OPENER>",
            "[CITED_OBSERVATION]",
        ],
    )
    def test_positive(self, text: str) -> None:
        matches = scan_for_artifacts(text)
        assert any(m.category == "template_placeholder" for m in matches), (
            f"expected template_placeholder hit on {text!r}, got: {matches}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "[Subject]: [contrarian payoff]",          # mixed case bracket — title format
            "{lowercase}",                             # lowercase brace
            "<MixedCase>",                             # mixed-case angle
            "Claude said this AI is great",            # nothing bracketed
            "[some text here]",                        # lowercase in brackets
            "{a}",                                     # single lowercase char
            "an array index [0]",                      # digit-only bracket
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        hits = [m for m in scan_for_artifacts(text) if m.category == "template_placeholder"]
        assert hits == [], f"unexpected hit on {text!r}: {hits}"


# ---------------------------------------------------------------------------
# Internal-name patterns
# ---------------------------------------------------------------------------


class TestInternalNamePatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "script_body",
            "SCRIPT_BODY",
            "Script_Body",
            "script body",                              # whitespace variant
            "SCRIPT BODY",
            "hook_a",
            "hook_b",
            "hook_c",
            "HOOK_A",
            "hook a",                                   # whitespace variant
            "hook_a_verbal_opener",
            "Hook_B_punchline",
            "cited_observation",
            "Cited Observation",
            "broll_cue",
            "BROLL CUE",
            "verbal_opener",
            "verbal opener",
            "fact_check_queue",
            "FACT CHECK QUEUE",
            "quality_scores",
            "quality_score",                            # singular form
            "Quality Scores",
        ],
    )
    def test_positive(self, text: str) -> None:
        matches = scan_for_artifacts(text)
        assert any(m.category == "internal_name" for m in matches), (
            f"expected internal_name hit on {text!r}, got: {matches}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Claude shipped a new feature today",
            "OpenAI released GPT-5.5",
            "Anthropic announced AWS partnership",
            "the script keeps users hooked",            # script alone, no body
            "great body of work",                       # body alone, no script
            "no one mentions this",
            "the AI quality is great",                  # quality but not quality_scores
            "fact-check using sources",                 # hyphen, not underscore
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        hits = [m for m in scan_for_artifacts(text) if m.category == "internal_name"]
        assert hits == [], f"unexpected hit on {text!r}: {hits}"


# ---------------------------------------------------------------------------
# Stage-instruction patterns
# ---------------------------------------------------------------------------


class TestStageInstructionPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "(uses HOOK_A as the verbal opener)",          # the _12_002 trigger
            "(uses SCRIPT_BODY)",
            "(uses CITED_OBSERVATION verbatim)",
            "> some quoted annotation",
            "# Section header",
            "## Sub-section",
            "### Deep-sub",
            "**Annotation block**",
            "SCRIPT_BODY",                                  # all-caps single token
            "SCRIPT_BODY:",
            "HOOK_A",
            "BACKGROUND",                                   # 4+ chars no underscore
        ],
    )
    def test_positive(self, text: str) -> None:
        matches = scan_for_artifacts(text)
        assert any(m.category == "stage_instruction" for m in matches), (
            f"expected stage_instruction hit on {text!r}, got: {matches}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "He uses Claude every day",                     # 'uses' but not stage-instruction
            "Lorem ipsum dolor sit amet",
            "the win nobody mentions",
            "Claude said 'hello world'",
            "  some indented body line",
            "Pricing went up.",
            "He pivoted to AI.",                            # short, no all-caps line
            "Mixed Case Header",                            # not all-caps
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        hits = [m for m in scan_for_artifacts(text) if m.category == "stage_instruction"]
        assert hits == [], f"unexpected hit on {text!r}: {hits}"

    def test_markdown_header_requires_content(self) -> None:
        # '# ' alone (no following non-space char) should NOT match.
        assert scan_for_artifacts("#") == []
        assert scan_for_artifacts("# ") == []
        # '# x' should match.
        assert any(
            m.category == "stage_instruction"
            for m in scan_for_artifacts("# x")
        )

    def test_bold_only_line_must_be_standalone(self) -> None:
        # Inline bold inside a sentence shouldn't trigger the standalone pattern.
        text = "This is **important** stuff."
        hits = [m for m in scan_for_artifacts(text) if "STAGE_3" in m.pattern_name]
        assert hits == [], f"inline bold falsely flagged: {hits}"


# ---------------------------------------------------------------------------
# Match metadata
# ---------------------------------------------------------------------------


class TestMatchMetadata:
    def test_line_number_is_one_based(self) -> None:
        text = "first clean line\nsecond clean line\n{SCRIPT_BODY} here\n"
        matches = scan_for_artifacts(text)
        assert matches, "expected at least one match"
        assert matches[0].line_no == 3

    def test_line_text_excludes_newline(self) -> None:
        text = "before\n{SCRIPT_BODY}\nafter\n"
        matches = scan_for_artifacts(text)
        target = next(m for m in matches if m.matched_text == "{SCRIPT_BODY}")
        assert "\n" not in target.line_text
        assert target.line_text == "{SCRIPT_BODY}"

    def test_match_includes_all_fields(self) -> None:
        matches = scan_for_artifacts("script_body")
        assert matches
        m = matches[0]
        assert isinstance(m, ArtifactMatch)
        assert m.line_no == 1
        assert m.category == "internal_name"
        assert m.pattern_name.startswith("INTERNAL_")
        assert m.matched_text.lower() == "script_body"
        assert m.line_text == "script_body"

    def test_results_sorted_by_line_then_pattern(self) -> None:
        text = "{LATE}\n{EARLY}\nscript_body\n"
        matches = scan_for_artifacts(text)
        line_nos = [m.line_no for m in matches]
        assert line_nos == sorted(line_nos)


# ---------------------------------------------------------------------------
# Format helper
# ---------------------------------------------------------------------------


class TestFormatMatches:
    def test_empty_matches(self) -> None:
        assert format_matches([]) == "(no matches)"

    def test_non_empty_renders_one_line_per_match(self) -> None:
        text = "{SCRIPT_BODY}\nscript_body\n"
        rendered = format_matches(scan_for_artifacts(text))
        assert "line 1" in rendered
        assert "line 2" in rendered
        assert "{SCRIPT_BODY}" in rendered


# ---------------------------------------------------------------------------
# Pattern catalog sanity
# ---------------------------------------------------------------------------


class TestPatternCatalog:
    def test_all_three_groups_non_empty(self) -> None:
        assert len(TEMPLATE_PLACEHOLDER_PATTERNS) >= 3
        assert len(INTERNAL_NAME_PATTERNS) >= 7
        assert len(STAGE_INSTRUCTION_PATTERNS) >= 5

    def test_clean_script_passes(self) -> None:
        # A realistic clean general-audience AI script body.
        clean = (
            "Claude just dropped a wild update. Anthropic shipped a new feature "
            "that lets the model browse the web in real time. Most people will "
            "miss why this matters. It's not the speed. It's the trust. The "
            "model now cites every fact with a live URL. No more hallucinated "
            "sources. The catch? You have to enable it in settings. Anthropic's "
            "release notes confirm it shipped 2026-05-13."
        )
        assert scan_for_artifacts(clean) == []


# ---------------------------------------------------------------------------
# Regression: _12_002
# ---------------------------------------------------------------------------


class TestRegressionTwelveZeroZeroTwo:
    """Mirror of the 2026-05-13 ``_12_002`` failure: a stray annotation header
    copied from ``script_RESPONSE.txt`` into ``script_FINAL.txt`` and spoken
    aloud by edge-TTS. The patterns module MUST flag both the parenthetical
    ``(uses HOOK_A as the verbal opener)`` AND the ``SCRIPT_BODY`` token.
    """

    FLAWED_FIXTURE = (
        "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
        "Claude just admitted twenty five percent of its advice is flattery.\n"
        "Anthropic's own research post says people use it for personal guidance.\n"
        "And the spirituality category? Nearly triple the average for everything else.\n"
    )

    def test_flagged(self) -> None:
        matches = scan_for_artifacts(self.FLAWED_FIXTURE)
        assert matches, "regression fixture must NOT pass"

    def test_script_body_caught(self) -> None:
        matches = scan_for_artifacts(self.FLAWED_FIXTURE)
        assert any(
            m.category == "internal_name" and "script" in m.matched_text.lower()
            for m in matches
        )

    def test_uses_hook_a_paren_caught(self) -> None:
        matches = scan_for_artifacts(self.FLAWED_FIXTURE)
        assert any(
            "(uses HOOK_A" in m.matched_text
            for m in matches
        )

    def test_first_match_on_first_line(self) -> None:
        matches = scan_for_artifacts(self.FLAWED_FIXTURE)
        assert matches[0].line_no == 1
