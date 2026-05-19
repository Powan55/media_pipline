"""Unit tests for daily_batch_hook_addendum.format_hook_addendum.

All tests use ``tmp_path`` as the synthetic channel root — no test ever reads
the live channel data. The leaderboard footer's analytics path is exercised
two ways:

  - "natural" path: write synthetic JSONL + upload + analytics fixtures into
    ``tmp_path/01_research`` so ``join_hooks_to_analytics`` runs end-to-end.
  - "exception" path: monkeypatch ``join_hooks_to_analytics`` to raise so the
    catch-all renders the "(leaderboard unavailable: ...)" fallback.

Coverage (per the slice spec):

  - gate-2-not-passed (FINAL missing) -> awaiting placeholder
  - no-RESPONSE topic                 -> no-response placeholder
  - full happy path                   -> Chosen + Alternatives + Leaderboard block
  - EDITED formula                    -> "(EDITED - formula could not be matched)"
  - UNTAGGED formula                  -> "<text> - (formula tags missing in RESPONSE)"
  - leaderboard footer when n=0       -> "insufficient data for this formula yet"
  - leaderboard footer when n>=3      -> "median hold@3s = ... at n=N (label)"
  - leaderboard exception             -> "(leaderboard unavailable: ...)"
  - purity: no files written under tmp_path
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import daily_batch_hook_addendum  # noqa: E402
from daily_batch_hook_addendum import format_hook_addendum  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_RESPONSE_TAGGED = """\
HOOK_A: Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.   [formula: Specific-Number Promise]
HOOK_B: You think you're getting advice from Claude. You're getting a yes man.   [formula: Contradiction]
HOOK_C: Anthropic studied a million Claude chats. The relationship answer is the dishonest one.   [formula: Cited-Observation Lead]

[B-ROLL: phone] body irrelevant for hook extraction.
"""

_RESPONSE_UNTAGGED = """\
HOOK_A: Cursor users still paste context. A folder does it for you.
HOOK_B: I deleted half my Cursor prompts. The chat got better.
HOOK_C: Cursor reads this folder before your message. You're missing it.

Most Cursor users still paste context into chat.
"""


def _make_topic_dir(channel_root: Path, topic_id: str) -> Path:
    """Create the per-topic drafts dir and return its Path."""
    d = channel_root / "02_scripts" / "_drafts" / topic_id
    d.mkdir(parents=True)
    return d


def _write_response(topic_dir: Path, body: str) -> None:
    (topic_dir / "script_RESPONSE.txt").write_text(body, encoding="utf-8")


def _write_final(topic_dir: Path, body: str) -> None:
    (topic_dir / "script_FINAL.txt").write_text(body, encoding="utf-8")


def _snapshot_paths(root: Path) -> set[Path]:
    """Capture every existing path under ``root`` for purity assertions."""
    return {p for p in root.rglob("*")}


# ---------------------------------------------------------------------------
# Placeholder paths: gate 2 not passed / no RESPONSE
# ---------------------------------------------------------------------------


class TestPlaceholders:

    def test_missing_final_returns_awaiting_placeholder(self, tmp_path: Path) -> None:
        # No topic dir at all -> gate 2 not passed.
        assert (
            format_hook_addendum("2026-12-31_999", tmp_path)
            == "(awaiting gate 2 selection)"
        )

    def test_missing_final_dir_exists_but_no_files(self, tmp_path: Path) -> None:
        # Drafts dir exists but neither file is present.
        _make_topic_dir(tmp_path, "2026-05-12_007")
        assert (
            format_hook_addendum("2026-05-12_007", tmp_path)
            == "(awaiting gate 2 selection)"
        )

    def test_missing_response_returns_no_response_placeholder(
        self, tmp_path: Path,
    ) -> None:
        topic_dir = _make_topic_dir(tmp_path, "2026-05-12_008")
        _write_final(topic_dir, "Some shipped hook line.")
        # No script_RESPONSE.txt
        assert (
            format_hook_addendum("2026-05-12_008", tmp_path)
            == "(no LLM response logged for this topic)"
        )


# ---------------------------------------------------------------------------
# Full happy path: chosen hook + alternatives + leaderboard footer
# ---------------------------------------------------------------------------


class TestHappyPath:

    def test_full_block_shape_and_alternatives_listed(
        self, tmp_path: Path,
    ) -> None:
        topic_id = "2026-05-12_002"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        # Ship HOOK_A verbatim (first sentence is the hook).
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. "
            "[B-ROLL: phone] Twenty five percent of relationship chats are pure flattery.",
        )

        out = format_hook_addendum(topic_id, tmp_path)

        # Header + chosen line.
        assert "**Chosen hook:** A - Specific-Number Promise" in out
        # Verbatim shipped quote.
        assert "> Anthropic just told on Claude." in out
        # Alternatives section header + both un-chosen letters with formulas.
        assert "**Alternatives:**" in out
        assert "- B (Contradiction):" in out
        assert "- C (Cited-Observation Lead):" in out
        # Leaderboard footer present (n=0 path because no analytics fixtures).
        assert (
            '**Leaderboard for "Specific-Number Promise":** '
            "insufficient data for this formula yet (n=0)"
        ) in out
        # Chosen letter NOT listed under alternatives.
        assert "- A (Specific-Number Promise):" not in out


# ---------------------------------------------------------------------------
# EDITED + UNTAGGED branches
# ---------------------------------------------------------------------------


class TestEditedAndUntagged:

    def test_edited_formula_renders_marker_and_keeps_alternatives(
        self, tmp_path: Path,
    ) -> None:
        topic_id = "2026-05-12_003"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        # Operator wrote a totally fresh hook that doesn't match A/B/C.
        _write_final(topic_dir, "Brand new operator-written hook that nobody proposed.")

        out = format_hook_addendum(topic_id, tmp_path)

        assert "**Chosen hook:** (EDITED - formula could not be matched)" in out
        # Operator's shipped text still shown for context.
        assert "> Brand new operator-written hook that nobody proposed." in out
        # All three alternatives listed (none chosen, so all three appear).
        assert "- A (Specific-Number Promise):" in out
        assert "- B (Contradiction):" in out
        assert "- C (Cited-Observation Lead):" in out
        # No leaderboard footer for EDITED (no real formula to look up).
        assert "Leaderboard for" not in out

    def test_untagged_formula_renders_marker_and_keeps_alternatives(
        self, tmp_path: Path,
    ) -> None:
        topic_id = "2026-05-12_004"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_UNTAGGED)
        _write_final(topic_dir, "Cursor users still paste context. A folder does it for you.")

        out = format_hook_addendum(topic_id, tmp_path)

        assert "(formula tags missing in RESPONSE)" in out
        assert "**Chosen hook:**" in out
        # Shipped text appears in the chosen line for UNTAGGED.
        assert "Cursor users still paste context. A folder does it for you." in out
        # Alternatives shown — the chosen letter is unknown (None) for UNTAGGED,
        # so all three candidates appear (none filtered).
        assert "**Alternatives:**" in out
        # Untagged candidates render with "untagged" formula label.
        assert "untagged" in out
        # No leaderboard footer for UNTAGGED.
        assert "Leaderboard for" not in out


# ---------------------------------------------------------------------------
# Leaderboard footer: n=0 vs n>=3 vs exception
# ---------------------------------------------------------------------------


class _StubStat:
    """Mirror the FormulaStat shape — only the attrs the addendum reads."""

    def __init__(
        self,
        n: int,
        median_hold_at_3s: float | None,
        wilson_ci_above_cohort_median: tuple[float, float] | None,
    ) -> None:
        self.n = n
        self.median_hold_at_3s = median_hold_at_3s
        self.wilson_ci_above_cohort_median = wilson_ci_above_cohort_median


class TestLeaderboardFooter:

    def test_footer_n_zero_renders_insufficient(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_005"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        out = format_hook_addendum(topic_id, tmp_path)
        assert (
            '**Leaderboard for "Specific-Number Promise":** '
            "insufficient data for this formula yet (n=0)"
        ) in out

    def test_footer_n_3_renders_weak_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        topic_id = "2026-05-12_006"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        # Mock the join + medians so we don't need a full CSV fixture.
        def _fake_join(channel_root: Path):  # type: ignore[no-untyped-def]
            return []

        def _fake_medians(rows):  # type: ignore[no-untyped-def]
            return {
                "Specific-Number Promise": _StubStat(
                    n=3, median_hold_at_3s=0.72,
                    wilson_ci_above_cohort_median=(0.2, 0.9),
                ),
            }

        monkeypatch.setattr(
            daily_batch_hook_addendum, "join_hooks_to_analytics", _fake_join,
        )
        monkeypatch.setattr(
            daily_batch_hook_addendum, "formula_medians", _fake_medians,
        )

        out = format_hook_addendum(topic_id, tmp_path)
        assert (
            '**Leaderboard for "Specific-Number Promise":** '
            "median hold@3s = 0.72 at n=3 (weak)"
        ) in out

    def test_footer_n_6_strong_when_wilson_lower_above_half(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        topic_id = "2026-05-12_009"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        def _fake_join(channel_root: Path):  # type: ignore[no-untyped-def]
            return []

        def _fake_medians(rows):  # type: ignore[no-untyped-def]
            return {
                "Specific-Number Promise": _StubStat(
                    n=6, median_hold_at_3s=0.81,
                    wilson_ci_above_cohort_median=(0.55, 0.95),
                ),
            }

        monkeypatch.setattr(
            daily_batch_hook_addendum, "join_hooks_to_analytics", _fake_join,
        )
        monkeypatch.setattr(
            daily_batch_hook_addendum, "formula_medians", _fake_medians,
        )

        out = format_hook_addendum(topic_id, tmp_path)
        assert "at n=6 (strong)" in out

    def test_footer_exception_caught_and_fallback_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        topic_id = "2026-05-12_010"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        def _boom(channel_root: Path):  # type: ignore[no-untyped-def]
            raise RuntimeError("synthetic analytics CSV missing")

        monkeypatch.setattr(
            daily_batch_hook_addendum, "join_hooks_to_analytics", _boom,
        )

        out = format_hook_addendum(topic_id, tmp_path)
        assert (
            '**Leaderboard for "Specific-Number Promise":** '
            "(leaderboard unavailable: synthetic analytics CSV missing)"
        ) in out
        # The chosen-vs-alternatives block must still ship despite the failure.
        assert "**Chosen hook:** A - Specific-Number Promise" in out
        assert "**Alternatives:**" in out


# ---------------------------------------------------------------------------
# Purity: function does not write or modify files
# ---------------------------------------------------------------------------


class TestPurity:

    def test_does_not_create_or_modify_files(self, tmp_path: Path) -> None:
        topic_id = "2026-05-12_011"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        before = _snapshot_paths(tmp_path)
        before_mtimes = {
            p: p.stat().st_mtime for p in before if p.is_file()
        }

        # Call multiple times to be sure no lazy write is hiding.
        for _ in range(3):
            format_hook_addendum(topic_id, tmp_path)

        after = _snapshot_paths(tmp_path)
        assert after == before, "format_hook_addendum should not create files"
        for p, mtime in before_mtimes.items():
            assert p.stat().st_mtime == mtime, f"file mutated: {p}"

    def test_placeholder_paths_create_no_files(self, tmp_path: Path) -> None:
        before = _snapshot_paths(tmp_path)
        format_hook_addendum("never-existed-topic", tmp_path)
        assert _snapshot_paths(tmp_path) == before


# ---------------------------------------------------------------------------
# Threshold-source-of-truth: addendum delegates to hook_leaderboard_stats
#
# Slice 6 originally inlined the evidence_strength thresholds via a private
# `_evidence_label` helper, creating a drift risk vs the leaderboard report.
# Slice 8 promoted `hook_leaderboard_stats.evidence_strength` to public and
# deleted the duplicate. These tests pin that contract so a future refactor
# that re-introduces an inline copy fails loudly.
# ---------------------------------------------------------------------------


class TestEvidenceLabelDelegation:

    def test_addendum_imports_evidence_strength_from_stats(self) -> None:
        """The addendum module must import the shared helper, not a local copy."""
        import hook_leaderboard_stats

        assert hasattr(daily_batch_hook_addendum, "evidence_strength")
        assert (
            daily_batch_hook_addendum.evidence_strength
            is hook_leaderboard_stats.evidence_strength
        )

    def test_inline_evidence_label_helper_is_removed(self) -> None:
        """The duplicated `_evidence_label` helper must no longer exist."""
        assert not hasattr(daily_batch_hook_addendum, "_evidence_label"), (
            "daily_batch_hook_addendum._evidence_label was deleted in Slice 8 "
            "in favour of hook_leaderboard_stats.evidence_strength — do not "
            "re-introduce a local copy (drift risk)."
        )

    def test_footer_uses_shared_helper_when_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Patch the imported `evidence_strength` and confirm the footer uses it.

        If a future refactor accidentally re-inlined the threshold logic, the
        patch would be ignored and the assertion below would fail.
        """
        topic_id = "2026-05-12_012"
        topic_dir = _make_topic_dir(tmp_path, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. "
            "Twenty five percent of relationship chats are pure flattery.",
        )

        def _fake_join(channel_root: Path):  # type: ignore[no-untyped-def]
            return []

        def _fake_medians(rows):  # type: ignore[no-untyped-def]
            return {
                "Specific-Number Promise": _StubStat(
                    n=99, median_hold_at_3s=0.50,
                    wilson_ci_above_cohort_median=(0.0, 1.0),
                ),
            }

        sentinel_label = "SENTINEL_FROM_PATCHED_HELPER"

        def _fake_strength(n, wilson):  # type: ignore[no-untyped-def]
            return sentinel_label

        monkeypatch.setattr(
            daily_batch_hook_addendum, "join_hooks_to_analytics", _fake_join,
        )
        monkeypatch.setattr(
            daily_batch_hook_addendum, "formula_medians", _fake_medians,
        )
        monkeypatch.setattr(
            daily_batch_hook_addendum, "evidence_strength", _fake_strength,
        )

        out = format_hook_addendum(topic_id, tmp_path)
        assert sentinel_label in out, (
            "footer label must come from the imported evidence_strength — "
            f"got: {out!r}"
        )


# ---------------------------------------------------------------------------
# End-to-end: real join_hooks_to_analytics with a synthetic n=3 cohort
# ---------------------------------------------------------------------------


class TestLeaderboardEndToEnd:
    """Drive ``join_hooks_to_analytics`` for real with synthetic CSV fixtures.

    Belt-and-braces alongside the monkey-patched cases: makes sure the wiring
    between the addendum and the on-main analytics modules actually composes.
    """

    _LEGACY_HEADER = (
        "pull_date,platform,video_id,title,published_at,views,avg_view_pct,"
        "avg_view_duration_sec,likes,shares,comments,follower_delta,"
        "hold_at_3s,traffic_source_shorts_pct"
    )

    def _seed_research(self, channel_root: Path) -> None:
        """Drop a 3-eligible-row cohort under ``01_research/`` for the chosen formula."""
        research = channel_root / "01_research"
        research.mkdir(parents=True, exist_ok=True)

        hook_log = research / "hook_selection_log.jsonl"
        rows = [
            {
                "topic_id": f"2026-05-0{i}_001",
                "hook_letter": "A",
                "hook_text": f"Hook {i}",
                "formula": "Specific-Number Promise",
                "all_three_hooks": [],
            }
            for i in range(1, 4)
        ]
        with hook_log.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        upload_log = research / "upload_log.csv"
        with upload_log.open("w", encoding="utf-8", newline="") as f:
            f.write("uploaded_at,topic_id,video_id,url,privacy,title\n")
            for i in range(1, 4):
                f.write(
                    f"2026-05-0{i}T00:00:00+00:00,2026-05-0{i}_001,vid{i},"
                    f"https://youtu.be/vid{i},public,t{i}\n"
                )

        analytics = research / "_weekly_analytics.csv"
        with analytics.open("w", encoding="utf-8", newline="") as f:
            f.write(self._LEGACY_HEADER + "\n")
            # Three eligible rows: views >= 70 AND hold_at_3s present.
            for i, hold in enumerate([0.65, 0.72, 0.80], start=1):
                f.write(
                    f"2026-05-12,youtube,vid{i},title{i},2026-05-0{i},"
                    f"500,42.5,15.0,5,1,2,0,{hold},85.0\n"
                )

    def test_real_join_yields_n3_footer(self, tmp_path: Path) -> None:
        channel_root = tmp_path
        self._seed_research(channel_root)

        # Topic that ships HOOK_A (Specific-Number Promise) — must land in the
        # leaderboard cohort created above (formula matches).
        topic_id = "2026-05-12_999"
        topic_dir = _make_topic_dir(channel_root, topic_id)
        _write_response(topic_dir, _RESPONSE_TAGGED)
        _write_final(
            topic_dir,
            "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.",
        )

        out = format_hook_addendum(topic_id, channel_root)

        # n=3 with median hold = median([0.65, 0.72, 0.80]) = 0.72
        assert "at n=3" in out
        assert "median hold@3s = 0.72" in out
        # The label depends on the wilson interval which we don't mock here;
        # accept either of the documented post-n>=3 buckets.
        assert "(weak)" in out or "(strong)" in out
