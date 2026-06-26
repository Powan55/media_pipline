"""Unit tests for PU-7b check (#15): script_FINAL.txt sourcing-hygiene scan.

Like the #14 template-artifact tests, these are pure-Python regex work via
tools.prepublish_qa and DO NOT depend on ffmpeg.

Covers the three core cases from the PU-7b brief plus guard rails:
  - A FINAL with an open `[VERIFY:]` tag FAILs (residual_verify).
  - A FINAL with an anonymous "a Reddit user" citation FAILs
    (anonymous_citation).
  - A clean FINAL whose citation line carries a NAMED handle (`u/foo` / `@bar`)
    PASSes — the named source suppresses the anonymous-citation finding.
  - Bare `[VERIFY]` (no colon) FAILs.
  - Missing / wrong-suffix / binary inputs FAIL gracefully.

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_prepublish_qa_check_sourcing.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.prepublish_qa import (  # noqa: E402
    SCRIPT_SOURCING_CHECK_ID,
    SCRIPT_SOURCING_CHECK_NAME,
    CheckResult,
    check_script_sourcing,
)


# ---------------------------------------------------------------------------
# Sanity: the check ID / name are pinned
# ---------------------------------------------------------------------------


def test_check_id_is_15() -> None:
    """#14 was the template-artifact scan; the sourcing scan takes the next
    free integer, #15. Pin it so a renumber breaks loudly.
    """
    assert SCRIPT_SOURCING_CHECK_ID == 15
    assert SCRIPT_SOURCING_CHECK_NAME == "script_sourcing_hygiene"


# Reusable clean body that already satisfies cited-observation (named handle).
_CLEAN_NAMED_BODY = (
    "AI just got weirder, and you should care. Anthropic shipped a feature that "
    "lets Claude remember details between chats. As u/lreeves put it on r/ClaudeAI, "
    "it quietly changes how you work day to day. Honestly, it feels like the start "
    "of something bigger. Would you trust it with your inbox? Be honest."
)


# ---------------------------------------------------------------------------
# Core case 1 — open [VERIFY:] tag FAILs
# ---------------------------------------------------------------------------


def test_open_verify_tag_fails(tmp_path: Path) -> None:
    """A residual `[VERIFY: confirm version]` tag in a FINAL must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "Claude 3.7 [VERIFY: confirm version] just dropped and it is fast. "
        "As u/lreeves noted on r/ClaudeAI, the latency halved overnight.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert isinstance(result, CheckResult)
    assert result.ok is False
    assert result.check_id == SCRIPT_SOURCING_CHECK_ID
    assert "verify" in result.message.lower()
    assert "residual" in result.actual.lower() or "residual" in result.message.lower()


def test_bare_verify_tag_fails(tmp_path: Path) -> None:
    """A bare `[VERIFY]` with no colon must also FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "The model scored 92 percent [VERIFY] on the new benchmark. "
        "Source: @AnthropicAI on launch day.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False
    assert "verify" in result.message.lower()


# ---------------------------------------------------------------------------
# Core case 2 — anonymous citation FAILs
# ---------------------------------------------------------------------------


def test_anonymous_reddit_user_fails(tmp_path: Path) -> None:
    """An anonymous 'a Reddit user' citation (no handle) must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "AI agents are getting scary good. A Reddit user said it booked their "
        "entire vacation in one prompt. That is wild.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False
    assert "anonymous" in result.actual.lower() or "anonymous" in result.message.lower()


def test_anonymous_user_on_subreddit_fails(tmp_path: Path) -> None:
    """'a user on r/...' with no named handle must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "A user on r/OpenAI claimed the new voice mode feels human. "
        "Honestly, it is hard to argue.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False


# ---------------------------------------------------------------------------
# Core case 3 — clean FINAL with a NAMED handle PASSes
# ---------------------------------------------------------------------------


def test_named_handle_citation_passes(tmp_path: Path) -> None:
    """A clean FINAL whose citation line names a handle (u/foo, @bar) PASSes —
    the named source suppresses any anonymous-citation finding.
    """
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_CLEAN_NAMED_BODY, encoding="utf-8")
    result = check_script_sourcing(p)
    assert result.ok is True
    assert result.check_id == SCRIPT_SOURCING_CHECK_ID
    assert result.severity == "PASS"


def test_named_handle_on_same_line_suppresses_anon(tmp_path: Path) -> None:
    """An otherwise-anonymous shape PASSes when a named handle co-occurs on the
    SAME line — e.g. 'a Reddit user, u/lreeves, said ...'.
    """
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "A Reddit user, u/lreeves, said the agent booked their whole trip. "
        "It is the kind of thing that used to take an afternoon.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is True


def test_at_handle_citation_passes(tmp_path: Path) -> None:
    """An @handle named source on the citation line PASSes."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "OpenAI just shipped memory for everyone. @sama posted that it rolls out "
        "this week. Small change, big difference.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is True


# ---------------------------------------------------------------------------
# PU-4 (2026-06-19 review) — broadened anonymous-citation coverage
#
# The PU-7b baseline above caught "a Reddit user", "a user on r/…", and the
# "<role> + verb" shapes. PU-4 extends #15 to also trip on a bare "a developer",
# "a user on <surface>" beyond r/…, a leading "Researchers/scientists <verb>",
# and "someone <verb>" — while NOT tripping on a legitimately NAMED source that
# happens to use a role word (the name-adjacency guard).
# ---------------------------------------------------------------------------


def test_pu4_bare_a_developer_fails(tmp_path: Path) -> None:
    """A bare 'a developer' as the sole attribution (no name) must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "AI coding tools are exploding. A developer rebuilt their entire SaaS in "
        "a weekend using Claude. It is getting hard to keep up.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False
    assert "anonymous" in result.actual.lower() or "anonymous" in result.message.lower()


def test_pu4_user_on_reddit_surface_fails(tmp_path: Path) -> None:
    """'a user on Reddit' (surface beyond r/<sub>) must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "A user on Reddit claimed the new model writes better than they do. "
        "Bold, but maybe not wrong.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False


def test_pu4_researchers_found_fails(tmp_path: Path) -> None:
    """Anonymous 'Researchers found ...' with no named lab/person must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "Here is the scary part. Researchers found the model could clone a voice "
        "from three seconds of audio. Three seconds.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False


def test_pu4_someone_built_fails(tmp_path: Path) -> None:
    """'Someone built ...' as the sole attribution must FAIL."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "This one is wild. Someone built an AI agent that negotiates their bills "
        "for them. And it actually works.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is False


def test_pu4_named_researcher_passes(tmp_path: Path) -> None:
    """A NAMED researcher must PASS — the role word adjacent to a proper name is
    a legitimately attributed source, not an anonymous one (precision guard).
    """
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "Anthropic researcher Tom Brown explained how the model learns from "
        "feedback. See the writeup at https://anthropic.com/research for the "
        "details. It is a genuinely new idea.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is True
    assert result.severity == "PASS"


def test_pu4_developer_named_passes(tmp_path: Path) -> None:
    """'a developer named Jane' is a NAMED source and must PASS."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(
        "A developer named Jane Park shipped the plugin overnight, and posted the "
        "whole build log at https://example.com/build. Worth a read.",
        encoding="utf-8",
    )
    result = check_script_sourcing(p)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Failure-mode guard rails (mirror the #14 test style)
# ---------------------------------------------------------------------------


def test_missing_file_fails(tmp_path: Path) -> None:
    """A non-existent path FAILs with a clear message rather than passing."""
    result = check_script_sourcing(tmp_path / "nope.txt")
    assert result.ok is False
    assert "not found" in result.message.lower() or "missing" in result.actual.lower()


def test_mp4_path_rejected_up_front(tmp_path: Path) -> None:
    """An .mp4 misrouted as a script path FAILs on the suffix guard."""
    fake = tmp_path / "video.mp4"
    fake.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = check_script_sourcing(fake)
    assert result.ok is False
    assert ".mp4" in result.actual or "wrong file type" in result.actual.lower()


def test_null_bytes_fail(tmp_path: Path) -> None:
    """A .txt carrying null bytes is treated as binary and FAILs."""
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b"some text\x00\x00 [VERIFY: x] more")
    result = check_script_sourcing(p)
    assert result.ok is False
    assert "null" in result.message.lower() or "binary" in result.message.lower()


def test_empty_file_fails(tmp_path: Path) -> None:
    """An empty FINAL is a pipeline error, not 'clean'."""
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b"")
    result = check_script_sourcing(p)
    assert result.ok is False
    assert "empty" in result.message.lower()
