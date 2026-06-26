"""Unit tests for learning.analysis (reach-first analysis).

Builds synthetic LedgerRow cohorts directly and asserts: ranking is by median
views (reach), the retention floor flags low-watch-through features, the <38s
duration bucket and named anchor surface as reach leaders, and hold saturation
is detected. No I/O.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.analysis import analyze  # noqa: E402
from learning.ledger import LedgerRow  # noqa: E402


def _row(i, *, formula, views, avp, dur, anchor, slot, cluster, hold=1.0, eligible=True):
    return LedgerRow(
        topic_id=f"2026-06-0{i % 9 + 1}_{i:03d}",
        video_id=f"vid{i}",
        title="t",
        published_at="2026-06-01",
        pull_date="2026-06-20",
        days_live=10,
        matured=True,
        track="ai-vendor",
        slot=slot,
        hook_letter="A",
        hook_formula=formula,
        cluster=cluster,
        weighted_total=0.8,
        counter_conventional_bonus=0.0,
        ai_vendor_bonus=0.05,
        named_human_bonus=0.0,
        corporate_deal_damped=False,
        title_anchor_present=anchor,
        duration_s=dur,
        views=views,
        avg_view_pct=avp,
        hold_at_3s=hold,
        eligible=eligible,
        quarantine_reason="" if eligible else "immature",
    )


def _cohort():
    rows = []
    # 6 strong Cited-Observation videos (minority high performers): high views,
    # <38s, anchored, slot 1. All beat the cohort median -> strong evidence.
    for i, v in enumerate([450, 500, 600, 700, 800, 900]):
        rows.append(_row(i, formula="Cited-Observation Lead", views=v,
                         avp=50.0, dur=30.0, anchor=True, slot=1, cluster="A"))
    # 8 weak Result-First videos (majority low): low views, >=38s, no anchor,
    # slot 2, low retention.
    for j, v in enumerate([80, 90, 100, 110, 120, 130, 140, 150], start=10):
        rows.append(_row(j, formula="Result-First", views=v,
                         avp=21.0, dur=45.0, anchor=False, slot=2, cluster="C"))
    return rows


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.report = analyze(_cohort(), retention_floor_pct=25.0, min_sample=5)

    def test_cohort_stats(self):
        self.assertEqual(self.report.eligible_n, 14)
        self.assertEqual(self.report.cohort_median_views, 145.0)  # (140+150)/2
        self.assertTrue(self.report.hold_saturated)

    def test_hook_formula_ranked_by_views(self):
        dim = next(d for d in self.report.dimensions if d.dimension == "hook_formula")
        self.assertEqual(dim.features[0].value, "Cited-Observation Lead")
        self.assertEqual(dim.features[0].rank, 1)
        self.assertEqual(dim.features[0].evidence, "strong")
        self.assertEqual(dim.features[0].median_views, 650.0)

    def test_retention_floor_flags_low_watch_through(self):
        dim = next(d for d in self.report.dimensions if d.dimension == "hook_formula")
        rf = next(f for f in dim.features if f.value == "Result-First")
        self.assertTrue(rf.retention_risk)
        cited = next(f for f in dim.features if f.value == "Cited-Observation Lead")
        self.assertFalse(cited.retention_risk)

    def test_duration_lever(self):
        dim = next(d for d in self.report.dimensions if d.dimension == "duration_bucket")
        self.assertEqual(dim.features[0].value, "<38s")
        self.assertGreater(dim.features[0].median_views, dim.features[-1].median_views)

    def test_anchor_lever(self):
        dim = next(d for d in self.report.dimensions if d.dimension == "title_anchor")
        self.assertEqual(dim.features[0].value, "anchor")

    def test_leaders_exclude_small_or_risky(self):
        leader_keys = {(f.dimension, f.value) for f in self.report.leaders}
        self.assertIn(("hook_formula", "Cited-Observation Lead"), leader_keys)
        self.assertIn(("duration_bucket", "<38s"), leader_keys)
        # Result-First is both small (n=4 < 5) and retention-risky -> never a leader.
        self.assertNotIn(("hook_formula", "Result-First"), leader_keys)

    def test_empty_cohort_is_safe(self):
        empty = analyze([], retention_floor_pct=25.0, min_sample=5)
        self.assertEqual(empty.eligible_n, 0)
        self.assertIsNone(empty.cohort_median_views)
        self.assertEqual(empty.leaders, [])
        self.assertFalse(empty.hold_saturated)


def _breakout_cohort():
    """12 videos; 2 Cited-Observation videos clear the 1000-view ceiling."""
    rows = []
    for i, v in enumerate([1500, 1200, 900, 700, 500, 450]):
        rows.append(_row(i, formula="Cited-Observation Lead", views=v,
                         avp=50.0, dur=30.0, anchor=True, slot=1, cluster="A"))
    for j, v in enumerate([80, 90, 100, 110, 120, 130], start=10):
        rows.append(_row(j, formula="Result-First", views=v,
                         avp=40.0, dur=45.0, anchor=False, slot=2, cluster="C"))
    return rows


class TestTailInstrumentation(unittest.TestCase):
    """Right-tail (breakout) instrumentation — added 2026-06-24 deep-dive."""

    def setUp(self):
        self.report = analyze(_breakout_cohort(), retention_floor_pct=25.0,
                              min_sample=5, ceiling_views=1000)

    def test_tail_stats(self):
        self.assertEqual(self.report.ceiling_views, 1000)
        self.assertEqual(self.report.max_views, 1500.0)
        self.assertEqual(self.report.count_over_ceiling, 2)
        self.assertAlmostEqual(self.report.share_over_ceiling, 2 / 12)
        self.assertIsNotNone(self.report.p90_views)

    def test_breakout_ledger_sorted_desc(self):
        bvs = self.report.breakout_videos
        self.assertEqual([b.views for b in bvs], [1500, 1200])
        self.assertEqual(bvs[0].hook_formula, "Cited-Observation Lead")
        self.assertTrue(bvs[0].title_anchor_present)
        self.assertEqual(bvs[0].slot, 1)

    def test_ceiling_leaders_separate_from_median(self):
        keys = {(f.dimension, f.value) for f in self.report.ceiling_leaders}
        self.assertIn(("hook_formula", "Cited-Observation Lead"), keys)
        self.assertIn(("duration_bucket", "<38s"), keys)
        # Result-First never clears the ceiling -> never a ceiling leader.
        self.assertNotIn(("hook_formula", "Result-First"), keys)
        cited = next(
            f for d in self.report.dimensions if d.dimension == "hook_formula"
            for f in d.features if f.value == "Cited-Observation Lead"
        )
        self.assertEqual(cited.n_over_ceiling, 2)
        self.assertIsNotNone(cited.ceiling_hit_rate_ci)

    def test_no_breakout_when_none_clear(self):
        # The original _cohort() maxes at 900 -> nothing clears the 1000 ceiling.
        rep = analyze(_cohort(), retention_floor_pct=25.0, min_sample=5,
                      ceiling_views=1000)
        self.assertEqual(rep.count_over_ceiling, 0)
        self.assertEqual(rep.breakout_videos, [])
        self.assertEqual(rep.ceiling_leaders, [])
        self.assertEqual(rep.max_views, 900.0)


if __name__ == "__main__":
    unittest.main()
