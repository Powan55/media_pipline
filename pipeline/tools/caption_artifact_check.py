"""Caption-side template-artifact check (Layer 3 of Sprint 5 defense).

Background
----------
On 2026-05-13, topic ``_12_002`` shipped to scheduled-Private with the literal
line ``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` at the top of
``script_FINAL.txt``. edge-TTS spoke that line aloud, and the word-pop caption
pipeline (``tools/caption_word_pop.py``) faithfully transcribed the audio into
the rendered ``.ass`` file. Operator caught it only after publication.

The Sprint 5 three-layer defense:

    Layer 1  prompt hardening              (LLM-side, cheapest)
    Layer 2  Stage 11 check #13            (script_FINAL.txt scan, safety net)
    Layer 3  caption-side double-check     (THIS MODULE, belt-and-braces)

All three layers import :func:`scan_for_artifacts` from
``tools.script_artifact_patterns`` so the regex set is a single source of truth.
Any non-empty match is a hard halt.

Library use::

    from tools.caption_artifact_check import check_captions_for_artifacts

    result = check_captions_for_artifacts(Path(".../topic_captions.ass"))
    if not result.ok:
        raise PipelineQAFailed(failures={result.check_id: {
            "name": result.name,
            "expected": result.expected,
            "actual": result.actual,
            "message": result.message,
        }})

CLI::

    python tools/caption_artifact_check.py --ass-file <path.ass>

Exit 0 = PASS. Exit 1 = FAIL (message to stderr). Exit 2 = usage error.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Allow `from tools.script_artifact_patterns import ...` to resolve when this
# module is invoked as a script. When invoked as `python -m tools.caption_artifact_check`
# or imported, the package path is already registered and this no-ops.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.script_artifact_patterns import (  # noqa: E402
    format_matches,
    scan_for_artifacts,
)

# Reuse the QA `CheckResult` NamedTuple so a Layer-3 failure plugs straight
# into `PipelineQAFailed.failures_dict()` without a translation step. Import
# happens at module-load — `prepublish_qa` is a leaf module from Layer 3's
# perspective (it does NOT import this file), so no circular-import risk.
from tools.prepublish_qa import CheckResult  # noqa: E402

log = logging.getLogger("caption_artifact_check")

# ---------------------------------------------------------------------------
# CLI exit codes (match prepublish_qa convention)
# ---------------------------------------------------------------------------

CLI_EXIT_OK = 0
CLI_EXIT_FAIL = 1
CLI_EXIT_USAGE = 2

# This check's stable identifier inside the QA-failure table. We re-use the
# CheckResult schema rather than inventing a parallel one, but the id sits
# OUTSIDE the prepublish_qa 1..13 range so the operator can tell at a glance
# that a Layer-3 hit came from the caption gate, not from a prepublish check.
CHECK_ID = 14
CHECK_NAME = "caption_template_artifacts"

# ---------------------------------------------------------------------------
# ASS parsing constants
# ---------------------------------------------------------------------------

# ASS Dialogue lines have 9 commas before the free-form Text field:
#   Dialogue: <Layer>,<Start>,<End>,<Style>,<Name>,<MarginL>,<MarginR>,
#             <MarginV>,<Effect>,<Text>
# Anything after the 9th comma is the rendered text (may itself contain
# commas — split(",", 9) preserves them).
_DIALOGUE_PREFIX = "Dialogue:"
_DIALOGUE_TEXT_FIELD_INDEX = 9

# libass override-block tag stripper.
#
# A libass override block is delimited by ``{`` and ``}``. The FIRST character
# inside the braces is ``\`` (e.g. ``{\rPop\t(0,80,\fscx140)}``). Real template
# placeholders we want to catch (``{SCRIPT_BODY}``, ``{HOOK_A}``) start with an
# UPPERCASE letter, so this regex's lookahead for ``\`` cleanly distinguishes
# them: tags get stripped, placeholders survive into ``scan_for_artifacts``.
#
# ``[^}]*`` is non-greedy in practice because ``}`` is excluded, so the match
# stops at the closing brace of the current block. Empty ``{}`` is matched by
# `_EMPTY_BLOCK_RE` separately so harmless decoration is removed first.
_LIBASS_TAG_RE = re.compile(r"\{\\[^}]*\}")
_EMPTY_BLOCK_RE = re.compile(r"\{\s*\}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_libass_tags(text: str) -> str:
    """Remove libass override blocks (``{\\...}``) and empty ``{}`` decoration.

    Preserves template placeholders like ``{SCRIPT_BODY}`` (no leading
    backslash) so they reach :func:`scan_for_artifacts` intact.
    """
    text = _LIBASS_TAG_RE.sub("", text)
    text = _EMPTY_BLOCK_RE.sub("", text)
    return text


def _extract_dialogue_texts(ass_text: str) -> list[str]:
    """Pull the Text field out of every ``Dialogue:`` line in ``ass_text``.

    Returns the raw text fields in document order (still containing libass
    tags — caller is responsible for stripping). Lines that don't have 10
    comma-separated fields are skipped silently; libass would ignore them too.
    """
    extracted: list[str] = []
    for line in ass_text.splitlines():
        if not line.startswith(_DIALOGUE_PREFIX):
            continue
        # The portion after "Dialogue:" is the comma-separated event payload.
        payload = line[len(_DIALOGUE_PREFIX):].lstrip()
        parts = payload.split(",", _DIALOGUE_TEXT_FIELD_INDEX)
        if len(parts) <= _DIALOGUE_TEXT_FIELD_INDEX:
            # Malformed Dialogue line — libass would skip it; we do too.
            continue
        extracted.append(parts[_DIALOGUE_TEXT_FIELD_INDEX])
    return extracted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_captions_for_artifacts(ass_file: Path) -> CheckResult:
    """Scan an ``.ass`` caption file for template-artifact leakage.

    Reads ``ass_file``, extracts every ``Dialogue:`` event's Text field,
    strips libass override blocks, joins the cleaned events with newlines,
    and runs :func:`scan_for_artifacts` over the result.

    Returns a :class:`CheckResult` — PASS on a clean file, FAIL with a
    human-readable list of match locations on a hit. Raises
    :class:`FileNotFoundError` if the file does not exist (Layer 3 is wired
    AFTER caption generation, so a missing file is a real bug, not a
    user-input error).
    """
    ass_file = Path(ass_file)
    if not ass_file.exists():
        raise FileNotFoundError(f"caption file not found: {ass_file}")

    ass_text = ass_file.read_text(encoding="utf-8", errors="replace")
    dialogue_texts = _extract_dialogue_texts(ass_text)

    # Strip libass override tags BEFORE running the scan. This is the whole
    # point of Layer 3 doing its own pre-processing — the patterns module is
    # template-aware (catches `{FOO}`) and we must not feed it the libass
    # `{\rPop}` decoration noise.
    cleaned_lines = [_strip_libass_tags(t).strip() for t in dialogue_texts]
    # Drop events that resolved to empty strings after stripping (tag-only
    # events are harmless decoration).
    cleaned_lines = [line for line in cleaned_lines if line]

    # SPACE join, not newline join. word-pop emits one Dialogue event per
    # spoken word, so a newline join would put every single word on its own
    # line — that triggers the all-caps-line stage-instruction regex on
    # legitimate acronyms (``AI``, ``API``, ``TTS``). Joining with spaces
    # recreates the natural prose form so only multi-token annotations
    # ("SCRIPT_BODY (uses HOOK_A as the verbal opener):") fire the scan.
    #
    # Trade-off: we lose detection of markdown-structure stage-instruction
    # forms (``>``, ``#``, ``**bold**``, lone-all-caps-line) — but TTS does
    # not speak markdown, so those forms can't reach the caption file via
    # the transcription path anyway. Template-placeholder and internal-name
    # patterns (the _12_002 root-cause family) are NOT line-anchored, so
    # detection of the regression class we care about is preserved.
    cleaned_text = " ".join(cleaned_lines)

    matches = scan_for_artifacts(cleaned_text)

    if not matches:
        return CheckResult(
            check_id=CHECK_ID,
            name=CHECK_NAME,
            ok=True,
            severity="PASS",
            message="ok",
            expected="no template artifacts in any Dialogue event",
            actual=f"{len(dialogue_texts)} Dialogue events, all clean",
        )

    rendered = format_matches(matches)
    return CheckResult(
        check_id=CHECK_ID,
        name=CHECK_NAME,
        ok=False,
        severity="FAIL",
        message=(
            f"caption file {ass_file.name} contains "
            f"{len(matches)} template-artifact match(es):\n{rendered}"
        ),
        expected="no template artifacts in any Dialogue event",
        actual=(
            f"{len(matches)} match(es) across "
            f"{len({m.line_no for m in matches})} event(s)"
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="caption_artifact_check",
        description=(
            "Layer 3 of the Sprint 5 template-artifact defense: scan a "
            ".ass caption file for leaked template placeholders / internal "
            "field names / stage-instruction annotations."
        ),
    )
    ap.add_argument(
        "--ass-file",
        required=True,
        type=Path,
        help="Path to the rendered .ass caption file to scan.",
    )
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        result = check_captions_for_artifacts(args.ass_file)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return CLI_EXIT_USAGE

    if result.ok:
        print(f"PASS: {args.ass_file} ({result.actual})")
        return CLI_EXIT_OK

    print(
        f"FAIL: {args.ass_file}\n"
        f"  expected={result.expected}\n"
        f"  actual={result.actual}\n"
        f"  {result.message}",
        file=sys.stderr,
    )
    return CLI_EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
