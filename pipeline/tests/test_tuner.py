"""Unit tests for learning.tuner (auto-apply + propose routing + caps)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from learning.analysis import analyze  # noqa: E402
from learning.config_io import read_yaml_key  # noqa: E402
from learning.experiments import active_experiments  # noqa: E402
from learning.ledger import LedgerRow  # noqa: E402
from learning.tuner import run_tuner  # noqa: E402

TODAY = date(2026, 6, 23)

_CONFIG = """\
script_quality:
  word_count_max: 98       # upper bound
  word_count_min: 75       # lower bound
  anchor_gate_enabled: true

tts:
  rate: "+10%"             # speaking rate
"""


def _row(i, *, formula, views, dur, anchor, avp=50.0):
    return LedgerRow(
        topic_id=f"t{i}", video_id=f"v{i}", title="x", published_at="2026-06-01",
        pull_date="2026-06-20", days_live=10, matured=True, track="ai-vendor", slot=1,
        hook_letter="A", hook_formula=formula, cluster="A", weighted_total=0.8,
        counter_conventional_bonus=0.0, ai_vendor_bonus=0.0, named_human_bonus=0.0,
        corporate_deal_damped=False, title_anchor_present=anchor, duration_s=dur,
        views=views, avg_view_pct=avp, hold_at_3s=1.0, eligible=True, quarantine_reason="",
    )


def _cohort():
    rows = []
    for i, v in enumerate([450, 500, 600, 700, 800, 900]):
        rows.append(_row(i, formula="Cited-Observation Lead", views=v, dur=30.0, anchor=True))
    for j, v in enumerate([80, 90, 100, 110, 120, 130, 140, 150], start=10):
        rows.append(_row(j, formula="Result-First", views=v, dur=45.0, anchor=False))
    return rows


class TestTuner(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.cfg = self.root / "config.yaml"
        self.cfg.write_text(_CONFIG, encoding="utf-8")
        self.config = yaml.safe_load(_CONFIG)
        self.report = analyze(_cohort(), retention_floor_pct=25.0, min_sample=5)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, apply_enabled, **kw):
        return run_tuner(self.root, self.config, self.report, TODAY,
                         config_path=self.cfg, apply_enabled=apply_enabled, **kw)

    def test_report_only_applies_nothing(self):
        res = self._run(apply_enabled=False)
        self.assertEqual(res.applied, [])
        self.assertTrue(res.proposed)  # PROPOSE candidates present
        self.assertEqual(read_yaml_key(self.cfg, "script_quality.word_count_max"), "98")
        self.assertTrue(Path(res.proposals_path).exists())
        self.assertTrue(any("apply disabled" in r for _, r in res.skipped))

    def test_apply_changes_config_and_opens_experiment(self):
        res = self._run(apply_enabled=True)
        self.assertEqual(len(res.applied), 1)
        self.assertEqual(read_yaml_key(self.cfg, "script_quality.word_count_max"), "95")
        actives = active_experiments(self.root)
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0].target, "script_quality.word_count_max")
        self.assertEqual(actives[0].old_setting, 98)
        self.assertEqual(actives[0].new_setting, 95)

    def test_idempotent_while_experiment_active(self):
        self._run(apply_enabled=True)              # 98 -> 95, experiment open
        res2 = self._run(apply_enabled=True)       # same day, experiment still active
        self.assertEqual(res2.applied, [])
        self.assertEqual(read_yaml_key(self.cfg, "script_quality.word_count_max"), "95")
        self.assertTrue(any("already active" in r for _, r in res2.skipped))
        self.assertEqual(len(active_experiments(self.root)), 1)

    def test_per_cycle_cap_zero_applies_nothing(self):
        res = self._run(apply_enabled=True, max_auto_applies_per_cycle=0)
        self.assertEqual(res.applied, [])
        self.assertEqual(read_yaml_key(self.cfg, "script_quality.word_count_max"), "98")

    def test_propose_candidates_never_touch_config(self):
        self._run(apply_enabled=True)
        # tts.rate is PROPOSE-only -> must remain unchanged.
        self.assertEqual(read_yaml_key(self.cfg, "tts.rate"), "+10%")


if __name__ == "__main__":
    unittest.main()
