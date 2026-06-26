"""Unit tests for learning.policy (classification + candidate generation)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.analysis import analyze  # noqa: E402
from learning.ledger import LedgerRow  # noqa: E402
from learning.policy import (  # noqa: E402
    LOCKED,
    PROPOSE,
    SAFE_AUTO,
    classify,
    propose_changes,
)


def _row(i, *, formula, views, dur, anchor, avp=50.0):
    return LedgerRow(
        topic_id=f"t{i}", video_id=f"v{i}", title="x", published_at="2026-06-01",
        pull_date="2026-06-20", days_live=10, matured=True, track="ai-vendor", slot=1,
        hook_letter="A", hook_formula=formula, cluster="A", weighted_total=0.8,
        counter_conventional_bonus=0.0, ai_vendor_bonus=0.0, named_human_bonus=0.0,
        corporate_deal_damped=False, title_anchor_present=anchor, duration_s=dur,
        views=views, avg_view_pct=avp, hold_at_3s=1.0, eligible=True, quarantine_reason="",
    )


def _short_wins_cohort():
    rows = []
    for i, v in enumerate([450, 500, 600, 700, 800, 900]):
        rows.append(_row(i, formula="Cited-Observation Lead", views=v, dur=30.0, anchor=True))
    for j, v in enumerate([80, 90, 100, 110, 120, 130, 140, 150], start=10):
        rows.append(_row(j, formula="Result-First", views=v, dur=45.0, anchor=False))
    return rows


class TestClassify(unittest.TestCase):
    def test_known_classifications(self):
        self.assertEqual(classify("script_quality.word_count_max"), SAFE_AUTO)
        self.assertEqual(classify("script_quality.duration_warn_s"), SAFE_AUTO)
        self.assertEqual(classify("tts.rate"), PROPOSE)
        self.assertEqual(classify("fact_check.require_human_resolution"), LOCKED)
        self.assertEqual(classify("publishing.kill_switch"), LOCKED)

    def test_scoring_weights_are_propose(self):
        self.assertEqual(classify("niche_fit"), PROPOSE)
        self.assertEqual(classify("scoring_weights.hook_strength"), PROPOSE)

    def test_unknown_defaults_propose(self):
        self.assertEqual(classify("some.unknown_key"), PROPOSE)


class TestProposeChanges(unittest.TestCase):
    def setUp(self):
        self.report = analyze(_short_wins_cohort(), retention_floor_pct=25.0, min_sample=5)
        self.config = {"script_quality": {"word_count_max": 98, "anchor_gate_enabled": True}}

    def test_safe_word_count_candidate_generated(self):
        cands = propose_changes(self.report, self.config)
        safe = [c for c in cands if c.klass == SAFE_AUTO]
        self.assertEqual(len(safe), 1)
        c = safe[0]
        self.assertEqual(c.key, "script_quality.word_count_max")
        self.assertEqual(c.current_value, 98)
        self.assertEqual(c.proposed_value, 95)   # 98 - 3, within clamp
        self.assertFalse(c.sacred)

    def test_scoring_weight_is_proposed_not_safe(self):
        cands = propose_changes(self.report, self.config)
        weight = [c for c in cands if c.key.startswith("scoring_weights")]
        self.assertEqual(len(weight), 1)
        self.assertEqual(weight[0].klass, PROPOSE)
        self.assertTrue(weight[0].sacred)

    def test_anchor_proposed(self):
        cands = propose_changes(self.report, self.config)
        anchor = [c for c in cands if c.key == "script_quality.anchor_gate_enabled"]
        self.assertEqual(len(anchor), 1)
        self.assertEqual(anchor[0].klass, PROPOSE)

    def test_no_safe_candidate_when_short_does_not_win(self):
        # Flip the advantage: long videos win -> no duration tightening proposed.
        rows = []
        for i, v in enumerate([450, 500, 600, 700, 800, 900]):
            rows.append(_row(i, formula="Result-First", views=v, dur=45.0, anchor=False))
        for j, v in enumerate([80, 90, 100, 110, 120, 130], start=10):
            rows.append(_row(j, formula="Cited-Observation Lead", views=v, dur=30.0, anchor=True))
        report = analyze(rows, retention_floor_pct=25.0, min_sample=5)
        cands = propose_changes(report, self.config)
        self.assertEqual([c for c in cands if c.klass == SAFE_AUTO], [])

    def test_clamp_respected_at_floor(self):
        # Already at the clamp floor -> no change proposed even if short wins.
        config = {"script_quality": {"word_count_max": 88}}
        cands = propose_changes(self.report, config)
        self.assertEqual([c for c in cands if c.key == "script_quality.word_count_max"], [])


if __name__ == "__main__":
    unittest.main()
