"""Unit tests for prepublish_qa check #16: script_FINAL.txt pre-render lint.

Pure-Python (no ffmpeg). Proves the gate catches the two real published defects
and that the banned-CTA list pulled from the style guide stays in sync, while
clean / approved-CTA scripts pass and misrouted inputs fail gracefully.

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_prepublish_qa_check_prerender.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.prepublish_qa import (  # noqa: E402
    SCRIPT_PRERENDER_CHECK_ID,
    SCRIPT_PRERENDER_CHECK_NAME,
    CheckResult,
    check_script_prerender,
)

# Faithful reproductions of the two shipped defects (the originals on disk are
# deliberately left untouched — this is a forward-looking guard).
DEFECT_08_001_FINAL = (
    "[B-ROLL: highlighted Reddit thread on a laptop screen, timestamps circled]\n"
    "A Reddit user on r/ClaudeAI [VERIFY: find a specific recent post with handle "
    "and URL — claim is that posters report shorter, lower-quality responses after "
    "hitting daily limits, with no banner or model-swap notification] posted "
    "screenshots showing their Claude answers got noticeably shorter after they hit "
    "their daily limit.\n"
    "[B-ROLL: side-by-side AI replies, the second one obviously shorter]\n"
    "Same prompt. Lesser model. No banner.\n"
)
DEFECT_11_002_FINAL = (
    "OpenAI just opened a consulting firm.\n"
    "[B-ROLL: OpenAI logo morphing into a briefcase]\n"
    "They announced it on their blog today, openai.com, on 2026-05-11.\n"
    "That is a chatbot company quietly becoming your workplace partner.\n"
    'Comment "deploy" and I will send you the link to the announcement.\n'
    "[B-ROLL: phone screen with a comment box, fingers typing the word deploy]\n"
)

# A clean FINAL: named source, approved CTA, no placeholder, no banned CTA.
CLEAN_FINAL = (
    "OpenAI just opened a consulting firm.\n"
    "[B-ROLL: OpenAI logo morphing into a briefcase]\n"
    "As @sama posted on openai.com on 2026-05-11, they install AI inside other "
    "businesses.\n"
    "That is a chatbot company quietly becoming your workplace partner.\n"
    "Tag the friend who needs to see this.\n"
)

_STYLE_GUIDE_FIXTURE = """\
## Forbidden patterns
- **Generic engagement-begging in the VO or overlay:** "smash that like button" / "hit that like button," "like and subscribe," "if you enjoyed this video..." Banned outright.

## CTA style
  **Save / share / follow (keep):**
  1. "Save this, share it with the AI-curious friend in your group chat."

  > **RETIRED (do not use):** "Comment [keyword] and I'll send you the link." Transactional comment-bait.
  > **RETIRED (do not use):** "Drop a like if this helped you." Engagement-beg.
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "script_FINAL.txt"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_check_id_is_16() -> None:
    assert SCRIPT_PRERENDER_CHECK_ID == 16
    assert SCRIPT_PRERENDER_CHECK_NAME == "script_prerender_lint"


# ---------------------------------------------------------------------------
# The two real defects FAIL (with style guide absent -> baseline still catches)
# ---------------------------------------------------------------------------


def test_defect_08_001_verify_in_vo_fails(tmp_path: Path) -> None:
    p = _write(tmp_path, DEFECT_08_001_FINAL)
    result = check_script_prerender(p, style_guide_path=tmp_path / "no_sg.md")
    assert isinstance(result, CheckResult)
    assert result.ok is False
    assert result.check_id == 16
    assert "placeholder" in result.actual.lower()
    assert "VERIFY" in result.message


def test_defect_11_002_comment_bait_fails(tmp_path: Path) -> None:
    p = _write(tmp_path, DEFECT_11_002_FINAL)
    result = check_script_prerender(p, style_guide_path=tmp_path / "no_sg.md")
    assert result.ok is False
    assert "banned cta" in result.actual.lower()
    # The offending VO line is surfaced in the message.
    assert "comment" in result.message.lower()


# ---------------------------------------------------------------------------
# Clean / approved scripts PASS
# ---------------------------------------------------------------------------


def test_clean_script_passes(tmp_path: Path) -> None:
    sg = tmp_path / "style_guide.md"
    sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
    p = _write(tmp_path, CLEAN_FINAL)
    result = check_script_prerender(p, style_guide_path=sg)
    assert result.ok is True
    assert result.severity == "PASS"


# ---------------------------------------------------------------------------
# "Stays in sync": a NEW retired phrase in the style guide is enforced
# ---------------------------------------------------------------------------


def test_style_guide_new_retired_phrase_is_enforced(tmp_path: Path) -> None:
    """`Drop a like if this helped you.` is not in the baseline — it must FAIL
    only because the style-guide parse picked it up (proves the sync path)."""
    sg = tmp_path / "style_guide.md"
    sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
    p = _write(tmp_path, "Great breakdown today. Drop a like if this helped you.\n")
    result = check_script_prerender(p, style_guide_path=sg)
    assert result.ok is False
    assert "banned cta" in result.actual.lower()


def test_baseline_enforced_when_style_guide_unreadable(tmp_path: Path) -> None:
    """Even with no style guide, the baseline catches the known comment-bait."""
    p = _write(tmp_path, DEFECT_11_002_FINAL)
    result = check_script_prerender(p, style_guide_path=tmp_path / "missing.md")
    assert result.ok is False


# ---------------------------------------------------------------------------
# Input guards (mirror #14 / #15)
# ---------------------------------------------------------------------------


def test_missing_file_fails(tmp_path: Path) -> None:
    result = check_script_prerender(tmp_path / "nope.txt")
    assert result.ok is False
    assert "not found" in result.message.lower() or "missing" in result.actual.lower()


def test_mp4_path_rejected_up_front(tmp_path: Path) -> None:
    fake = tmp_path / "video.mp4"
    fake.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = check_script_prerender(fake)
    assert result.ok is False
    assert ".mp4" in result.actual or "wrong file type" in result.actual.lower()


def test_null_bytes_fail(tmp_path: Path) -> None:
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b'some text\x00\x00 Comment "x" and I more')
    result = check_script_prerender(p)
    assert result.ok is False
    assert "null" in result.message.lower() or "binary" in result.message.lower()


def test_empty_file_fails(tmp_path: Path) -> None:
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b"")
    result = check_script_prerender(p)
    assert result.ok is False
    assert "empty" in result.message.lower()
