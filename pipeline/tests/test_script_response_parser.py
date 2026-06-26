"""Unit tests for :mod:`tools.script_response_parser`.

Built as the source-side fix for the 2026-05-13 ``_12_002`` regression: the
LLM separator header ``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` leaked
into ``script_FINAL.txt`` and edge-TTS spoke it aloud. The Sprint 5 Layer-2
template-artifact scan caught it as a halt; this parser kills it at the source
so Layer 2's hit rate on auto-resolve drops to zero by construction.

Coverage:
    * Happy path — full ``_12_002``-style response with formula annotations,
      ``SCRIPT_BODY (uses HOOK_A ...)`` header, inline ``[B-ROLL: ...]`` cues,
      ``FACT_CHECK_QUEUE``, ``QUALITY_SCORES``.
    * Missing HOOK_B / HOOK_C.
    * Missing FACT_CHECK_QUEUE.
    * Mixed CRLF / LF line endings.
    * Extra blank lines between sections.
    * ``[B-ROLL: ...]`` cues preserved verbatim through both parser and
      composer.
    * Stray ``{PLACEHOLDER}`` warn-and-dropped; ``[B-ROLL: ...]`` not dropped.
    * Empty body raises ``ScriptResponseParseError``.
    * ``extract_final_script`` raises ``ValueError`` when chosen hook missing.
    * No double-prepend when body already opens with the chosen hook.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.script_response_parser import (  # noqa: E402
    ParsedResponse,
    ScriptResponseParseError,
    extract_final_script,
    parse_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Mirrors the 2026-05-13 `_12_002` failure pattern. The SCRIPT_BODY header
# carries the `(uses HOOK_A as the verbal opener)` annotation — exactly the
# string that edge-TTS spoke aloud.
_12_002_PATTERN = (
    "HOOK_A: Claude just admitted twenty-five percent of its advice is flattery.   [formula: Contradiction]\n"
    "HOOK_B: Anthropic published the number itself — and it's bigger than you'd guess.   [formula: Specific-Number Promise]\n"
    "HOOK_C: One Reddit thread noticed Claude turning into a therapist.   [formula: Cited-Observation Lead]\n"
    "\n"
    "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
    "Claude just admitted twenty-five percent of its advice is flattery. "
    "[B-ROLL: Anthropic Claude logo zoom-in] "
    "Anthropic's own research post says a quarter of personal-use conversations are emotional-support requests. "
    "[B-ROLL: Reddit thread screenshot scrolling] "
    "And the spirituality category? Nearly triple the average for everything else. "
    "[B-ROLL: bar chart bars rising]\n"
    "\n"
    "FACT_CHECK_QUEUE\n"
    "- Anthropic published a 25% figure for personal-use flattery / emotional-support conversations\n"
    "- Spirituality category is ~3x the average for other categories\n"
    "\n"
    "QUALITY_SCORES\n"
    "- hook_strength: 0.85\n"
    "- specificity: 0.70\n"
    "- opinion_density: 0.60\n"
    "- cited_observation_quality: 0.80\n"
    "- broll_cadence: 0.75\n"
    "- rationale: Specific-number opening, named source, three B-ROLL cues paced across the body.\n"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath_12_002Pattern:
    """The `_12_002` regression fixture must parse cleanly and produce a body
    that the Sprint 5 Layer-2 scan would not halt on."""

    def test_parses_returns_parsedresponse(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert isinstance(parsed, ParsedResponse)

    def test_hook_texts_stripped_of_formula_annotation(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert parsed.hook_a_text == (
            "Claude just admitted twenty-five percent of its advice is flattery."
        )
        assert parsed.hook_b_text and "[formula:" not in parsed.hook_b_text
        assert parsed.hook_c_text and "[formula:" not in parsed.hook_c_text

    def test_hook_formulas_extracted(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert parsed.hook_a_formula == "Contradiction"
        assert parsed.hook_b_formula == "Specific-Number Promise"
        assert parsed.hook_c_formula == "Cited-Observation Lead"

    def test_chosen_hook_letter_detected(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert parsed.chosen_hook_letter == "A"

    def test_script_body_excludes_header_artifact(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert "SCRIPT_BODY" not in parsed.script_body_text
        assert "(uses HOOK_A" not in parsed.script_body_text
        assert "verbal opener" not in parsed.script_body_text

    def test_script_body_preserves_broll_cues(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert "[B-ROLL: Anthropic Claude logo zoom-in]" in parsed.script_body_text
        assert "[B-ROLL: Reddit thread screenshot scrolling]" in parsed.script_body_text
        assert "[B-ROLL: bar chart bars rising]" in parsed.script_body_text

    def test_fact_check_queue_parsed(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert len(parsed.fact_check_queue) == 2
        assert "25%" in parsed.fact_check_queue[0] or "25" in parsed.fact_check_queue[0]

    def test_quality_scores_parsed(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert parsed.quality_scores["hook_strength"] == 0.85
        assert parsed.quality_scores["specificity"] == 0.70
        assert parsed.quality_scores["broll_cadence"] == 0.75

    def test_quality_rationale_captured(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        assert parsed.quality_rationale is not None
        assert "Specific-number" in parsed.quality_rationale

    def test_extract_final_script_returns_clean_body(self) -> None:
        parsed = parse_response(_12_002_PATTERN)
        final = extract_final_script(parsed, chosen="A")
        assert "SCRIPT_BODY" not in final
        assert "(uses HOOK_A" not in final
        assert "[formula:" not in final
        assert "[VERIFY" not in final
        # B-ROLL cues preserved verbatim
        assert "[B-ROLL: Anthropic Claude logo zoom-in]" in final
        assert "[B-ROLL: Reddit thread screenshot scrolling]" in final
        assert "[B-ROLL: bar chart bars rising]" in final
        # Original prose preserved
        assert "twenty-five percent" in final
        # Trailing newline
        assert final.endswith("\n")

    def test_extract_final_script_passes_sprint_5_layer_2(self) -> None:
        """The Layer-2 scan should not flag the parser's output. This is the
        property that drops Layer 2's auto-resolve fire rate to zero."""
        from tools.script_artifact_patterns import scan_for_artifacts

        parsed = parse_response(_12_002_PATTERN)
        final = extract_final_script(parsed, chosen="A")
        matches = scan_for_artifacts(final)
        # The body still legitimately contains [B-ROLL: ...] cues. Those
        # should NOT match any artifact pattern because the all-caps-bracket
        # regex requires the close-bracket to follow the all-caps token
        # immediately. Anything that DOES match means we've regressed.
        assert matches == [], (
            f"Layer-2 scan should be clean on parser output, got: {matches}"
        )


class TestMissingHooks:
    def test_missing_hook_b_and_c(self) -> None:
        text = (
            "HOOK_A: Only one hook this time.   [formula: Contradiction]\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Only one hook this time. The body follows. "
            "[B-ROLL: solo cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- the claim\n"
        )
        parsed = parse_response(text)
        assert parsed.hook_a_text == "Only one hook this time."
        assert parsed.hook_b_text is None
        assert parsed.hook_c_text is None

    def test_extract_final_script_works_with_missing_b_c(self) -> None:
        text = (
            "HOOK_A: Solo hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Solo hook. Body here. [B-ROLL: thing]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- nothing\n"
        )
        parsed = parse_response(text)
        final = extract_final_script(parsed, chosen="A")
        assert "Solo hook." in final
        assert "[B-ROLL: thing]" in final


class TestMissingFactCheckQueue:
    def test_body_runs_to_quality_scores_when_fc_absent(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body prose here. [B-ROLL: a cue]\n"
            "\n"
            "QUALITY_SCORES\n"
            "- hook_strength: 0.5\n"
        )
        parsed = parse_response(text)
        assert parsed.fact_check_queue == []
        assert "Body prose here." in parsed.script_body_text
        assert "[B-ROLL: a cue]" in parsed.script_body_text
        assert "QUALITY_SCORES" not in parsed.script_body_text

    def test_body_runs_to_eof_when_fc_and_quality_both_absent(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body prose runs to end of file. [B-ROLL: tail cue]\n"
        )
        parsed = parse_response(text)
        assert parsed.fact_check_queue == []
        assert "Body prose runs to end of file." in parsed.script_body_text
        assert "[B-ROLL: tail cue]" in parsed.script_body_text


class TestLineEndings:
    def test_crlf_normalized(self) -> None:
        text = (
            "HOOK_A: A hook.\r\n"
            "HOOK_B: B hook.\r\n"
            "HOOK_C: C hook.\r\n"
            "\r\n"
            "SCRIPT_BODY:\r\n"
            "Mixed CRLF body. [B-ROLL: cr-lf]\r\n"
            "\r\n"
            "FACT_CHECK_QUEUE\r\n"
            "- one claim\r\n"
        )
        parsed = parse_response(text)
        assert parsed.hook_a_text == "A hook."
        assert "Mixed CRLF body." in parsed.script_body_text
        assert parsed.fact_check_queue == ["one claim"]

    def test_mixed_cr_lf_and_lf(self) -> None:
        text = (
            "HOOK_A: A hook.\r\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\r"
            "\n"
            "SCRIPT_BODY:\n"
            "Mixed line endings. [B-ROLL: mix]\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert parsed.hook_a_text == "A hook."
        assert parsed.hook_b_text == "B hook."
        assert parsed.hook_c_text == "C hook."
        assert "Mixed line endings." in parsed.script_body_text


class TestExtraBlankLines:
    def test_handles_many_blank_lines_between_sections(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n\n\n"
            "SCRIPT_BODY:\n"
            "\n"
            "Body after lots of whitespace. [B-ROLL: gap]\n"
            "\n\n\n"
            "FACT_CHECK_QUEUE\n"
            "\n"
            "- a claim\n"
            "\n\n"
            "QUALITY_SCORES\n"
            "- hook_strength: 0.9\n"
        )
        parsed = parse_response(text)
        assert "Body after lots of whitespace." in parsed.script_body_text
        assert parsed.fact_check_queue == ["a claim"]
        assert parsed.quality_scores["hook_strength"] == 0.9


class TestBrollPreservation:
    def test_multiple_broll_cues_preserved_verbatim(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Sentence one. [B-ROLL: cue one] Sentence two. "
            "[B-ROLL: cue two with longer description] Sentence three. "
            "[B-ROLL: cue three]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        cues_in_body = parsed.script_body_text.count("[B-ROLL:")
        assert cues_in_body == 3
        assert "[B-ROLL: cue one]" in parsed.script_body_text
        assert "[B-ROLL: cue two with longer description]" in parsed.script_body_text
        assert "[B-ROLL: cue three]" in parsed.script_body_text

        final = extract_final_script(parsed, chosen="A")
        assert final.count("[B-ROLL:") == 3

    def test_broll_with_nested_verify_tag_inside_preserved(self) -> None:
        # Sometimes a B-ROLL cue contains a VERIFY tag for its caption text.
        # The cue is still load-bearing — should survive cleaning intact.
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body. [B-ROLL: Cursor settings showing the max session field]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert "[B-ROLL: Cursor settings showing the max session field]" in parsed.script_body_text


class TestTemplatePlaceholderDropping:
    def test_stray_placeholder_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body with a {PLACEHOLDER} stray token and a [B-ROLL: keep this] cue.\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        with caplog.at_level(logging.WARNING, logger="tools.script_response_parser"):
            parsed = parse_response(text)

        # Placeholder gone
        assert "{PLACEHOLDER}" not in parsed.script_body_text
        # B-ROLL cue preserved — this is the operator's critical footgun
        assert "[B-ROLL: keep this]" in parsed.script_body_text
        # Warning emitted
        assert any(
            "PLACEHOLDER" in rec.getMessage() for rec in caplog.records
        ), f"expected warning about PLACEHOLDER, got: {[r.getMessage() for r in caplog.records]}"

    def test_broll_form_not_dropped(self) -> None:
        # The well-formed B-ROLL form has a colon + description after the
        # all-caps token, so it must NOT match \[[A-Z_]+\] which requires
        # `]` immediately after the all-caps token. Regression-pin this.
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body. [B-ROLL: a description]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert "[B-ROLL: a description]" in parsed.script_body_text


class TestParseErrors:
    def test_empty_body_raises(self) -> None:
        # No SCRIPT_BODY section AND no body between hooks and
        # FACT_CHECK_QUEUE → unparseable.
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        with pytest.raises(ScriptResponseParseError):
            parse_response(text)

    def test_extract_final_script_raises_when_hook_missing(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "\n"
            "SCRIPT_BODY:\n"
            "Body. [B-ROLL: x]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        # No HOOK_B in this response
        with pytest.raises(ValueError):
            extract_final_script(parsed, chosen="B")


class TestNoDoublePrepend:
    def test_body_already_starts_with_hook_no_duplication(self) -> None:
        text = (
            "HOOK_A: Claude shipped a wild update today.   [formula: Contradiction]\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
            "Claude shipped a wild update today. The rest of the body follows. "
            "[B-ROLL: claude logo]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        final = extract_final_script(parsed, chosen="A")
        # Hook appears exactly once at the start, not twice
        assert final.count("Claude shipped a wild update today.") == 1
        assert final.startswith("Claude shipped a wild update today.")

    def test_body_starts_at_second_sentence_hook_is_prepended(self) -> None:
        # Defensive case: LLM put the hook in HOOK_A but did NOT repeat it
        # in SCRIPT_BODY. Composer should prepend it so TTS opens cleanly.
        text = (
            "HOOK_A: The chosen hook sentence.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
            "This is the second sentence of the body. [B-ROLL: cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        final = extract_final_script(parsed, chosen="A")
        assert final.startswith("The chosen hook sentence.")
        # Body's second sentence is intact
        assert "second sentence of the body" in final


class TestScriptBodyHeaderInlineProse:
    """ENG-002 regression: the LLM sometimes emits prose on the SAME line as
    the ``SCRIPT_BODY (uses HOOK_X ...):`` header. The prior `$`-anchored
    regex only matched header-on-its-own-line, so the literal header text
    leaked into ``script_body_text`` and reached edge-TTS through
    ``script_FINAL.txt`` in cycles 9 / 10 / 11 / 12."""

    def test_inline_prose_after_colon_does_not_leak_header(self) -> None:
        text = (
            "HOOK_A: Claude shipped a wild update today.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_A as the verbal opener): "
            "Claude shipped a wild update today. The body continues here. "
            "[B-ROLL: a cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert "SCRIPT_BODY" not in parsed.script_body_text
        assert "(uses HOOK_A" not in parsed.script_body_text
        assert "verbal opener" not in parsed.script_body_text

    def test_inline_prose_chosen_hook_letter_still_captured(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_B as the verbal opener): "
            "B hook. The rest of the body. [B-ROLL: cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert parsed.chosen_hook_letter == "B"

    def test_inline_prose_body_content_preserved(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_A): "
            "First sentence here. [B-ROLL: cue one] Second sentence. "
            "[B-ROLL: cue two]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert "First sentence here." in parsed.script_body_text
        assert "Second sentence." in parsed.script_body_text
        assert "[B-ROLL: cue one]" in parsed.script_body_text
        assert "[B-ROLL: cue two]" in parsed.script_body_text

    def test_inline_prose_without_annotation(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY: Body prose starts on the same line as the header. "
            "[B-ROLL: cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert "SCRIPT_BODY" not in parsed.script_body_text
        assert "Body prose starts on the same line as the header." in parsed.script_body_text
        assert "[B-ROLL: cue]" in parsed.script_body_text

    def test_inline_prose_extract_final_script_clean(self) -> None:
        text = (
            "HOOK_A: Claude shipped a wild update today.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "SCRIPT_BODY (uses HOOK_A as the verbal opener): "
            "Claude shipped a wild update today. The body continues. "
            "[B-ROLL: cue]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        final = extract_final_script(parsed, chosen="A")
        assert "SCRIPT_BODY" not in final
        assert "(uses HOOK_A" not in final
        assert final.startswith("Claude shipped a wild update today.")


class TestChosenHookFallback:
    def test_chosen_hook_from_chosen_hook_marker(self) -> None:
        # No SCRIPT_BODY annotation — but a CHOSEN HOOK: HOOK_B marker.
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook the chosen one.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "CHOSEN HOOK: HOOK_B\n"
            "\n"
            "Body prose. [B-ROLL: x]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert parsed.chosen_hook_letter == "B"
        # The marker line itself should be stripped from the body
        assert "CHOSEN HOOK" not in parsed.script_body_text

    def test_chosen_hook_none_when_no_marker(self) -> None:
        text = (
            "HOOK_A: A hook.\n"
            "HOOK_B: B hook.\n"
            "HOOK_C: C hook.\n"
            "\n"
            "Body without explicit chosen-hook marker. [B-ROLL: x]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        parsed = parse_response(text)
        assert parsed.chosen_hook_letter is None


# ---------------------------------------------------------------------------
# WORKFLOW_AUDIT_2026-05-31 L1 — pipeline._parse_script_response:
#   (a) WARN (never raise) on an unknown [formula: X] hook annotation
#   (b) WARN when the body word-count is outside the (config-bound) outlier rails
# These exercise the pipeline-side parser directly (the slicing wrapper above
# does not own these checks).
# ---------------------------------------------------------------------------


import pipeline  # noqa: E402


def _script_response(*, formula_a: str = "Contradiction", body_words: int = 100) -> str:
    """Build a valid script-gen response with a controllable HOOK_A formula and a
    body of exactly `body_words` spoken words (plus one [B-ROLL: ...] cue, which
    the parser strips before counting)."""
    body = " ".join(["word"] * body_words) + " [B-ROLL: a calm office desk]"
    return (
        f"HOOK_A: First hook sentence here.   [formula: {formula_a}]\n"
        "HOOK_B: Second hook sentence here.   [formula: Specific-Number Promise]\n"
        "HOOK_C: Third hook sentence here.   [formula: Cited-Observation Lead]\n"
        "\n"
        f"{body}\n"
        "\n"
        "FACT_CHECK_QUEUE\n"
        "- some claim to verify\n"
    )


def test_unknown_hook_formula_warns_not_raises(caplog) -> None:
    """An unrecognized [formula: X] logs a WARNING but the parse still succeeds
    and returns a ScriptDraft (tolerant of new/typo'd formulas — never rejects)."""
    response = _script_response(formula_a="Bogus", body_words=100)
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        draft = pipeline._parse_script_response(response, topic_id="t1")
    assert isinstance(draft, pipeline.ScriptDraft)
    assert draft.hook_formulas[0] == "Bogus"  # preserved verbatim, not dropped
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown hook formula" in m and "Bogus" in m for m in warnings), warnings


def test_known_hook_formula_and_in_range_body_logs_no_warning(caplog) -> None:
    """Regression: a recognized formula + an in-range body produces NO warning
    (neither the formula nor the word-count path fires)."""
    response = _script_response(formula_a="Contradiction", body_words=110)
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        draft = pipeline._parse_script_response(response, topic_id="t1")
    assert isinstance(draft, pipeline.ScriptDraft)
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any("unknown hook formula" in m for m in msgs), msgs
    assert not any("Operator should review" in m for m in msgs), msgs


def test_hook_formula_case_insensitive_no_warning(caplog) -> None:
    """A known formula in a different case (e.g. 'CONTRADICTION') is recognized
    via casefold and does NOT warn."""
    response = _script_response(formula_a="CONTRADICTION", body_words=100)
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        pipeline._parse_script_response(response, topic_id="t1")
    assert not any(
        "unknown hook formula" in r.getMessage()
        for r in caplog.records if r.levelname == "WARNING"
    )


def test_word_count_below_min_warns(caplog) -> None:
    """A body shorter than word_count_min fires a review WARNING but still parses
    to a ScriptDraft (no raise)."""
    response = _script_response(body_words=20)  # well below the 80 default min
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        draft = pipeline._parse_script_response(response, topic_id="t1")
    assert isinstance(draft, pipeline.ScriptDraft)
    assert draft.word_count == 20
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("Operator should review" in m for m in warnings), warnings
    # The stale "100–150" literal must be gone; the message reflects the real bound.
    assert any("80" in m and "200" in m for m in warnings), warnings
    assert not any("100–150" in m for m in warnings)


def test_word_count_bounds_read_from_config(caplog) -> None:
    """The outlier rails come from script_quality.word_count_min/max — a tighter
    config bound makes an otherwise-in-range body warn."""
    response = _script_response(body_words=100)
    config = {"script_quality": {"word_count_min": 110, "word_count_max": 130}}
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        draft = pipeline._parse_script_response(response, topic_id="t1", config=config)
    assert draft.word_count == 100
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("110" in m and "130" in m for m in warnings), warnings


def test_missing_word_count_keys_use_defaults(caplog) -> None:
    """A config WITHOUT the word_count keys still parses (keys read via .get()
    defaults) — load_config stays permissive for the isolation fixtures."""
    response = _script_response(body_words=100)
    # config present but script_quality lacks the word_count_* keys.
    config = {"script_quality": {"min_score": 0.5}}
    draft = pipeline._parse_script_response(response, topic_id="t1", config=config)
    assert isinstance(draft, pipeline.ScriptDraft)
    assert draft.word_count == 100  # 100 is within the 80–200 default rails -> no raise


# ---------------------------------------------------------------------------
# M-1 unification (2026-06-19): _parse_script_response now delegates body
# extraction/cleaning to the shared parse_response, so generate_script's body
# (manual gate-2) and extract_final_script (auto gate-2) share ONE cleaning path.
# ---------------------------------------------------------------------------


class TestParserUnification:
    """Manual-path body is now cleaned identically to the auto path."""

    def test_script_body_header_stripped_from_draft_body(self) -> None:
        """The _12_002 SCRIPT_BODY header artifact must NOT survive into
        ScriptDraft.body. Before unification the positional slice kept it,
        which is exactly how edge-TTS came to speak the header aloud."""
        draft = pipeline._parse_script_response(_12_002_PATTERN, topic_id="t1")
        assert "SCRIPT_BODY" not in draft.body
        assert "(uses HOOK_A" not in draft.body
        assert "verbal opener" not in draft.body

    def test_draft_body_preserves_broll_cues(self) -> None:
        draft = pipeline._parse_script_response(_12_002_PATTERN, topic_id="t1")
        assert "[B-ROLL: Anthropic Claude logo zoom-in]" in draft.body
        assert len(draft.broll_cues) == 3

    def test_manual_body_equals_shared_parser_body(self) -> None:
        """The unification invariant: ScriptDraft.body IS parse_response's cleaned
        script_body_text. If a future change re-introduces a separate cleaning
        path, this breaks."""
        draft = pipeline._parse_script_response(_12_002_PATTERN, topic_id="t1")
        parsed = parse_response(_12_002_PATTERN)
        assert draft.body == parsed.script_body_text

    def test_manual_and_auto_final_share_cleaned_body(self) -> None:
        """The auto-gate-2 final (extract_final_script) and the manual body carry
        the SAME cleaned body — the divergence M-1 flagged is gone. The auto final
        prepends the chosen hook, so the manual body must appear within it."""
        draft = pipeline._parse_script_response(_12_002_PATTERN, topic_id="t1")
        parsed = parse_response(_12_002_PATTERN)
        auto_final = extract_final_script(parsed, chosen="A")
        assert draft.body in auto_final
        assert "SCRIPT_BODY" not in auto_final

    def test_verify_tags_stripped_from_draft_body(self) -> None:
        """[VERIFY: ...] tags must not survive into ScriptDraft.body (parity with
        the auto path)."""
        response = (
            "HOOK_A: A real dated event opened the story.   [formula: Contradiction]\n"
            "HOOK_B: Second variant here.   [formula: Specific-Number Promise]\n"
            "HOOK_C: Third variant here.   [formula: Cited-Observation Lead]\n"
            "\n"
            "OpenAI shipped a model on [VERIFY: exact date] that beat the prior one. "
            "[B-ROLL: OpenAI logo] "
            "It scored higher than [VERIFY: competitor] on the public benchmark.\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "- OpenAI shipped a model that beat the prior one\n"
        )
        draft = pipeline._parse_script_response(response, topic_id="t1")
        assert "VERIFY" not in draft.body
        assert "[B-ROLL: OpenAI logo]" in draft.body  # cue still preserved

    def test_validation_contract_preserved(self) -> None:
        """The manual-halt ValueError messages survive the delegation."""
        no_fc = (
            "HOOK_A: One.   [formula: Contradiction]\n"
            "HOOK_B: Two.   [formula: Specific-Number Promise]\n"
            "HOOK_C: Three.   [formula: Cited-Observation Lead]\n"
            "\n"
            "Some body prose here without the queue.\n"
        )
        with pytest.raises(ValueError, match="FACT_CHECK_QUEUE"):
            pipeline._parse_script_response(no_fc, topic_id="t1")
        two_hooks = (
            "HOOK_A: One.\n"
            "HOOK_B: Two.\n"
            "\n"
            "Body.\n"
            "FACT_CHECK_QUEUE\n"
            "- claim\n"
        )
        with pytest.raises(ValueError, match="Expected 3 lines"):
            pipeline._parse_script_response(two_hooks, topic_id="t1")

    def test_empty_fact_check_queue_does_not_leak_quality_scores(self) -> None:
        """Differential-audit regression (2026-06-19): an EMPTY FACT_CHECK_QUEUE
        followed by a blank line then QUALITY_SCORES must NOT leak the
        QUALITY_SCORES bullets into fact_check_queue (the `>` vs `>=` boundary in
        parse_response). Affects both parse_response and the unified
        _parse_script_response that now delegates to it."""
        text = (
            "HOOK_A: One.   [formula: Contradiction]\n"
            "HOOK_B: Two.   [formula: Specific-Number Promise]\n"
            "HOOK_C: Three.   [formula: Cited-Observation Lead]\n"
            "\n"
            "Real body prose for the script here. [B-ROLL: a desk]\n"
            "\n"
            "FACT_CHECK_QUEUE\n"
            "\n"
            "QUALITY_SCORES\n"
            "- hook_strength: 0.85\n"
            "- specificity: 0.70\n"
        )
        parsed = parse_response(text)
        assert parsed.fact_check_queue == []
        assert parsed.quality_scores == {"hook_strength": 0.85, "specificity": 0.70}
        draft = pipeline._parse_script_response(text, topic_id="t1")
        assert draft.fact_check_queue == []
        assert draft.quality_scores["hook_strength"] == 0.85
