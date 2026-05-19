"""Unit tests for Sprint 5 Layer-2 check (#14): template-artifact scan of script_FINAL.txt.

These tests intentionally DO NOT depend on ffmpeg — the script-side check is
pure-Python regex work via tools.script_artifact_patterns, so they should
run on a minimal CI image too.

Covers:
  - Each artifact category (template_placeholder / internal_name /
    stage_instruction) FAILs with the line + matched-pattern surfaced.
  - A clean ~120-word general-audience AI script body PASSes.
  - Regression fixture mirroring `_12_002`'s flawed body FAILs and surfaces
    both `SCRIPT_BODY` and the `(uses HOOK_A ...)` stage annotation.
  - Missing file FAILs gracefully.
  - Binary-cast-as-script (.mp4 misrouted, null-byte payload, oversized
    file) FAILs without crashing.
  - check_topic_script() convenience wrapper resolves the canonical path.
  - The CLI exits 0 / 1 correctly under `--script`.

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_prepublish_qa_check_artifacts.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.prepublish_qa import (  # noqa: E402
    SCRIPT_ARTIFACT_CHECK_ID,
    SCRIPT_ARTIFACT_CHECK_NAME,
    CheckResult,
    check_script,
    check_topic_script,
)


# ---------------------------------------------------------------------------
# Sanity: the check ID matches what the brief documented
# ---------------------------------------------------------------------------


def test_check_id_is_14_documented_deviation_from_brief() -> None:
    """The Sprint 5 brief called this "#13" but #13 was already taken
    (cited-observation opt-in, commit 817bc13). We deliberately use the
    next free integer, #14, and document the rationale in the module
    docstring + the check function's docstring. This test pins that
    decision so an unintended renumber breaks loudly.
    """
    assert SCRIPT_ARTIFACT_CHECK_ID == 14
    assert SCRIPT_ARTIFACT_CHECK_NAME == "script_template_artifacts"


# ---------------------------------------------------------------------------
# Clean body PASS
# ---------------------------------------------------------------------------


_CLEAN_BODY = (
    "AI just got weirder, and you should care. OpenAI's new feature lets ChatGPT "
    "remember details between chats, which sounds tiny but changes how millions "
    "of people use it day to day. Think about it: you ask it once how you like "
    "your emails worded, and it just keeps doing it that way, forever. No more "
    "re-explaining yourself every Monday morning. Researchers at the University "
    "of Toronto flagged the same shift in a paper this week, calling it a quiet "
    "step toward what they describe as ambient assistants. We are not quite "
    "there yet, but if you have ever wished your computer remembered you a "
    "little better, here is the start of that, finally."
)


def test_clean_body_passes(tmp_path: Path) -> None:
    """A realistic ~120-word general-audience AI script body must PASS."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_CLEAN_BODY, encoding="utf-8")
    result = check_script(p)
    assert isinstance(result, CheckResult)
    assert result.ok is True
    assert result.check_id == SCRIPT_ARTIFACT_CHECK_ID
    assert result.severity == "PASS"


# ---------------------------------------------------------------------------
# Each artifact category FAILs
# ---------------------------------------------------------------------------


def test_template_placeholder_fails(tmp_path: Path) -> None:
    """An ALL-CAPS curly-brace placeholder triggers the template_placeholder
    category and the FAIL must name the line + the matched pattern.
    """
    text = "Welcome to the show.\nToday we cover {TOPIC_NAME} in plain English.\n"
    p = tmp_path / "script_FINAL.txt"
    p.write_text(text, encoding="utf-8")
    result = check_script(p)
    assert result.ok is False
    assert result.severity == "FAIL"
    # The line number (2) and the matched substring must appear in the message.
    assert "line 2" in result.message
    assert "{TOPIC_NAME}" in result.message
    assert "template_placeholder" in result.message


def test_internal_name_fails(tmp_path: Path) -> None:
    """``script_body`` (with underscore) anywhere in the body must FAIL via
    the internal_name category."""
    text = "Hello.\nThis is the script_body talking, allegedly.\nBye.\n"
    p = tmp_path / "script_FINAL.txt"
    p.write_text(text, encoding="utf-8")
    result = check_script(p)
    assert result.ok is False
    assert "line 2" in result.message
    assert "internal_name" in result.message
    assert "script_body" in result.message


def test_stage_instruction_fails(tmp_path: Path) -> None:
    """A literal markdown header (``## ...``) — a stage_instruction shape —
    must FAIL."""
    text = "## Section header that should not exist in a script body\nThe real script begins here.\n"
    p = tmp_path / "script_FINAL.txt"
    p.write_text(text, encoding="utf-8")
    result = check_script(p)
    assert result.ok is False
    assert "stage_instruction" in result.message


# ---------------------------------------------------------------------------
# The _12_002 regression fixture (the exact failure that motivated this check)
# ---------------------------------------------------------------------------


_12_002_BODY = (
    "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
    "\n"
    "AI just got weirder, and you should care. OpenAI's new memory feature lets "
    "ChatGPT remember details between chats, which sounds tiny but changes how "
    "millions of people use it day to day. Per https://openai.com/blog/memory "
    "dated 2026-05-12, the rollout is global. If you have ever wished your "
    "computer remembered you a little better, here is the start of that.\n"
)


def test_12_002_regression_body_fails_with_both_artifacts_surfaced(
    tmp_path: Path,
) -> None:
    """The exact phrase that shipped in `_12_002` (SCRIPT_BODY + (uses HOOK_A ...))
    must be detected, with BOTH the internal-name match AND the
    stage-instruction match surfaced in the FAIL message.

    This is the canonical regression: if this test ever passes silently,
    Sprint 5 Layer 2 has regressed.
    """
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_12_002_BODY, encoding="utf-8")
    result = check_script(p)
    assert result.ok is False
    msg = result.message

    # `SCRIPT_BODY` should be flagged via the internal_name regex.
    assert "internal_name" in msg, f"internal_name not in message: {msg}"
    assert "SCRIPT_BODY" in msg or "script_body" in msg.lower()

    # The `(uses HOOK_A as the verbal opener)` clause should be flagged via the
    # stage_instruction regex.
    assert "stage_instruction" in msg, f"stage_instruction not in message: {msg}"
    assert "HOOK_A" in msg

    # Confirm the actual field carries a useful count summary.
    assert "match" in result.actual.lower()


# ---------------------------------------------------------------------------
# Failure modes — missing file, wrong file type, binary contents
# ---------------------------------------------------------------------------


def test_missing_file_fails(tmp_path: Path) -> None:
    """A path that does not exist must FAIL with a clear message — NOT silently pass."""
    missing = tmp_path / "does_not_exist.txt"
    result = check_script(missing)
    assert result.ok is False
    assert result.severity == "FAIL"
    assert "not found" in result.message or "missing" in result.actual.lower()


def test_directory_passed_fails(tmp_path: Path) -> None:
    """Passing a directory must FAIL — not crash, not silently pass."""
    d = tmp_path / "some_directory"
    d.mkdir()
    result = check_script(d)
    assert result.ok is False
    assert "not a regular file" in result.message.lower() or "not a regular file" in result.actual.lower()


def test_mp4_path_rejected_up_front(tmp_path: Path) -> None:
    """An .mp4 misrouted as a script path must FAIL gracefully without
    actually scanning the binary contents.
    """
    fake_mp4 = tmp_path / "video.mp4"
    # We don't even need real mp4 bytes — the suffix check fires first.
    fake_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = check_script(fake_mp4)
    assert result.ok is False
    assert ".mp4" in result.actual or "wrong file type" in result.actual.lower()


def test_null_bytes_in_txt_fails(tmp_path: Path) -> None:
    """A .txt file whose contents contain null bytes is almost certainly a
    misrouted binary. Must FAIL without crashing the regex layer.
    """
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b"some text\x00\x00more text\nmaybe a SCRIPT_BODY line too")
    result = check_script(p)
    assert result.ok is False
    assert "null" in result.message.lower() or "binary" in result.message.lower()


def test_empty_file_fails(tmp_path: Path) -> None:
    """An empty script_FINAL.txt is not "clean" — it's a pipeline error."""
    p = tmp_path / "script_FINAL.txt"
    p.write_bytes(b"")
    result = check_script(p)
    assert result.ok is False
    assert "empty" in result.message.lower()


def test_oversized_file_fails(tmp_path: Path) -> None:
    """A suspiciously large 'script' must FAIL — refuses to scan in case
    a non-text payload was misrouted as the script.
    """
    p = tmp_path / "script_FINAL.txt"
    # 300 KiB of repeated harmless text — exceeds the 256 KiB cap.
    p.write_text("Hello world. " * (300 * 1024 // 13), encoding="utf-8")
    assert p.stat().st_size > 256 * 1024
    result = check_script(p)
    assert result.ok is False
    assert "large" in result.message.lower() or "cap" in result.actual.lower()


# ---------------------------------------------------------------------------
# check_topic_script convenience wrapper
# ---------------------------------------------------------------------------


def test_check_topic_script_resolves_canonical_path(tmp_path: Path) -> None:
    """check_topic_script() finds <root>/02_scripts/_drafts/<topic_id>/script_FINAL.txt."""
    topic_id = "2099-01-01_001"
    script_dir = tmp_path / "02_scripts" / "_drafts" / topic_id
    script_dir.mkdir(parents=True)
    (script_dir / "script_FINAL.txt").write_text(_CLEAN_BODY, encoding="utf-8")
    result = check_topic_script(topic_id, tmp_path)
    assert result.ok is True


def test_check_topic_script_missing_topic_fails(tmp_path: Path) -> None:
    """check_topic_script() returns FAIL (not raises) when the script doesn't exist."""
    result = check_topic_script("2099-12-31_999", tmp_path)
    assert result.ok is False
    assert "not found" in result.message.lower() or "missing" in result.actual.lower()


# ---------------------------------------------------------------------------
# CLI: --script
# ---------------------------------------------------------------------------


def test_cli_script_mode_clean_exits_0(tmp_path: Path) -> None:
    """python tools/prepublish_qa.py --script <clean.txt> exits 0."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_CLEAN_BODY, encoding="utf-8")
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "prepublish_qa.py"),
        "--script", str(p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "PASS" in proc.stdout


def test_cli_script_mode_dirty_exits_1(tmp_path: Path) -> None:
    """python tools/prepublish_qa.py --script <dirty.txt> exits 1."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_12_002_BODY, encoding="utf-8")
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "prepublish_qa.py"),
        "--script", str(p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "FAIL" in proc.stdout


def test_cli_script_mode_json(tmp_path: Path) -> None:
    """--script --json emits a single one-line JSON object."""
    p = tmp_path / "script_FINAL.txt"
    p.write_text(_CLEAN_BODY, encoding="utf-8")
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "prepublish_qa.py"),
        "--script", str(p),
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["result"]["check_id"] == SCRIPT_ARTIFACT_CHECK_ID
    assert payload["result"]["name"] == SCRIPT_ARTIFACT_CHECK_NAME
