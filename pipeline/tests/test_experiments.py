"""Unit tests for learning.experiments (journal + auto-rollback evaluator)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.experiments import (  # noqa: E402
    STATUS_CONFIRMED,
    STATUS_INCONCLUSIVE,
    STATUS_REVERTED,
    active_experiments,
    evaluate_active,
    load_experiments,
    open_experiment,
)
from learning.ledger import LedgerRow  # noqa: E402

OPENED_AT = "2026-06-01T00:00:00+00:00"


def _erow(i, *, published, views, avp=50.0):
    return LedgerRow(
        topic_id=f"t{i}", video_id=f"v{i}", title="t", published_at=published,
        pull_date="2026-06-20", days_live=10, matured=True, track="ai-vendor",
        slot=1, hook_letter="A", hook_formula="X", cluster="A", weighted_total=0.8,
        counter_conventional_bonus=0.0, ai_vendor_bonus=0.0, named_human_bonus=0.0,
        corporate_deal_damped=False, title_anchor_present=True, duration_s=30.0,
        views=views, avg_view_pct=avp, hold_at_3s=1.0, eligible=True, quarantine_reason="",
    )


def _open(channel_root, *, baseline=100.0, old=38.0, new=37.0,
          min_effect=5.0, rollback=-10.0, min_sample=5, window=7):
    return open_experiment(
        channel_root, target="script_quality.duration_warn_s", target_file="config.yaml",
        kind="auto", hypothesis="lower warn surfaces more <38s -> more reach",
        baseline_value=baseline, baseline_n=10, old_setting=old, new_setting=new,
        measurement_window_days=window, min_sample=min_sample, min_effect_pct=min_effect,
        rollback_threshold_pct=rollback, opened_at=OPENED_AT, config_snapshot="config.bak",
    )


class TestExperiments(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_open_and_dedupe(self):
        e1 = _open(self.root)
        self.assertIsNotNone(e1)
        e2 = _open(self.root)  # same target already active
        self.assertIsNone(e2)
        self.assertEqual(len(active_experiments(self.root)), 1)

    def test_hold_before_window_end(self):
        _open(self.root)
        rows = [_erow(i, published="2026-06-03", views=300) for i in range(6)]
        out = evaluate_active(self.root, rows, date(2026, 6, 5))  # before 2026-06-08
        self.assertEqual(out[0].decision, "hold")
        self.assertEqual(active_experiments(self.root)[0].status, "active")

    def test_confirmed_on_improvement(self):
        _open(self.root, baseline=100.0, min_effect=5.0)
        rows = [_erow(i, published="2026-06-03", views=200) for i in range(6)]
        out = evaluate_active(self.root, rows, date(2026, 6, 10))
        self.assertEqual(out[0].decision, STATUS_CONFIRMED)
        self.assertIsNone(out[0].revert_to)
        self.assertEqual(load_experiments(self.root)[0].status, STATUS_CONFIRMED)

    def test_reverted_on_regression(self):
        _open(self.root, baseline=200.0, rollback=-10.0)
        rows = [_erow(i, published="2026-06-03", views=100) for i in range(6)]  # -50%
        out = evaluate_active(self.root, rows, date(2026, 6, 10))
        self.assertEqual(out[0].decision, STATUS_REVERTED)
        self.assertEqual(out[0].revert_to, 38.0)

    def test_reverted_on_retention_breach(self):
        _open(self.root, baseline=100.0)
        rows = [_erow(i, published="2026-06-03", views=300, avp=10.0) for i in range(6)]  # avp<25
        out = evaluate_active(self.root, rows, date(2026, 6, 10), retention_floor_pct=25.0)
        self.assertEqual(out[0].decision, STATUS_REVERTED)
        self.assertEqual(out[0].revert_to, 38.0)

    def test_inconclusive_reverts_to_safety(self):
        _open(self.root, baseline=100.0, min_effect=20.0, rollback=-20.0)
        rows = [_erow(i, published="2026-06-03", views=105) for i in range(6)]  # +5%, between thresholds
        out = evaluate_active(self.root, rows, date(2026, 6, 10))
        self.assertEqual(out[0].decision, STATUS_INCONCLUSIVE)
        self.assertEqual(out[0].revert_to, 38.0)

    def test_extend_then_inconclusive_when_starved(self):
        _open(self.root, min_sample=5, window=7)
        rows = [_erow(i, published="2026-06-03", views=300) for i in range(2)]  # only 2 post rows
        out1 = evaluate_active(self.root, rows, date(2026, 6, 10))
        self.assertEqual(out1[0].decision, "extended")
        self.assertTrue(active_experiments(self.root)[0].extended)
        # Still starved after extension window -> inconclusive_reverted.
        ends = active_experiments(self.root)[0].measurement_window_ends
        out2 = evaluate_active(self.root, rows, date.fromisoformat(ends))
        self.assertEqual(out2[0].decision, STATUS_INCONCLUSIVE)
        self.assertEqual(out2[0].revert_to, 38.0)

    def test_only_post_change_videos_count(self):
        _open(self.root, baseline=100.0, min_effect=5.0)
        # 5 high-view videos published BEFORE the change must be ignored.
        pre = [_erow(i, published="2026-05-01", views=9999) for i in range(5)]
        post = [_erow(i + 100, published="2026-06-03", views=50) for i in range(6)]
        out = evaluate_active(self.root, pre + post, date(2026, 6, 10))
        # Post median is 50 vs baseline 100 -> regression, not confirmed by the pre rows.
        self.assertIn(out[0].decision, (STATUS_REVERTED, STATUS_INCONCLUSIVE))


if __name__ == "__main__":
    unittest.main()
