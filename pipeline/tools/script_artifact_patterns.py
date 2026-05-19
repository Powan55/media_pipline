"""Single source of truth for script / caption template-artifact detection.

Layer 1, 2, and 3 of the Sprint 5 prevention defense all import
:func:`scan_for_artifacts` from this module — the regex set never drifts.

Background
----------
2026-05-13: ``_12_002`` shipped to scheduled-Private with the literal phrase
``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` at the top of
``script_FINAL.txt``, which edge-TTS spoke aloud. Root cause: the gate-2 LLM
copied the ``script_RESPONSE.txt`` body verbatim, including the separator
annotation header. The three-layer prevention plan:

    Layer 1  prompt hardening              (LLM-side, cheapest)
    Layer 2  Stage 11 check #13            (script_FINAL.txt scan, safety net)
    Layer 3  caption-side double-check     (.ass file scan, belt-and-braces)

All three call :func:`scan_for_artifacts` and treat any non-empty result as
a halt.
"""

from __future__ import annotations

import re
from typing import Iterable, NamedTuple

# ---------------------------------------------------------------------------
# Pattern catalog
# ---------------------------------------------------------------------------

TEMPLATE_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\{[A-Z_]+\}"),
    re.compile(r"<[A-Z_]+>"),
    re.compile(r"\[[A-Z_]+\]"),
)
"""Literal ``{FOO}`` / ``<FOO>`` / ``[FOO]`` markers with ALL-CAPS names.

Case-sensitive. The LLM-side annotations we want to catch are uppercase
identifiers (``SCRIPT_BODY``, ``HOOK_A``). A title bracket like
``[Subject]`` is mixed-case and does NOT match.
"""

INTERNAL_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bscript[_\s]body\b", re.IGNORECASE),
    re.compile(r"\bhook[_\s][abc](?:[_\s][a-z_]+)?\b", re.IGNORECASE),
    re.compile(r"\bcited[_\s]observation\b", re.IGNORECASE),
    re.compile(r"\bbroll[_\s]cue\b", re.IGNORECASE),
    re.compile(r"\bverbal[_\s]opener\b", re.IGNORECASE),
    re.compile(r"\bfact[_\s]check[_\s]queue\b", re.IGNORECASE),
    re.compile(r"\bquality[_\s]scores?\b", re.IGNORECASE),
)
"""Pipeline-internal field names. Case-insensitive; matches the underscore
form (``script_body``) AND the single-whitespace form (``script body``)
so neither styling sneaks past."""

STAGE_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\(uses [A-Z_]+(?:[^)]*)?\)"),
    re.compile(r"^\s*>\s+", re.MULTILINE),
    re.compile(r"^\s*#{1,3}\s+\S", re.MULTILINE),
    re.compile(r"^\s*\*\*[^*]+\*\*\s*$", re.MULTILINE),
    re.compile(r"^\s*[A-Z][A-Z_]+\s*:?\s*$", re.MULTILINE),
)
"""Annotation-structure patterns: the literal ``(uses HOOK_A ...)`` form
plus markdown quote / header / bold-only / all-caps-line shapes.

The all-caps-line pattern (last) will match short acronyms like ``USA`` on
their own line — accepted: a script body should not contain standalone
all-caps single-token lines in any natural prose context."""

# (category, pattern_name, compiled regex). Order is stable so test output
# is deterministic.
_PATTERN_CATALOG: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    *(
        ("template_placeholder", f"TEMPLATE_{i}", p)
        for i, p in enumerate(TEMPLATE_PLACEHOLDER_PATTERNS)
    ),
    *(
        ("internal_name", f"INTERNAL_{i}", p)
        for i, p in enumerate(INTERNAL_NAME_PATTERNS)
    ),
    *(
        ("stage_instruction", f"STAGE_{i}", p)
        for i, p in enumerate(STAGE_INSTRUCTION_PATTERNS)
    ),
)


class ArtifactMatch(NamedTuple):
    """One regex hit.

    ``line_no`` is 1-based. ``line_text`` is the full text of the line
    containing the match (no trailing newline), trimmed of nothing — caller
    can strip / truncate for display.
    """

    line_no: int
    category: str
    pattern_name: str
    matched_text: str
    line_text: str


def _line_of(text: str, pos: int) -> tuple[int, str]:
    """Return ``(line_no, line_text)`` for character offset ``pos`` in ``text``.

    line_no is 1-based; line_text does NOT include the trailing newline.
    """
    line_no = text.count("\n", 0, pos) + 1
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return line_no, text[start:end]


def scan_for_artifacts(text: str) -> list[ArtifactMatch]:
    """Scan ``text`` for any of the canonical artifact patterns.

    Returns matches sorted by ``(line_no, pattern_name)``. An empty list
    means the text is clean. Layer 2 and Layer 3 each treat a non-empty
    result as a hard halt.

    The text is scanned in full (not line-by-line) so multi-line patterns
    using ``re.MULTILINE`` behave correctly. Overlapping matches from
    different patterns are all reported.
    """
    matches: list[ArtifactMatch] = []
    for category, name, pattern in _PATTERN_CATALOG:
        for m in pattern.finditer(text):
            line_no, line_text = _line_of(text, m.start())
            matches.append(
                ArtifactMatch(
                    line_no=line_no,
                    category=category,
                    pattern_name=name,
                    matched_text=m.group(0),
                    line_text=line_text,
                )
            )
    matches.sort(key=lambda x: (x.line_no, x.pattern_name))
    return matches


def format_matches(matches: Iterable[ArtifactMatch]) -> str:
    """Human-readable rendering of matches for FAIL messages."""
    rendered = [
        f"  line {m.line_no} [{m.category}/{m.pattern_name}]: "
        f"{m.matched_text!r} (in: {m.line_text.strip()[:80]!r})"
        for m in matches
    ]
    return "\n".join(rendered) if rendered else "(no matches)"
