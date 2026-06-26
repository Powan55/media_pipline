"""Unit tests for :mod:`tools.script_prerender_patterns`.

Pure-Python regex / text work — no ffmpeg, no network. Covers the two rule
families behind prepublish_qa check #16:

  * placeholder tokens ([VERIFY / [NEEDS / [TODO / [FIXME)
  * retired/forbidden CTAs, pulled from the style guide's retired/forbidden
    sections, normalized for contractions / quotes / placeholders / whitespace.

Anchored on the two real published defects:
  * `_08_001`: a `[VERIFY: ...]` note spoken aloud mid-VO.
  * `_11_002`: `Comment "deploy" and I will send you the link to the announcement.`

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_script_prerender_patterns.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.script_prerender_patterns import (  # noqa: E402
    BASELINE_BANNED_CTAS,
    DEFAULT_STYLE_GUIDE_PATH,
    PLACEHOLDER_TOKEN_RE,
    _normalize,
    _phrase_to_regex,
    build_cta_matchers,
    load_banned_cta_phrases,
    scan_script_for_lint,
)

# Exact offending lines from the two published defects (do NOT edit the
# originals on disk — these are forward-looking regression fixtures).
DEFECT_08_001_VERIFY_LINE = (
    "A Reddit user on r/ClaudeAI [VERIFY: find a specific recent post with handle "
    "and URL — claim is that posters report shorter, lower-quality responses "
    "after hitting daily limits, with no banner or model-swap notification] posted "
    "screenshots showing their Claude answers got noticeably shorter after they hit "
    "their daily limit."
)
DEFECT_11_002_CTA_LINE = (
    'Comment "deploy" and I will send you the link to the announcement.'
)

# A minimal style-guide fixture mirroring the real file's structure: a marker-free
# "keep" CTA block (must NOT be harvested) plus the two ban lines (must be).
_STYLE_GUIDE_FIXTURE = """\
## Forbidden patterns

- **Generic engagement-begging in the VO or overlay:** "smash that like button" / "hit that like button," "like and subscribe," "if you enjoyed this video..." These are the loudest tells of a low-effort channel. Banned outright.

## CTA style

  **Save / share / follow (keep):**
  1. "Save this, share it with the AI-curious friend in your group chat."
  2. "Follow for one wild AI story a day."

  **Genuine-question CTAs (replace the old transactional bait):**
  3. "Which AI would you actually trust with this?"

  > **RETIRED (do not use):** "Comment [keyword] and I'll send you the link." Transactional comment-bait.
"""


# ---------------------------------------------------------------------------
# Placeholder token regex
# ---------------------------------------------------------------------------


class TestPlaceholderRegex:
    @pytest.mark.parametrize(
        "text",
        [
            "[VERIFY: find a source]",
            "[VERIFY]",
            "[NEEDS citation]",
            "[NEEDS: handle]",
            "[TODO]",
            "[TODO: tighten hook]",
            "[FIXME swap clip]",
            "lowercase [todo] still caught",
            "mid line [verify: x] text",
            "nested cue [B-ROLL: [TODO pick clip]] here",
        ],
    )
    def test_positive(self, text: str) -> None:
        assert PLACEHOLDER_TOKEN_RE.search(text), f"expected a placeholder hit on {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "[B-ROLL: phone screen, hands typing]",
            "[formula: Contradiction]",
            "[Subject]: [contrarian payoff]",  # title-format mixed-case bracket
            "an array index [0]",
            "Claude shipped a verified update today",  # 'verif...' not bracketed
            "todo list without brackets",
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        assert not PLACEHOLDER_TOKEN_RE.search(text), f"unexpected placeholder hit on {text!r}"

    def test_captures_whole_tag_for_reporting(self) -> None:
        m = PLACEHOLDER_TOKEN_RE.search(DEFECT_08_001_VERIFY_LINE)
        assert m is not None
        assert m.group(0).startswith("[VERIFY")
        assert m.group(0).endswith("]")


# ---------------------------------------------------------------------------
# Normalization + phrase compilation
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_expands_contraction(self) -> None:
        assert _normalize("I'll send you the link") == "i will send you the link"

    def test_strips_double_quotes(self) -> None:
        assert _normalize('Comment "deploy" and I') == "comment deploy and i"

    def test_folds_smart_quotes_and_apostrophes(self) -> None:
        assert _normalize("“I’ll” go") == "i will go"

    def test_collapses_whitespace(self) -> None:
        assert _normalize("smash   that\tlike") == "smash that like"


class TestPhraseToRegex:
    def test_placeholder_and_contraction_match_real_defect(self) -> None:
        """The style guide's `Comment [keyword] and I'll send you the link.`
        must catch the live `Comment "deploy" and I will send you the link.`"""
        rx = _phrase_to_regex("Comment [keyword] and I'll send you the link.")
        assert rx is not None
        assert rx.search(_normalize(DEFECT_11_002_CTA_LINE))

    def test_baseline_comment_bait_matches(self) -> None:
        rx = _phrase_to_regex("Comment <word> and I")
        assert rx is not None
        assert rx.search(_normalize(DEFECT_11_002_CTA_LINE))

    def test_prefix_phrase_matches_longer_line(self) -> None:
        # "smash that like" baseline matches the full "smash that like button".
        rx = _phrase_to_regex("smash that like")
        assert rx is not None
        assert rx.search(_normalize("Now smash that like button for me."))

    def test_word_boundary_prevents_midword(self) -> None:
        rx = _phrase_to_regex("hit that like")
        assert rx is not None
        # "like" must be a whole word, not the start of "likely".
        assert not rx.search(_normalize("Markets hit that likely high again."))

    def test_all_placeholder_phrase_rejected(self) -> None:
        # Too broad to ban safely (< 3 literal alpha chars).
        assert _phrase_to_regex("<x>") is None
        assert _phrase_to_regex("...") is None


# ---------------------------------------------------------------------------
# Style-guide extraction ("stays in sync")
# ---------------------------------------------------------------------------


class TestLoadBannedCtaPhrases:
    def test_extracts_from_marker_lines(self, tmp_path: Path) -> None:
        sg = tmp_path / "style_guide.md"
        sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
        phrases = load_banned_cta_phrases(sg)
        assert any("smash that like button" in p for p in phrases)
        assert any("hit that like button" in p for p in phrases)
        assert any("like and subscribe" in p for p in phrases)
        assert any(p.lower().startswith("comment [keyword] and") for p in phrases)

    def test_does_not_harvest_approved_keep_ctas(self, tmp_path: Path) -> None:
        """The marker-free 'keep' and genuine-question CTAs must NOT be banned."""
        sg = tmp_path / "style_guide.md"
        sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
        phrases = load_banned_cta_phrases(sg)
        joined = " || ".join(phrases).lower()
        assert "save this" not in joined
        assert "follow for one wild" not in joined
        assert "which ai would you actually trust" not in joined

    def test_missing_style_guide_returns_empty(self, tmp_path: Path) -> None:
        phrases = load_banned_cta_phrases(tmp_path / "does_not_exist.md")
        assert phrases == []

    @pytest.mark.skipif(
        not DEFAULT_STYLE_GUIDE_PATH.exists(),
        reason="real style guide not present on this machine",
    )
    def test_real_style_guide_yields_retired_cta(self) -> None:
        """End-to-end: the live style guide's RETIRED callout is harvested."""
        phrases = load_banned_cta_phrases()
        assert any(
            "comment" in p.lower() and "send you the link" in p.lower()
            for p in phrases
        )


# ---------------------------------------------------------------------------
# Matcher assembly (baseline + style guide)
# ---------------------------------------------------------------------------


class TestBuildCtaMatchers:
    def test_baseline_present_even_without_style_guide(self, tmp_path: Path) -> None:
        matchers = build_cta_matchers(tmp_path / "missing.md")
        assert matchers, "baseline must always be enforced"
        assert all(m.origin == "baseline" for m in matchers)
        assert len(matchers) == len(BASELINE_BANNED_CTAS)

    def test_style_guide_phrases_augment_baseline(self, tmp_path: Path) -> None:
        sg = tmp_path / "style_guide.md"
        sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
        matchers = build_cta_matchers(sg)
        assert any(m.origin == "style_guide" for m in matchers)
        assert any(m.origin == "baseline" for m in matchers)

    def test_can_disable_baseline(self, tmp_path: Path) -> None:
        matchers = build_cta_matchers(tmp_path / "missing.md", include_baseline=False)
        assert matchers == []


# ---------------------------------------------------------------------------
# Full-script scan
# ---------------------------------------------------------------------------


class TestScanScriptForLint:
    def _baseline_only(self, tmp_path: Path):
        # Force baseline-only matchers for deterministic CTA scanning.
        return build_cta_matchers(tmp_path / "no_style_guide.md")

    def test_clean_script_passes(self, tmp_path: Path) -> None:
        clean = (
            "Claude just shipped a wild update.\n"
            "[B-ROLL: Anthropic logo zoom]\n"
            "As u/lreeves put it on r/ClaudeAI, it changes everything.\n"
            "Tag the friend who needs to see this.\n"
        )
        assert scan_script_for_lint(clean, self._baseline_only(tmp_path)) == []

    def test_defect_1_verify_flags_placeholder(self, tmp_path: Path) -> None:
        matches = scan_script_for_lint(
            DEFECT_08_001_VERIFY_LINE, self._baseline_only(tmp_path)
        )
        kinds = {m.kind for m in matches}
        assert "placeholder" in kinds
        assert matches[0].line_no == 1

    def test_defect_2_cta_flags_banned_cta(self, tmp_path: Path) -> None:
        matches = scan_script_for_lint(
            DEFECT_11_002_CTA_LINE, self._baseline_only(tmp_path)
        )
        kinds = {m.kind for m in matches}
        assert "banned_cta" in kinds

    def test_broll_cue_line_not_cta_scanned(self, tmp_path: Path) -> None:
        # A stage direction that happens to describe a like-beg is NOT a VO/overlay
        # CTA, so it must not trip the banned_cta rule.
        line = "[B-ROLL: youtuber yelling smash that like button at the camera]"
        matches = scan_script_for_lint(line, self._baseline_only(tmp_path))
        assert all(m.kind != "banned_cta" for m in matches)

    def test_placeholder_inside_broll_cue_still_caught(self, tmp_path: Path) -> None:
        line = "[B-ROLL: [TODO pick a better clip] reddit thread]"
        matches = scan_script_for_lint(line, self._baseline_only(tmp_path))
        assert any(m.kind == "placeholder" for m in matches)

    def test_approved_ctas_pass(self, tmp_path: Path) -> None:
        sg = tmp_path / "style_guide.md"
        sg.write_text(_STYLE_GUIDE_FIXTURE, encoding="utf-8")
        body = (
            "Save this, share it with the AI-curious friend in your group chat.\n"
            "Which AI would you actually trust with this?\n"
            "Follow for one wild AI story a day.\n"
        )
        assert scan_script_for_lint(body, build_cta_matchers(sg)) == []

    def test_results_sorted_by_line_then_kind(self, tmp_path: Path) -> None:
        body = (
            "clean opener line\n"
            'Comment "deploy" and I will send you the link.\n'
            "[VERIFY: missing source] still here\n"
        )
        matches = scan_script_for_lint(body, self._baseline_only(tmp_path))
        assert [m.line_no for m in matches] == sorted(m.line_no for m in matches)
        assert {m.kind for m in matches} == {"banned_cta", "placeholder"}
