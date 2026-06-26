"""Operator-chosen hook + formula tracker for ShadowVerse.

Per `prompts/03_script_generation.md`, the script-generation LLM emits three hook
variants (HOOK_A / HOOK_B / HOOK_C) at the top of `script_RESPONSE.txt`, each
annotated with a `[formula: <name>]` tag. The operator then signs off on a
`script_FINAL.txt` whose first verbal line is one of those three (sometimes
edited slightly, sometimes replaced wholesale).

This module reconciles the two files for a given topic_id and persists the
operator's effective hook choice + formula to a JSONL log
(`<channel_root>/01_research/hook_selection_log.jsonl`). Downstream uses:

  - analytics correlation: which hook formulas drive views/retention
  - prompt feedback loop: surface the operator's preferred formulas back into
    `02_idea_generation.md` and `03_script_generation.md` once enough data accrues

Pure helper `extract_chosen_hook` does the read+match (no I/O beyond the two
input files). Writer `append_to_log` is the only function that touches the log.

This module never invokes an LLM, never imports a framework, and depends only
on the standard library.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hook_selection_log")

# Sentinel formula names. Real formulas are emitted by the LLM (e.g. "Contradiction",
# "Cited-Observation Lead", "Specific-Number Promise") and pass through verbatim.
FORMULA_EDITED = "EDITED"      # operator rewrote the hook past prefix-match recognition
FORMULA_UNTAGGED = "UNTAGGED"  # legacy RESPONSE missing the [formula: ...] annotation

# `HOOK_A: <text>   [formula: <name>]`
# - letter group: A | B | C
# - text group: greedy up to the formula tag (or end of line if missing)
# - formula group: contents of the [formula: ...] tag (optional)
# Tolerates leading/trailing whitespace and case-insensitive `formula:` keyword.
_HOOK_LINE_RE = re.compile(
    r"^\s*HOOK_(?P<letter>[ABC])\s*:\s*(?P<text>.+?)"
    r"(?:\s*\[\s*formula\s*:\s*(?P<formula>[^\]]+?)\s*\])?\s*$",
    re.IGNORECASE,
)

# `[B-ROLL: ...]` cue — non-greedy so multiple cues on one line are stripped one-by-one.
_BROLL_CUE_RE = re.compile(r"\[B-ROLL:[^\]]*\]", re.IGNORECASE)

# Optional preamble line in script_FINAL.txt (e.g. "SCRIPT_BODY (uses HOOK_A as
# the verbal opener):") — recognized so we skip past it to find the real hook.
_SCRIPT_BODY_PREAMBLE_RE = re.compile(r"^\s*SCRIPT_BODY\b.*?:\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class HookCandidate:
    """One of the 3 LLM-proposed hooks (HOOK_A/B/C) parsed from script_RESPONSE.txt."""

    letter: str   # "A", "B", or "C"
    text: str     # the hook line as written by the LLM (single line, stripped)
    formula: str  # e.g. "Contradiction", "Cited-Observation Lead", "Specific-Number Promise"


@dataclass(frozen=True)
class ChosenHook:
    """Which hook the operator shipped, plus the alternatives."""

    topic_id: str
    hook_letter: str | None       # "A" / "B" / "C" if matched; None if EDITED beyond match
    hook_text: str                # verbatim from script_FINAL.txt first non-broll line
    formula: str                  # canonical formula name, "EDITED", or "UNTAGGED"
    all_three_hooks: list[HookCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsers (pure helpers — no I/O)
# ---------------------------------------------------------------------------


def _parse_hook_candidates(response_text: str) -> tuple[list[HookCandidate], bool]:
    """Parse HOOK_A/B/C lines out of a script_RESPONSE.txt blob.

    Returns ``(candidates, all_tagged)`` — ``all_tagged`` is False if any matched
    HOOK_* line was missing its ``[formula: ...]`` tag (the topic is then UNTAGGED
    even if some of the three did carry tags). Returns ``([], False)`` if no
    HOOK_* lines are present at all.
    """
    candidates: list[HookCandidate] = []
    seen_letters: set[str] = set()
    any_untagged = False

    for line in response_text.splitlines():
        m = _HOOK_LINE_RE.match(line)
        if not m:
            continue
        letter = m.group("letter").upper()
        if letter in seen_letters:
            # Duplicates shouldn't happen in well-formed output; keep the first.
            continue
        seen_letters.add(letter)
        text = m.group("text").strip()
        formula_raw = m.group("formula")
        if formula_raw is None or not formula_raw.strip():
            any_untagged = True
            formula = ""
        else:
            formula = formula_raw.strip()
        candidates.append(HookCandidate(letter=letter, text=text, formula=formula))

    if not candidates:
        return [], False
    # Sort by letter so output order is stable A, B, C regardless of source ordering.
    candidates.sort(key=lambda c: c.letter)
    return candidates, not any_untagged


def _extract_first_hook_line(final_text: str) -> str:
    """Pull the first verbal line out of script_FINAL.txt.

    Strategy: walk the lines, skip blanks and the optional ``SCRIPT_BODY ...:``
    preamble, then for the first content line strip ``[B-ROLL: ...]`` cues and
    take the first sentence (up to ``.``, ``!``, or ``?``).

    Returns an empty string if nothing usable is found.
    """
    for raw_line in final_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SCRIPT_BODY_PREAMBLE_RE.match(line):
            continue
        # Strip B-ROLL cues; they may be inline anywhere on the line.
        cleaned = _BROLL_CUE_RE.sub(" ", line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        # Take just the first sentence — the hook is one beat, the rest is body.
        # Match up to and including the first sentence terminator.
        m = re.match(r"^(.+?[.!?])(?:\s|$)", cleaned)
        if m:
            return m.group(1).strip()
        return cleaned
    return ""


def _normalize_for_match(text: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation for prefix-match."""
    s = text.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _first_n_words(text: str, n: int = 3) -> str:
    """Return the first ``n`` words of ``text`` after normalization, joined by single spaces."""
    words = _normalize_for_match(text).split(" ")
    return " ".join(words[:n])


def _match_hook(shipped: str, candidates: list[HookCandidate]) -> HookCandidate | None:
    """Return the candidate whose text matches ``shipped``, or None.

    Strategy: exact normalized match first; then first-3-words prefix match
    (case-insensitive, whitespace-collapsed).
    """
    if not shipped or not candidates:
        return None
    shipped_norm = _normalize_for_match(shipped)
    for c in candidates:
        if _normalize_for_match(c.text) == shipped_norm:
            return c
    shipped_prefix = _first_n_words(shipped, 3)
    if not shipped_prefix:
        return None
    for c in candidates:
        if _first_n_words(c.text, 3) == shipped_prefix:
            return c
    return None


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------


def extract_chosen_hook(topic_id: str, channel_root: Path) -> ChosenHook:
    """Read script_RESPONSE.txt + script_FINAL.txt for ``topic_id`` and figure out
    which hook variant the operator selected.

    Matching strategy (in order):
      1. Exact match: shipped first line == one of HOOK_A/B/C text → that letter+formula
      2. First-3-words prefix match (case-insensitive, whitespace-collapsed) → that letter+formula
      3. No match: hook_letter=None, formula="EDITED", hook_text=verbatim shipped line
      4. RESPONSE.txt missing or has no [formula:] tags → hook_letter=None, formula="UNTAGGED"

    Raises FileNotFoundError if script_FINAL.txt missing. Returns ChosenHook with
    empty all_three_hooks if script_RESPONSE.txt missing.
    """
    topic_dir = Path(channel_root) / "02_scripts" / "_drafts" / topic_id
    final_path = topic_dir / "script_FINAL.txt"
    response_path = topic_dir / "script_RESPONSE.txt"

    if not final_path.exists():
        raise FileNotFoundError(
            f"script_FINAL.txt not found for topic {topic_id!r} at {final_path}"
        )

    final_text = final_path.read_text(encoding="utf-8", errors="replace")
    shipped = _extract_first_hook_line(final_text)

    if response_path.exists():
        response_text = response_path.read_text(encoding="utf-8", errors="replace")
        candidates, all_tagged = _parse_hook_candidates(response_text)
    else:
        log.info("script_RESPONSE.txt missing for %s; all_three_hooks will be empty", topic_id)
        candidates, all_tagged = [], False

    # Source missing or untagged → operator's pick can't be formula-tagged either.
    if not candidates:
        return ChosenHook(
            topic_id=topic_id,
            hook_letter=None,
            hook_text=shipped,
            formula=FORMULA_UNTAGGED,
            all_three_hooks=[],
        )
    if not all_tagged:
        return ChosenHook(
            topic_id=topic_id,
            hook_letter=None,
            hook_text=shipped,
            formula=FORMULA_UNTAGGED,
            all_three_hooks=candidates,
        )

    matched = _match_hook(shipped, candidates)
    if matched is None:
        return ChosenHook(
            topic_id=topic_id,
            hook_letter=None,
            hook_text=shipped,
            formula=FORMULA_EDITED,
            all_three_hooks=candidates,
        )

    return ChosenHook(
        topic_id=topic_id,
        hook_letter=matched.letter,
        hook_text=shipped,
        formula=matched.formula,
        all_three_hooks=candidates,
    )


# ---------------------------------------------------------------------------
# Formula-tag validation (PU-5a, 2026-06-09 weekly review — R2 H3 / CL-7.1)
# ---------------------------------------------------------------------------

# "Cited-Observation Lead" requires a named THIRD-PARTY HUMAN source (handle,
# byline, named person/outlet) per the channel's cited-observation durable
# rule. The 2026-06-09 review found >=5 vendor-only hooks tagged CO-Lead
# (2026-06-05_001/_002, 2026-06-07_002, 2026-06-09_001/_002), contaminating
# formula attribution. This validation NEVER blocks the append — it adds a
# `tag_warning` field to the JSONL row + logs a WARNING so the next review
# can bucket the row correctly.

_CITED_OBSERVATION_FORMULA = "cited-observation lead"
TAG_WARNING_VENDOR_ONLY = "vendor-only hook tagged as Cited-Observation Lead"

# @handle (X/Twitter etc.) or reddit u/name / r/sub.
_NAMED_HANDLE_RE = re.compile(r"(?:^|[\s(\[\"'])@\w{2,}|\b[ur]/[\w-]{2,}", re.IGNORECASE)

# Named outlets — bylines/publications count as named third-party sources.
_NAMED_OUTLET_RE = re.compile(
    r"\b(?:bloomberg|reuters|the\s+verge|techcrunch|wired|forbes|cnbc|cnn|bbc"
    r"|axios|semafor|politico|fortune|the\s+atlantic|new\s+york\s+times|nyt"
    r"|wall\s+street\s+journal|wsj|washington\s+post|business\s+insider"
    r"|ars\s+technica|the\s+information|404\s+media|9to5mac|hacker\s+news"
    r"|mit\s+technology\s+review)\b",
    re.IGNORECASE,
)

# Vendor / product tokens (lowercase). A capitalized bigram whose words BOTH
# avoid this set is treated as a person/outlet name ("Jamie Dimon", "Terence
# Tao"); a bigram touching the set is a vendor/product pair ("Google DeepMind",
# "Apple Intelligence", "Claude Code") and does NOT count as a named human.
_VENDOR_PRODUCT_TOKENS = frozenset({
    "openai", "anthropic", "google", "deepmind", "microsoft", "meta", "apple",
    "amazon", "tesla", "nvidia", "xai", "chatgpt", "claude", "gemini", "grok",
    "copilot", "llama", "gpt", "sora", "bing", "windows", "siri", "alexa",
    "iphone", "android", "intelligence", "code", "pro", "plus", "max", "ultra",
    "mini", "flash", "store", "studio", "cloud", "watch", "vision", "ai",
})

_CAP_BIGRAM_RE = re.compile(r"\b([A-Z][\w’']+)\s+([A-Z][\w’']+)\b")


def _has_named_source(text: str) -> bool:
    """Heuristic: does the hook text name a third-party source?

    True on any of: an @handle / u/name / r/sub, a known outlet name, or a
    capitalized bigram where neither word is a vendor/product token (a
    person-name like "Sam Altman" / "Jamie Dimon").

    Known limitation (documented, not blocking): single-surname citations
    ("Karpathy says...") and status-noun anonymous sources ("a Fields
    medalist") are not detected and will draw a (non-blocking) tag_warning.
    """
    if _NAMED_HANDLE_RE.search(text):
        return True
    if _NAMED_OUTLET_RE.search(text):
        return True
    for a, b in _CAP_BIGRAM_RE.findall(text):
        if a.lower() in _VENDOR_PRODUCT_TOKENS or b.lower() in _VENDOR_PRODUCT_TOKENS:
            continue
        return True
    return False


def cited_observation_tag_warning(formula: str, hook_text: str) -> str | None:
    """Return the PU-5a tag-warning string, or None when the tag looks valid.

    Fires only when ``formula`` is "Cited-Observation Lead" (case-insensitive)
    AND ``hook_text`` contains no named-source pattern. Pure + non-blocking:
    callers attach the result to the JSONL row; they never reject the row.
    """
    if (formula or "").strip().lower() != _CITED_OBSERVATION_FORMULA:
        return None
    if _has_named_source(hook_text or ""):
        return None
    return TAG_WARNING_VENDOR_ONLY


# ---------------------------------------------------------------------------
# Public writer
# ---------------------------------------------------------------------------


def _chosen_to_record(chosen: ChosenHook) -> dict:
    """Convert a ChosenHook into the JSONL row schema (sans ``logged_at``)."""
    record = {
        "topic_id": chosen.topic_id,
        "hook_letter": chosen.hook_letter,
        "hook_text": chosen.hook_text,
        "formula": chosen.formula,
        "all_three_hooks": [asdict(c) for c in chosen.all_three_hooks],
    }
    # PU-5a: non-blocking formula-tag validation — annotate, never reject.
    warning = cited_observation_tag_warning(chosen.formula, chosen.hook_text)
    if warning is not None:
        record["tag_warning"] = warning
        log.warning(
            "hook-log tag validation for %s: %s (hook=%r)",
            chosen.topic_id, warning, chosen.hook_text,
        )
    return record


def _records_equal(a: dict, b: dict) -> bool:
    """Compare two log records ignoring the ``logged_at`` timestamp."""
    a_cmp = {k: v for k, v in a.items() if k != "logged_at"}
    b_cmp = {k: v for k, v in b.items() if k != "logged_at"}
    return a_cmp == b_cmp


def append_to_log(chosen: ChosenHook, log_path: Path) -> bool:
    """Append a JSON line to hook_selection_log.jsonl. Idempotent on (topic_id).

    Behavior:
      - If no existing line for this topic_id: append, return True.
      - If an existing line for this topic_id has the SAME content (ignoring the
        ``logged_at`` timestamp): no-op, return False.
      - If an existing line for this topic_id has DIFFERENT content (operator
        re-edited): rewrite the file with the existing line replaced by the new
        record, return True.

    JSONL schema (one row per topic):
      {"topic_id": "...", "hook_letter": "A", "hook_text": "...", "formula": "...",
       "all_three_hooks": [{"letter": "A", "text": "...", "formula": "..."}, ...],
       "logged_at": "<UTC ISO timestamp>"}
    """
    log_path = Path(log_path)
    new_record = _chosen_to_record(chosen)
    new_record["logged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not log_path.exists():
        with log_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(new_record, ensure_ascii=False) + "\n")
        log.info("created hook selection log at %s with first row for %s",
                 log_path, chosen.topic_id)
        return True

    existing_lines: list[str] = log_path.read_text(encoding="utf-8").splitlines()
    found_index: int | None = None
    found_record: dict | None = None
    for i, raw in enumerate(existing_lines):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            log.warning("skipping malformed JSONL line %d in %s", i + 1, log_path)
            continue
        if row.get("topic_id") == chosen.topic_id:
            found_index = i
            found_record = row
            break

    if found_index is None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_record, ensure_ascii=False) + "\n")
        log.info("appended new row for %s to %s", chosen.topic_id, log_path)
        return True

    assert found_record is not None  # noqa: S101 — guarded by found_index check
    if _records_equal(found_record, new_record):
        log.debug("no-op: %s row already up to date in %s", chosen.topic_id, log_path)
        return False

    existing_lines[found_index] = json.dumps(new_record, ensure_ascii=False)
    # Preserve trailing newline behavior: rewrite with newline-terminated rows.
    rewritten = "\n".join(line for line in existing_lines if line.strip()) + "\n"
    log_path.write_text(rewritten, encoding="utf-8")
    log.info("overwrote %s row in %s (operator re-edited)", chosen.topic_id, log_path)
    return True
