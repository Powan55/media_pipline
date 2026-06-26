"""Section-aware parser for ``script_RESPONSE.txt`` LLM outputs.

Background
----------
On 2026-05-13, video ``_12_002`` shipped to scheduled-Private with the literal
phrase ``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` at the top of
``script_FINAL.txt``. edge-TTS dutifully spoke the annotation aloud. The
2026-05-14 ``/start -auto`` cycle 6 confirmed the artifact recurs every cycle
(the manager stripped it by hand each time).

Root cause: when an LLM script-gen response carries a separator header line
like ``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` between the hook
variants and the body, ``pipeline._parse_script_response()`` slices
``response[hook_matches[-1].end():fc_marker.start()]`` and that slice
**includes** the header line. The body retains the header. The existing
stripper only knows about ``CHOSEN HOOK:`` and ``SCRIPT:`` divider markers,
not ``SCRIPT_BODY...:``. The auto-resolve gate-2 branch then writes that body
to ``script_FINAL.txt`` verbatim.

This module is the source-side fix. It is a strict regex state machine that
splits the response into named sections (``HOOK_A``, ``HOOK_B``, ``HOOK_C``,
``SCRIPT_BODY``, ``FACT_CHECK_QUEUE``, ``QUALITY_SCORES``) and returns each
section's clean payload via :class:`ParsedResponse`. The companion
:func:`extract_final_script` composes the spoken-aloud body from the parsed
result. The Sprint 5 Layer-2 template-artifact scan
(``tools.script_artifact_patterns.scan_for_artifacts``) stays in place as
defense-in-depth — after this fix, its hit rate on auto-resolve output should
drop to zero by construction.

Public API
----------
* :class:`ParsedResponse` — frozen dataclass with every named section.
* :class:`ScriptResponseParseError` — raised on unparseable response.
* :func:`parse_response` — section-aware parser.
* :func:`extract_final_script` — compose the TTS-ready body.

Implementation rules:
    * Pure-Python (``re``, ``dataclasses``, ``logging``, ``typing`` only).
    * Body cleaning preserves ``[B-ROLL: <description>]`` cues verbatim. The
      well-formed B-ROLL cue has a colon and a description, so it never
      collides with the ``\\[[A-Z_]+\\]`` placeholder regex (which requires
      ``]`` immediately after the all-caps token).
    * ``[VERIFY]`` / ``[VERIFY: ...]`` tags are stripped from the body.
    * Other ALL-CAPS template placeholders (matching
      ``TEMPLATE_PLACEHOLDER_PATTERNS`` from
      :mod:`tools.script_artifact_patterns`) are dropped with a warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from tools.script_artifact_patterns import TEMPLATE_PLACEHOLDER_PATTERNS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section header patterns
# ---------------------------------------------------------------------------

# `HOOK_A: <text>   [formula: Contradiction]` — capture letter, raw remainder.
# Trailing `[formula: ...]` is optional; cleaned out of hook text downstream.
_HOOK_LINE_RE = re.compile(
    r"^HOOK_([ABC])\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)

# Optional `(uses HOOK_X ...)` annotation on the SCRIPT_BODY header.
# When present, captures the chosen-hook letter.
#
# The regex deliberately does NOT anchor on `$` and uses `[ \t]*` (not `\s*`)
# for trailing whitespace — `\s` would consume `\n` and we must leave the
# newline intact so body extraction can decide whether the body starts on
# the next line or mid-line after the colon. This handles the cycle-9/10/11/12
# regression where the LLM emitted prose on the same line as the header
# (e.g. `SCRIPT_BODY (uses HOOK_A): <prose>`), which the prior `$`-anchored
# regex could not match. See ENG-002 (2026-05-20 engineering sweep).
_SCRIPT_BODY_HEADER_RE = re.compile(
    r"^SCRIPT_BODY(?:[ \t]*\(uses\s+HOOK_([ABC])[^)]*\))?[ \t]*:?[ \t]*",
    re.MULTILINE,
)

# Section header markers (case-insensitive — the response-parser side never
# auto-mints these, so we match the operator-visible spellings).
_FACT_CHECK_HEADER_RE = re.compile(
    r"^[\s#*]*FACT[_\s]*CHECK[_\s]*QUEUE[\s:#*]*$",
    re.MULTILINE | re.IGNORECASE,
)
_QUALITY_SCORES_HEADER_RE = re.compile(
    r"^[\s#*]*QUALITY[_\s]*SCORES[\s:#*]*$",
    re.MULTILINE | re.IGNORECASE,
)

# Inline annotations to strip from the body.
_VERIFY_TAG_RE = re.compile(r"\s*\[VERIFY(?::[^\]]*)?\]\s*", re.IGNORECASE)

# `[formula: <name>]` annotation on a hook line.
_HOOK_FORMULA_RE = re.compile(r"\[\s*formula\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)

# Legacy `CHOSEN HOOK: HOOK_X` marker some LLMs emit between hooks and body.
_CHOSEN_HOOK_LINE_RE = re.compile(
    r"^[ \t]*CHOSEN[ \t]+HOOK[ \t]*:[ \t]*HOOK_([ABC])[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# Standalone `SCRIPT:` divider (no payload) that some LLMs emit.
_SCRIPT_DIVIDER_RE = re.compile(
    r"^[ \t]*SCRIPT[ \t]*:[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

# Bullet line under FACT_CHECK_QUEUE.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)

# `- key: value` line under QUALITY_SCORES.
_QUALITY_LINE_RE = re.compile(r"^\s*[-*]\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$", re.MULTILINE)

# B-ROLL opener — used to detect whether a stretch of text contains a cue we
# must preserve. Note this is just a quick sanity check; the body keeps cues
# inline verbatim, so we DON'T re-extract them here.
_BROLL_OPEN_RE = re.compile(r"\[B-?ROLL\s*:", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Exceptions + dataclass
# ---------------------------------------------------------------------------


class ScriptResponseParseError(Exception):
    """Raised when :func:`parse_response` cannot find a usable body section."""


@dataclass(frozen=True)
class ParsedResponse:
    """Section-by-section view of a ``script_RESPONSE.txt`` payload.

    Attributes:
        hook_a_text: Cleaned hook A first-sentence (no ``[formula: ...]``).
        hook_b_text: Cleaned hook B first-sentence (or ``None`` if absent).
        hook_c_text: Cleaned hook C first-sentence (or ``None`` if absent).
        hook_a_formula: Hook A formula name (or ``None``).
        hook_b_formula: Hook B formula name (or ``None``).
        hook_c_formula: Hook C formula name (or ``None``).
        script_body_text: Body prose with ``[B-ROLL: ...]`` cues preserved
            inline. Stripped of ``[VERIFY]`` tags, ``SCRIPT_BODY...:`` header,
            ``CHOSEN HOOK:`` / ``SCRIPT:`` divider markers, and ALL-CAPS
            ``{FOO}``/``<FOO>``/``[FOO]`` template placeholders.
        fact_check_queue: Bullet items under the ``FACT_CHECK_QUEUE`` header.
        quality_scores: Parsed ``name: float`` entries under ``QUALITY_SCORES``.
        quality_rationale: Final ``- rationale: ...`` line, or ``None``.
        chosen_hook_letter: Inferred chosen-hook letter from either
            ``SCRIPT_BODY (uses HOOK_X ...)`` or a ``CHOSEN HOOK: HOOK_X``
            marker. ``None`` when neither is present (caller defaults to ``A``).
    """

    hook_a_text: str | None
    hook_b_text: str | None
    hook_c_text: str | None
    hook_a_formula: str | None
    hook_b_formula: str | None
    hook_c_formula: str | None
    script_body_text: str
    fact_check_queue: list[str]
    quality_scores: dict[str, float]
    quality_rationale: str | None
    chosen_hook_letter: Literal["A", "B", "C"] | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    """Collapse CRLF / CR line endings to LF so ``^`` anchors behave."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_hook_formula(line: str) -> tuple[str, str | None]:
    """Return ``(clean_hook_text, formula_name_or_None)``."""
    m = _HOOK_FORMULA_RE.search(line)
    if not m:
        return line.strip(), None
    formula = m.group(1).strip() or None
    cleaned = _HOOK_FORMULA_RE.sub("", line).strip()
    return cleaned, formula


def _drop_template_placeholders(body: str) -> str:
    """Drop ALL-CAPS ``{FOO}``/``<FOO>``/``[FOO]`` placeholders with a warning.

    Critical preservation rule: a well-formed ``[B-ROLL: <description>]`` cue
    does NOT match the ``\\[[A-Z_]+\\]`` regex from
    ``TEMPLATE_PLACEHOLDER_PATTERNS`` because that pattern requires ``]`` to
    follow immediately after the all-caps token. ``[B-ROLL: ...]`` has a
    colon-plus-description in between, so it survives this scrub unchanged.
    """
    out = body
    for pattern in TEMPLATE_PLACEHOLDER_PATTERNS:
        for hit in pattern.findall(out):
            log.warning(
                "dropping ALL-CAPS template placeholder %r from script body",
                hit,
            )
        out = pattern.sub("", out)
    return out


def _clean_body(raw_body: str) -> str:
    """Apply the body-cleaning pipeline: VERIFY tags → placeholders → trim."""
    cleaned = _VERIFY_TAG_RE.sub(" ", raw_body)
    cleaned = _drop_template_placeholders(cleaned)
    # Collapse any double-spaces introduced by tag-stripping, but DON'T touch
    # newlines (paragraph structure may matter to downstream stages).
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    # Trim trailing whitespace per line.
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    # Drop a trailing markdown horizontal rule (`---`) some LLMs append after the
    # body — it must never reach TTS / script_FINAL (legacy _parse_script_response
    # parity; folded into the shared cleaner during the 2026-06-19 unification).
    cleaned = re.sub(r"\n+-{3,}\s*$", "", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def parse_response(text: str) -> ParsedResponse:
    """Parse a raw ``script_RESPONSE.txt`` payload into named sections.

    Algorithm:
        1. Normalize line endings.
        2. Find every ``HOOK_X:`` line. Strip the optional
           ``[formula: ...]`` annotation; capture cleaned text + formula name
           per letter.
        3. Locate the ``SCRIPT_BODY...:`` header (optional). If present, body
           starts on the line AFTER the header. If absent, body starts after
           the last ``HOOK_X:`` line (legacy compatibility).
        4. Body ends at the ``FACT_CHECK_QUEUE`` header, or the
           ``QUALITY_SCORES`` header, or EOF — whichever comes first.
        5. Strip ``CHOSEN HOOK:`` and ``SCRIPT:`` divider markers from the
           body. Strip ``[VERIFY]`` tags. Drop ALL-CAPS template
           placeholders (warn-and-drop; preserves ``[B-ROLL: ...]``).
        6. Parse ``FACT_CHECK_QUEUE`` bullet list and ``QUALITY_SCORES``
           ``key: value`` pairs.

    Raises:
        ScriptResponseParseError: when neither a ``SCRIPT_BODY`` header nor a
            non-empty body region between hooks and ``FACT_CHECK_QUEUE`` /
            EOF exists.
    """
    text = _normalize_newlines(text)

    # --- Hooks -----------------------------------------------------------
    hook_texts: dict[str, str | None] = {"A": None, "B": None, "C": None}
    hook_formulas: dict[str, str | None] = {"A": None, "B": None, "C": None}
    hook_matches = list(_HOOK_LINE_RE.finditer(text))
    last_hook_end: int | None = None
    for m in hook_matches:
        letter = m.group(1)
        raw = m.group(2)
        cleaned, formula = _strip_hook_formula(raw)
        hook_texts[letter] = cleaned
        hook_formulas[letter] = formula
        last_hook_end = m.end()

    # --- SCRIPT_BODY header (optional) ----------------------------------
    body_header_match = _SCRIPT_BODY_HEADER_RE.search(text)
    chosen_letter: Literal["A", "B", "C"] | None = None
    if body_header_match and body_header_match.group(1):
        chosen_letter = body_header_match.group(1)  # type: ignore[assignment]

    # --- Locate end of body (FACT_CHECK_QUEUE or QUALITY_SCORES or EOF) -
    fc_match = _FACT_CHECK_HEADER_RE.search(text)
    quality_match = _QUALITY_SCORES_HEADER_RE.search(text)

    if fc_match and quality_match:
        # Pick whichever header comes first as the body terminator.
        body_end = min(fc_match.start(), quality_match.start())
    elif fc_match:
        body_end = fc_match.start()
    elif quality_match:
        body_end = quality_match.start()
    else:
        body_end = len(text)

    # --- Locate start of body -------------------------------------------
    if body_header_match and body_header_match.start() < body_end:
        # Body starts on the line AFTER the SCRIPT_BODY header.
        body_start = body_header_match.end()
        # Skip a single trailing newline if present.
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1
    elif last_hook_end is not None and last_hook_end < body_end:
        body_start = last_hook_end
    else:
        body_start = 0

    raw_body = text[body_start:body_end]

    # --- Strip legacy divider markers -----------------------------------
    # CHOSEN HOOK: HOOK_X — captures chosen letter as a fallback when there
    # was no SCRIPT_BODY annotation.
    chosen_marker_match = _CHOSEN_HOOK_LINE_RE.search(raw_body)
    if chosen_marker_match and chosen_letter is None:
        chosen_letter = chosen_marker_match.group(1)  # type: ignore[assignment]
    raw_body = _CHOSEN_HOOK_LINE_RE.sub("", raw_body)
    raw_body = _SCRIPT_DIVIDER_RE.sub("", raw_body)

    # --- Clean body -----------------------------------------------------
    body = _clean_body(raw_body)

    # Defense: if a stray ``SCRIPT_BODY ...:`` line slipped through (it
    # shouldn't, since we anchored the body start past it), drop it.
    body = _SCRIPT_BODY_HEADER_RE.sub("", body).strip()

    if not body:
        raise ScriptResponseParseError(
            "Could not locate a usable body section. Expected either a "
            "'SCRIPT_BODY:' header or prose between the HOOK_X lines and "
            "the FACT_CHECK_QUEUE / QUALITY_SCORES section."
        )

    # --- Fact-check queue + quality scores ------------------------------
    fact_check_queue: list[str] = []
    quality_scores: dict[str, float] = {}
    quality_rationale: str | None = None

    if fc_match:
        # Slice between FACT_CHECK_QUEUE header and either QUALITY_SCORES
        # header or EOF. Use >= (not >): when FACT_CHECK_QUEUE is empty and the
        # FC header regex's trailing `\s` consumed the blank line up to the
        # QUALITY_SCORES header, quality_match.start() == fc_match.end(); a strict
        # `>` would fall to the else branch and slice to EOF, leaking the
        # QUALITY_SCORES bullets into fact_check_queue (differential audit
        # 2026-06-19). >= yields the correct empty slice.
        if quality_match and quality_match.start() >= fc_match.end():
            fc_section = text[fc_match.end():quality_match.start()]
        else:
            fc_section = text[fc_match.end():]
        fact_check_queue = [
            m.group(1).strip()
            for m in _BULLET_RE.finditer(fc_section)
            if m.group(1).strip()
        ]

    if quality_match:
        quality_section = text[quality_match.end():]
        for m in _QUALITY_LINE_RE.finditer(quality_section):
            name = m.group(1).strip().lower()
            raw = m.group(2).strip()
            if name == "rationale":
                quality_rationale = raw or None
                continue
            raw_num = re.sub(r"\s*\(.*\)\s*$", "", raw).strip()
            try:
                quality_scores[name] = max(0.0, min(1.0, float(raw_num)))
            except ValueError:
                continue

    return ParsedResponse(
        hook_a_text=hook_texts["A"],
        hook_b_text=hook_texts["B"],
        hook_c_text=hook_texts["C"],
        hook_a_formula=hook_formulas["A"],
        hook_b_formula=hook_formulas["B"],
        hook_c_formula=hook_formulas["C"],
        script_body_text=body,
        fact_check_queue=fact_check_queue,
        quality_scores=quality_scores,
        quality_rationale=quality_rationale,
        chosen_hook_letter=chosen_letter,
    )


# ---------------------------------------------------------------------------
# Final-script composition
# ---------------------------------------------------------------------------


def extract_final_script(
    parsed: ParsedResponse, chosen: Literal["A", "B", "C"] = "A"
) -> str:
    """Compose the TTS-ready body using the chosen hook variant.

    Algorithm:
        * Take ``parsed.script_body_text`` (already clean — B-ROLL preserved,
          ``[VERIFY]`` stripped, no ``SCRIPT_BODY:`` header).
        * If the body does NOT begin with the chosen hook's verbal text,
          prepend ``"<hook> "`` + body. (Most LLMs DO repeat the chosen hook
          as the first sentence — the defensive prepend covers the case where
          ``SCRIPT_BODY`` starts at the second sentence.)
        * Return with a trailing newline.

    Raises:
        ValueError: when ``parsed.hook_<chosen>_text`` is ``None``.
    """
    hook_text = {
        "A": parsed.hook_a_text,
        "B": parsed.hook_b_text,
        "C": parsed.hook_c_text,
    }[chosen]
    if hook_text is None:
        raise ValueError(
            f"Cannot compose final script: HOOK_{chosen} text is missing "
            f"from the parsed response."
        )

    body = parsed.script_body_text.strip()
    hook_stripped = hook_text.strip()

    # Case-sensitive, whitespace-tolerant "starts with hook" check.
    if not _body_starts_with_hook(body, hook_stripped):
        body = f"{hook_stripped} {body}"

    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _body_starts_with_hook(body: str, hook: str) -> bool:
    """Return True iff ``body`` opens with ``hook`` (whitespace-tolerant).

    The first sentence of the body may differ from the hook in trailing
    punctuation or whitespace. We normalize both to a sequence of
    non-whitespace tokens and compare the prefix.
    """
    if not hook:
        return False

    def _tokens(s: str) -> list[str]:
        return s.split()

    body_tokens = _tokens(body)
    hook_tokens = _tokens(hook)
    if not hook_tokens or len(body_tokens) < len(hook_tokens):
        return False

    # Compare hook tokens against the body prefix, stripping trailing
    # punctuation from each pair so "wild." matches "wild".
    def _strip_punct(t: str) -> str:
        return t.strip(".,;:!?\"'`()[]{}")

    for hook_tok, body_tok in zip(hook_tokens, body_tokens):
        if _strip_punct(hook_tok).lower() != _strip_punct(body_tok).lower():
            return False
    return True
