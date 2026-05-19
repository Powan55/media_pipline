"""Tests for :mod:`tools.caption_artifact_check` (Sprint 5 Layer 3).

Regression net for the _12_002 failure: edge-TTS spoke ``SCRIPT_BODY (uses
HOOK_A as the verbal opener):`` aloud and the word-pop transcriber turned the
audio back into a Dialogue line. Layer 3 must catch any future repeat in the
``.ass`` file before the render step consumes it.

Coverage:
    1. Synthetic Dialogue line carrying a template artifact (with libass tags)
       -> FAIL.
    2. Clean .ass with realistic libass tags but innocent word text -> PASS.
    3. Dialogue line that's ONLY libass tags, no rendered text -> PASS.
    4. Pure libass tag ``{\\rPop}`` does NOT false-positive on the
       ``{FOO}`` placeholder regex (the key correctness test).
    5. Missing file -> FileNotFoundError.
    6. The exact _12_002 failure phrase wrapped in a Dialogue line -> FAIL.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable regardless of the cwd the test runner uses.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.caption_artifact_check import (  # noqa: E402
    CHECK_ID,
    CHECK_NAME,
    _extract_dialogue_texts,
    _strip_libass_tags,
    check_captions_for_artifacts,
)
from tools.prepublish_qa import CheckResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Montserrat Black,84,&H00FFFFFF,&H000000FF,&H00000000,"
    "&H4D000000,-1,0,0,0,100,100,2,0,1,6,4,2,60,60,540,1\n"
    "Style: Pop,Montserrat Black,96,&H0000E6FF,&H000000FF,&H00000000,"
    "&H4D000000,-1,0,0,0,100,100,2,0,1,6,4,2,60,60,540,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\n"
)


def _dialogue(text: str, *, start: str = "0:00:00.10", end: str = "0:00:00.40") -> str:
    """Build one ASS Dialogue line with the canonical Word-Pop field layout."""
    return f"Dialogue: 0,{start},{end},Default,,0,0,540,,{text}\n"


def _write_ass(tmp_path: Path, body_lines: list[str], name: str = "cap.ass") -> Path:
    """Write a full ``.ass`` file (header + caller-supplied Dialogue lines)."""
    out = tmp_path / name
    out.write_text(_ASS_HEADER + "".join(body_lines), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLibassTagStripping(unittest.TestCase):
    """Regex correctness — the heart of Layer 3."""

    def test_strips_simple_pop_tag(self) -> None:
        self.assertEqual(_strip_libass_tags(r"{\rPop}Claude"), "Claude")

    def test_strips_complex_tween_tag(self) -> None:
        cleaned = _strip_libass_tags(
            r"{\rPop\t(0,80,1,\fscx140\fscy140)\t(80,200,1,\fscx100\fscy100)}AI"
        )
        self.assertEqual(cleaned, "AI")

    def test_strips_fad_tag(self) -> None:
        self.assertEqual(_strip_libass_tags(r"{\fad(40,0)}word"), "word")

    def test_strips_multiple_tags(self) -> None:
        cleaned = _strip_libass_tags(
            r"{\fad(40,0)}{\rPop\t(0,80,\fscx125)}word{\r}"
        )
        self.assertEqual(cleaned, "word")

    def test_strips_empty_block(self) -> None:
        self.assertEqual(_strip_libass_tags("hello{}world"), "helloworld")

    def test_preserves_template_placeholder(self) -> None:
        """The single most important correctness property."""
        # First char inside ``{}`` is 'S', not '\\', so the tag-stripper must
        # leave it untouched for `scan_for_artifacts` to catch.
        self.assertEqual(
            _strip_libass_tags("{SCRIPT_BODY} and {HOOK_A}"),
            "{SCRIPT_BODY} and {HOOK_A}",
        )

    def test_strips_libass_but_keeps_placeholder_in_mixed_text(self) -> None:
        cleaned = _strip_libass_tags(r"{\rPop}{SCRIPT_BODY}{\r}")
        self.assertEqual(cleaned, "{SCRIPT_BODY}")


class TestDialogueExtraction(unittest.TestCase):
    """ASS Dialogue line parsing — split(',', 9) preserves embedded commas."""

    def test_extracts_simple_text(self) -> None:
        line = _dialogue("Claude")
        texts = _extract_dialogue_texts(line)
        self.assertEqual(texts, ["Claude"])

    def test_extracts_text_with_libass_tags(self) -> None:
        line = _dialogue(r"{\rPop\t(0,80,\fscx125)}word{\r}")
        texts = _extract_dialogue_texts(line)
        self.assertEqual(texts, [r"{\rPop\t(0,80,\fscx125)}word{\r}"])

    def test_extracts_text_with_embedded_commas(self) -> None:
        """Text field may contain commas — split(',', 9) must preserve them."""
        line = _dialogue(r"{\t(0,80,\fscx125)}hello, world")
        texts = _extract_dialogue_texts(line)
        self.assertEqual(texts, [r"{\t(0,80,\fscx125)}hello, world"])

    def test_skips_non_dialogue_lines(self) -> None:
        ass = _ASS_HEADER + _dialogue("Claude") + "Comment: header noise\n"
        texts = _extract_dialogue_texts(ass)
        self.assertEqual(texts, ["Claude"])

    def test_skips_malformed_dialogue(self) -> None:
        # Only 3 commas — libass would skip it, so do we.
        bad = "Dialogue: 0,0:00:00.10,0:00:00.40\n"
        self.assertEqual(_extract_dialogue_texts(bad), [])


class TestCheckCaptionsForArtifacts(unittest.TestCase):
    """End-to-end check_captions_for_artifacts behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -----------------------------------------------------------------
    # FAIL cases
    # -----------------------------------------------------------------

    def test_dialogue_with_template_placeholder_fails(self) -> None:
        """Case 1: realistic libass-wrapped Dialogue line carrying ``{SCRIPT_BODY}``."""
        body = [
            _dialogue(r"{\rPop\t(0,80,\fscx125)}{SCRIPT_BODY}{\r}"),
            _dialogue("clean word"),
        ]
        ass = _write_ass(self.tmp_path, body)
        result = check_captions_for_artifacts(ass)
        self.assertIsInstance(result, CheckResult)
        self.assertFalse(result.ok, msg=f"expected FAIL, got: {result}")
        self.assertEqual(result.severity, "FAIL")
        self.assertEqual(result.check_id, CHECK_ID)
        self.assertEqual(result.name, CHECK_NAME)
        self.assertIn("SCRIPT_BODY", result.message)

    def test_12_002_regression_phrase_fails(self) -> None:
        """Case 6: the exact _12_002 failure phrase as a Dialogue line -> FAIL.

        Simulates what the word-pop transcriber would have produced if it had
        heard edge-TTS speak the literal annotation header.
        """
        # The transcriber emits one Dialogue event per word; spread the phrase
        # across realistic per-word events with libass tags to match what
        # caption_word_pop.render_ass actually writes.
        failure_phrase_words = [
            "SCRIPT_BODY", "uses", "HOOK_A", "as", "the", "verbal", "opener",
        ]
        body = [
            _dialogue(
                r"{\rPop\t(0,80,\fscx125\fscy125)\t(80,140,\fscx100\fscy100)}"
                f"{w}" + r"{\r}",
                start=f"0:00:00.{10 + i * 10:02d}",
                end=f"0:00:00.{30 + i * 10:02d}",
            )
            for i, w in enumerate(failure_phrase_words)
        ]
        ass = _write_ass(self.tmp_path, body, name="_12_002_captions.ass")
        result = check_captions_for_artifacts(ass)
        self.assertFalse(result.ok, msg=f"expected FAIL for _12_002 regression, got: {result}")
        # Both internal-name detectors should fire: script_body AND hook_a.
        self.assertIn("script_body", result.message.lower())
        # hook_a may show up either as the underscore form or as the
        # template_placeholder (uppercase) — accept either.
        self.assertTrue(
            "hook_a" in result.message.lower() or "HOOK_A" in result.message,
            msg=f"expected hook_a or HOOK_A in failure: {result.message}",
        )

    # -----------------------------------------------------------------
    # PASS cases
    # -----------------------------------------------------------------

    def test_clean_ass_passes(self) -> None:
        """Case 2: realistic word-pop output with innocent word text."""
        body = [
            _dialogue(
                r"{\rPop\t(0,80,\fscx125\fscy125)\t(80,140,\fscx100\fscy100)}"
                + word + r"{\r}",
                start=f"0:00:00.{10 + i * 10:02d}",
                end=f"0:00:00.{30 + i * 10:02d}",
            )
            for i, word in enumerate(["Claude", "is", "the", "best", "AI"])
        ]
        ass = _write_ass(self.tmp_path, body)
        result = check_captions_for_artifacts(ass)
        self.assertTrue(result.ok, msg=f"expected PASS, got: {result}")
        self.assertEqual(result.severity, "PASS")
        self.assertEqual(result.check_id, CHECK_ID)

    def test_libass_tags_only_dialogue_passes(self) -> None:
        """Case 3: a Dialogue line that's ONLY libass decoration -> PASS."""
        body = [
            _dialogue(r"{\rPop\t(0,80,1)}{\fad(100,100)}"),
            _dialogue(r"{\rPop}{\r}"),
        ]
        ass = _write_ass(self.tmp_path, body)
        result = check_captions_for_artifacts(ass)
        self.assertTrue(result.ok, msg=f"expected PASS for tag-only dialogue, got: {result}")

    def test_legitimate_acronyms_do_not_false_positive(self) -> None:
        """word-pop emits one Dialogue event per word. Acronyms like ``AI``,
        ``API``, ``TTS`` are legitimate content. If Layer 3 joined events with
        newlines instead of spaces, the all-caps-line stage-instruction regex
        (``^\\s*[A-Z][A-Z_]+\\s*:?\\s*$``) would fire on each acronym word —
        false-positive storm. Confirm the space-join strategy holds.
        """
        body = [
            _dialogue(f"{{\\rPop}}{w}{{\\r}}")
            for w in ["AI", "API", "TTS", "LLM", "GPU"]
        ]
        ass = _write_ass(self.tmp_path, body)
        result = check_captions_for_artifacts(ass)
        self.assertTrue(
            result.ok,
            msg=(
                "single-word acronyms must not trigger the all-caps-line "
                f"stage_instruction regex; got: {result.message}"
            ),
        )

    def test_pop_tag_does_not_false_positive_on_placeholder_regex(self) -> None:
        """Case 4: the KEY correctness test.

        ``{\\rPop}`` is a libass tag. The template-placeholder regex matches
        ``{FOO}`` (ALL-CAPS-underscore name inside braces). If the tag-strip
        is omitted, the placeholder pattern might fire on ``{\\rPop}`` etc.
        Confirm it does NOT.
        """
        # The most-used libass tags in caption_word_pop.render_ass output:
        tag_only_lines = [
            r"{\rPop}",
            r"{\r}",
            r"{\fad(40,0)}",
            r"{\t(0,80,\fscx125)}",
            # Even ALL-CAPS-looking tag names would be safe since they start
            # with a backslash:
            r"{\BLAH}",
        ]
        body = [_dialogue(t) for t in tag_only_lines]
        ass = _write_ass(self.tmp_path, body)
        result = check_captions_for_artifacts(ass)
        self.assertTrue(
            result.ok,
            msg=(
                "libass tags must NOT false-positive on {FOO} placeholder "
                f"regex; got: {result.message}"
            ),
        )

    # -----------------------------------------------------------------
    # Error path
    # -----------------------------------------------------------------

    def test_missing_file_raises_filenotfounderror(self) -> None:
        """Case 5: missing .ass file is a real bug, not a soft FAIL."""
        with self.assertRaises(FileNotFoundError):
            check_captions_for_artifacts(self.tmp_path / "does_not_exist.ass")


class TestPipelineQAFailedCompatibility(unittest.TestCase):
    """Layer 3's CheckResult must be drop-in compatible with PipelineQAFailed."""

    def test_fail_result_serializes_into_failures_dict(self) -> None:
        from tools.prepublish_qa import PipelineQAFailed

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ass = _write_ass(tmp, [_dialogue("{SCRIPT_BODY}")])
            result = check_captions_for_artifacts(ass)
            self.assertFalse(result.ok)

            # Reproduce the call shape the pipeline wiring uses.
            failures = {
                result.check_id: {
                    "name": result.name,
                    "expected": result.expected,
                    "actual": result.actual,
                    "message": result.message,
                }
            }
            exc = PipelineQAFailed(failures=failures, video_path=ass)
            self.assertIn("caption_template_artifacts", str(exc))
            self.assertIn("SCRIPT_BODY", str(exc))


if __name__ == "__main__":
    unittest.main()
