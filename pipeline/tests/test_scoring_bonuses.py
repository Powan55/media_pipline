"""Bonus-function tests for scoring.py.

Covers _counter_conventional_bonus, _ai_vendor_bonus, and _named_human_bonus.
Each bonus is an additive +0.05 nudge on top of the weighted-component sum;
these tests exercise the regex patterns directly and the rank_candidates
integration path.

Added 2026-05-12 alongside _named_human_bonus to give the new function (and
the two pre-existing siblings) a regression net before the next idea-gen run.
"""
from __future__ import annotations

import pytest

from scoring import (
    _AI_VENDOR_BONUS,
    _COUNTER_CONVENTIONAL_BONUS,
    _NAMED_HUMAN_BONUS,
    _ai_vendor_bonus,
    _counter_conventional_bonus,
    _named_human_bonus,
    rank_candidates,
)


# -----------------------------------------------------------------------------
# _counter_conventional_bonus
# -----------------------------------------------------------------------------

class TestCounterConventionalBonus:
    @pytest.mark.parametrize("text", [
        "the win nobody mentions",
        "no one talks about this",
        "most devs ignore this setting",
        "the trap nobody catches",
        "Anthropic did Y, but the real story is X",
    ])
    def test_positive_matches(self, text: str) -> None:
        assert _counter_conventional_bonus(text) == _COUNTER_CONVENTIONAL_BONUS

    @pytest.mark.parametrize("text", [
        "Claude shipped a new feature today",
        "OpenAI released GPT-5.5",
        "Anthropic announced a partnership with AWS",
        "",
    ])
    def test_negative_matches(self, text: str) -> None:
        assert _counter_conventional_bonus(text) == 0.0

    def test_multiple_texts_joined(self) -> None:
        # Pattern only needs to fire in ONE of the joined inputs.
        assert _counter_conventional_bonus(
            "Claude shipped a feature",
            "But the win nobody mentions is the pricing.",
            "",
        ) == _COUNTER_CONVENTIONAL_BONUS


# -----------------------------------------------------------------------------
# Counter-conventional broadening — cycle-4 named-authority verbs (2026-05-12)
#
# Sprint 3 / item 2: the 2026-05-12 /start -auto cycle-4 candidates all scored
# 0.000 on counter_conventional_bonus because the regex was too literal. These
# fixtures are the verbatim 5 cycle-4 phrasings that should now register, plus
# 3 truly-neutral phrasings that must continue to return 0.0.
# -----------------------------------------------------------------------------

CYCLE_4_POSITIVE_CASES: list[dict[str, str]] = [
    {
        "label": "anthropic-sycophancy (Anthropic studied X, but)",
        "topic": "Anthropic studied 1 million Claude chats. In relationships, Claude is sycophantic 25% of the time — Anthropic itself published the receipts.",
        "angle": "Anthropic studied Claude, but their own paper shows the chatbot tells people what they want to hear in one of four relationship conversations.",
        "hook_concept": "Anthropic just told on Claude. Twenty-five percent.",
    },
    {
        "label": "jamie-dimon (called X a fraud, but)",
        "topic": "Jamie Dimon admitted Claude built him a Treasury dashboard in 20 minutes — the bank CEO most known for tech skepticism is personally using Claude Code.",
        "angle": "JPMorgan's Jamie Dimon called crypto a fraud and dismissed Web3, but on stage at Anthropic's Code With Claude event he revealed he uses Claude Code himself.",
        "hook_concept": "Jamie Dimon admitted he uses Claude. Twenty minutes.",
    },
    {
        "label": "deepmind-pentagon (tried to stop)",
        "topic": "A Google DeepMind scientist named Alex Turner publicly admitted he spent two months trying to stop Gemini from being deployed at the Pentagon, and lost.",
        "angle": "Google's official line is that DeepMind employees support military AI work, but research scientist Alex Turner went on the record saying he tried for two months to block it.",
        "hook_concept": "Alex Turner tried to stop Gemini. Two months.",
    },
    {
        "label": "murati-thinking-machines (every AI waits, but)",
        "topic": "Mira Murati's new startup just unveiled an AI assistant that replies in 0.4 seconds — faster than human reaction time.",
        "angle": "Every AI assistant waits for you to stop talking, but Mira Murati's Thinking Machines just shipped TML-Interaction-Small with a 0.4-second response latency.",
        "hook_concept": "Mira Murati's AI replies in zero point four seconds.",
    },
    {
        "label": "karpathy-pivot (most-cited voice for X, but)",
        "topic": "Andrej Karpathy publicly said he stopped using AI to write his code — the man who coined vibe coding is now using AI to organize his notes instead.",
        "angle": "Karpathy is the most-cited public voice for AI writes your code now, but an April 2026 Medium piece and his own X posts show he pivoted off that workflow — he uses LLMs to build a wiki from raw notes, not to ship features.",
        "hook_concept": "Karpathy quit using AI to write code.",
    },
]

NEUTRAL_NEGATIVE_CASES: list[str] = [
    "Anthropic released a new feature today.",
    "ChatGPT 5.5 is faster than ChatGPT 5.0.",
    "Cursor 0.9.2 ships with a redesigned UI.",
]


@pytest.mark.parametrize("case", CYCLE_4_POSITIVE_CASES, ids=lambda c: c["label"])
def test_counter_conventional_bonus_named_authority_verbs(case: dict[str, str]) -> None:
    """Each cycle-4 phrasing must score the full counter-conventional bonus.

    Drives the broadening of `_COUNTER_CONVENTIONAL_PATTERNS` to cover vendor
    self-snitch verbs (admits, studied, exposed, told on), named-authority
    reaction verbs (tried to stop, contradicts, shocked, got caught), and a
    wider "X <verb> Y, but ..." setup pattern. Each fixture is the verbatim
    topic / angle / hook_concept from the 2026-05-12 /start -auto cycle-4 run.
    """
    assert _counter_conventional_bonus(
        case["topic"], case["angle"], case["hook_concept"]
    ) == _COUNTER_CONVENTIONAL_BONUS, f"failed for case: {case['label']}"


@pytest.mark.parametrize("text", NEUTRAL_NEGATIVE_CASES)
def test_counter_conventional_bonus_neutral_phrasings_unchanged(text: str) -> None:
    """Truly-neutral product announcements must continue to return 0.0.

    Guards against the broadened verb set (admitted, studied, shipped, etc.)
    over-firing on plain vendor news that lacks a contradiction or self-snitch.
    """
    assert _counter_conventional_bonus(text) == 0.0, f"unexpected bonus for: {text!r}"


# -----------------------------------------------------------------------------
# _ai_vendor_bonus
# -----------------------------------------------------------------------------

class TestAiVendorBonus:
    @pytest.mark.parametrize("text", [
        "Claude just shipped agent view",
        "OpenAI announced a new company",
        "Gemini multimodal file search",
        "ChatGPT 5.5 Pro got slower",
        "AI agents will replace your job",
    ])
    def test_positive_matches(self, text: str) -> None:
        assert _ai_vendor_bonus(text) == _AI_VENDOR_BONUS

    @pytest.mark.parametrize("text", [
        "uv replaces pip in CI",
        "ruff has a new noqa rule",
        "Generic productivity tip",
        "",
    ])
    def test_negative_matches(self, text: str) -> None:
        assert _ai_vendor_bonus(text) == 0.0


# -----------------------------------------------------------------------------
# _named_human_bonus (new 2026-05-12)
# -----------------------------------------------------------------------------

class TestNamedHumanBonus:
    @pytest.mark.parametrize("text", [
        # Reddit handle
        "u/airider says GPT-5 wrote his vows",
        # HN reference
        "hn:38291841 reports Claude rewrote his test",
        # X / Twitter handle
        "@karpathy stopped using AI to write code",
        # "named <First> <Last>"
        "A researcher named Adam Dunkels made Claude pretend to be the internet",
        # "a <role> named ..."
        "A scientist named Alex Turner tried to stop the deal",
        "An OpenAI researcher named Mira Murati announced a new product",
        # Vendor + titled role + bare name
        "Anthropic CEO Dario Amodei admitted on stage",
        "OpenAI's CTO Mira Murati launched a startup",
        "a16z partner Justine Moore reverse-engineered the model",
        # Known-figure allowlist
        "Tim Gowers tested ChatGPT 5.5 Pro on PhD math",
        "Mark Gurman reports iOS 27 will let you swap ChatGPT",
        "Andrej Karpathy quit using AI to write code",
        # Titled-expert reference
        "A Fields medalist tested the new model",
        "A Nobel laureate weighed in on the AI race",
    ])
    def test_positive_matches(self, text: str) -> None:
        assert _named_human_bonus(text) == _NAMED_HUMAN_BONUS

    @pytest.mark.parametrize("text", [
        "Claude shipped a new feature today",
        "A developer says the new tool is faster",  # anonymous "a developer says" — rejected
        "OpenAI announced GPT-5.5",  # vendor only, no human
        "uv replaces pip in CI",  # no AI, no human
        "",
    ])
    def test_negative_matches(self, text: str) -> None:
        assert _named_human_bonus(text) == 0.0

    def test_signal_from_cited_observation_handle(self) -> None:
        # The cited_observation source_handle is one of the canonical sources.
        assert _named_human_bonus(
            "Generic AI news headline",  # topic
            "",  # angle
            "",  # hook
            "u/airider",  # cited_observation.source_handle
        ) == _NAMED_HUMAN_BONUS

    # -----------------------------------------------------------------------------
    # Cycle-9 allowlist extension (2026-05-16) — Sam Bowman regression case
    #
    # The /start -auto cycle-9 idea-gen passed over a textbook Claude-internet
    # template candidate ("Anthropic safety researcher Sam Bowman got an email
    # from Claude Mythos sent from outside its sandbox") because Sam Bowman
    # wasn't on the allowlist. Add representative fixtures from each cohort
    # extended below, plus a negative case to guard against the allowlist
    # over-firing on arbitrary capitalized two-word phrases.
    # -----------------------------------------------------------------------------

    def test_sam_bowman_cycle9_regression(self) -> None:
        # The verbatim regression case from cycle 9: must now register.
        assert _named_human_bonus(
            "Anthropic safety researcher Sam Bowman got an email from Claude Mythos"
        ) == _NAMED_HUMAN_BONUS

    def test_demis_hassabis_matches(self) -> None:
        # Google DeepMind cohort representative.
        assert _named_human_bonus("Demis Hassabis on Gemini 3") == _NAMED_HUMAN_BONUS

    def test_karen_hao_matches(self) -> None:
        # Independent journalist cohort representative.
        assert _named_human_bonus(
            "Karen Hao reports on the latest Anthropic paper"
        ) == _NAMED_HUMAN_BONUS

    def test_unlisted_name_does_not_match(self) -> None:
        # Guard against the allowlist over-firing on arbitrary capitalized names.
        # "John Smith" is a plain capitalized two-word phrase with no vendor + role
        # prefix, no "named" marker, and no allowlist entry — should NOT match.
        assert _named_human_bonus("John Smith said AI is cool") == 0.0


# -----------------------------------------------------------------------------
# rank_candidates integration — bonuses are surfaced on the ScoredCandidate
# -----------------------------------------------------------------------------

class TestRankCandidatesIntegration:
    def _baseline_candidate(self) -> dict:
        return {
            "topic": "Generic AI news",
            "angle": "Vendor shipped a thing",
            "hook_concept": "AI did stuff",
            "scores": {
                "niche_fit": 0.5,
                "hook_strength": 0.5,
                "specificity": 0.5,
                "trend_signal": 0.5,
                "verifiability": 0.5,
                "broll_feasibility": 0.5,
                "observation_availability": 0.5,
                "anti_cannibalization": 0.5,
            },
        }

    def test_no_bonuses_when_no_signals(self) -> None:
        c = self._baseline_candidate()
        result = rank_candidates([c])[0]
        # Baseline contains the word "AI" — _ai_vendor_bonus DOES fire (generic AI mention),
        # so we expect ai_vendor_bonus only.
        assert result.counter_conventional_bonus == 0.0
        assert result.named_human_bonus == 0.0
        assert result.ai_vendor_bonus == _AI_VENDOR_BONUS

    def test_named_human_bonus_via_cited_observation(self) -> None:
        c = self._baseline_candidate()
        c["cited_observation_candidate"] = {
            "summary": "A Fields medalist named Tim Gowers tested it.",
            "source_handle": "Tim Gowers blog",
            "retrievable_quote": "PhD math in an hour",
        }
        result = rank_candidates([c])[0]
        assert result.named_human_bonus == _NAMED_HUMAN_BONUS

    def test_all_three_bonuses_compound_additively(self) -> None:
        c = self._baseline_candidate()
        c["topic"] = "Anthropic CEO Dario Amodei admitted the win nobody mentions about Claude"
        # Contains: named human ("Anthropic CEO Dario Amodei"), counter-conventional
        # ("the win nobody mentions"), and AI vendor ("Anthropic", "Claude")
        result = rank_candidates([c])[0]
        assert result.named_human_bonus == _NAMED_HUMAN_BONUS
        assert result.counter_conventional_bonus == _COUNTER_CONVENTIONAL_BONUS
        assert result.ai_vendor_bonus == _AI_VENDOR_BONUS
        # All three bonuses must show up in weighted_total
        expected_min_delta = _NAMED_HUMAN_BONUS + _COUNTER_CONVENTIONAL_BONUS + _AI_VENDOR_BONUS
        baseline = rank_candidates([self._baseline_candidate()])[0]
        # Baseline has ai_vendor_bonus but not the other two; the delta against this
        # candidate should be at least _NAMED_HUMAN_BONUS + _COUNTER_CONVENTIONAL_BONUS
        # (the ai_vendor_bonus is already in the baseline).
        delta = result.weighted_total - baseline.weighted_total
        assert delta == pytest.approx(_NAMED_HUMAN_BONUS + _COUNTER_CONVENTIONAL_BONUS, abs=1e-4)
