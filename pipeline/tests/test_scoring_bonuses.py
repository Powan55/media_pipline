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

import scoring
from scoring import (
    _AI_VENDOR_BONUS,
    _COUNTER_CONVENTIONAL_BONUS,
    _HIGH_SALIENCE_ANCHOR_BONUS,
    _NAMED_HUMAN_BONUS,
    DEFAULT_CORPORATE_DEAL_BONUS_DAMP,
    DEFAULT_CORPORATE_DEAL_PENALTY,
    DEFAULT_WEIGHTS,
    GENERAL_TECH_WEIGHTS_PATH,
    WEIGHTS_PATH,
    _ai_vendor_bonus,
    _counter_conventional_bonus,
    _high_salience_anchor_bonus,
    _is_corporate_deal,
    _named_human_bonus,
    load_weights,
    rank_candidates,
    weights_path_for_track,
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


# -----------------------------------------------------------------------------
# Corporate-FINANCE-deal damp (2026-06-11, CODE_AUDIT item (d))
#
# A dead-floor corporate-finance topic (OpenAI S-1 / IPO) bonus-stacked its way
# to rank-2 via ai_vendor + named_human bonuses. The damp multiplies those two
# bonuses by corporate_deal_bonus_damp (0.25) and subtracts corporate_deal_penalty
# (0.10) ONLY when FINANCIAL deal language is present — never on generic
# collaboration words. The two MUST-NOT-DAMP regression anchors are legit picks.
# -----------------------------------------------------------------------------

class TestIsCorporateDeal:
    @pytest.mark.parametrize("text", [
        "OpenAI files for an IPO",
        "OpenAI's S-1 reveals the numbers",
        "Anthropic raises $4 billion in a new funding round",
        "the startup raised $200 million",
        "a fresh funding round led by SoftBank",
        "the deal values the company at a $90 billion valuation",
        "a Series C round",
        "Series F at last",
        "the SPAC merger closed",
        "Acme acquires a rival lab",          # plural-stem 'acquires'
        "the acquisition of the chip startup",
        "a flurry of acquisitions this quarter",  # plural 'acquisitions'
        "investors poured in",
        "a single investor backed it",
    ])
    def test_financial_language_matches(self, text: str) -> None:
        assert _is_corporate_deal(text) is True

    @pytest.mark.parametrize("text", [
        # The two MUST-NOT-DAMP regression anchors.
        "Apple reveals new AI architecture built around Google Gemini",
        "Apple's new Siri secretly runs on Google's Gemini",
        # Generic collaboration words that MUST NOT match.
        "Anthropic announced a partnership with AWS",
        "a new deal between two AI labs",
        "the app is powered by Claude",
        "an AI assistant built around your calendar",
        # Word-boundary guard: 'requisition' must NOT match 'acquisition'.
        "the purchase requisition form was updated",
        "",
    ])
    def test_non_financial_language_does_not_match(self, text: str) -> None:
        assert _is_corporate_deal(text) is False

    def test_case_insensitive(self) -> None:
        assert _is_corporate_deal("openai ipo filing") is True
        assert _is_corporate_deal("ACQUISITIONS galore") is True
        assert _is_corporate_deal("Series a round") is True


class TestCorporateDealDamp:
    def _consumer_candidate(self) -> dict:
        """A mid-tier consumer-AI candidate: ai_vendor fires, no finance language."""
        return {
            "topic": "ChatGPT now remembers your past chats across sessions",
            "angle": "OpenAI quietly shipped long-term memory to ChatGPT",
            "hook_concept": "ChatGPT just got a memory upgrade",
            "scores": {
                "niche_fit": 0.6,
                "hook_strength": 0.6,
                "specificity": 0.6,
                "trend_signal": 0.6,
                "verifiability": 0.6,
                "broll_feasibility": 0.6,
                "observation_availability": 0.6,
                "anti_cannibalization": 0.6,
            },
        }

    def _s1_candidate(self) -> dict:
        """An OpenAI-S-1-style finance candidate that names a vendor + a CEO.

        Without the damp it would farm ai_vendor (OpenAI) + named_human
        (OpenAI CEO Sam Altman) bonuses on top of a deliberately HIGHER base
        than the consumer candidate, floating it above the floor.
        """
        return {
            "topic": "OpenAI files for a $300 billion IPO",
            "angle": "OpenAI's S-1 names Sam Altman as the CEO leading the offering",
            "hook_concept": "OpenAI just filed to go public",
            "scores": {
                # Higher base than the consumer candidate, to mimic the LLM
                # over-scoring a press-release topic (the footgun).
                "niche_fit": 0.65,
                "hook_strength": 0.65,
                "specificity": 0.65,
                "trend_signal": 0.65,
                "verifiability": 0.65,
                "broll_feasibility": 0.65,
                "observation_availability": 0.65,
                "anti_cannibalization": 0.65,
            },
        }

    def test_s1_candidate_drops_below_consumer_after_damp(self) -> None:
        """(i) The finance candidate ranks BELOW the consumer one it previously beat."""
        consumer = self._consumer_candidate()
        s1 = self._s1_candidate()

        ranked = rank_candidates([s1, consumer])
        topics = [r.topic for r in ranked]
        # The consumer candidate must now outrank the S-1 candidate.
        assert topics.index(consumer["topic"]) < topics.index(s1["topic"])

        s1_scored = next(r for r in ranked if r.topic == s1["topic"])
        consumer_scored = next(r for r in ranked if r.topic == consumer["topic"])
        assert s1_scored.corporate_deal_damped is True
        assert s1_scored.corporate_deal_penalty == pytest.approx(DEFAULT_CORPORATE_DEAL_PENALTY)
        assert consumer_scored.corporate_deal_damped is False
        # The damped finance bonuses are recorded as their post-damp value.
        assert s1_scored.ai_vendor_bonus == pytest.approx(
            _AI_VENDOR_BONUS * DEFAULT_CORPORATE_DEAL_BONUS_DAMP
        )
        assert s1_scored.named_human_bonus == pytest.approx(
            _NAMED_HUMAN_BONUS * DEFAULT_CORPORATE_DEAL_BONUS_DAMP
        )
        assert s1_scored.weighted_total < consumer_scored.weighted_total

    def test_would_have_beaten_consumer_without_damp(self) -> None:
        """Sanity: with the damp disabled, the S-1 candidate DOES beat the consumer one.

        Confirms the damp is what flips the ranking, not the base scores alone.
        """
        consumer = self._consumer_candidate()
        s1 = self._s1_candidate()

        # Disable the damp by forcing tuning to (1.0, 0.0).
        import scoring as _scoring
        orig = _scoring.load_corporate_deal_tuning
        _scoring.load_corporate_deal_tuning = lambda *a, **k: (1.0, 0.0)
        try:
            ranked = rank_candidates([s1, consumer])
        finally:
            _scoring.load_corporate_deal_tuning = orig

        topics = [r.topic for r in ranked]
        # Without the damp, the higher-base S-1 candidate wins.
        assert topics.index(s1["topic"]) < topics.index(consumer["topic"])

    def test_must_not_damp_anchor_built_around(self, monkeypatch) -> None:
        """(ii) "...built around Google Gemini" scores IDENTICALLY with/without the damp."""
        candidate = {
            "topic": "Apple reveals new AI architecture built around Google Gemini",
            "angle": "Apple's new on-device model is built around Google Gemini",
            "hook_concept": "Apple's AI secretly leans on Gemini",
            "scores": {k: 0.7 for k in (
                "niche_fit", "hook_strength", "specificity", "trend_signal",
                "verifiability", "broll_feasibility", "observation_availability",
                "anti_cannibalization",
            )},
        }
        with_damp = rank_candidates([candidate])[0]
        assert with_damp.corporate_deal_damped is False
        assert with_damp.corporate_deal_penalty == 0.0

        # Force the damp ON for everything would change a damped score — here it must NOT,
        # because the detector returns False. Compare against a run with damp disabled.
        monkeypatch.setattr(scoring, "load_corporate_deal_tuning", lambda *a, **k: (1.0, 0.0))
        no_damp = rank_candidates([candidate])[0]
        assert with_damp.weighted_total == pytest.approx(no_damp.weighted_total)

    def test_must_not_damp_anchor_siri_runs_on_gemini(self, monkeypatch) -> None:
        """(ii) "Apple's new Siri secretly runs on Google's Gemini" scores unchanged."""
        candidate = {
            "topic": "Apple's new Siri secretly runs on Google's Gemini",
            "angle": "Apple licensed Google's Gemini to power the next Siri",
            "hook_concept": "Siri is secretly Gemini now",
            "scores": {k: 0.7 for k in (
                "niche_fit", "hook_strength", "specificity", "trend_signal",
                "verifiability", "broll_feasibility", "observation_availability",
                "anti_cannibalization",
            )},
        }
        with_damp = rank_candidates([candidate])[0]
        assert with_damp.corporate_deal_damped is False

        monkeypatch.setattr(scoring, "load_corporate_deal_tuning", lambda *a, **k: (1.0, 0.0))
        no_damp = rank_candidates([candidate])[0]
        assert with_damp.weighted_total == pytest.approx(no_damp.weighted_total)

    def test_disabled_tuning_behaves_identically_to_before(self, monkeypatch) -> None:
        """(iii) damp disabled (damp=1.0, penalty=0.0) == pre-feature behavior.

        For a finance candidate, the disabled-damp score must equal base + the
        UNDAMPED bonuses, i.e. exactly what the old code produced.
        """
        s1 = self._s1_candidate()

        monkeypatch.setattr(scoring, "load_corporate_deal_tuning", lambda *a, **k: (1.0, 0.0))
        scored = rank_candidates([s1])[0]
        # Bonuses are recorded undamped (multiplied by 1.0), penalty 0.0 applied.
        assert scored.ai_vendor_bonus == pytest.approx(_AI_VENDOR_BONUS)
        assert scored.named_human_bonus == pytest.approx(_NAMED_HUMAN_BONUS)
        assert scored.corporate_deal_penalty == 0.0
        # corporate_deal_damped still reports the DETECTION (the language is present),
        # but with damp=1.0/penalty=0.0 the number is unchanged from the old behavior.
        assert scored.corporate_deal_damped is True

    def test_weighted_total_floored_at_zero(self, monkeypatch) -> None:
        """The penalty can never drive weighted_total negative (floor at 0)."""
        # All-zero base, finance language present, an outsized penalty.
        candidate = {
            "topic": "OpenAI IPO",
            "angle": "the offering",
            "hook_concept": "go public",
            "scores": {k: 0.0 for k in (
                "niche_fit", "hook_strength", "specificity", "trend_signal",
                "verifiability", "broll_feasibility", "observation_availability",
                "anti_cannibalization",
            )},
        }
        monkeypatch.setattr(scoring, "load_corporate_deal_tuning", lambda *a, **k: (0.25, 5.0))
        scored = rank_candidates([candidate])[0]
        assert scored.weighted_total == 0.0


# -----------------------------------------------------------------------------
# Dual-track plumbing (2026-06-21) — per-track weights + AI-vendor-bonus suppress
#
# The general-tech track (broad consumer-tech + crazy-story slot) uses its own
# weights profile and suppresses the AI-vendor +0.05 bonus so the scorer doesn't
# bias against iPhone/Meta/Windows/Tesla topics. The ai-vendor track and all
# pre-existing callers must be byte-identical (suppress defaults False).
# -----------------------------------------------------------------------------

class TestWeightsPathForTrack:
    def test_ai_vendor_track_uses_default_weights_path(self) -> None:
        assert weights_path_for_track("ai-vendor") == WEIGHTS_PATH

    def test_default_arg_is_ai_vendor(self) -> None:
        assert weights_path_for_track() == WEIGHTS_PATH

    def test_general_tech_track_uses_profile_path(self) -> None:
        assert weights_path_for_track("general-tech") == GENERAL_TECH_WEIGHTS_PATH

    def test_unknown_track_falls_back_to_ai_vendor(self) -> None:
        # Fail-soft: an unrecognized track name returns the default path.
        assert weights_path_for_track("nonsense-track") == WEIGHTS_PATH

    def test_general_tech_profile_file_exists_and_is_valid(self) -> None:
        # The profile must be on disk, carry exactly the 8 component keys, and
        # (after load_weights normalization) sum to 1.0.
        assert GENERAL_TECH_WEIGHTS_PATH.exists(), "general-tech weights profile missing"
        w = load_weights(GENERAL_TECH_WEIGHTS_PATH)
        assert set(w) == set(DEFAULT_WEIGHTS)
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)

    def test_general_tech_profile_lowers_niche_fit_vs_default(self) -> None:
        # Sanity on the intended re-weighting direction (not exact values, which
        # may be tuned): general-tech down-weights niche_fit, up-weights hook.
        w = load_weights(GENERAL_TECH_WEIGHTS_PATH)
        assert w["niche_fit"] < DEFAULT_WEIGHTS["niche_fit"]
        assert w["hook_strength"] > DEFAULT_WEIGHTS["hook_strength"]


class TestSuppressAiVendorBonus:
    def _ai_candidate(self) -> dict:
        return {
            "topic": "ChatGPT just shipped a memory upgrade",
            "angle": "OpenAI quietly added long-term memory to ChatGPT",
            "hook_concept": "ChatGPT now remembers your chats",
            "scores": {k: 0.5 for k in DEFAULT_WEIGHTS},
        }

    def test_bonus_fires_by_default(self) -> None:
        # Pre-existing behavior: the AI-vendor bonus applies when not suppressed.
        result = rank_candidates([self._ai_candidate()])[0]
        assert result.ai_vendor_bonus == _AI_VENDOR_BONUS

    def test_bonus_suppressed_for_general_tech(self) -> None:
        result = rank_candidates(
            [self._ai_candidate()], suppress_ai_vendor_bonus=True
        )[0]
        assert result.ai_vendor_bonus == 0.0

    def test_suppress_lowers_weighted_total_by_exactly_the_bonus(self) -> None:
        c = self._ai_candidate()
        on = rank_candidates([c])[0]
        off = rank_candidates([c], suppress_ai_vendor_bonus=True)[0]
        assert (on.weighted_total - off.weighted_total) == pytest.approx(
            _AI_VENDOR_BONUS, abs=1e-4
        )

    def test_suppress_does_not_touch_other_bonuses(self) -> None:
        # A general-tech crazy-story candidate: named human + counter-conventional
        # must still apply even with the AI-vendor bonus suppressed.
        c = {
            "topic": "A nurse named Sarah Chen caught a misdiagnosis three doctors missed",
            "angle": "Sarah Chen did the obvious thing, but her phone's camera spotted it",
            "hook_concept": "She caught what nobody mentions",
            "scores": {k: 0.5 for k in DEFAULT_WEIGHTS},
        }
        result = rank_candidates([c], suppress_ai_vendor_bonus=True)[0]
        assert result.ai_vendor_bonus == 0.0
        assert result.named_human_bonus == _NAMED_HUMAN_BONUS
        assert result.counter_conventional_bonus == _COUNTER_CONVENTIONAL_BONUS


# -----------------------------------------------------------------------------
# _high_salience_anchor_bonus (2026-06-24 analytics deep-dive)
#
# Rewards big CONSUMER-TECH named entities (Apple/iPhone/Tesla/Neuralink/Windows/
# Meta...) that _ai_vendor_bonus misses. Lexicon is DISJOINT from the AI-vendor
# set (no double-count), TRACK-AGNOSTIC (fires even when ai-vendor is suppressed —
# the general-tech gap-fill), and damped on FINANCIAL-deal topics.
# -----------------------------------------------------------------------------

class TestHighSalienceAnchorBonus:
    @pytest.mark.parametrize("text", [
        "Apple just killed the home button again",
        "iPhone 19 ships a periscope camera",
        "Tesla's Optimus folded the laundry",
        "Elon's Neuralink let a paralyzed man play Warcraft",
        "Windows 12 quietly added an AI layer",
        "Meta's Quest 4 leaked overnight",
        "A Boston Dynamics robot ran a marathon",
        "Samsung Galaxy fold survived a year",
        "PlayStation 6 specs just leaked",
        "Nvidia's new chip melts benchmarks",
    ])
    def test_positive_matches(self, text: str) -> None:
        assert _high_salience_anchor_bonus(text) == _HIGH_SALIENCE_ANCHOR_BONUS

    @pytest.mark.parametrize("text", [
        # AI-vendor entities belong to _ai_vendor_bonus, NOT here (disjoint).
        "Claude shipped a new feature",
        "OpenAI announced GPT-5.5",
        "Gemini reads your PDFs",
        # No recognizable consumer-tech entity.
        "A developer optimized a build pipeline",
        "Generic productivity tip",
        "",
    ])
    def test_negative_matches(self, text: str) -> None:
        assert _high_salience_anchor_bonus(text) == 0.0

    def test_disjoint_from_ai_vendor(self) -> None:
        # An AI-vendor-only topic gets ai_vendor but NOT the anchor bonus.
        assert _ai_vendor_bonus("Claude did X") == _AI_VENDOR_BONUS
        assert _high_salience_anchor_bonus("Claude did X") == 0.0

    def test_fires_on_general_tech_track_when_ai_vendor_suppressed(self) -> None:
        # THE gap-fill: a Tesla/Neuralink general-tech topic still earns an entity
        # reward even with the AI-vendor bonus suppressed.
        c = {
            "topic": "Tesla's Optimus robot walked itself out of the factory",
            "angle": "Tesla shipped a humanoid that does a real chore",
            "hook_concept": "Tesla's robot just clocked out",
            "scores": {k: 0.5 for k in DEFAULT_WEIGHTS},
        }
        result = rank_candidates([c], suppress_ai_vendor_bonus=True)[0]
        assert result.ai_vendor_bonus == 0.0
        assert result.high_salience_anchor_bonus == _HIGH_SALIENCE_ANCHOR_BONUS

    def test_can_co_occur_with_ai_vendor(self) -> None:
        # A dual-entity topic earns BOTH (disjoint entities, both legitimately present).
        c = {
            "topic": "Apple's Siri secretly runs on Google's Gemini",
            "angle": "Apple licensed Gemini to power the next Siri",
            "hook_concept": "Siri is Gemini now",
            "scores": {k: 0.5 for k in DEFAULT_WEIGHTS},
        }
        result = rank_candidates([c])[0]
        assert result.ai_vendor_bonus == _AI_VENDOR_BONUS      # Gemini
        assert result.high_salience_anchor_bonus == _HIGH_SALIENCE_ANCHOR_BONUS  # Apple/Siri

    def test_damped_on_financial_deal(self) -> None:
        # A finance topic naming a consumer-tech entity gets the anchor bonus damped
        # alongside its siblings (no farming the floor).
        c = {
            "topic": "Apple acquires a robotics startup for $4 billion",
            "angle": "the acquisition values the lab at a fresh valuation",
            "hook_concept": "Apple just bought its way into robots",
            "scores": {k: 0.5 for k in DEFAULT_WEIGHTS},
        }
        result = rank_candidates([c])[0]
        assert result.corporate_deal_damped is True
        assert result.high_salience_anchor_bonus == pytest.approx(
            _HIGH_SALIENCE_ANCHOR_BONUS * DEFAULT_CORPORATE_DEAL_BONUS_DAMP
        )
