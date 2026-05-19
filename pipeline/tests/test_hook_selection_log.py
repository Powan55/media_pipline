"""Unit tests for hook_selection_log.py.

All tests use the pytest ``tmp_path`` fixture — no test ever touches the real
channel root. Coverage target (per Sprint 4 slice 1 spec):

  - exact-match (shipped == HOOK_A text)
  - first-3-words prefix-match (operator tweaked the tail)
  - EDITED fallback (no match)
  - missing FINAL raises FileNotFoundError
  - missing RESPONSE returns ChosenHook with empty all_three_hooks (formula UNTAGGED)
  - UNTAGGED legacy RESPONSE (no [formula: ...] tags)
  - idempotent re-append (no duplicate line on identical content)
  - overwrite on content change (operator re-edited the FINAL)
  - inline B-ROLL stripping (single-line script with cues interspersed)
  - SCRIPT_BODY preamble skipped
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hook_selection_log import (  # noqa: E402
    FORMULA_EDITED,
    FORMULA_UNTAGGED,
    ChosenHook,
    HookCandidate,
    append_to_log,
    extract_chosen_hook,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_topic_dir(channel_root: Path, topic_id: str) -> Path:
    """Create the per-topic drafts dir and return its Path."""
    d = channel_root / "02_scripts" / "_drafts" / topic_id
    d.mkdir(parents=True)
    return d


def _write_response(topic_dir: Path, body: str) -> None:
    """Write a script_RESPONSE.txt with the given body."""
    (topic_dir / "script_RESPONSE.txt").write_text(body, encoding="utf-8")


def _write_final(topic_dir: Path, body: str) -> None:
    """Write a script_FINAL.txt with the given body."""
    (topic_dir / "script_FINAL.txt").write_text(body, encoding="utf-8")


# Canonical RESPONSE/FINAL pair modeled on the real 2026-05-12_002 sample.
_RESPONSE_TAGGED = """\
HOOK_A: Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.   [formula: Specific-Number Promise]
HOOK_B: You think you're getting advice from Claude. You're getting a yes man.   [formula: Contradiction]
HOOK_C: Anthropic studied a million Claude chats. The relationship answer is the dishonest one.   [formula: Cited-Observation Lead]

[B-ROLL: phone] body irrelevant for hook extraction.

FACT_CHECK_QUEUE
- something

QUALITY_SCORES
- hook_strength: 0.88
"""

# RESPONSE with no [formula: ...] tags (legacy from 2026-05-05 batch).
_RESPONSE_UNTAGGED = """\
HOOK_A: Cursor users still paste context. A folder does it for you.
HOOK_B: I deleted half my Cursor prompts. The chat got better.
HOOK_C: Cursor reads this folder before your message. You're missing it.

[B-ROLL: a wall of pasted context being typed into Cursor's chat panel]
Most Cursor users still paste context into chat.
"""


# ---------------------------------------------------------------------------
# extract_chosen_hook — matching strategies
# ---------------------------------------------------------------------------


class TestExtractExactMatch:

    def test_exact_match_returns_letter_and_formula(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        # FINAL: shipped HOOK_A verbatim, with B-ROLL cues interleaved on the same line.
        _write_final(
            topic_dir,
            "[B-ROLL: phone in hand] Anthropic just told on Claude. "
            "Twenty five percent of relationship chats are pure flattery. "
            "[B-ROLL: laptop screen] body line that we ignore.",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.topic_id == topic_id
        assert result.hook_letter == "A"
        assert result.formula == "Specific-Number Promise"
        # First-sentence extraction stops at the first "."
        assert result.hook_text == "Anthropic just told on Claude."
        assert len(result.all_three_hooks) == 3
        assert {c.letter for c in result.all_three_hooks} == {"A", "B", "C"}

    def test_exact_match_when_final_is_multiline_with_broll_separate(
        self, tmp_path: Path,
    ) -> None:
        """Sample modeled on 2026-05-11_002 — hook on its own line, B-ROLL on the next."""
        topic_id = "2026-05-11_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude.\n"
            "[B-ROLL: phone screen]\n"
            "Twenty five percent of relationship chats are pure flattery.\n",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter == "A"
        assert result.formula == "Specific-Number Promise"
        assert result.hook_text == "Anthropic just told on Claude."


class TestExtractPrefixMatch:

    def test_first_three_words_match_picks_hook(self, tmp_path: Path) -> None:
        """Operator edited the tail but kept the first three words from HOOK_C."""
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic studied a different kind of advice problem here.\n",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter == "C"
        assert result.formula == "Cited-Observation Lead"

    def test_prefix_match_is_case_insensitive(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(topic_dir, "ANTHROPIC JUST TOLD on the entire industry today.\n")

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter == "A"
        assert result.formula == "Specific-Number Promise"


class TestExtractEditedFallback:

    def test_no_match_returns_edited_with_verbatim_text(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "OpenAI shipped a brand-new agent product nobody asked for.\n",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter is None
        assert result.formula == FORMULA_EDITED
        assert result.hook_text == "OpenAI shipped a brand-new agent product nobody asked for."
        # Alternatives are still surfaced for analytics review.
        assert len(result.all_three_hooks) == 3


# ---------------------------------------------------------------------------
# extract_chosen_hook — missing files
# ---------------------------------------------------------------------------


class TestExtractMissingFiles:

    def test_missing_final_raises(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_002"
        _make_topic_dir(tmp_path, topic_id)
        # No script_FINAL.txt written.
        with pytest.raises(FileNotFoundError):
            extract_chosen_hook(topic_id, tmp_path)

    def test_missing_response_returns_untagged_with_empty_alternatives(
        self, tmp_path: Path,
    ) -> None:
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_final(topic_dir, "Some shipped hook line.\n[B-ROLL: x]\nbody\n")
        # No script_RESPONSE.txt.

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter is None
        assert result.formula == FORMULA_UNTAGGED
        assert result.hook_text == "Some shipped hook line."
        assert result.all_three_hooks == []


class TestExtractUntaggedLegacy:

    def test_legacy_response_without_formula_tags_returns_untagged(
        self, tmp_path: Path,
    ) -> None:
        topic_id = "2026-05-05_001"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_UNTAGGED)
        _write_final(topic_dir, "Cursor users still paste context.\n[B-ROLL: x]\n")

        result = extract_chosen_hook(topic_id, tmp_path)

        # Even though the text matches HOOK_A exactly, the lack of formula tags
        # means we don't have a confident formula attribution → UNTAGGED.
        assert result.hook_letter is None
        assert result.formula == FORMULA_UNTAGGED
        # all_three_hooks is still populated so analytics can see the proposals.
        assert len(result.all_three_hooks) == 3
        assert all(c.formula == "" for c in result.all_three_hooks)


# ---------------------------------------------------------------------------
# extract_chosen_hook — edge cases
# ---------------------------------------------------------------------------


class TestExtractEdgeCases:

    def test_script_body_preamble_is_skipped(self, tmp_path: Path) -> None:
        """Modeled on 2026-05-12_002 real FINAL with the SCRIPT_BODY preamble line."""
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
            "\n"
            "[B-ROLL: phone in hand, Claude AI app open] "
            "Anthropic just told on Claude. [B-ROLL: laptop] body...",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter == "A"
        assert result.hook_text == "Anthropic just told on Claude."

    def test_inline_broll_cues_are_stripped_before_match(self, tmp_path: Path) -> None:
        """All-on-one-line script (real 2026-05-12_002 shape)."""
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "[B-ROLL: a] Anthropic just told on Claude. [B-ROLL: b] more body. "
            "[B-ROLL: c] still more.",
        )

        result = extract_chosen_hook(topic_id, tmp_path)

        assert result.hook_letter == "A"
        assert result.hook_text == "Anthropic just told on Claude."


# ---------------------------------------------------------------------------
# append_to_log — idempotency + overwrite
# ---------------------------------------------------------------------------


def _make_chosen(topic_id: str = "2026-05-12_002", *, formula: str = "Contradiction",
                 hook_letter: str | None = "B", hook_text: str = "Sample hook.") -> ChosenHook:
    return ChosenHook(
        topic_id=topic_id,
        hook_letter=hook_letter,
        hook_text=hook_text,
        formula=formula,
        all_three_hooks=[
            HookCandidate(letter="A", text="Hook A text.", formula="Specific-Number Promise"),
            HookCandidate(letter="B", text="Sample hook.", formula="Contradiction"),
            HookCandidate(letter="C", text="Hook C text.", formula="Cited-Observation Lead"),
        ],
    )


class TestAppendToLog:

    def test_first_write_creates_file_and_returns_true(self, tmp_path: Path) -> None:
        log_path = tmp_path / "01_research" / "hook_selection_log.jsonl"
        chosen = _make_chosen()

        wrote = append_to_log(chosen, log_path)

        assert wrote is True
        assert log_path.exists()
        rows = [json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line]
        assert len(rows) == 1
        row = rows[0]
        assert row["topic_id"] == "2026-05-12_002"
        assert row["hook_letter"] == "B"
        assert row["formula"] == "Contradiction"
        assert "logged_at" in row
        assert len(row["all_three_hooks"]) == 3

    def test_idempotent_reappend_returns_false_and_no_duplicate(
        self, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "hook_selection_log.jsonl"
        chosen = _make_chosen()

        first = append_to_log(chosen, log_path)
        second = append_to_log(chosen, log_path)

        assert first is True
        assert second is False
        rows = [json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line]
        assert len(rows) == 1

    def test_overwrite_on_content_change_replaces_row_in_place(
        self, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "hook_selection_log.jsonl"
        original = _make_chosen(hook_letter="B", formula="Contradiction",
                                hook_text="Sample hook.")
        revised = _make_chosen(hook_letter="A", formula="Specific-Number Promise",
                               hook_text="Hook A text.")

        first = append_to_log(original, log_path)
        second = append_to_log(revised, log_path)

        assert first is True
        assert second is True
        rows = [json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line]
        # Still one row for this topic_id, but now with the revised content.
        assert len(rows) == 1
        assert rows[0]["hook_letter"] == "A"
        assert rows[0]["formula"] == "Specific-Number Promise"

    def test_distinct_topic_ids_get_separate_rows(self, tmp_path: Path) -> None:
        log_path = tmp_path / "hook_selection_log.jsonl"
        a = _make_chosen(topic_id="2026-05-12_001")
        b = _make_chosen(topic_id="2026-05-12_002")

        wrote_a = append_to_log(a, log_path)
        wrote_b = append_to_log(b, log_path)

        assert wrote_a is True
        assert wrote_b is True
        rows = [json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line]
        assert len(rows) == 2
        assert {r["topic_id"] for r in rows} == {"2026-05-12_001", "2026-05-12_002"}
