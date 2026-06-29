"""Unit tests for tools.longform_captions.render_lower_third_ass (no whisper dep)."""
from tools.caption_word_pop import Line, Word
from tools.longform_captions import render_lower_third_ass


def _line(triples):
    return Line(words=tuple(Word(start=s, end=e, text=t) for s, e, t in triples))


def test_render_lower_third_ass_basic(tmp_path):
    lines = [
        _line([(0.0, 0.5, "Last"), (0.5, 1.0, "month"), (1.0, 1.5, "an"), (1.5, 2.0, "AI")]),
        _line([(2.0, 2.5, "agent"), (2.5, 3.0, "did"), (3.0, 3.6, "something")]),
    ]
    out = tmp_path / "cap.ass"
    render_lower_third_ass(lines, out, video_width=1920, video_height=1080)
    text = out.read_text(encoding="utf-8")
    assert "PlayResX: 1920" in text
    assert "PlayResY: 1080" in text
    assert "Style: LF,Arial,54" in text
    assert "Alignment" in text  # format line present
    assert text.count("Dialogue:") == 2  # one static line per group (no per-word events)
    assert "Last month an AI" in text
    assert "agent did something" in text


def test_render_lower_third_skips_empty(tmp_path):
    lines = [_line([(0.0, 0.5, "")]), _line([(1.0, 1.5, "hello")])]
    out = tmp_path / "c.ass"
    render_lower_third_ass(lines, out)
    assert out.read_text(encoding="utf-8").count("Dialogue:") == 1


def test_render_lower_third_custom_geometry(tmp_path):
    out = tmp_path / "g.ass"
    render_lower_third_ass([_line([(0.0, 1.0, "hi")])], out,
                           video_width=1280, video_height=720, font_size=40, margin_v=60)
    text = out.read_text(encoding="utf-8")
    assert "PlayResX: 1280" in text
    assert "Style: LF,Arial,40" in text
