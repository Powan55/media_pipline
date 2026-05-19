"""Unit tests for tools/caption_word_pop.py.

Uses stdlib ``unittest`` to avoid adding pytest as a dependency. Run via:
    python -m unittest tests.test_caption_word_pop -v

Structural tests (``pack_into_lines``, ``render_ass`` output, color presence,
bounce on/off) run pure-Python without ``faster-whisper``. The transcription
smoke test is auto-skipped when ``faster_whisper`` isn't importable, the
``large-v3`` model is not in the local cache, or the sample VO is missing.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable so `from tools.caption_word_pop import ...`
# works regardless of the cwd the test runner uses.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.caption_word_pop import (  # noqa: E402
    Line,
    Word,
    _format_ass_time,
    pack_into_lines,
    render_ass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_words(
    count: int,
    *,
    step: float = 0.3,
    dur: float = 0.25,
    base_text: str = "w",
) -> list[Word]:
    """Build a list of ``Word`` fixtures with regular spacing.

    ``step`` is the inter-word interval (start-to-start); ``dur`` is each
    word's spoken duration. Default keeps inter-word gap small enough that
    the long-gap rule never trips unless explicitly engineered.
    """
    out: list[Word] = []
    t = 0.0
    for i in range(count):
        out.append(Word(start=t, end=t + dur, text=f"{base_text}{i}"))
        t += step
    return out


def _two_line_corpus() -> list[Line]:
    """A 6-word, two-line corpus: 'AI just changed everything in 2026'."""
    words = [
        Word(0.00, 0.30, "AI"),
        Word(0.30, 0.55, "just"),
        Word(0.55, 0.90, "changed"),
        Word(0.90, 1.40, "everything"),
        Word(1.40, 1.70, "in"),
        Word(1.70, 2.00, "2026"),
    ]
    return [Line(words=tuple(words[:4])), Line(words=tuple(words[4:]))]


# ---------------------------------------------------------------------------
# pack_into_lines tests (pure Python — no whisper)
# ---------------------------------------------------------------------------


class PackIntoLinesTests(unittest.TestCase):
    """Cover the four packing rules + empty-input edge case."""

    def test_pack_respects_max_words(self) -> None:
        """20 words at tight spacing, max_words=4 -> >= 5 lines, none > 4 words."""
        words = _fake_words(20, step=0.3, dur=0.2)
        lines = pack_into_lines(
            words,
            max_words_per_line=4,
            max_line_duration_s=999.0,
            max_chars_per_line=999,
            max_inter_word_gap_s=999.0,
        )
        self.assertGreaterEqual(len(lines), 5)
        self.assertTrue(all(len(line.words) <= 4 for line in lines))
        # Exactly 20 words preserved (no drops, no dupes).
        self.assertEqual(sum(len(line.words) for line in lines), 20)

    def test_pack_breaks_on_long_gap(self) -> None:
        """A 1.3s silence between two word clusters must force a line break."""
        first = [
            Word(start=0.0, end=0.2, text="a"),
            Word(start=0.5, end=0.7, text="b"),
            Word(start=1.0, end=1.2, text="c"),
        ]
        # Gap: previous word ends 1.2, next word starts 2.5 -> 1.3s silence.
        second = [
            Word(start=2.5, end=2.7, text="d"),
            Word(start=3.0, end=3.2, text="e"),
            Word(start=3.5, end=3.7, text="f"),
        ]
        words = first + second
        lines = pack_into_lines(
            words,
            max_words_per_line=99,
            max_line_duration_s=99.0,
            max_chars_per_line=999,
            max_inter_word_gap_s=0.6,
        )
        # No line should contain both 'c' and 'd' -- the gap must split them.
        for line in lines:
            texts = {w.text for w in line.words}
            self.assertFalse(
                {"c", "d"}.issubset(texts),
                msg=f"Line straddles silent gap: {texts}",
            )

    def test_pack_respects_max_chars(self) -> None:
        """Char-count rule: never produce a line whose rendered chars > limit."""
        words = [
            Word(start=i * 0.3, end=i * 0.3 + 0.2, text="abcdef") for i in range(10)
        ]
        lines = pack_into_lines(
            words,
            max_words_per_line=99,
            max_line_duration_s=99.0,
            max_chars_per_line=20,
            max_inter_word_gap_s=99.0,
        )
        for line in lines:
            rendered = " ".join(w.text for w in line.words)
            self.assertLessEqual(len(rendered), 20, msg=f"Line too wide: {rendered!r}")

    def test_pack_respects_max_duration(self) -> None:
        """Duration rule: never produce a line whose total span > limit."""
        # Each word covers 0.4s with 0.2s inter-word silence (gap < 0.6 default).
        words = _fake_words(20, step=0.6, dur=0.4)
        lines = pack_into_lines(
            words,
            max_words_per_line=99,
            max_line_duration_s=2.5,
            max_chars_per_line=999,
            max_inter_word_gap_s=99.0,
        )
        for line in lines:
            if len(line.words) > 1:
                self.assertLessEqual(
                    line.end - line.start,
                    2.5 + 1e-6,
                    msg=f"Line duration {line.end - line.start:.3f}s exceeds 2.5s",
                )

    def test_pack_empty_input(self) -> None:
        """Empty -> empty. No exceptions, no fake events."""
        self.assertEqual(pack_into_lines([]), [])

    def test_pack_single_word(self) -> None:
        """Single-word audio (very short VO) packs cleanly into one line."""
        words = [Word(start=0.0, end=0.4, text="Hi")]
        lines = pack_into_lines(words)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].words[0].text, "Hi")


# ---------------------------------------------------------------------------
# render_ass structural tests (pure Python — no whisper)
# ---------------------------------------------------------------------------


class RenderAssTests(unittest.TestCase):
    """Validate the libass-renderable ASS output shape and content."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_render_ass_structure(self) -> None:
        """Required ASS sections must all be present and non-empty."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out)
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")

        for section in ("[Script Info]", "[V4+ Styles]", "[Events]"):
            self.assertIn(section, text, msg=f"Missing section: {section}")

        # Style block must declare both Default (white) and Pop (yellow).
        self.assertIn("Style: Default,", text)
        self.assertIn("Style: Pop,", text)

        # One Dialogue per (line, word) pairing -> 6 dialogues for 6 words.
        dialogue_lines = [ln for ln in text.splitlines() if ln.startswith("Dialogue:")]
        self.assertEqual(
            len(dialogue_lines),
            6,
            msg=f"Expected 6 dialogue events for 6 words, got {len(dialogue_lines)}",
        )

        # PlayRes matches the 1080x1920 default for Shorts.
        self.assertIn("PlayResX: 1080", text)
        self.assertIn("PlayResY: 1920", text)

    def test_render_ass_font_fallback_chain_in_style(self) -> None:
        """The font fallback chain must be comma-joined into the Style fontname."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out)
        text = out.read_text(encoding="utf-8")
        expected_chain = "Montserrat Black,Anton,Bebas Neue,Impact,Arial Black"
        self.assertIn(
            expected_chain, text, msg="Fallback chain not present in Style line"
        )

    def test_render_ass_active_color_yellow(self) -> None:
        """Active-word yellow color hex (BGR) must appear in the output."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out)
        text = out.read_text(encoding="utf-8")
        # &H0000E6FF is yellow #FFE600 in ASS BGR.
        self.assertIn("&H0000E6FF", text, msg="Active color (yellow) not found")
        # And the inactive white must also be present.
        self.assertIn("&H00FFFFFF", text, msg="Inactive color (white) not found")

    def test_render_ass_bounce_includes_pop_attack(self) -> None:
        """With bounce on, both the scale-attack and tween tag must appear."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out, enable_bounce=True)
        text = out.read_text(encoding="utf-8")
        # The exact peak percentage depends on font_size_active_peak / inactive
        # (110/84 ~= 131); both 125 and 131 are acceptable canonical values.
        self.assertTrue(
            "\\fscx131" in text or "\\fscx125" in text,
            msg="Peak scale tag missing (expected \\fscx125 or \\fscx131)",
        )
        self.assertIn("\\t(0,", text, msg="Animation tween tag missing")

    def test_render_ass_no_bounce_omits_scale_tween(self) -> None:
        """With bounce off, no scale tween should appear in output."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out, enable_bounce=False)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn(
            "\\fscx125", text, msg="125% peak leaked into --no-bounce render"
        )
        self.assertNotIn(
            "\\fscx131", text, msg="131% peak leaked into --no-bounce render"
        )
        self.assertNotIn(
            "\\t(0,", text, msg="Animation tween tag leaked into --no-bounce render"
        )

    def test_render_ass_active_word_markup_per_event(self) -> None:
        """Each event marks exactly one word as active (one \\rPop tag)."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out)
        text = out.read_text(encoding="utf-8")
        dialogue_lines = [ln for ln in text.splitlines() if ln.startswith("Dialogue:")]
        for ln in dialogue_lines:
            # Exactly one \rPop per Dialogue event = exactly one active word.
            self.assertEqual(
                ln.count("\\rPop"),
                1,
                msg=f"Expected exactly one \\rPop tag per event: {ln!r}",
            )

    def test_render_ass_dialogue_timing_matches_word(self) -> None:
        """First Dialogue event timing must span the first active word's interval."""
        out = self.tmp_path / "captions.ass"
        render_ass(_two_line_corpus(), out)
        text = out.read_text(encoding="utf-8")
        dialogue = [ln for ln in text.splitlines() if ln.startswith("Dialogue:")]
        self.assertIn(
            "0:00:00.00,0:00:00.30",
            dialogue[0],
            msg=f"First dialogue timing wrong: {dialogue[0]!r}",
        )
        # Last event covers 1.70 -> 2.00.
        self.assertIn(
            "0:00:01.70,0:00:02.00",
            dialogue[-1],
            msg=f"Last dialogue timing wrong: {dialogue[-1]!r}",
        )

    def test_render_ass_empty_lines_writes_header_only(self) -> None:
        """Edge case: zero lines -> file still has [Events] but no Dialogue."""
        out = self.tmp_path / "empty.ass"
        render_ass([], out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("[Events]", text)
        self.assertNotIn("Dialogue:", text)

    def test_render_ass_single_word_corpus(self) -> None:
        """Edge case: a 1-word, 1-line corpus -> exactly one Dialogue event."""
        out = self.tmp_path / "single.ass"
        line = Line(words=(Word(0.0, 0.4, "Hi"),))
        render_ass([line], out)
        text = out.read_text(encoding="utf-8")
        dialogue_lines = [ln for ln in text.splitlines() if ln.startswith("Dialogue:")]
        self.assertEqual(len(dialogue_lines), 1)
        # Active word's text is in the event.
        self.assertIn("Hi", dialogue_lines[0])

    def test_render_ass_invalid_inactive_size_raises(self) -> None:
        """Error path: zero or negative ``font_size_inactive`` is invalid."""
        out = self.tmp_path / "x.ass"
        with self.assertRaises(ValueError):
            render_ass(_two_line_corpus(), out, font_size_inactive=0)

    def test_render_ass_creates_parent_dirs(self) -> None:
        """Idempotent: render_ass mkdirs its parent dir."""
        out = self.tmp_path / "deeply" / "nested" / "path" / "captions.ass"
        render_ass(_two_line_corpus(), out)
        self.assertTrue(out.exists())


# ---------------------------------------------------------------------------
# Time format helper
# ---------------------------------------------------------------------------


class FormatAssTimeTests(unittest.TestCase):
    """``_format_ass_time`` produces ``H:MM:SS.cc`` strings."""

    def test_zero(self) -> None:
        self.assertEqual(_format_ass_time(0), "0:00:00.00")

    def test_subsecond(self) -> None:
        self.assertEqual(_format_ass_time(1.23), "0:00:01.23")

    def test_minute_rollover(self) -> None:
        self.assertEqual(_format_ass_time(61.5), "0:01:01.50")

    def test_hour_rollover(self) -> None:
        self.assertEqual(_format_ass_time(3661.05), "1:01:01.05")

    def test_negative_clamps_to_zero(self) -> None:
        self.assertEqual(_format_ass_time(-0.5), "0:00:00.00")

    def test_centisecond_rounding_does_not_overflow(self) -> None:
        # 0.999 rounds to 100cs -> must roll the seconds field, not emit ".100".
        out = _format_ass_time(0.999)
        self.assertEqual(out, "0:00:01.00")


# ---------------------------------------------------------------------------
# Module API surface (for Stage 09 wiring in Wave 3)
# ---------------------------------------------------------------------------


class ModuleApiTests(unittest.TestCase):
    """Public callables + dataclasses must be importable for downstream wiring."""

    def test_module_api_exports(self) -> None:
        mod = importlib.import_module("tools.caption_word_pop")
        for name in ("transcribe_words", "pack_into_lines", "render_ass", "Word", "Line"):
            self.assertTrue(
                hasattr(mod, name), msg=f"Missing public export: {name}"
            )


# ---------------------------------------------------------------------------
# Transcription smoke test (skipped if faster_whisper or model missing)
# ---------------------------------------------------------------------------


_MODEL_CACHE_HINT = Path(r"C:\ContentOps\_models\whisper")
_VO_SAMPLE = Path(
    r"C:\ContentOps\channels\ShadowVerse\03_assets\audio_vo\2026-05-07_001\2026-05-07_001_vo.wav"
)


def _faster_whisper_available() -> bool:
    try:
        importlib.import_module("faster_whisper")
        return True
    except ImportError:
        return False


def _model_cached() -> bool:
    """Look for any large-v3-ish snapshot in the local hf cache layout."""
    if not _MODEL_CACHE_HINT.exists():
        return False
    return any(_MODEL_CACHE_HINT.rglob("model.bin"))


@unittest.skipUnless(_faster_whisper_available(), "faster_whisper not importable")
@unittest.skipUnless(_model_cached(), "large-v3 model not in local cache")
@unittest.skipUnless(_VO_SAMPLE.exists(), f"sample VO not found at {_VO_SAMPLE}")
class TranscribeWordsSmokeTests(unittest.TestCase):
    """End-to-end smoke against a real cached VO + cached model."""

    def test_transcribe_words_smoke(self) -> None:
        from tools.caption_word_pop import transcribe_words

        words = transcribe_words(
            _VO_SAMPLE,
            model_name="large-v3",
            compute_type="float16",
            language="en",
            download_root=_MODEL_CACHE_HINT,
        )
        self.assertGreater(len(words), 0)
        starts = [w.start for w in words]
        self.assertEqual(
            starts,
            sorted(starts),
            msg="word starts must be monotonically non-decreasing",
        )


if __name__ == "__main__":
    unittest.main()
