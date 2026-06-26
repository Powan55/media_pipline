"""Unit tests for learning.sentinels (flops, quality escapes, broken uploads)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.analysis import analyze  # noqa: E402
from learning.ledger import LedgerRow  # noqa: E402
from learning.sentinels import run_sentinels  # noqa: E402


def _row(i, *, views, dur=30.0, anchor=True, avp=50.0, avd=15.0, formula="Cited-Observation Lead",
         cluster="A"):
    return LedgerRow(
        topic_id=f"t{i}", video_id=f"v{i}", title="x", published_at="2026-06-01",
        pull_date="2026-06-20", days_live=10, matured=True, track="ai-vendor", slot=1,
        hook_letter="A", hook_formula=formula, cluster=cluster, weighted_total=0.8,
        counter_conventional_bonus=0.0, ai_vendor_bonus=0.0, named_human_bonus=0.0,
        corporate_deal_damped=False, title_anchor_present=anchor, duration_s=dur,
        views=views, avg_view_pct=avp, avg_view_duration_sec=avd, hold_at_3s=1.0,
        eligible=True, quarantine_reason="",
    )


class TestSentinels(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _report(self, rows):
        return analyze(rows, retention_floor_pct=25.0, min_sample=5)

    def test_flop_diagnosis_identifies_missed_levers(self):
        rows = [_row(i, views=600) for i in range(8)]  # strong cohort, median 600
        # One clear flop missing both anchor and <38s.
        rows.append(_row(99, views=50, dur=45.0, anchor=False, formula="UNTAGGED", cluster="DROP"))
        report = self._report(rows)
        res = run_sentinels(self.root, rows, report, date(2026, 6, 23))
        flops = [i for i in res.incidents if i.type == "content_flop"]
        self.assertEqual(len(flops), 1)
        self.assertEqual(flops[0].topic_id, "t99")
        self.assertIn("no_anchor", res.flop_lever_counts)
        self.assertIn("over_38s", res.flop_lever_counts)

    def test_missing_anchor_is_not_a_quality_escape(self):
        # A well-performing unanchored video is NOT a durable-rule escape (anchor
        # is a soft lever, surfaced via flop diagnosis only when reach is low).
        rows = [_row(i, views=600) for i in range(6)]
        rows.append(_row(7, views=600, anchor=False))
        report = self._report(rows)
        res = run_sentinels(self.root, rows, report, date(2026, 6, 23))
        escapes = [i for i in res.incidents if i.type == "quality_escape"]
        self.assertEqual(escapes, [])

    def test_verify_tag_escape_from_script_file(self):
        rows = [_row(1, views=600)]
        draft = self.root / "02_scripts" / "_drafts" / "t1"
        draft.mkdir(parents=True)
        (draft / "script_FINAL.txt").write_text(
            "Great hook here. [VERIFY: did Anthropic really ship this?] payoff.", encoding="utf-8")
        report = self._report([_row(i, views=600) for i in range(6)] + rows)
        res = run_sentinels(self.root, [_row(i, views=600) for i in range(6)] + rows,
                            report, date(2026, 6, 23))
        self.assertTrue(any("VERIFY" in e.detail for e in res.incidents))

    def test_zero_duration_footgun(self):
        rows = [_row(i, views=600) for i in range(6)]
        rows.append(_row(50, views=500, avp=0.0, avd=0.0))  # views but no watch-time
        report = self._report(rows)
        res = run_sentinels(self.root, rows, report, date(2026, 6, 23))
        broken = [i for i in res.incidents if i.type == "engineering"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].topic_id, "t50")

    def test_write_persists_incidents(self):
        rows = [_row(i, views=600) for i in range(6)]
        rows.append(_row(99, views=50, anchor=False))
        report = self._report(rows)
        run_sentinels(self.root, rows, report, date(2026, 6, 23), write=True)
        inc_file = self.root / "01_research" / "_learning" / "incidents.jsonl"
        self.assertTrue(inc_file.exists())
        self.assertGreater(len(inc_file.read_text(encoding="utf-8").strip().splitlines()), 0)


if __name__ == "__main__":
    unittest.main()
