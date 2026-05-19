"""Unit tests for hook_leaderboard.py.

All tests build synthetic ``HookPerformanceRow`` instances and synthetic
``viral_hooks.md`` fixtures inside `tempfile.TemporaryDirectory` — no test
ever reads the live channel data or the live prompt library. Runnable under
both `pytest` and stdlib `unittest` discovery
(`python -m unittest tests.test_hook_leaderboard`).

Style mirrors the rest of the suite: unittest.TestCase + tempfile, per
`tests/test_topics.py`, `tests/test_archive_published.py`,
`tests/test_cleanup_orphans.py`, `tests/test_caption_word_pop.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analytics_join import (  # noqa: E402
    FORMULA_EDITED,
    FORMULA_UNTAGGED,
    REASON_LOW_VIEWS,
    REASON_NOT_UPLOADED,
    REASON_NO_HOLD_DATA,
    HookPerformanceRow,
)
from hook_leaderboard import (  # noqa: E402
    LeaderboardReport,
    _canonicalize_formula_name,
    _extract_canonical_formulas,
    _render_coverage_gaps,
    render_report,
    write_report,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_row(
    *,
    topic_id: str,
    formula: str,
    views: int | None = 100,
    hold_at_3s: float | None = 0.65,
    avg_view_pct: float | None = 60.0,
    days_live: int | None = 7,
    eligible: bool = True,
    reason: str | None = None,
    video_id: str | None = "vid123",
    hook_letter: str | None = "A",
    hook_text: str = "synthetic hook",
) -> HookPerformanceRow:
    """Build a HookPerformanceRow with sane defaults for tests."""
    return HookPerformanceRow(
        topic_id=topic_id,
        video_id=video_id,
        hook_letter=hook_letter,
        hook_text=hook_text,
        formula=formula,
        views=views,
        hold_at_3s=hold_at_3s,
        avg_view_pct=avg_view_pct,
        days_live=days_live,
        eligible_for_leaderboard=eligible,
        reason=reason,
    )


def _write_viral_hooks_md(path: Path, formulas: list[str]) -> None:
    """Write a minimal viral_hooks.md with `### N. <name>` headers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Viral hooks library", ""]
    for i, name in enumerate(formulas, start=1):
        lines.append(f"### {i}. {name}")
        lines.append("")
        lines.append(f"Some body text for {name}.")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _seed_channel_root(channel_root: Path, jsonl_rows: list[dict]) -> None:
    """Create the minimal channel-root layout the joiner expects."""
    research = channel_root / "01_research"
    research.mkdir(parents=True, exist_ok=True)
    log_path = research / "hook_selection_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for row in jsonl_rows:
            f.write(json.dumps(row) + "\n")
    # Empty upload + analytics CSVs — joiner is fine with missing files,
    # but empty headers exercise the "no data" path more realistically.
    (research / "upload_log.csv").write_text(
        "uploaded_at,topic_id,video_id,url,privacy,title\n",
        encoding="utf-8",
    )
    (research / "_weekly_analytics.csv").write_text(
        "pull_date,platform,video_id,title,published_at,views,avg_view_pct,"
        "avg_view_duration_sec,likes,shares,comments,follower_delta,"
        "hold_at_3s,traffic_source_shorts_pct\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Canonicalizer tests (small but load-bearing — exercises a recent bugfix)
# ---------------------------------------------------------------------------


class CanonicalizeFormulaNameTests(unittest.TestCase):
    """`_canonicalize_formula_name` must match the spec's 11-formula naming."""

    def test_strips_the_and_hook_wrappers(self) -> None:
        self.assertEqual(
            _canonicalize_formula_name("The Contradiction Hook"),
            "Contradiction",
        )

    def test_strips_trailing_parenthetical(self) -> None:
        self.assertEqual(
            _canonicalize_formula_name("The Comparison Frame (Skill Leap signature)"),
            "Comparison Frame",
        )

    def test_strips_surrounding_quotes_around_inner_title(self) -> None:
        # Real header from viral_hooks.md — quotes wrap the inner title and
        # would survive a naive single-pass strip.
        self.assertEqual(
            _canonicalize_formula_name('The "You\'re Doing It Wrong" Hook'),
            "You're Doing It Wrong",
        )

    def test_preserves_internal_punctuation(self) -> None:
        self.assertEqual(
            _canonicalize_formula_name("The Result-First / Mid-Action Hook"),
            "Result-First / Mid-Action",
        )


class ExtractCanonicalFormulasTests(unittest.TestCase):
    """`_extract_canonical_formulas` parses `### N. ...` headers from disk."""

    def test_parses_synthetic_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            md = Path(tmp) / "viral_hooks.md"
            _write_viral_hooks_md(md, [
                "The Contradiction Hook",
                "The Comparison Frame (signature)",
                'The "You\'re Doing It Wrong" Hook',
            ])
            names = _extract_canonical_formulas(md)
        self.assertEqual(
            names,
            ["Contradiction", "Comparison Frame", "You're Doing It Wrong"],
        )

    def test_falls_back_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            missing = Path(tmp) / "no_such.md"
            names = _extract_canonical_formulas(missing)
        # Fallback list has 11 entries (locked in module).
        self.assertEqual(len(names), 11)
        self.assertIn("Contradiction", names)


# ---------------------------------------------------------------------------
# render_report — the main entry point
# ---------------------------------------------------------------------------


class RenderReportTests(unittest.TestCase):
    """End-to-end rendering on synthetic channel-root data."""

    def test_empty_cohort_renders_no_eligible_message(self) -> None:
        # JSONL has no rows → joiner emits no rows → report should render
        # the empty-cohort message and still list every formula in gaps.
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            channel_root = tmp_path / "channel"
            _seed_channel_root(channel_root, jsonl_rows=[])
            md = tmp_path / "viral_hooks.md"
            _write_viral_hooks_md(md, [
                "The Contradiction Hook",
                "The Comparison Frame",
                "The Anti-Pattern Setup",
            ])
            report = render_report(
                channel_root,
                viral_hooks_md_path=md,
                today=date(2026, 5, 12),
            )

        self.assertIn("Total topics in hook log: 0", report.cohort_summary)
        self.assertIn("No eligible videos yet", report.cohort_summary)
        # Formula table renders the empty placeholder, not a header row.
        self.assertIn("No eligible videos yet", report.formula_table)
        # All 3 canonical formulas appear in coverage gaps.
        for name in ("Contradiction", "Comparison Frame", "Anti-Pattern Setup"):
            self.assertIn(f"- {name}", report.coverage_gaps)
        # Header line carries the date stem.
        self.assertIn("2026-05-12", report.full_markdown)

    def test_all_insufficient_cohort_marks_every_formula_insufficient(self) -> None:
        # Two formulas, one eligible row each → n=1 each → both insufficient.
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            channel_root = tmp_path / "channel"
            research = channel_root / "01_research"
            research.mkdir(parents=True)
            # Build the JSONL + upload + analytics CSVs so the joiner produces
            # eligible rows directly. Two topics, two formulas, one eligible
            # video each.
            log_path = research / "hook_selection_log.jsonl"
            with log_path.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "topic_id": "2026-05-10_001",
                    "hook_letter": "A",
                    "hook_text": "h1",
                    "formula": "Contradiction",
                }) + "\n")
                f.write(json.dumps({
                    "topic_id": "2026-05-10_002",
                    "hook_letter": "B",
                    "hook_text": "h2",
                    "formula": "Comparison Frame",
                }) + "\n")
            (research / "upload_log.csv").write_text(
                "uploaded_at,topic_id,video_id,url,privacy,title\n"
                "2026-05-10T00:00:00Z,2026-05-10_001,vid111,u,public,t\n"
                "2026-05-10T00:00:00Z,2026-05-10_002,vid222,u,public,t\n",
                encoding="utf-8",
            )
            (research / "_weekly_analytics.csv").write_text(
                "pull_date,platform,video_id,title,published_at,views,avg_view_pct,"
                "avg_view_duration_sec,likes,shares,comments,follower_delta,"
                "hold_at_3s,traffic_source_shorts_pct\n"
                "2026-05-11,youtube,vid111,t,2026-05-09,200,55.0,15,5,1,0,0,0.70,0.5\n"
                "2026-05-11,youtube,vid222,t,2026-05-09,250,60.0,18,7,2,0,0,0.65,0.5\n",
                encoding="utf-8",
            )
            md = tmp_path / "viral_hooks.md"
            _write_viral_hooks_md(md, [
                "The Contradiction Hook",
                "The Comparison Frame",
            ])
            report = render_report(
                channel_root,
                viral_hooks_md_path=md,
                today=date(2026, 5, 12),
            )

        # Both formulas appear in the table, both labelled `insufficient`
        # (each n=1, threshold for insufficient is n<3).
        self.assertIn("Contradiction", report.formula_table)
        self.assertIn("Comparison Frame", report.formula_table)
        # Count "insufficient" occurrences in the leaderboard table.
        self.assertGreaterEqual(report.formula_table.count("insufficient"), 2)
        # Cohort summary reports 2 eligible.
        self.assertIn("Eligible for leaderboard: 2", report.cohort_summary)


# ---------------------------------------------------------------------------
# Tests using directly-constructed HookPerformanceRow instances
# ---------------------------------------------------------------------------
# These avoid the joiner entirely so we can exercise the renderer on bespoke
# row sets (mixed evidence, sort order, etc.) without setting up CSV plumbing.


class RenderHelpersTests(unittest.TestCase):
    """Direct calls into the section renderers + render_report on a curated set.

    We can't easily call render_report directly on synthetic rows (it pulls
    rows via join_hooks_to_analytics from disk), so for these targeted tests
    we exercise `_render_coverage_gaps` and friends in isolation, then use
    the joiner for end-to-end determinism.
    """

    def test_coverage_gap_includes_uncovered_formulas_only(self) -> None:
        canonical = ["Contradiction", "Comparison Frame", "Anti-Pattern Setup"]
        eligible = [
            _make_row(topic_id="2026-05-01_001", formula="Comparison Frame"),
        ]
        rendered = _render_coverage_gaps(canonical, eligible)
        # `Comparison Frame` IS covered → must NOT show in gaps.
        # `Contradiction` and `Anti-Pattern Setup` are NOT covered → must show.
        self.assertNotIn("- Comparison Frame", rendered)
        self.assertIn("- Anti-Pattern Setup", rendered)
        self.assertIn("- Contradiction", rendered)

    def test_coverage_gap_ignores_sentinel_formulas(self) -> None:
        # A row whose formula is the EDITED sentinel does not "cover" any
        # canonical formula, even if its name happens to collide.
        canonical = ["Contradiction"]
        eligible = [
            _make_row(topic_id="2026-05-01_001", formula=FORMULA_EDITED),
            _make_row(topic_id="2026-05-01_002", formula=FORMULA_UNTAGGED),
        ]
        rendered = _render_coverage_gaps(canonical, eligible)
        self.assertIn("- Contradiction", rendered)

    def test_coverage_gap_alphabetical_sort(self) -> None:
        canonical = ["Zeta", "Alpha", "Mu"]
        rendered = _render_coverage_gaps(canonical, [])
        # Alpha appears before Mu before Zeta in the rendered output.
        alpha_idx = rendered.index("- Alpha")
        mu_idx = rendered.index("- Mu")
        zeta_idx = rendered.index("- Zeta")
        self.assertLess(alpha_idx, mu_idx)
        self.assertLess(mu_idx, zeta_idx)


# ---------------------------------------------------------------------------
# Determinism + per-video sort + write_report
# ---------------------------------------------------------------------------


class DeterminismAndAppendixTests(unittest.TestCase):
    """End-to-end determinism + per-video appendix sort + file write."""

    def _build_mixed_cohort(self, channel_root: Path) -> None:
        """Seed a channel root with 8 topics covering mixed evidence states."""
        research = channel_root / "01_research"
        research.mkdir(parents=True)
        # JSONL — unsorted insertion order to prove the appendix re-sorts.
        jsonl_rows = [
            # 6 eligible videos for "Contradiction" → could go strong if CI passes.
            {"topic_id": "2026-05-09_002", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            {"topic_id": "2026-05-09_001", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            {"topic_id": "2026-05-09_003", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            {"topic_id": "2026-05-09_004", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            {"topic_id": "2026-05-09_005", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            {"topic_id": "2026-05-09_006", "hook_letter": "A", "hook_text": "h",
             "formula": "Contradiction"},
            # 1 eligible "Comparison Frame" → n=1 → insufficient.
            {"topic_id": "2026-05-09_007", "hook_letter": "B", "hook_text": "h",
             "formula": "Comparison Frame"},
            # 1 not-uploaded "Measured-Claim" → ineligible (REASON_NOT_UPLOADED).
            {"topic_id": "2026-05-09_008", "hook_letter": "C", "hook_text": "h",
             "formula": "Measured-Claim"},
        ]
        log_path = research / "hook_selection_log.jsonl"
        with log_path.open("w", encoding="utf-8") as f:
            for row in jsonl_rows:
                f.write(json.dumps(row) + "\n")
        # Upload log: everything but _008 is uploaded.
        upload_lines = ["uploaded_at,topic_id,video_id,url,privacy,title"]
        for i in range(1, 8):
            upload_lines.append(
                f"2026-05-09T00:00:00Z,2026-05-09_00{i},vid00{i},u,public,t"
            )
        (research / "upload_log.csv").write_text(
            "\n".join(upload_lines) + "\n", encoding="utf-8",
        )
        # Analytics: 6 Contradiction with diverse hold values straddling the
        # cohort median, plus 1 Comparison Frame eligible.
        analytics_lines = [
            "pull_date,platform,video_id,title,published_at,views,avg_view_pct,"
            "avg_view_duration_sec,likes,shares,comments,follower_delta,"
            "hold_at_3s,traffic_source_shorts_pct",
            "2026-05-11,youtube,vid001,t,2026-05-08,500,55.0,15,5,1,0,0,0.80,0.5",
            "2026-05-11,youtube,vid002,t,2026-05-08,400,50.0,14,4,0,0,0,0.75,0.5",
            "2026-05-11,youtube,vid003,t,2026-05-08,350,52.0,14,3,0,0,0,0.70,0.5",
            "2026-05-11,youtube,vid004,t,2026-05-08,300,48.0,13,2,0,0,0,0.68,0.5",
            "2026-05-11,youtube,vid005,t,2026-05-08,250,46.0,12,2,0,0,0,0.66,0.5",
            "2026-05-11,youtube,vid006,t,2026-05-08,200,44.0,11,1,0,0,0,0.62,0.5",
            "2026-05-11,youtube,vid007,t,2026-05-08,180,40.0,10,1,0,0,0,0.55,0.5",
        ]
        (research / "_weekly_analytics.csv").write_text(
            "\n".join(analytics_lines) + "\n", encoding="utf-8",
        )

    def test_render_report_byte_equal_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            channel_root = tmp_path / "channel"
            self._build_mixed_cohort(channel_root)
            md = tmp_path / "viral_hooks.md"
            _write_viral_hooks_md(md, [
                "The Contradiction Hook",
                "The Comparison Frame",
                "The Measured-Claim Hook",
                "The Anti-Pattern Setup",
            ])
            r1 = render_report(channel_root, viral_hooks_md_path=md,
                               today=date(2026, 5, 12))
            r2 = render_report(channel_root, viral_hooks_md_path=md,
                               today=date(2026, 5, 12))
        self.assertEqual(r1.full_markdown, r2.full_markdown)
        self.assertEqual(r1.cohort_summary, r2.cohort_summary)
        self.assertEqual(r1.formula_table, r2.formula_table)
        self.assertEqual(r1.per_video_appendix, r2.per_video_appendix)
        self.assertEqual(r1.coverage_gaps, r2.coverage_gaps)

    def test_per_video_appendix_sorted_by_topic_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            channel_root = tmp_path / "channel"
            self._build_mixed_cohort(channel_root)
            md = tmp_path / "viral_hooks.md"
            _write_viral_hooks_md(md, ["The Contradiction Hook"])
            report = render_report(channel_root, viral_hooks_md_path=md,
                                   today=date(2026, 5, 12))

        # Find the lines containing topic_ids in the appendix and assert they
        # appear in lex order (which is also chronological for these IDs).
        appendix = report.per_video_appendix
        topic_ids = ["2026-05-09_001", "2026-05-09_002", "2026-05-09_003",
                     "2026-05-09_004", "2026-05-09_005", "2026-05-09_006",
                     "2026-05-09_007", "2026-05-09_008"]
        positions = [appendix.index(tid) for tid in topic_ids]
        self.assertEqual(positions, sorted(positions))

    def test_mixed_cohort_renders_weak_and_insufficient(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            channel_root = tmp_path / "channel"
            self._build_mixed_cohort(channel_root)
            md = tmp_path / "viral_hooks.md"
            _write_viral_hooks_md(md, [
                "The Contradiction Hook",
                "The Comparison Frame",
            ])
            report = render_report(channel_root, viral_hooks_md_path=md,
                                   today=date(2026, 5, 12))

        # Contradiction: n=6, but proportion above cohort median is exactly
        # half (3 of 6 strictly exceed the median of [0.55, 0.62, 0.66, 0.68,
        # 0.70, 0.75, 0.80] = 0.68 → only 0.70, 0.75, 0.80 succeed).
        # Wilson CI lower bound on 3/7 won't exceed 0.5 → label is "weak"
        # (n>=6 fails the CI floor, NOT strong).
        # Comparison Frame: n=1 → "insufficient".
        self.assertIn("weak", report.formula_table)
        self.assertIn("insufficient", report.formula_table)

    def test_write_report_uses_utc_date_in_filename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sv-leaderboard-") as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "out"
            report = LeaderboardReport(
                cohort_summary="cs", formula_table="ft",
                per_video_appendix="pa", coverage_gaps="cg",
                full_markdown="# title\n\nbody\n",
            )
            written = write_report(report, output_dir, today=date(2026, 5, 12))
            # Assert inside the with-block — TemporaryDirectory tears down on
            # exit, so written.exists() would be False after.
            self.assertEqual(written.name, "hook_leaderboard_2026-05-12.md")
            self.assertEqual(written.parent, output_dir)
            self.assertTrue(written.exists())
            self.assertEqual(
                written.read_text(encoding="utf-8"),
                "# title\n\nbody\n",
            )


if __name__ == "__main__":
    unittest.main()
