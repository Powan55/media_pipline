"""Topic-candidate scoring for ShadowVerse idea-generation.

Each candidate idea (output of the idea_generation prompt) has 8 component scores
in 0.0..1.0. The total is a weighted sum. Weights live in `scoring_weights.json`
so they can be tuned once analytics data accumulates without code changes.

Component meanings (kept in sync with prompts/02_idea_generation.md):
  - niche_fit: how well the topic matches the channel's "general-consumer AI" niche
    AND passes the hard accessibility rule a non-coder grasps the title in 2 seconds
    (1.0 clearly accessible AI-vendor / AI-app / AI-news topic, 0.0 pure dev-infra).
    Pivoted 2026-05-07 evening from "mid-career devs" to general consumer.
  - hook_strength: quality of the proposed hook concept (1.0 strong claim/measured/contrarian
    in plain English, 0.0 generic OR requires dev knowledge to grasp)
  - specificity: how concrete vs vague (1.0 names a specific AI product/version/date/result,
    0.0 abstract)
  - trend_signal: timeliness per source data (HN points, GH stars, recent release date,
    news cycle)
  - verifiability: are the technical claims fact-checkable (1.0 clean, 0.0 opinion-only)
  - broll_feasibility: can we get clean general-audience stock footage (person on phone,
    AI chat on screen, AI-generated images, hands typing on a laptop showing an AI app).
    1.0 obvious consumer-facing matches, 0.0 needs custom screen recording. Note:
    dev-themed stock (terminals, code editors, IDE screens) is NOT a strength here.
  - observation_availability: confidence a real cited observation exists in source data
    or via easy web search (1.0 explicit, 0.0 would need to fabricate)
  - anti_cannibalization: differs from the channel's last-30-day topics (1.0 novel,
    0.0 direct rehash)

Pipeline integration:
  pipeline.idea_generation_stage()  →  loads trends_<DATE>.json
                                    →  runs prompts/02_idea_generation.md (manual LLM)
                                    →  parses JSON list of candidates
                                    →  scoring.rank_candidates(candidates)
                                    →  pick top N (default N=2 from config)
                                    →  spawn topic dirs for each pick
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

log = logging.getLogger("scoring")

# Default weights — sum to 1.0. Tune `scoring_weights.json` once we have analytics.
# These priors lean on:
#   - niche_fit and specificity dominate (the channel's brand-defining features)
#   - hook_strength matters but post-launch retention is the truer signal
#   - trend_signal modest weight (timeliness helps but isn't decisive for tactical content)
#   - anti_cannibalization sufficient to nudge away from rehashes
DEFAULT_WEIGHTS: dict[str, float] = {
    "niche_fit":                0.18,
    "hook_strength":            0.15,
    "specificity":              0.18,
    "trend_signal":             0.10,
    "verifiability":            0.10,
    "broll_feasibility":        0.07,
    "observation_availability": 0.12,
    "anti_cannibalization":     0.10,
}

WEIGHTS_PATH = Path(__file__).resolve().parent / "scoring_weights.json"

# Per-track scoring-weights profiles. The ai-vendor track uses the default
# scoring_weights.json; the general-tech track (broad consumer-tech + crazy-story
# slot, added 2026-06-21) uses a profile that lowers niche_fit (the AI-niche
# accessibility rubric doesn't apply) and raises hook_strength / verifiability /
# broll_feasibility (hook quality, fact-checkability of human-interest claims, and
# stock-footage availability for product/people shots all matter more here).
GENERAL_TECH_WEIGHTS_PATH = Path(__file__).resolve().parent / "scoring_weights_general_tech.json"

_TRACK_WEIGHTS_PATHS: dict[str, Path] = {
    "ai-vendor": WEIGHTS_PATH,
    "general-tech": GENERAL_TECH_WEIGHTS_PATH,
}


def weights_path_for_track(track: str = "ai-vendor") -> Path:
    """Return the scoring-weights JSON path for a track.

    Unknown track falls back to the ai-vendor default (fail-soft, mirroring
    load_weights' behavior on a missing/invalid file). The general-tech profile
    is created in Phase 1; if it is absent on disk, load_weights() returns
    DEFAULT_WEIGHTS, so a missing profile degrades to ai-vendor weights rather
    than crashing.
    """
    return _TRACK_WEIGHTS_PATHS.get(track, WEIGHTS_PATH)


# Counter-conventional framing produces an additive bonus on top of the weighted-component
# sum. Capped at _COUNTER_CONVENTIONAL_BONUS so it nudges ranking without overriding the
# 8-component model. Calibrated 2026-05-06 from analytics on the 2026-05-05 batch: the
# "win nobody mentions" framing got ~5x reach vs a neutral-titled peer on the same channel
# the same day. Intent: promote contrarian framings, don't dictate.
_COUNTER_CONVENTIONAL_BONUS: float = 0.05

_COUNTER_CONVENTIONAL_PATTERNS: list[re.Pattern] = [
    # "nobody mentions / talks about / notices / catches / knows"
    re.compile(r"\bnobody\s+(?:mentions?|talks?\s+about|notices?|catches?|knows?)\b", re.IGNORECASE),
    # "no one mentions / talks about / ..."
    re.compile(r"\bno\s+one\s+(?:mentions?|talks?\s+about|notices?|catches?|knows?)\b", re.IGNORECASE),
    # "most devs / users / people ignore / miss / skip / don't / never / fail to"
    re.compile(r"\bmost\s+(?:devs?|developers?|users?|people)\s+(?:ignore|miss|skip|don'?t|never|fail\s+to)\b", re.IGNORECASE),
    # "the win / trap / flaw / catch / gotcha / secret (that) nobody / no one ..."
    re.compile(r"\bthe\s+(?:win|trap|flaw|catch|gotcha|secret)\s+(?:that\s+)?(?:nobody|no\s+one)\b", re.IGNORECASE),
    # "X did Y, but ..." — original subject-verb-object contradiction setup (kept for back-compat).
    re.compile(r"\b\w+\s+did\s+\w+,?\s+but\b", re.IGNORECASE),
    # Broadened "X <verb> Y[ ... ], but ..." — same shape as the line above but with a wider
    # reaction-verb set and up to ~12 intervening tokens before "but" (so phrasings like
    # "Dimon called crypto a fraud and dismissed Web3, but on stage..." or "Karpathy is the
    # most-cited public voice for AI writes your code now, but..." still register). Verb list
    # mirrors the operator-specified expansion (2026-05-12). Intervening-window cap keeps the
    # pattern from spanning unrelated sentences.
    # Example: "Anthropic studied Claude, but their own paper shows..."
    re.compile(
        r"\b\w+\s+(?:did|admits?|admitted|studied|investigated|tried|claims?|claimed|"
        r"promises?|promised|launched|launches?|shipped|ships?|said|says?|told|publishes?|"
        r"published|reveals?|revealed|denies|denied|called|dismissed|pivoted|pivots?|"
        r"waits?)\b(?:[^.!?\n]{0,120}?),?\s+but\b",
        re.IGNORECASE,
    ),
    # Vendor / authority self-snitch: "Anthropic admits..." / "OpenAI admitted..."
    # Example: "Anthropic admits Claude is sycophantic 25% of the time."
    re.compile(r"\b(?:admits|admitted)\b", re.IGNORECASE),
    # Vendor self-study reveal: "Anthropic studied Claude, but..." / "Google investigated Gemini"
    # Example: "Anthropic studied 1 million Claude chats."
    re.compile(r"\b(?:studied|investigated)\b", re.IGNORECASE),
    # Self-snitch surfacing: "OpenAI's own data exposed..." / "the paper exposes..."
    # Example: "Anthropic's own paper exposed the sycophancy rate."
    re.compile(r"\b(?:exposed|exposes)\b", re.IGNORECASE),
    # Insider resistance: "tried to stop / kill / block / prevent" — tolerant of "tried for
    # N months to block" via an intervening-tokens window.
    # Example: "Alex Turner tried to stop Gemini" / "tried for two months to block it"
    re.compile(r"\btried\s+(?:[\w\s]{0,40}?\s+)?to\s+(?:stop|kill|block|prevent)\b", re.IGNORECASE),
    # Authority-contradicts-self: "Karpathy contradicts his own advice"
    # Example: "Karpathy contradicted his own vibe-coding pitch."
    re.compile(r"\b(?:contradicts?|contradicted)\b", re.IGNORECASE),
    # Named-authority reaction verbs: shocked / surprised / humiliated by an AI result.
    # Example: "ChatGPT shocked a Fields medalist."
    re.compile(r"\b(?:shocked|surprised|humiliated)\b", re.IGNORECASE),
    # Caught-in-the-act framing: "got caught" / "caught X doing Y" / "caught X saying Y"
    # Example: "Google got caught lying about Gemini benchmarks."
    re.compile(r"\bgot\s+caught\b", re.IGNORECASE),
    re.compile(r"\bcaught\s+\w+\s+(?:doing|saying|lying|faking)\b", re.IGNORECASE),
    # Vendor "told on" its own model — the operator's house phrasing.
    # Example: "Anthropic just told on Claude."
    re.compile(r"\btold\s+on\b", re.IGNORECASE),
    # Generic "every X ..., but ..." setup-and-twist — case-handles "every AI assistant waits
    # for you to stop talking, but Mira Murati just shipped..." where the verb itself isn't in
    # the broadened verb list. Intervening-window cap keeps it sentence-scoped.
    # Example: "Every AI assistant waits for you to stop talking, but Mira Murati shipped..."
    re.compile(r"\bevery\s+\w+(?:\s+\w+){0,3}\s+\w+(?:[^.!?\n]{0,120}?),?\s+but\b", re.IGNORECASE),
    # Pivot-off-own-position framing: "pivoted off / from / away" — for the Karpathy archetype
    # where the cited authority later abandons the workflow they popularized.
    # Example: "He pivoted off that workflow."
    re.compile(r"\bpivoted\s+(?:off|from|away)\b", re.IGNORECASE),
]


def _counter_conventional_bonus(*texts: str) -> float:
    """Return _COUNTER_CONVENTIONAL_BONUS if any input matches a counter-conventional pattern.

    Joins all non-empty inputs into one blob and runs each pattern. One match is enough;
    multiple matches don't compound (the cap is the cap). Returns 0.0 when no pattern fires.
    Inputs are typically (topic, angle, hook_concept) — pass any combination.
    """
    blob = " ".join(t for t in texts if t)
    for pattern in _COUNTER_CONVENTIONAL_PATTERNS:
        if pattern.search(blob):
            return _COUNTER_CONVENTIONAL_BONUS
    return 0.0


# Consumer-facing AI signals produce an additive bonus on top of the weighted-component sum,
# mirroring the counter-conventional pattern. Originally calibrated 2026-05-07 from the
# 2026-05-05 batch (Cursor 1108v vs uv 229v, ~5x view delta). **Broadened 2026-05-07 evening**
# after the audience pivot to general consumers — added consumer-AI vendor names (Perplexity,
# Midjourney, Stable Diffusion, Runway, ElevenLabs, Suno, Udio, Character.AI, Inflection)
# plus generic consumer-AI signals (AI agents, AI assistants, AI tools/apps, AI news, AGI).
# This bonus is the quantitative arm of the `project_topic_focus.md` directive — the
# qualitative arm lives in prompts/02_idea_generation.md.
_AI_VENDOR_BONUS: float = 0.05

# Single combined alternation, word-bounded, case-insensitive. One pattern fires the cap;
# multiple matches don't compound. Term list mirrors `project_topic_focus.md` consumer-AI list.
_AI_VENDOR_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:"
        # Foundation-model vendors (chat/text)
        r"Claude|Anthropic"
        r"|ChatGPT|GPT|OpenAI|Sora|DALL[\-·]?E"
        r"|Gemini|Bard|Google\s+AI"
        r"|xAI|Grok"
        r"|Mistral|Llama|Ollama"
        # Consumer-facing AI products (image / video / voice / music / search)
        r"|Perplexity|Midjourney|Stable\s+Diffusion|Runway|ElevenLabs|Suno|Udio"
        r"|Character\.?AI|Inflection"
        # Dev-tool AI (bridge-tier — still scored if angled for general audience)
        r"|Cursor|Aider|Cline|Copilot"
        r"|MCP|RAG"
        # Generic consumer-AI signals
        r"|AI\s+agents?|agent\s+frameworks?"
        r"|AI\s+assistants?"
        r"|AI\s+(?:tool|app)s?"
        r"|AI\s+news"
        r"|AGI"
        r")\b",
        re.IGNORECASE,
    ),
]


def _ai_vendor_bonus(*texts: str) -> float:
    """Return _AI_VENDOR_BONUS if any input mentions a canonical AI-vendor term.

    Joins all non-empty inputs into one blob and runs each pattern. One match is enough;
    multiple matches don't compound (the cap is the cap). Returns 0.0 when no pattern fires.
    Inputs are typically (topic, angle, hook_concept) — pass any combination.
    """
    blob = " ".join(t for t in texts if t)
    for pattern in _AI_VENDOR_PATTERNS:
        if pattern.search(blob):
            return _AI_VENDOR_BONUS
    return 0.0


# Named-human-in-the-first-8-words signal produces an additive bonus on top of the weighted
# sum, mirroring _counter_conventional_bonus and _ai_vendor_bonus. Calibrated 2026-05-12
# post-cycle-3 audit: 4 of 5 highest-volume videos carry a named third-party human in the
# title or first beat (Aider/lreeves HN, iOS 27/Mark Gurman Bloomberg, Fields medalist/Tim
# Gowers, Gemini multimodal/Givi Beridze Klipy CEO). Vendor-only sourcing correlates with
# mid-tier; named human ~doubles the floor. Intent: nudge ranking toward candidates whose
# cited_observation traces to a real named human, not "a developer says".
_NAMED_HUMAN_BONUS: float = 0.05

_NAMED_HUMAN_PATTERNS: list[re.Pattern] = [
    # Reddit handle "u/<handle>"
    re.compile(r"\bu/[A-Za-z0-9_\-]{3,}\b", re.IGNORECASE),
    # Hacker News item / user reference
    re.compile(r"\bhn:[A-Za-z0-9_\-]+\b", re.IGNORECASE),
    # X / Twitter handle "@<handle>" — 3-15 chars
    re.compile(r"@[A-Za-z0-9_]{3,15}\b"),
    # "named <Capitalized First> <Capitalized Last>" (e.g., "named Tim Gowers")
    re.compile(r"\bnamed\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b"),
    # "a <role> named ..." — researcher, developer, scientist, journalist, etc.
    re.compile(
        r"\ba\s+(?:researcher|developer|scientist|engineer|builder|founder|"
        r"reporter|journalist|professor|mathematician|partner|analyst|"
        r"executive|user|CEO|CTO)\s+named\b",
        re.IGNORECASE,
    ),
    # Vendor + titled role + bare name: "Anthropic CEO Dario Amodei", "OpenAI's Mira Murati"
    # Case-sensitive deliberately: under IGNORECASE the trailing `[A-Z][a-z]+\s+[A-Z][a-z]+`
    # would match any two-word phrase ("announced GPT") and false-positive on vendor-only news.
    # Real cited observations capitalize names properly, so case-sensitive is the right call.
    re.compile(
        r"\b(?:Anthropic|OpenAI|Google(?:\s+DeepMind)?|DeepMind|Microsoft|Apple|"
        r"Amazon|Meta|JPMorgan|Tesla|xAI|a16z|Andreessen\s+Horowitz)"
        r"(?:'s)?\s+(?:CEO\s+|CTO\s+|founder\s+|president\s+|partner\s+|"
        r"head\s+of\s+\w+\s+|chief\s+\w+\s+)?[A-Z][a-z]+\s+[A-Z][a-z]+\b"
    ),
    # Known-figure allowlist (specific named humans we've seen drive performance in the audit;
    # expand as new winners ship). Extended 2026-05-16 post-cycle-9: Sam Bowman was missing
    # and the "Claude Mythos email from outside the sandbox" candidate lost its named-human
    # bonus despite being a textbook Claude-internet template. Batch-extended to cover the
    # working set of figures in the consumer-AI news cycle across four cohorts:
    #   1) Anthropic safety researchers / leaders (Bowman, Kaplan, Olah, the Amodeis,
    #      Olsson, Leike, Perez, Hubinger, Brown, Krieger)
    #   2) OpenAI leaders / researchers (Altman, Brockman, Sutskever, Murati, McGrew,
    #      Pachocki, Schulman, Zaremba)
    #   3) Google DeepMind leaders / researchers (Hassabis, Pichai, Dean, Manyika, Ibrahim,
    #      Legg, Turner)
    #   4) xAI / Meta / Apple AI (Musk, Babuschkin, LeCun, Pineau, Chintala, Giannandrea,
    #      Federighi)
    #   5) Independent commentators / journalists (Willison, Karpathy, Hinton, Bengio,
    #      Russell, Marcus, Vance, Newton, Roose, Metz, Hao, Knight, Field)
    re.compile(
        r"\b(?:"
        # OpenAI
        r"Sam\s+Altman|Greg\s+Brockman|Ilya\s+Sutskever|Mira\s+Murati|"
        r"Bob\s+McGrew|Jakub\s+Pachocki|John\s+Schulman|Wojciech\s+Zaremba|"
        # Anthropic
        r"Sam\s+Bowman|Jared\s+Kaplan|Chris\s+Olah|Dario\s+Amodei|Daniela\s+Amodei|"
        r"Catherine\s+Olsson|Jan\s+Leike|Ethan\s+Perez|Evan\s+Hubinger|"
        r"Tom\s+Brown|Mike\s+Krieger|"
        # Google DeepMind
        r"Demis\s+Hassabis|Sundar\s+Pichai|Jeff\s+Dean|James\s+Manyika|"
        r"Lila\s+Ibrahim|Shane\s+Legg|Alex\s+Turner|"
        # xAI / Meta / Apple
        r"Elon\s+Musk|Igor\s+Babuschkin|Yann\s+LeCun|Joelle\s+Pineau|"
        r"Soumith\s+Chintala|John\s+Giannandrea|Federighi|"
        # Independent commentators / researchers / journalists
        r"Simon\s+Willison|Andrej\s+Karpathy|Geoffrey\s+Hinton|Yoshua\s+Bengio|"
        r"Stuart\s+Russell|Gary\s+Marcus|Ashlee\s+Vance|Casey\s+Newton|"
        r"Kevin\s+Roose|Cade\s+Metz|Karen\s+Hao|Will\s+Knight|Hayden\s+Field|"
        # Pre-existing performers preserved
        r"Mark\s+Gurman|Tim\s+Gowers|Adam\s+Dunkels|Jamie\s+Dimon|"
        r"Justine\s+Moore|John\s+Hultquist|Mustafa\s+Suleyman|"
        r"Kevin\s+Scott|Aidan\s+McLaughlin"
        r")\b"
    ),
    # Titled-expert references that imply a named human even when the name itself is omitted
    re.compile(
        r"\b(?:Fields\s+medalist|Nobel\s+laureate|MacArthur\s+(?:fellow|genius)|"
        r"Turing\s+(?:laureate|award\s+winner))\b",
        re.IGNORECASE,
    ),
]


def _named_human_bonus(*texts: str) -> float:
    """Return _NAMED_HUMAN_BONUS if any input matches a named-human signal.

    Joins all non-empty inputs into one blob and runs each pattern. One match is enough;
    multiple matches don't compound. Returns 0.0 when no pattern fires. Inputs are typically
    (topic, angle, hook_concept, cited_observation_handle, cited_observation_summary) — pass
    any combination.
    """
    blob = " ".join(t for t in texts if t)
    for pattern in _NAMED_HUMAN_PATTERNS:
        if pattern.search(blob):
            return _NAMED_HUMAN_BONUS
    return 0.0


# High-salience NAMED-ENTITY anchor bonus (2026-06-24 analytics deep-dive). The
# title-anchor lever is the strongest measured REACH signal in the learning ledger
# (anchored titles ~1.75x the median views of unanchored, n=50) and EVERY video
# that has ever cleared the breakout ceiling carries one. `_ai_vendor_bonus` already
# rewards AI-vendor anchors, but it (a) misses big CONSUMER-TECH entities
# (Apple/iPhone/Tesla/Neuralink/Windows/Meta...) that the general-tech track exists
# to surface, and (b) is SUPPRESSED entirely on the general-tech track — leaving
# that track with no entity reward at all. This bonus fills exactly that gap. Its
# lexicon is DISJOINT from `_AI_VENDOR_PATTERNS` (so the SAME entity is never
# double-counted), it is TRACK-AGNOSTIC (fires on both tracks — the whole point is
# to reward Tesla/Apple/Neuralink on the general-tech slot), and it is damped on
# FINANCIAL-deal topics alongside its siblings. Set just below the 0.05 siblings
# because it can legitimately co-occur with _ai_vendor_bonus on a dual-entity topic
# (e.g. "Apple's Siri runs on Gemini").
_HIGH_SALIENCE_ANCHOR_BONUS: float = 0.04

# Consumer-tech entities NOT covered by _AI_VENDOR_PATTERNS. Word-bounded,
# case-insensitive, single combined alternation (one match fires the cap).
_HIGH_SALIENCE_ANCHOR_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:"
        # Apple ecosystem
        r"Apple|iPhone|iPad|iPod|iOS|macOS|MacBook|Mac|Siri|AirPods|AirTag|"
        r"Vision\s+Pro|Apple\s+Watch|Apple\s+Vision"
        # Big tech (non-AI-vendor standalone names)
        r"|Microsoft|Windows|Xbox|Surface"
        r"|Amazon|Alexa|Echo|Kindle|AWS"
        r"|Meta|Facebook|Instagram|WhatsApp|Threads|Quest|Oculus"
        r"|Samsung|Galaxy"
        r"|Sony|PlayStation|Nintendo|Switch"
        # Chips / hardware
        r"|Nvidia|Intel|AMD|Qualcomm|Snapdragon|TSMC|ARM"
        # Musk-verse hardware (the protagonist names are handled by _named_human_bonus)
        r"|Tesla|SpaceX|Starlink|Neuralink"
        # Robotics / auto / drones / wearables / space
        r"|Boston\s+Dynamics|Waymo|Rivian|Cybertruck|Roomba|iRobot"
        r"|GoPro|DJI|Garmin|Ring\s+doorbell|Dyson"
        r"|NASA|Blue\s+Origin|Boeing"
        # Major consumer platforms / devices commonly anchoring tech stories
        r"|Android|Pixel|Chrome|Chromebook|YouTube|TikTok|Snapchat|Spotify|Netflix"
        r")\b",
        re.IGNORECASE,
    ),
]


def _high_salience_anchor_bonus(*texts: str) -> float:
    """Return _HIGH_SALIENCE_ANCHOR_BONUS if any input names a high-salience
    consumer-tech entity (disjoint from the AI-vendor lexicon).

    Joins all non-empty inputs into one blob and runs the combined pattern. One
    match is enough; multiple don't compound. Returns 0.0 when nothing fires.
    Inputs are typically (topic, angle, hook_concept).
    """
    blob = " ".join(t for t in texts if t)
    for pattern in _HIGH_SALIENCE_ANCHOR_PATTERNS:
        if pattern.search(blob):
            return _HIGH_SALIENCE_ANCHOR_BONUS
    return 0.0


# Corporate-FINANCE-deal damp (2026-06-11 sweep, CODE_AUDIT item (d)). Footgun: a
# dead-floor corporate-finance topic (e.g. an OpenAI S-1 / IPO) bonus-stacked the
# ai_vendor + named_human bonuses (it names a vendor AND a CEO) and floated to
# rank-2 despite the consumer-AI dead floor (mean 155 views). This DAMPS those two
# bonuses and applies a small flat penalty when FINANCIAL deal language is present.
#
# Detection is FINANCIAL-deal language ONLY. It deliberately does NOT match generic
# collaboration words ("partnership", "deal", "built around", "powered by") so that
# legitimate product/architecture stories that merely involve two companies are
# left UNTOUCHED. Two regression anchors that MUST score unchanged:
#   - "Apple reveals new AI architecture built around Google Gemini" (a legit
#     1.045-score rank-1 pick — "built around" is collaboration, not finance)
#   - "Apple's new Siri secretly runs on Google's Gemini"
# Both contain no IPO / funding / valuation / acquisition / investor language.
#
# Tuning lives in NEW keys in scoring_weights.json (corporate_deal_bonus_damp,
# corporate_deal_penalty) — these sit OUTSIDE the 8 normalized component weights
# (load_weights ignores non-DEFAULT_WEIGHTS keys, exactly like the existing _doc /
# _tuning_history meta-keys), so the PU-10 governance HOLD on the component numbers
# is untouched and the runtime sum-to-1.0 normalization is unaffected.
DEFAULT_CORPORATE_DEAL_BONUS_DAMP: float = 0.25   # multiply ai_vendor + named_human bonuses by this on a hit
DEFAULT_CORPORATE_DEAL_PENALTY: float = 0.10      # flat amount subtracted from weighted_total on a hit

_CORPORATE_DEAL_PATTERNS: list[re.Pattern] = [
    # Public-offering / filing language.
    re.compile(r"\bIPO\b", re.IGNORECASE),
    re.compile(r"\bS-1\b", re.IGNORECASE),
    re.compile(r"\bSPAC\b", re.IGNORECASE),
    # Fundraising language. "funding round", "raises/raised $...", "Series A".."Series F".
    re.compile(r"\bfunding\s+round\b", re.IGNORECASE),
    re.compile(r"\brais(?:e|es|ed|ing)\s+\$", re.IGNORECASE),
    re.compile(r"\bseries\s+[A-F]\b", re.IGNORECASE),
    # Valuation / investor language.
    re.compile(r"\bvaluation\b", re.IGNORECASE),
    re.compile(r"\binvestors?\b", re.IGNORECASE),
    # M&A language. Plural-aware ("acquisition"/"acquisitions", "acquire"/"acquires"),
    # case-insensitive. Word boundaries keep "requisition" from matching.
    re.compile(r"\bacquisitions?\b", re.IGNORECASE),
    re.compile(r"\bacquires?\b", re.IGNORECASE),
    re.compile(r"\bmergers?\b", re.IGNORECASE),
]


def _is_corporate_deal(*texts: str) -> bool:
    """True if any input carries FINANCIAL corporate-deal language.

    Joins all non-empty inputs into one blob and runs each financial-deal pattern.
    Matches ONLY finance signals (IPO / S-1 / funding round / raises $ / valuation /
    acquisition / merger / SPAC / series A-F / investors). Does NOT match generic
    collaboration words ("partnership", "deal", "built around", "powered by").
    Inputs are typically (topic, angle, hook_concept).
    """
    blob = " ".join(t for t in texts if t)
    for pattern in _CORPORATE_DEAL_PATTERNS:
        if pattern.search(blob):
            return True
    return False


def load_corporate_deal_tuning(path: Path = WEIGHTS_PATH) -> tuple[float, float]:
    """Load (bonus_damp, penalty) for the corporate-deal damp from scoring_weights.json.

    Reads the NEW non-component keys `corporate_deal_bonus_damp` and
    `corporate_deal_penalty`. These live alongside the existing meta-keys (_doc,
    _tuning_history) and are NOT part of the normalized 8-component weight set, so
    load_weights() never sees them and the PU-10 HOLD is untouched. Missing/invalid
    values fall back to the module defaults. Returns the defaults if the file is
    absent or unreadable (mirrors load_weights' fail-soft behavior).
    """
    damp = DEFAULT_CORPORATE_DEAL_BONUS_DAMP
    penalty = DEFAULT_CORPORATE_DEAL_PENALTY
    if not path.exists():
        return damp, penalty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corporate-deal tuning unreadable (%s); using defaults", e)
        return damp, penalty
    if "corporate_deal_bonus_damp" in raw:
        try:
            damp = float(raw["corporate_deal_bonus_damp"])
        except (TypeError, ValueError):
            log.warning("corporate_deal_bonus_damp not float-coercible; using default %.3f", damp)
    if "corporate_deal_penalty" in raw:
        try:
            penalty = float(raw["corporate_deal_penalty"])
        except (TypeError, ValueError):
            log.warning("corporate_deal_penalty not float-coercible; using default %.3f", penalty)
    return damp, penalty


@dataclass
class ScoreComponents:
    """The 8 component scores, each in 0.0..1.0. Field order = ranking display order."""
    niche_fit: float = 0.0
    hook_strength: float = 0.0
    specificity: float = 0.0
    trend_signal: float = 0.0
    verifiability: float = 0.0
    broll_feasibility: float = 0.0
    observation_availability: float = 0.0
    anti_cannibalization: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreComponents":
        """Tolerant constructor: missing fields default to 0.0, extras ignored."""
        return cls(**{k: float(d.get(k, 0.0)) for k in cls.__dataclass_fields__})

    def clamped(self) -> "ScoreComponents":
        """Return a copy with each field clipped to [0.0, 1.0]."""
        return ScoreComponents(**{
            k: max(0.0, min(1.0, getattr(self, k)))
            for k in self.__dataclass_fields__
        })


@dataclass
class ScoredCandidate:
    """A candidate idea after scoring. Mirrors the input dict shape, plus weighted_total."""
    topic: str
    angle: str
    hook_concept: str
    why_now: str = ""
    audience: str = ""
    source_indexes: list[int] = field(default_factory=list)
    cited_observation_candidate: dict = field(default_factory=dict)
    components: ScoreComponents = field(default_factory=ScoreComponents)
    counter_conventional_bonus: float = 0.0
    ai_vendor_bonus: float = 0.0
    named_human_bonus: float = 0.0
    high_salience_anchor_bonus: float = 0.0  # consumer-tech named-entity anchor (2026-06-24)
    corporate_deal_damped: bool = False      # True when FINANCIAL-deal language damped the bonuses + applied a penalty
    corporate_deal_penalty: float = 0.0      # flat penalty actually subtracted (0.0 when no hit)
    weighted_total: float = 0.0
    rationale: str = ""


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Load weights from JSON if present, else return DEFAULT_WEIGHTS.

    The file must contain exactly the keys in DEFAULT_WEIGHTS. Sum is normalized to
    1.0 if it drifts (so partial-tuning JSON files don't bias scores accidentally).
    """
    if not path.exists():
        log.info("scoring weights: using defaults (no %s on disk)", path.name)
        return dict(DEFAULT_WEIGHTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("scoring weights file unreadable (%s); falling back to defaults", e)
        return dict(DEFAULT_WEIGHTS)
    missing = set(DEFAULT_WEIGHTS) - set(raw)
    if missing:
        log.warning("scoring weights file missing keys %s; falling back to defaults", missing)
        return dict(DEFAULT_WEIGHTS)
    weights = {k: float(raw[k]) for k in DEFAULT_WEIGHTS}
    total = sum(weights.values())
    if total <= 0:
        log.warning("scoring weights sum to <=0; falling back to defaults")
        return dict(DEFAULT_WEIGHTS)
    if abs(total - 1.0) > 0.01:
        log.info("scoring weights sum=%.4f; normalizing to 1.0", total)
        weights = {k: v / total for k, v in weights.items()}
    return weights


def score_topic(components: ScoreComponents, weights: dict[str, float] | None = None) -> float:
    """Weighted sum of components. Both inputs must use the same key set."""
    weights = weights or DEFAULT_WEIGHTS
    c = components.clamped()
    return sum(getattr(c, k) * weights[k] for k in weights)


def rank_candidates(
    candidates: list[dict],
    weights: dict[str, float] | None = None,
    *,
    suppress_ai_vendor_bonus: bool = False,
) -> list[ScoredCandidate]:
    """Score and rank a list of candidate dicts (as produced by 02_idea_generation.md).

    Each candidate dict must have keys: topic, angle, hook_concept, scores (a dict of
    the 8 component scores). Optional: why_now, audience, source_indexes,
    cited_observation_candidate, rationale.

    `suppress_ai_vendor_bonus` zeroes the +0.05 AI-vendor bonus for this batch —
    set True for the general-tech track (broad consumer-tech + crazy-story slot),
    where rewarding AI-vendor mentions would bias against the very topics the track
    exists to surface (iPhone/Meta/Windows/Tesla/Neuralink). Defaults False, so the
    ai-vendor track and every existing caller are unchanged. The counter-conventional
    and named-human bonuses are track-agnostic and always apply.

    Returns ScoredCandidates sorted descending by weighted_total.
    """
    w = weights or load_weights()
    cd_damp, cd_penalty = load_corporate_deal_tuning()
    out: list[ScoredCandidate] = []
    for raw in candidates:
        scores = raw.get("scores") or {}
        comps = ScoreComponents.from_dict(scores)
        base = score_topic(comps, w)
        cc_bonus = _counter_conventional_bonus(
            raw.get("topic", ""),
            raw.get("angle", ""),
            raw.get("hook_concept", ""),
        )
        ai_bonus = 0.0 if suppress_ai_vendor_bonus else _ai_vendor_bonus(
            raw.get("topic", ""),
            raw.get("angle", ""),
            raw.get("hook_concept", ""),
        )
        cited = raw.get("cited_observation_candidate") or {}
        nh_bonus = _named_human_bonus(
            raw.get("topic", ""),
            raw.get("angle", ""),
            raw.get("hook_concept", ""),
            str(cited.get("source_handle", "")),
            str(cited.get("summary", "")),
            str(cited.get("retrievable_quote", "")),
        )
        hs_bonus = _high_salience_anchor_bonus(
            raw.get("topic", ""),
            raw.get("angle", ""),
            raw.get("hook_concept", ""),
        )
        # Corporate-FINANCE-deal damp: a press-release-style finance topic that names
        # a vendor + a human can farm ai_vendor + named_human bonuses to float above
        # the dead floor. When FINANCIAL deal language is present, damp BOTH bonuses
        # and subtract a flat penalty; floor the total at 0. counter_conventional is
        # left intact (a finance topic with a genuine contrarian twist keeps it).
        is_deal = _is_corporate_deal(
            raw.get("topic", ""),
            raw.get("angle", ""),
            raw.get("hook_concept", ""),
        )
        applied_penalty = 0.0
        if is_deal:
            ai_bonus *= cd_damp
            nh_bonus *= cd_damp
            hs_bonus *= cd_damp
            applied_penalty = cd_penalty
        total = base + cc_bonus + ai_bonus + nh_bonus + hs_bonus - applied_penalty
        total = max(0.0, total)  # floor at 0
        out.append(ScoredCandidate(
            topic=raw.get("topic", ""),
            angle=raw.get("angle", ""),
            hook_concept=raw.get("hook_concept", ""),
            why_now=raw.get("why_now", ""),
            audience=raw.get("audience", ""),
            source_indexes=list(raw.get("source_indexes") or []),
            cited_observation_candidate=cited,
            components=comps,
            counter_conventional_bonus=cc_bonus,
            ai_vendor_bonus=ai_bonus,
            named_human_bonus=nh_bonus,
            high_salience_anchor_bonus=hs_bonus,
            corporate_deal_damped=is_deal,
            corporate_deal_penalty=applied_penalty,
            weighted_total=round(total, 4),
            rationale=raw.get("rationale", ""),
        ))
    out.sort(key=lambda s: s.weighted_total, reverse=True)
    return out


def pick_top_n(
    candidates: list[dict],
    n: int,
    weights: dict[str, float] | None = None,
    *,
    suppress_ai_vendor_bonus: bool = False,
) -> list[ScoredCandidate]:
    """Convenience: rank + take top n."""
    return rank_candidates(
        candidates, weights, suppress_ai_vendor_bonus=suppress_ai_vendor_bonus
    )[:n]


# -----------------------------------------------------------------------------
# CLI for offline testing — `python scoring.py --candidates path/to/file.json`
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score and rank candidate ideas")
    parser.add_argument("--candidates", required=True,
                        help="Path to a JSON file containing a list of candidate dicts")
    parser.add_argument("--weights", default=None,
                        help=f"Path to a weights JSON (default: {WEIGHTS_PATH})")
    parser.add_argument("--top", type=int, default=0, help="Print only top N (0 = all)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    raw = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"expected a JSON list at {args.candidates}, got {type(raw).__name__}")

    weights = load_weights(Path(args.weights)) if args.weights else load_weights()
    ranked = rank_candidates(raw, weights)
    if args.top > 0:
        ranked = ranked[:args.top]

    print(json.dumps([asdict(s) for s in ranked], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
