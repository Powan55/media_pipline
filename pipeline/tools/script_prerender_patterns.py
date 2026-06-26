"""Pre-render lint patterns: unresolved placeholders + retired/forbidden CTAs.

This is the data / logic layer for prepublish_qa check #16
(:func:`tools.prepublish_qa.check_script_prerender`). It is the single source
of truth for two forward-looking script-hygiene rules, mirroring how
:mod:`tools.script_artifact_patterns` is the source of truth for the Sprint-5
template-artifact patterns.

Background — two real defects that shipped to YouTube and cost views
-------------------------------------------------------------------
1. ``2026-05-08_001`` (vid Eaxrx6CVJ0s, 75 views): a literal
   ``[VERIFY: find a specific recent post with handle and URL ...]`` fact-check
   note survived gate-2 into ``script_FINAL.txt`` INSIDE a spoken VO line and
   edge-TTS read it aloud.
2. ``2026-05-11_002``: the body ended with
   ``Comment "deploy" and I will send you the link to the announcement.`` — the
   transactional comment-bait CTA explicitly RETIRED in the channel style guide
   (``Channels\\ShadowVerse\\style_guide.md`` § CTA style), whose promised
   affiliate payload is not a live funnel.

Two rule families
-----------------
* **placeholder** — any ``[VERIFY``, ``[NEEDS``, ``[TODO`` or ``[FIXME``
  bracket token, anywhere in the file (an editor/fact-check marker must never
  survive into a FINAL body that TTS will speak).
* **banned_cta** — any retired / forbidden CTA phrase. The phrase list is
  *pulled from the style guide's own retired/forbidden sections* so it stays in
  sync with the operator's single source of truth, on top of a built-in
  baseline (the phrases named in the original ask) that is ALWAYS enforced even
  when the style guide can't be read. CTA matching is normalized for
  contractions (``I'll`` == ``I will``), surrounding quotes
  (``"deploy"`` == ``deploy``), placeholders (``[keyword]`` / ``<word>`` ==
  any token) and flexible whitespace, so the style guide's
  ``Comment [keyword] and I'll send you the link.`` catches the live
  ``Comment "deploy" and I will send you the link.`` defect.

The historical published scripts are NOT modified — this is a guard against the
NEXT one.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, NamedTuple

log = logging.getLogger("script_prerender_patterns")

# ---------------------------------------------------------------------------
# Canonical style guide location (matches config.channel.style_guide_path).
# Callers may override; this is only the fallback default.
# ---------------------------------------------------------------------------

DEFAULT_STYLE_GUIDE_PATH = Path(
    r"C:\Users\laxmi\Documents\Project\Channels\ShadowVerse\style_guide.md"
)

# ---------------------------------------------------------------------------
# Rule family 1: unresolved placeholder tokens
# ---------------------------------------------------------------------------

PLACEHOLDER_TOKEN_RE: re.Pattern[str] = re.compile(
    r"\[\s*(?:VERIFY|NEEDS|TODO|FIXME)\b[^\]\n]*\]?",
    re.IGNORECASE,
)
"""Opening-bracket editor/fact-check markers.

Matches ``[VERIFY``, ``[NEEDS``, ``[TODO`` and ``[FIXME`` regardless of what
follows (colon, text, or an immediate ``]``), case-insensitively. Captures
through the closing ``]`` when present so the FAIL message shows the whole tag.

Deliberately does NOT match ``[B-ROLL: ...]`` or ``[formula: ...]`` (the only
brackets a clean ShadowVerse script carries) because neither starts with one
of the four marker keywords.
"""

# ---------------------------------------------------------------------------
# Rule family 2: retired / forbidden CTAs
# ---------------------------------------------------------------------------

BASELINE_BANNED_CTAS: tuple[str, ...] = (
    "Comment <word> and I",
    "I'll send you the link",
    "smash that like",
    "hit that like",
)
"""Always-enforced baseline (the phrases named in the original ask).

These mirror the style guide's retired/forbidden sections but live in code so
the gate keeps catching the two known-bad families even if the style-guide file
is missing/unreadable. The style-guide parse (:func:`load_banned_cta_phrases`)
ADDS to this set; it never replaces it.
"""

# A line in the style guide contributes its quoted phrases to the banned-CTA
# list only when it carries one of these markers (lower-cased substring match).
# This scopes extraction to the "RETIRED (do not use)" CTA callout and the
# "engagement-begging ... Banned outright." forbidden-patterns bullet, WITHOUT
# sweeping up the quoted *approved* CTAs ("Save this...", "Follow for...") that
# live on neighbouring, marker-free lines.
_CTA_BAN_LINE_MARKERS: tuple[str, ...] = (
    "retired",
    "engagement-begging",
    "banned outright",
)

# Double-quoted phrase extractor (smart quotes are normalized to straight ones
# before this runs).
_QUOTED_PHRASE_RE: re.Pattern[str] = re.compile(r'"([^"]+)"')

# Contractions expanded so the style guide's "I'll" matches a script's "I will"
# (and vice-versa) — we expand BOTH the pattern and the scanned line.
_CONTRACTIONS: dict[str, str] = {
    "i'll": "i will",
    "you'll": "you will",
    "we'll": "we will",
    "they'll": "they will",
    "it'll": "it will",
    "he'll": "he will",
    "she'll": "she will",
    "that'll": "that will",
    "i've": "i have",
    "you've": "you have",
    "we've": "we have",
    "i'm": "i am",
    "don't": "do not",
    "won't": "will not",
    "can't": "cannot",
}

# Placeholder tokens inside a banned phrase (`[keyword]` / `<word>`) become a
# single-token wildcard when compiled to a regex.
_PHRASE_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\[[^\]]*\]|<[^>]*>")

# Trailing punctuation stripped off a banned phrase before compiling.
_PHRASE_TRAILING_PUNCT = " .,;:!?…"

# A line that is wholly a B-ROLL stage direction — excluded from CTA scanning
# (the CTA ban is about spoken VO / on-screen overlay, not stage directions),
# but still placeholder-scanned.
_BROLL_CUE_LINE_RE: re.Pattern[str] = re.compile(r"^\s*\[\s*b[-_ ]?roll\b", re.IGNORECASE)


class CtaMatcher(NamedTuple):
    """One compiled banned-CTA matcher.

    ``origin`` is ``"baseline"`` or ``"style_guide"`` so a FAIL / audit can say
    where the rule came from.
    """

    source_phrase: str
    origin: str
    regex: re.Pattern[str]


def _normalize(text: str, *, strip_edges: bool = True) -> str:
    """Lower-case, fold smart quotes, expand contractions, drop double quotes,
    collapse whitespace. Used symmetrically on banned phrases and scanned lines
    so the two are compared in the same shape.
    """
    s = text.lower()
    s = s.replace("“", '"').replace("”", '"')   # smart double quotes
    s = s.replace("‘", "'").replace("’", "'")   # smart single quotes
    for contraction, expanded in _CONTRACTIONS.items():
        s = re.sub(rf"\b{re.escape(contraction)}\b", expanded, s)
    s = s.replace('"', "")                                  # drop straight double quotes
    s = re.sub(r"\s+", " ", s)
    return s.strip() if strip_edges else s


def _phrase_to_regex(phrase: str) -> re.Pattern[str] | None:
    """Compile a raw banned-CTA phrase into a tolerant, case-insensitive regex.

    Handles: placeholder tokens (``[keyword]`` / ``<word>`` -> one ``\\S+``
    token), contraction folding, surrounding-quote stripping, and flexible
    whitespace. Word-boundary lookarounds keep it from matching mid-word.

    Returns ``None`` for a phrase that has fewer than 3 literal alphabetic
    characters once placeholders are removed (too broad to ban safely).
    """
    raw = phrase.strip().strip(_PHRASE_TRAILING_PUNCT)
    if not raw:
        return None

    # Split on placeholder tokens; whitespace adjacent to a placeholder stays in
    # the literal segments so word separation survives.
    segments = _PHRASE_PLACEHOLDER_RE.split(raw)
    literal_alpha = 0
    fragments: list[str] = []
    for seg in segments:
        norm = _normalize(seg, strip_edges=False)
        literal_alpha += sum(ch.isalpha() for ch in norm)
        escaped = re.escape(norm).replace(r"\ ", r"\s+")   # flexible whitespace
        fragments.append(escaped)

    if literal_alpha < 3:
        return None

    body = r"\S+".join(fragments)
    pattern = rf"(?<![A-Za-z]){body}(?![A-Za-z])"
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:  # pragma: no cover - defensive; construction is controlled
        log.warning("could not compile banned-CTA phrase %r -> %r", phrase, pattern)
        return None


def load_banned_cta_phrases(style_guide_path: str | Path | None = None) -> list[str]:
    """Extract retired/forbidden CTA phrases from the style guide.

    Scans each line; on any line carrying a :data:`_CTA_BAN_LINE_MARKERS`
    marker, pulls every double-quoted phrase. Returns the phrases in document
    order, de-duplicated. A missing / unreadable style guide returns ``[]`` and
    logs a warning — the baseline list still applies (see
    :func:`build_cta_matchers`), so we never silently drop the guard, but we
    also never halt a render just because the strategy repo wasn't readable.
    """
    path = Path(style_guide_path) if style_guide_path else DEFAULT_STYLE_GUIDE_PATH
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning(
            "could not read style guide %s for banned-CTA sync (%s); "
            "falling back to the built-in baseline list only",
            path, exc,
        )
        return []

    phrases: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        low = line.lower()
        if not any(marker in low for marker in _CTA_BAN_LINE_MARKERS):
            continue
        normalized_line = line.replace("“", '"').replace("”", '"')
        for quoted in _QUOTED_PHRASE_RE.findall(normalized_line):
            candidate = quoted.strip()
            if len(candidate) < 3 or candidate in seen:
                continue
            seen.add(candidate)
            phrases.append(candidate)
    return phrases


def build_cta_matchers(
    style_guide_path: str | Path | None = None,
    *,
    include_baseline: bool = True,
) -> list[CtaMatcher]:
    """Build the full banned-CTA matcher list: baseline + style-guide-sourced.

    De-duplicated by compiled-pattern string, so a style-guide phrase that
    compiles to the same regex as a baseline one is not added twice.
    """
    matchers: list[CtaMatcher] = []
    seen_patterns: set[str] = set()

    def _add(phrase: str, origin: str) -> None:
        regex = _phrase_to_regex(phrase)
        if regex is None or regex.pattern in seen_patterns:
            return
        seen_patterns.add(regex.pattern)
        matchers.append(CtaMatcher(source_phrase=phrase, origin=origin, regex=regex))

    if include_baseline:
        for phrase in BASELINE_BANNED_CTAS:
            _add(phrase, "baseline")
    for phrase in load_banned_cta_phrases(style_guide_path):
        _add(phrase, "style_guide")
    return matchers


class LintMatch(NamedTuple):
    """One pre-render lint hit.

    ``kind`` is ``"placeholder"`` or ``"banned_cta"``. ``line_no`` is 1-based.
    ``matched_text`` is the offending span; ``line_text`` is the full line.
    """

    line_no: int
    kind: str
    matched_text: str
    line_text: str


def _is_broll_cue_line(line: str) -> bool:
    """True if ``line`` is wholly a ``[B-ROLL: ...]`` stage direction."""
    return bool(_BROLL_CUE_LINE_RE.match(line))


def scan_script_for_lint(
    text: str,
    cta_matchers: Iterable[CtaMatcher],
) -> list[LintMatch]:
    """Scan a script body for placeholder tokens and banned CTAs.

    * Placeholders are scanned on EVERY line (a stray marker anywhere is a
      defect), against the raw line.
    * Banned CTAs are scanned on every line EXCEPT pure B-ROLL cue lines, against
      the normalized line. At most one finding of each kind is reported per line.

    Returns matches sorted by ``(line_no, kind)``; an empty list means clean.
    """
    cta_matchers = list(cta_matchers)
    matches: list[LintMatch] = []
    seen_keys: set[tuple[int, str]] = set()

    for line_no, line in enumerate(text.splitlines(), start=1):
        ph = PLACEHOLDER_TOKEN_RE.search(line)
        if ph:
            key = (line_no, "placeholder")
            if key not in seen_keys:
                seen_keys.add(key)
                matches.append(LintMatch(line_no, "placeholder", ph.group(0), line))

        if _is_broll_cue_line(line):
            continue
        normalized = _normalize(line)
        if not normalized:
            continue
        for matcher in cta_matchers:
            cm = matcher.regex.search(normalized)
            if cm:
                key = (line_no, "banned_cta")
                if key not in seen_keys:
                    seen_keys.add(key)
                    matches.append(LintMatch(line_no, "banned_cta", cm.group(0), line))
                break

    matches.sort(key=lambda m: (m.line_no, m.kind))
    return matches


def format_lint_matches(matches: Iterable[LintMatch]) -> str:
    """Human-readable rendering of matches for a FAIL message."""
    rendered = [
        f"  line {m.line_no} [{m.kind}]: {m.matched_text.strip()[:60]!r} "
        f"(in: {m.line_text.strip()[:80]!r})"
        for m in matches
    ]
    return "\n".join(rendered) if rendered else "(no matches)"
