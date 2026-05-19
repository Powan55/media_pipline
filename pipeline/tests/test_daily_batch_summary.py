"""Unit tests for ``daily_batch._write_batch_summary`` Slice-8 wiring.

Slice 8 wires ``daily_batch_hook_addendum.format_hook_addendum`` into the
per-topic Markdown block ``_write_batch_summary`` renders. These tests pin
the integration:

  1. A topic with ``script_FINAL.txt`` produces a ``**Chosen hook:**`` line in
     its section of the rendered summary.
  2. A topic with no ``script_FINAL.txt`` (still at gate 2) renders the
     ``(awaiting gate 2 selection)`` placeholder.
  3. If ``format_hook_addendum`` raises, the summary file is still written
     with no crash and a WARNING is logged.
  4. With two topics, both addendums appear in their respective sections in
     the right order.

All tests use ``tempfile.TemporaryDirectory`` for the channel root — no test
touches the real channel data. The unittest+TestCase style matches the
existing ``test_daily_batch_allocator.py`` convention.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import daily_batch  # noqa: E402
from daily_batch import TopicResult, _write_batch_summary  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: synthetic channel_root + minimal per-topic drafts dir
# ---------------------------------------------------------------------------


_RESPONSE_TAGGED = """\
HOOK_A: Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.   [formula: Specific-Number Promise]
HOOK_B: You think you're getting advice from Claude. You're getting a yes man.   [formula: Contradiction]
HOOK_C: Anthropic studied a million Claude chats. The relationship answer is the dishonest one.   [formula: Cited-Observation Lead]

[B-ROLL: phone] body irrelevant for hook extraction.
"""


class _ChannelRoot:
    """Per-test scratch channel root with the subdirs daily_batch expects."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.mkdtemp(prefix="shadowverse-summary-test-")
        root = Path(self._tmp)
        (root / "01_research").mkdir(parents=True)
        (root / "02_scripts" / "_drafts").mkdir(parents=True)
        return root

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


def _seed_topic_with_final(channel_root: Path, topic_id: str) -> None:
    """Write minimal RESPONSE + FINAL files so the addendum hits the happy path."""
    topic_dir = channel_root / "02_scripts" / "_drafts" / topic_id
    topic_dir.mkdir(parents=True)
    (topic_dir / "script_RESPONSE.txt").write_text(_RESPONSE_TAGGED, encoding="utf-8")
    (topic_dir / "script_FINAL.txt").write_text(
        "Anthropic just told on Claude. "
        "Twenty five percent of relationship chats are pure flattery.",
        encoding="utf-8",
    )


def _seed_topic_at_gate_2(channel_root: Path, topic_id: str) -> None:
    """Create the topic dir but no script_FINAL.txt — still at gate 2."""
    topic_dir = channel_root / "02_scripts" / "_drafts" / topic_id
    topic_dir.mkdir(parents=True)


def _make_topic_result(
    topic_id: str,
    *,
    topic: str = "test topic",
    angle: str = "test angle",
    score: float = 0.85,
    status: str = "completed",
    halt_stage: str | None = None,
    halt_message: str = "(fully shipped through schedule_publishing)",
    next_action_path: Path | None = None,
) -> TopicResult:
    return TopicResult(
        topic_id=topic_id,
        topic=topic,
        angle=angle,
        weighted_total=score,
        status=status,
        halt_stage=halt_stage,
        halt_message=halt_message,
        next_action_path=next_action_path,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class WriteBatchSummaryAddendumTests(unittest.TestCase):
    """Slice 8: addendum block appended after each topic's existing entry."""

    def test_topic_with_final_renders_chosen_hook_line(self) -> None:
        """A topic past gate 2 must produce ``**Chosen hook:**`` in its section."""
        with _ChannelRoot() as channel_root:
            topic_id = "2026-05-12_002"
            _seed_topic_with_final(channel_root, topic_id)

            result = _make_topic_result(topic_id, topic="Anthropic Claude flattery study")
            summary_path = _write_batch_summary(channel_root, picks=[], results=[result])

            text = summary_path.read_text(encoding="utf-8")
            self.assertIn(f"## {topic_id} — Anthropic Claude flattery study", text)
            self.assertIn("**Chosen hook:** A - Specific-Number Promise", text)
            # Existing fields still rendered above the addendum.
            self.assertIn("- **Status:** completed", text)
            # The addendum should appear AFTER the message backtick block.
            chosen_idx = text.index("**Chosen hook:**")
            backtick_idx = text.rindex("```", 0, chosen_idx)
            self.assertGreater(
                chosen_idx, backtick_idx,
                "addendum must appear after the message backtick block, not before",
            )

    def test_topic_at_gate_2_renders_awaiting_placeholder(self) -> None:
        """A topic with no script_FINAL.txt renders the gate-2 placeholder."""
        with _ChannelRoot() as channel_root:
            topic_id = "2026-05-12_003"
            _seed_topic_at_gate_2(channel_root, topic_id)

            result = _make_topic_result(
                topic_id,
                status="halted_manual_llm",
                halt_stage="script_generation",
                halt_message="awaiting LLM response file",
            )
            summary_path = _write_batch_summary(channel_root, picks=[], results=[result])

            text = summary_path.read_text(encoding="utf-8")
            self.assertIn("(awaiting gate 2 selection)", text)
            # The pre-existing per-topic block must still be rendered.
            self.assertIn(f"## {topic_id}", text)
            self.assertIn("- **Status:** halted_manual_llm", text)
            # No chosen-hook line since gate 2 hasn't been passed.
            self.assertNotIn("**Chosen hook:**", text)

    def test_addendum_failure_does_not_block_summary_write(self) -> None:
        """If format_hook_addendum raises, the summary still writes + WARNING logged."""
        with _ChannelRoot() as channel_root:
            topic_id = "2026-05-12_004"
            _seed_topic_with_final(channel_root, topic_id)

            result = _make_topic_result(topic_id)

            def _boom(topic_id: str, channel_root: Path) -> str:
                raise RuntimeError("synthetic addendum failure")

            with mock.patch.object(daily_batch, "format_hook_addendum", side_effect=_boom):
                with self.assertLogs("daily_batch", level="WARNING") as captured:
                    summary_path = _write_batch_summary(
                        channel_root, picks=[], results=[result],
                    )

            # Summary file exists and has the per-topic block.
            self.assertTrue(summary_path.exists(), "summary file must still be written")
            text = summary_path.read_text(encoding="utf-8")
            self.assertIn(f"## {topic_id}", text)
            self.assertIn("- **Status:** completed", text)
            # The chosen-hook line is absent because the addendum was suppressed.
            self.assertNotIn("**Chosen hook:**", text)
            # WARNING surfaced with the topic_id and the underlying error.
            self.assertTrue(
                any(
                    "hook addendum unavailable" in record.getMessage()
                    and topic_id in record.getMessage()
                    and "synthetic addendum failure" in record.getMessage()
                    for record in captured.records
                ),
                f"expected addendum-failure warning, got: "
                f"{[r.getMessage() for r in captured.records]}",
            )

    def test_two_topics_both_addendums_appear_in_order(self) -> None:
        """With two topics, both addendums render under their own sections,
        in the same order as the results list."""
        with _ChannelRoot() as channel_root:
            topic_id_a = "2026-05-12_010"
            topic_id_b = "2026-05-12_011"
            _seed_topic_with_final(channel_root, topic_id_a)
            _seed_topic_with_final(channel_root, topic_id_b)

            result_a = _make_topic_result(topic_id_a, topic="topic alpha")
            result_b = _make_topic_result(topic_id_b, topic="topic bravo")

            summary_path = _write_batch_summary(
                channel_root, picks=[], results=[result_a, result_b],
            )

            text = summary_path.read_text(encoding="utf-8")
            # Both sections present.
            self.assertIn(f"## {topic_id_a} — topic alpha", text)
            self.assertIn(f"## {topic_id_b} — topic bravo", text)
            # Both addendums present (count of "**Chosen hook:**" should be 2).
            self.assertEqual(
                text.count("**Chosen hook:**"), 2,
                "expected one chosen-hook line per topic",
            )
            # Order preserved: section A before section B; addendum A before
            # addendum B; the addendum for A must sit BETWEEN the A heading
            # and the B heading (not after both).
            idx_section_a = text.index(f"## {topic_id_a}")
            idx_section_b = text.index(f"## {topic_id_b}")
            self.assertLess(idx_section_a, idx_section_b)
            # First "**Chosen hook:**" lives in section A.
            first_chosen = text.index("**Chosen hook:**")
            self.assertGreater(first_chosen, idx_section_a)
            self.assertLess(first_chosen, idx_section_b)
            # Second "**Chosen hook:**" lives in section B.
            second_chosen = text.index("**Chosen hook:**", first_chosen + 1)
            self.assertGreater(second_chosen, idx_section_b)


class WriteBatchSummaryDroppedTopicTests(unittest.TestCase):
    """Sentinel `<dropped>` topic_id has no per-topic dir — must skip the
    addendum cleanly without invoking format_hook_addendum."""

    def test_dropped_pick_does_not_call_addendum(self) -> None:
        with _ChannelRoot() as channel_root:
            result = _make_topic_result(
                topic_id="<dropped>",
                topic="duplicate pick",
                status="dropped_already_uploaded",
                halt_stage="picks_allocation",
                halt_message="already uploaded; pick dropped",
            )

            with mock.patch.object(daily_batch, "format_hook_addendum") as mock_addendum:
                summary_path = _write_batch_summary(
                    channel_root, picks=[], results=[result],
                )

            mock_addendum.assert_not_called()
            text = summary_path.read_text(encoding="utf-8")
            self.assertIn("## <dropped> — duplicate pick", text)
            self.assertIn("- **Status:** dropped_already_uploaded", text)
            self.assertNotIn("**Chosen hook:**", text)
            self.assertNotIn("(awaiting gate 2 selection)", text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
