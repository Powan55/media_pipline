"""Hook-A/B addendum for the daily batch summary.

Renders a Markdown block per topic that:
  - Shows which of HOOK_A/B/C the operator selected, with its formula tag
  - Lists the two un-chosen variants for context
  - Appends a one-line "leaderboard says formula X has median hold@3s Y at n=N" footer

Slice 8 will wire this into ``daily_batch._write_batch_summary``, appending the
returned block under each topic's existing entry. This module is pure — no
file writes, just string formatting.

The function never raises: the worst case is a single-line placeholder when
the topic hasn't yet passed gate 2 (no ``script_FINAL.txt``). The leaderboard
footer is best-effort and isolated in its own try/except so analytics failures
never block the chosen-vs-alternatives display, which is the addendum's
primary value.
"""

from __future__ import annotations

import logging
from pathlib import Path

from analytics_join import join_hooks_to_analytics
from hook_leaderboard_stats import evidence_strength, formula_medians
from hook_selection_log import (
    FORMULA_EDITED,
    FORMULA_UNTAGGED,
    ChosenHook,
    HookCandidate,
    extract_chosen_hook,
)

log = logging.getLogger("daily_batch_hook_addendum")

# Sentinel single-line placeholders. Returned verbatim when the topic isn't
# ready for the full block yet — Slice 8 appends these straight under the
# topic's existing summary entry.
_AWAITING_GATE_2 = "(awaiting gate 2 selection)"
_NO_RESPONSE = "(no LLM response logged for this topic)"


def format_hook_addendum(topic_id: str, channel_root: Path) -> str:
    """Render the Markdown addendum for one topic.

    Behavior matrix:
      - ``script_FINAL.txt`` missing (gate 2 not yet passed) -> returns
        ``"(awaiting gate 2 selection)"`` verbatim, single line.
      - ``script_RESPONSE.txt`` missing -> returns
        ``"(no LLM response logged for this topic)"`` single line.
      - Both present, hook extraction succeeds -> returns the full block:

        ```
        **Chosen hook:** A - Specific-Number Promise
        > <hook text>

        **Alternatives:**
        - B (Cited-Observation Lead): <hook B text>
        - C (Contradiction): <hook C text>

        **Leaderboard for "Specific-Number Promise":** median hold@3s = 0.72 at n=3 (weak)
        ```

      - Hook extraction returns formula="EDITED" ->
        ``"Chosen hook: (EDITED - formula could not be matched)"``
        plus alternatives still listed if available.
      - Hook extraction returns formula="UNTAGGED" ->
        ``"Chosen hook: <text> - (formula tags missing in RESPONSE)"``
        plus alternatives still listed.

    The leaderboard footer is best-effort: if Slice 3's
    ``join_hooks_to_analytics`` has zero eligible rows for the chosen formula,
    the footer reads "Leaderboard: insufficient data for this formula yet
    (n=0)". Any exception inside the leaderboard path is logged + caught and
    rendered as ``"(leaderboard unavailable: <error>)"`` so the addendum still
    ships.

    Args:
        topic_id: Canonical topic id, e.g. ``"2026-05-10_001"``.
        channel_root: Repo-root for ShadowVerse, e.g.
            ``C:/ContentOps/channels/ShadowVerse``.

    Returns:
        A Markdown string. Either a single placeholder line or the multi-line
        chosen-hook + alternatives + leaderboard block. Never raises.
    """
    channel_root = Path(channel_root)
    topic_dir = channel_root / "02_scripts" / "_drafts" / topic_id
    final_path = topic_dir / "script_FINAL.txt"
    response_path = topic_dir / "script_RESPONSE.txt"

    if not final_path.exists():
        return _AWAITING_GATE_2
    if not response_path.exists():
        return _NO_RESPONSE

    try:
        chosen = extract_chosen_hook(topic_id, channel_root)
    except FileNotFoundError:
        # Defensive — both files existed at the .exists() checks above. If a
        # race or filesystem oddity drops one between then and here, fall
        # through to the awaiting placeholder rather than crashing.
        return _AWAITING_GATE_2

    return _render_block(chosen, channel_root)


# ---------------------------------------------------------------------------
# Internal renderers
# ---------------------------------------------------------------------------


def _render_chosen_line(chosen: ChosenHook) -> str:
    """Render the ``**Chosen hook:** ...`` header line for the given selection."""
    if chosen.formula == FORMULA_EDITED:
        return "**Chosen hook:** (EDITED - formula could not be matched)"
    if chosen.formula == FORMULA_UNTAGGED:
        text = chosen.hook_text or "(no text)"
        return f"**Chosen hook:** {text} - (formula tags missing in RESPONSE)"
    letter = chosen.hook_letter or "?"
    return f"**Chosen hook:** {letter} - {chosen.formula}"


def _render_alternatives(chosen: ChosenHook) -> list[str]:
    """Render the ``**Alternatives:**`` block as a list of lines.

    Skips the chosen letter when known. Returns an empty list when no
    alternatives are available (RESPONSE had no candidates), so the caller can
    drop the section entirely.
    """
    if not chosen.all_three_hooks:
        return []
    alternatives: list[HookCandidate] = [
        c for c in chosen.all_three_hooks if c.letter != chosen.hook_letter
    ]
    if not alternatives:
        # Edge case: hook_letter matched but no other candidates parsed.
        return []
    lines: list[str] = ["**Alternatives:**"]
    for c in alternatives:
        formula_label = c.formula or "untagged"
        lines.append(f"- {c.letter} ({formula_label}): {c.text}")
    return lines


def _render_leaderboard_footer(formula: str, channel_root: Path) -> str:
    """Render the one-line leaderboard footer for ``formula``.

    Best-effort: catches any exception from ``join_hooks_to_analytics`` /
    ``formula_medians`` and renders an "(leaderboard unavailable: ...)" line
    so the addendum still ships when analytics CSVs are missing or malformed.
    """
    if formula in (FORMULA_EDITED, FORMULA_UNTAGGED):
        # No formula to look up. Skip the footer entirely; caller will drop it.
        return ""

    try:
        rows = join_hooks_to_analytics(channel_root)
        medians = formula_medians(rows)
    except Exception as exc:  # noqa: BLE001 — leaderboard must never block
        log.warning(
            "leaderboard footer unavailable for formula=%r: %s", formula, exc,
        )
        return f'**Leaderboard for "{formula}":** (leaderboard unavailable: {exc})'

    stat = medians.get(formula)
    if stat is None or stat.n == 0 or stat.median_hold_at_3s is None:
        return (
            f'**Leaderboard for "{formula}":** insufficient data for this '
            f"formula yet (n=0)"
        )

    strength = evidence_strength(stat.n, stat.wilson_ci_above_cohort_median)
    return (
        f'**Leaderboard for "{formula}":** median hold@3s = '
        f"{stat.median_hold_at_3s:.2f} at n={stat.n} ({strength})"
    )


def _render_block(chosen: ChosenHook, channel_root: Path) -> str:
    """Compose the full multi-line Markdown block for a parsed selection."""
    lines: list[str] = []
    lines.append(_render_chosen_line(chosen))

    # Chosen-hook quote line (skip for EDITED to avoid a duplicate text echo —
    # the EDITED chosen line already implies the operator rewrote it; the text
    # appears as `chosen.hook_text` if we wanted it but rendering the verbatim
    # operator line under the "could not be matched" label is more confusing
    # than helpful).
    if chosen.formula not in (FORMULA_EDITED, FORMULA_UNTAGGED) and chosen.hook_text:
        lines.append(f"> {chosen.hook_text}")
    elif chosen.formula == FORMULA_EDITED and chosen.hook_text:
        # For EDITED, still show the operator's shipped text so the operator
        # can see what they shipped vs the alternatives below.
        lines.append(f"> {chosen.hook_text}")

    alt_lines = _render_alternatives(chosen)
    if alt_lines:
        lines.append("")
        lines.extend(alt_lines)

    footer = _render_leaderboard_footer(chosen.formula, channel_root)
    if footer:
        lines.append("")
        lines.append(footer)

    return "\n".join(lines)


__all__ = ["format_hook_addendum"]
