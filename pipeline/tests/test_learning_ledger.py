"""Unit tests for learning.ledger.build_learning_ledger.

Builds a complete synthetic channel_root in tmp_path (hook log, upload log,
analytics CSV with the legacy 12-col header + 15-field rows, daily picks +
RANKED, quality log, clusters) and asserts the join, the derived features, and
the per-row quarantine reasons. Never reads live data.

Runs under pytest and `python -m unittest tests.test_learning_ledger`.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.ledger import (  # noqa: E402
    LEDGER_COLUMNS,
    build_learning_ledger,
    ledger_row_to_dict,
    write_ledger_csv,
)
from learning.telemetry import append_script_quality  # noqa: E402

_LEGACY_HEADER = (
    "pull_date", "platform", "video_id", "title", "published_at", "views",
    "avg_view_pct", "avg_view_duration_sec", "likes", "shares", "comments",
    "follower_delta",
)

TODAY = date(2026, 6, 23)


def _research(root: Path) -> Path:
    d = root / "01_research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_hook_log(root: Path, rows: list[dict]) -> None:
    p = _research(root) / "hook_selection_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_upload_log(root: Path, rows: list[dict]) -> None:
    p = _research(root) / "upload_log.csv"
    headers = ["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in headers})


def _write_analytics(root: Path, rows: list[list]) -> None:
    """Write the analytics CSV with the LEGACY 12-col header but full 15-field
    rows (the real-world drift the positional loader must handle)."""
    p = _research(root) / "_weekly_analytics.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(_LEGACY_HEADER)
        for r in rows:
            w.writerow(r)


def _arow(video_id, *, published, views, avp, avd, hold, traffic, title="t",
          pull_date="2026-06-21"):
    return [pull_date, "youtube", video_id, title, published, str(views),
            f"{avp:.2f}", f"{avd:.2f}", "10", "0", "1", "2",
            f"{hold:.4f}", f"{traffic:.4f}", "False"]


def _write_daily(root: Path, datestr: str, assignments: list[dict],
                 ranked: list[dict]) -> None:
    d = root / "02_scripts" / "_drafts" / f"_daily_{datestr}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "picks_assignment.json").write_text(
        json.dumps({"date": datestr, "assignments": assignments}), encoding="utf-8")
    (d / "idea_generation_RANKED.json").write_text(
        json.dumps({"ranked": ranked}), encoding="utf-8")


def _write_clusters(root: Path, rows: list[dict]) -> None:
    p = _research(root) / "_video_clusters.csv"
    headers = ["topic_id", "video_id", "title", "cluster", "hook_formula"]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in headers})


class TestBuildLearningLedger(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

        # Universe: 4 topics.
        _write_hook_log(self.root, [
            {"topic_id": "2026-06-19_001", "hook_letter": "A",
             "hook_text": "Claude could quietly delete your work",
             "formula": "Cited-Observation Lead", "all_three_hooks": []},
            {"topic_id": "2026-06-22_001", "hook_letter": "B",
             "hook_text": "OpenAI ships something", "formula": "Result-First", "all_three_hooks": []},
            {"topic_id": "2026-06-05_001", "hook_letter": "A",
             "hook_text": "Old low view video", "formula": "Authority Flip", "all_three_hooks": []},
            {"topic_id": "2026-06-18_001", "hook_letter": "C",
             "hook_text": "Never uploaded", "formula": "Result-First", "all_three_hooks": []},
        ])
        _write_upload_log(self.root, [
            {"topic_id": "2026-06-19_001", "video_id": "vidA",
             "privacy": "scheduled (publishAt=2026-06-23T21:25:00Z)",
             "title": "Claude could quietly delete your work"},
            {"topic_id": "2026-06-22_001", "video_id": "vidB", "privacy": "public",
             "title": "OpenAI ships something"},
            {"topic_id": "2026-06-05_001", "video_id": "vidC",
             "privacy": "scheduled (publishAt=2026-06-05T22:35:00Z)", "title": "Old low view video"},
            # 2026-06-18_001 intentionally absent -> not_uploaded
        ])
        _write_analytics(self.root, [
            _arow("vidA", published="2026-06-19", views=500, avp=50.0, avd=18.0,
                  hold=0.60, traffic=0.80, title="Claude could quietly delete your work"),
            _arow("vidB", published="2026-06-22", views=200, avp=40.0, avd=12.0,
                  hold=0.50, traffic=0.60, title="OpenAI ships something"),
            _arow("vidC", published="2026-06-05", views=30, avp=20.0, avd=6.0,
                  hold=0.30, traffic=0.40, title="Old low view video"),
        ])
        _write_daily(self.root, "2026-06-19",
                     assignments=[{"topic": "T1 Claude autonomy brake", "topic_id": "2026-06-19_001"}],
                     ranked=[{"topic": "T1 Claude autonomy brake", "weighted_total": 0.8986,
                              "counter_conventional_bonus": 0.0, "ai_vendor_bonus": 0.05,
                              "named_human_bonus": 0.0, "corporate_deal_damped": False}])
        _write_clusters(self.root, [
            {"topic_id": "2026-06-19_001", "video_id": "vidA", "cluster": "A",
             "hook_formula": "Cited-Observation Lead"},
        ])
        append_script_quality(self.root, topic_id="2026-06-19_001",
                              dims={"hook_strength": 0.9, "specificity": 0.8,
                                    "cited_observation_quality": 0.85},
                              weighted_total=0.85, now_iso="2026-06-19T03:00:00+00:00")

        self.rows = {r.topic_id: r for r in build_learning_ledger(self.root, today=TODAY)}

    def tearDown(self):
        self._td.cleanup()

    def test_universe_is_all_four_topics(self):
        self.assertEqual(set(self.rows), {
            "2026-06-19_001", "2026-06-22_001", "2026-06-05_001", "2026-06-18_001"})

    def test_eligible_row_fully_joined(self):
        r = self.rows["2026-06-19_001"]
        self.assertTrue(r.eligible)
        self.assertEqual(r.quarantine_reason, "")
        self.assertEqual(r.hook_formula, "Cited-Observation Lead")
        self.assertEqual(r.cluster, "A")
        self.assertEqual(r.weighted_total, 0.8986)
        self.assertEqual(r.ai_vendor_bonus, 0.05)
        self.assertEqual(r.track, "ai-vendor")
        self.assertEqual(r.slot, 1)
        self.assertEqual(r.views, 500)
        self.assertAlmostEqual(r.hold_at_3s, 0.60)
        self.assertAlmostEqual(r.traffic_source_shorts_pct, 0.80)
        self.assertAlmostEqual(r.duration_s, 36.0)  # 18 * 100 / 50
        self.assertTrue(r.title_anchor_present)
        self.assertEqual(r.quality_dims.get("hook_strength"), 0.9)
        self.assertEqual(r.q_weighted_total, 0.85)
        self.assertTrue(r.matured)
        self.assertEqual(r.days_live, 4)

    def test_immature_row_quarantined(self):
        r = self.rows["2026-06-22_001"]
        self.assertEqual(r.quarantine_reason, "immature")
        self.assertFalse(r.eligible)
        self.assertFalse(r.matured)

    def test_low_views_row_quarantined(self):
        r = self.rows["2026-06-05_001"]
        self.assertEqual(r.quarantine_reason, "low_views")
        self.assertFalse(r.eligible)
        self.assertEqual(r.slot, 2)  # publishAt minute :35

    def test_not_uploaded_row_quarantined(self):
        r = self.rows["2026-06-18_001"]
        self.assertEqual(r.quarantine_reason, "not_uploaded")
        self.assertIsNone(r.video_id)

    def test_write_ledger_csv_shape(self):
        out = write_ledger_csv(list(self.rows.values()), self.root / "ledger.csv")
        with out.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            data = list(reader)
        self.assertEqual(tuple(header), LEDGER_COLUMNS)
        self.assertIn("q_hook_strength", header)
        self.assertEqual(len(data), 4)

    def test_row_to_dict_blanks_none(self):
        r = self.rows["2026-06-18_001"]  # not uploaded -> many None fields
        d = ledger_row_to_dict(r)
        self.assertEqual(d["video_id"], "")
        self.assertEqual(d["views"], "")
        self.assertEqual(d["quarantine_reason"], "not_uploaded")
        self.assertEqual(d["matured"], "False")


if __name__ == "__main__":
    unittest.main()
