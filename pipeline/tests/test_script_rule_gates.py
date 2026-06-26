"""Stage 1.5 hard-rule gates — 2026-06-09 weekly review change-set.

Covers:
  - PU-3 anchor gate (R2 H1): ``pipeline.anchor_gate_violation`` — the first 4
    spoken words must contain a recognizable named anchor (person, brand,
    concrete number, or universal consumer concept).
  - PU-4 modal ban (R2 H2): ``pipeline.modal_opener_violation`` — sentence 1
    must state a dated factual event; could/might/imagine-if/what-if banned.
  - PU-11 word-count halt (R2 H4): word_count outside
    [word_count_min, word_count_max] halts when word_count_halt_enabled.
  - Manager C3 semantics: halts are capped regenerate-with-feedback — the
    counter file increments per halt, the message flips to operator-escalation
    past STAGE15_MAX_REGEN_ATTEMPTS, and the topic is never auto-killed.
  - One-flip rollback: every check is config-flagged and defaults OFF.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import (  # noqa: E402
    SCRIPT_QUALITY_DIMENSIONS,
    STAGE15_MAX_REGEN_ATTEMPTS,
    ScriptDraft,
    ScriptRuleViolation,
    anchor_gate_violation,
    evaluate_script_quality,
    modal_opener_violation,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_script(
    *,
    topic_id: str = "t1",
    body: str = "ChatGPT just rewrote its own code. [B-ROLL: phone] And nobody noticed.",
    word_count: int = 100,
) -> ScriptDraft:
    return ScriptDraft(
        topic_id=topic_id,
        hook_variants=["A", "B", "C"],
        body=body,
        broll_cues=[],
        fact_check_queue=[],
        word_count=word_count,
        quality_scores={dim: 1.0 for dim in SCRIPT_QUALITY_DIMENSIONS},
    )


def _make_config(tmp_path: Path, **quality_flags) -> dict:
    return {
        "llm": {"manual_io_dir": str(tmp_path)},
        "script_quality": {
            "min_score": 0.50,
            "enforce_min_score": True,
            **quality_flags,
        },
    }


# ---------------------------------------------------------------------------
# anchor_gate_violation — pure heuristic
# ---------------------------------------------------------------------------


class TestAnchorGateHeuristic:
    @pytest.mark.parametrize("body", [
        "ChatGPT just rewrote its own code.",          # known vendor, word 1
        "Yesterday Elon Musk sued his own AI.",        # person name, words 2-3
        "Over 3 million people lost access today.",    # digit in first 4 words
        "Your AI just got downgraded overnight.",      # universal concept "AI"
        "The lawsuit nobody saw coming just landed.",  # universal concept "lawsuit"
        "An iPhone feature got quietly deleted.",      # interior capital (camelCase)
        "This week Anthropic shipped something weird.",  # vendor past position 1
        "[B-ROLL: phone closeup] ChatGPT just broke the internet.",  # cue stripped
    ])
    def test_anchored_openings_pass(self, body: str) -> None:
        assert anchor_gate_violation(body) is None

    @pytest.mark.parametrize("body", [
        "Something strange happened to every chatbot yesterday."
        .replace("chatbot", "assistant"),               # no anchor in first 4
        "There is a new way to think about work.",      # generic opener
        "Imagine waking up to find everything changed.",  # no named anchor
    ])
    def test_anchorless_openings_flagged(self, body: str) -> None:
        violation = anchor_gate_violation(body)
        assert violation is not None
        assert "no recognizable named anchor" in violation

    def test_empty_body_flagged(self) -> None:
        assert anchor_gate_violation("") is not None

    def test_first_word_capitalization_is_not_evidence(self) -> None:
        # Word 1 is always capitalized — that alone must not count as a proper noun.
        assert anchor_gate_violation("Something big happened over there.") is not None


# ---------------------------------------------------------------------------
# modal_opener_violation — pure heuristic
# ---------------------------------------------------------------------------


class TestModalBanHeuristic:
    @pytest.mark.parametrize("sentence", [
        "ChatGPT just rewrote its own code.",
        "On Tuesday, Anthropic shipped a model that argues back.",
        "Elon Musk lost his biggest AI fight this week.",
    ])
    def test_factual_openers_pass(self, sentence: str) -> None:
        assert modal_opener_violation(sentence) is None

    @pytest.mark.parametrize("sentence", [
        "AI could take your job next year.",
        "This might be the end of search engines.",
        "Imagine if your phone started lying to you.",
        "What if ChatGPT never forgot anything?",
        "WHAT IF ChatGPT never forgot anything?",  # case-insensitive
    ])
    def test_modal_openers_flagged(self, sentence: str) -> None:
        violation = modal_opener_violation(sentence)
        assert violation is not None
        assert "dated factual event" in violation

    def test_mightily_is_not_might(self) -> None:
        # Word-boundary check: substrings of longer words must not trip the ban.
        assert modal_opener_violation("ChatGPT mightily impressed a Nobel laureate.") is None


# ---------------------------------------------------------------------------
# Gate wiring in evaluate_script_quality
# ---------------------------------------------------------------------------


class TestGateWiring:
    def test_flags_off_means_legacy_passthrough(self, tmp_path: Path) -> None:
        """Default-off flags: an anchor-less, modal, overlong script passes
        through to the score gate untouched (one-flip rollback contract)."""
        config = _make_config(tmp_path)
        script = _make_script(
            body="What if everything you knew about search might be wrong?",
            word_count=300,
        )
        assert evaluate_script_quality(script, config) is script

    def test_anchor_gate_halts(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, anchor_gate_enabled=True)
        script = _make_script(body="Something strange happened to every assistant.")
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        assert "anchor gate" in str(excinfo.value)

    def test_modal_ban_halts(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        script = _make_script(body="ChatGPT could take your job. It happened fast.")
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        assert "modal ban" in str(excinfo.value)

    def test_modal_ban_checks_first_sentence_only(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        script = _make_script(
            body="ChatGPT just rewrote its own code. This could change everything."
        )
        assert evaluate_script_quality(script, config) is script

    def test_word_count_halt(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            word_count_halt_enabled=True,
            word_count_min=80,
            word_count_max=120,
        )
        script = _make_script(word_count=174)
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        msg = str(excinfo.value)
        assert "word count" in msg
        assert "174" in msg
        # The documented loosen path must be named in the feedback.
        assert "word_count_max" in msg

    def test_word_count_within_bounds_passes(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            word_count_halt_enabled=True,
            word_count_min=80,
            word_count_max=120,
        )
        script = _make_script(word_count=110)
        assert evaluate_script_quality(script, config) is script

    def test_word_count_loosened_back_to_200_passes(self, tmp_path: Path) -> None:
        """Documented rollback: set word_count_max back to 200."""
        config = _make_config(
            tmp_path,
            word_count_halt_enabled=True,
            word_count_min=80,
            word_count_max=200,
        )
        script = _make_script(word_count=174)
        assert evaluate_script_quality(script, config) is script

    def test_violations_aggregate_in_one_halt(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            anchor_gate_enabled=True,
            modal_ban_enabled=True,
            word_count_halt_enabled=True,
            word_count_max=120,
        )
        script = _make_script(
            body="Something out there might be watching everything quietly.",
            word_count=300,
        )
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        assert len(excinfo.value.violations) == 3


# ---------------------------------------------------------------------------
# Manager C3 — capped regenerate-with-feedback, never auto-kill
# ---------------------------------------------------------------------------


class TestRegenerateSemantics:
    def test_attempt_counter_increments_per_halt(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        script = _make_script(topic_id="2026-06-10_001",
                              body="AI might replace your doctor.")
        with pytest.raises(ScriptRuleViolation) as first:
            evaluate_script_quality(script, config)
        with pytest.raises(ScriptRuleViolation) as second:
            evaluate_script_quality(script, config)
        assert first.value.attempt == 1
        assert second.value.attempt == 2
        counter = tmp_path / "2026-06-10_001" / "stage15_regen_attempts.txt"
        assert counter.read_text(encoding="utf-8").strip() == "2"

    def test_regenerate_message_within_cap(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        script = _make_script(body="AI might replace your doctor.")
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        msg = str(excinfo.value)
        assert f"attempt 1 of {STAGE15_MAX_REGEN_ATTEMPTS}" in msg
        assert "rewrite" in msg.lower()

    def test_escalates_to_operator_past_cap_never_kills(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        script = _make_script(topic_id="2026-06-10_002",
                              body="AI might replace your doctor.")
        last_exc: ScriptRuleViolation | None = None
        for _ in range(STAGE15_MAX_REGEN_ATTEMPTS + 1):
            with pytest.raises(ScriptRuleViolation) as excinfo:
                evaluate_script_quality(script, config)
            last_exc = excinfo.value
        assert last_exc is not None
        assert last_exc.attempt == STAGE15_MAX_REGEN_ATTEMPTS + 1
        msg = str(last_exc)
        assert "OPERATOR" in msg
        assert "auto-kill" in msg

    def test_pass_clears_the_counter(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, modal_ban_enabled=True)
        topic_id = "2026-06-10_003"
        bad = _make_script(topic_id=topic_id, body="AI might replace your doctor.")
        with pytest.raises(ScriptRuleViolation):
            evaluate_script_quality(bad, config)
        counter = tmp_path / topic_id / "stage15_regen_attempts.txt"
        assert counter.exists()
        fixed = _make_script(topic_id=topic_id,
                             body="ChatGPT just replaced a doctor. For real.")
        evaluate_script_quality(fixed, config)
        assert not counter.exists()

    def test_missing_manual_io_dir_still_halts(self, tmp_path: Path) -> None:
        """The counter is bookkeeping; the halt must fire even without llm config."""
        config = {
            "script_quality": {
                "min_score": 0.50,
                "enforce_min_score": True,
                "modal_ban_enabled": True,
            },
        }
        script = _make_script(body="AI might replace your doctor.")
        with pytest.raises(ScriptRuleViolation) as excinfo:
            evaluate_script_quality(script, config)
        assert excinfo.value.attempt == 1
