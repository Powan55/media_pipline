"""Unit tests for learning.csv_health.

Synthetic fixtures only — never reads the live channel data. Runs under both
pytest and `python -m unittest tests.test_csv_health`.

The point of these tests is to lock in the QUOTE-AWARE audit: a properly-quoted
comma-bearing title must NOT be flagged as anomalous (the bug that produced the
mythical "144 corrupt rows" was a naive comma-split), and the positional loader
must read hold_at_3s / traffic_source_shorts_pct correctly against the legacy
12-column header.
"""

from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.csv_health import (  # noqa: E402
    CANONICAL_ANALYTICS_HEADER,
    LEGACY_ANALYTICS_HEADER,
    audit_analytics_csv,
    load_latest_analytics_rows,
)


def _write_csv(path: Path, header: tuple[str, ...], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _row(video_id, *, pull_date, n=15, title="A clean title", views=500,
         avp="50.00", avd="18.00", hold="0.6000", traffic="0.8000"):
    """Build one analytics row of length n (12 | 14 | 15) in canonical order."""
    base = [pull_date, "youtube", video_id, title, "2026-06-19", str(views),
            avp, avd, "10", "0", "1", "2"]
    if n == 12:
        return base
    base += [hold, traffic]
    if n == 14:
        return base
    return base + ["False"]


class TestAuditAnalyticsCsv(unittest.TestCase):
    def test_clean_mixed_eras_is_healthy(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("vid12", pull_date="2026-05-07", n=12),
            _row("vid14", pull_date="2026-05-20", n=14),
            _row("vid15", pull_date="2026-06-19", n=15),
        ])
        rep = audit_analytics_csv(p)
        self.assertTrue(rep.exists)
        self.assertTrue(rep.healthy)
        self.assertEqual(rep.field_count_distribution, {12: 1, 14: 1, 15: 1})
        self.assertFalse(rep.header_is_canonical)  # legacy 12-col on disk

    def test_quoted_comma_title_is_not_flagged(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        # A title with commas, properly quoted by csv.writer -> still 15 fields.
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("vidc", pull_date="2026-06-19", n=15,
                 title="Aider 0.86: three frontier models, one CLI, no fuss"),
        ])
        rep = audit_analytics_csv(p)
        self.assertTrue(rep.healthy)
        self.assertEqual(rep.anomalous_rows, ())
        self.assertEqual(rep.field_count_distribution, {15: 1})

    def test_genuine_anomaly_is_flagged(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("good", pull_date="2026-06-19", n=15),
            _row("bad", pull_date="2026-06-19", n=15) + ["EXTRA", "FIELD"],  # 17 fields
        ])
        rep = audit_analytics_csv(p)
        self.assertFalse(rep.healthy)
        self.assertEqual(len(rep.anomalous_rows), 1)
        line_no, n_fields, vid = rep.anomalous_rows[0]
        self.assertEqual(n_fields, 17)
        self.assertEqual(vid, "bad")

    def test_canonical_header_recognized(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, CANONICAL_ANALYTICS_HEADER, [_row("v", pull_date="2026-06-19", n=15)])
        rep = audit_analytics_csv(p)
        self.assertTrue(rep.header_is_canonical)

    def test_missing_file(self):
        rep = audit_analytics_csv(Path(self.tmp) / "nope.csv")
        self.assertFalse(rep.exists)
        self.assertFalse(rep.healthy)

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


class TestLoadLatestAnalyticsRows(unittest.TestCase):
    def test_positional_read_of_hold_and_traffic(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("vid14", pull_date="2026-05-20", n=14, hold="0.5500", traffic="0.7000"),
            _row("vid15", pull_date="2026-06-19", n=15, hold="0.6100", traffic="0.8200"),
        ])
        rows = load_latest_analytics_rows(p)
        self.assertEqual(rows["vid14"]["hold_at_3s"], "0.5500")
        self.assertEqual(rows["vid14"]["traffic_source_shorts_pct"], "0.7000")
        self.assertEqual(rows["vid15"]["hold_at_3s"], "0.6100")
        self.assertEqual(rows["vid15"]["analytics_error"], "False")

    def test_latest_pull_date_wins(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("v", pull_date="2026-06-01", n=15, views=100),
            _row("v", pull_date="2026-06-20", n=15, views=900),
        ])
        rows = load_latest_analytics_rows(p)
        self.assertEqual(rows["v"]["views"], "900")
        self.assertEqual(rows["v"]["pull_date"], "2026-06-20")

    def test_anomalous_rows_skipped(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("ok", pull_date="2026-06-19", n=15),
            _row("bad", pull_date="2026-06-19", n=15) + ["X", "Y"],  # 17 -> skipped
        ])
        rows = load_latest_analytics_rows(p)
        self.assertIn("ok", rows)
        self.assertNotIn("bad", rows)

    def test_quoted_comma_title_preserved(self):
        p = Path(self.tmp) / "_weekly_analytics.csv"
        _write_csv(p, LEGACY_ANALYTICS_HEADER, [
            _row("v", pull_date="2026-06-19", n=15, title="Models, agents, and you"),
        ])
        rows = load_latest_analytics_rows(p)
        self.assertEqual(rows["v"]["title"], "Models, agents, and you")

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


if __name__ == "__main__":
    unittest.main()
