"""Unit tests for analytics_join.py.

All tests build synthetic JSONL + CSV fixtures inside `tmp_path` — no test
ever reads the live channel data. Runnable under both `pytest` and the
stdlib `unittest` discovery (`python -m unittest tests.test_analytics_join`).

The Slice 1 schema is treated as locked: tests write JSONL bytes directly
rather than importing `hook_selection_log` (which is being built in
parallel).
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analytics_join import (  # noqa: E402
    FORMULA_UNTAGGED,
    REASON_LOW_VIEWS,
    REASON_NOT_UPLOADED,
    REASON_NO_ANALYTICS_ROW,
    REASON_NO_HOLD_DATA,
    HookPerformanceRow,
    join_hooks_to_analytics,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


# Header used by the live CSV before the additive columns landed (2026-05-08).
# Tests use this *legacy* header and append `hold_at_3s` /
# `traffic_source_shorts_pct` positionally so we exercise the back-compat
# path the joiner promises to handle.
_LEGACY_HEADER = (
    "pull_date,platform,video_id,title,published_at,views,avg_view_pct,"
    "avg_view_duration_sec,likes,shares,comments,follower_delta"
)


def _write_hook_log(path: Path, rows: list[dict]) -> None:
    """Write a JSONL file mirroring the locked Slice 1 schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_upload_log(path: Path, rows: list[dict]) -> None:
    """Write upload_log.csv with the schema the live file uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = ["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"]
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(",".join(headers) + "\n")
        for row in rows:
            cells = [str(row.get(h, "")) for h in headers]
            f.write(",".join(cells) + "\n")


def _write_analytics(
    path: Path,
    rows: list[list[str]],
    *,
    header: str = _LEGACY_HEADER,
) -> None:
    """Write _weekly_analytics.csv. Each row is a list of pre-formatted cells.

    Default header is the legacy 12-column one — pass a 14-column header to
    test the new-header path explicitly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        for row in rows:
            f.write(",".join(row) + "\n")


def _hook_row(
    topic_id: str,
    *,
    letter: str = "A",
    text: str = "Hook text here",
    formula: str = "Named-Actor",
) -> dict:
    return {
        "topic_id": topic_id,
        "hook_letter": letter,
        "hook_text": text,
        "formula": formula,
        "all_three_hooks": [
            {"letter": "A", "text": "alt A", "formula": "Named-Actor"},
            {"letter": "B", "text": "alt B", "formula": "Number-Lead"},
            {"letter": "C", "text": "alt C", "formula": "Contradiction"},
        ],
        "logged_at": "2026-05-12T19:30:00+00:00",
    }


def _analytics_row(
    *,
    pull_date: str,
    video_id: str,
    published_at: str,
    views: int,
    avg_view_pct: float = 50.0,
    hold_at_3s: str = "0.8000",
    shorts_pct: str = "0.9000",
    title: str = "title",
) -> list[str]:
    """Build a 14-column legacy-header analytics row with appended additive cols."""
    return [
        pull_date, "youtube", video_id, title, published_at, str(views),
        f"{avg_view_pct:.2f}", "20.00", "1", "0", "0", "0",
        hold_at_3s, shorts_pct,
    ]


def _build_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Return (channel_root, hook_log, upload_log, analytics_csv) paths."""
    channel_root = tmp_path / "channel"
    research = channel_root / "01_research"
    return (
        channel_root,
        research / "hook_selection_log.jsonl",
        research / "upload_log.csv",
        research / "_weekly_analytics.csv",
    )


def _row_by_topic(rows: list[HookPerformanceRow]) -> dict[str, HookPerformanceRow]:
    return {r.topic_id: r for r in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class HappyPathTests(unittest.TestCase):
    """Single uploaded video with full analytics — eligible for leaderboard."""

    def test_happy_path_eligible(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "2026-05-10T00:00:00+00:00",
                 "topic_id": "2026-05-10_001", "video_id": "vidAAA",
                 "url": "https://youtu.be/vidAAA", "privacy": "public",
                 "title": "title"},
            ])
            _write_analytics(analytics, [
                _analytics_row(
                    pull_date="2026-05-12", video_id="vidAAA",
                    published_at="2026-05-10", views=500,
                    hold_at_3s="0.7500", avg_view_pct=80.0,
                ),
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r.topic_id, "2026-05-10_001")
            self.assertEqual(r.video_id, "vidAAA")
            self.assertEqual(r.hook_letter, "A")
            self.assertEqual(r.formula, "Named-Actor")
            self.assertEqual(r.views, 500)
            self.assertAlmostEqual(r.hold_at_3s, 0.75, places=4)
            self.assertAlmostEqual(r.avg_view_pct, 80.0, places=2)
            self.assertEqual(r.days_live, 2)
            self.assertTrue(r.eligible_for_leaderboard)
            self.assertIsNone(r.reason)


class LatestPullDateWinsTests(unittest.TestCase):
    """Multiple analytics rows per video_id — the latest pull_date wins."""

    def test_latest_pull_date_wins(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "x", "topic_id": "2026-05-10_001",
                 "video_id": "vidAAA", "url": "u", "privacy": "public",
                 "title": "t"},
            ])
            _write_analytics(analytics, [
                # Out of date order on purpose — picker must use max() not last.
                _analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                               published_at="2026-05-10", views=900,
                               hold_at_3s="0.8500"),
                _analytics_row(pull_date="2026-05-07", video_id="vidAAA",
                               published_at="2026-05-10", views=10,
                               hold_at_3s="0.1000"),
                _analytics_row(pull_date="2026-05-10", video_id="vidAAA",
                               published_at="2026-05-10", views=100,
                               hold_at_3s="0.5000"),
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 13))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].views, 900)
            self.assertAlmostEqual(rows[0].hold_at_3s, 0.85, places=4)


class EligibilityThresholdTests(unittest.TestCase):
    """`eligibility_min_views` is configurable; default is 70."""

    def _setup(self, tmp_path: Path, views: int) -> Path:
        channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
        _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
        _write_upload_log(upload_log, [
            {"uploaded_at": "x", "topic_id": "2026-05-10_001",
             "video_id": "vidAAA", "url": "u", "privacy": "public",
             "title": "t"},
        ])
        _write_analytics(analytics, [
            _analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                           published_at="2026-05-10", views=views,
                           hold_at_3s="0.5000"),
        ])
        return channel

    def test_default_threshold_is_70(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            channel = self._setup(Path(td), views=70)
            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))
            self.assertTrue(rows[0].eligible_for_leaderboard)

        with tempfile.TemporaryDirectory() as td:
            channel = self._setup(Path(td), views=69)
            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))
            self.assertFalse(rows[0].eligible_for_leaderboard)
            self.assertEqual(rows[0].reason, REASON_LOW_VIEWS)

    def test_threshold_override(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            channel = self._setup(Path(td), views=200)
            # Bump threshold above the row's views.
            rows = join_hooks_to_analytics(
                channel, today=date(2026, 5, 12),
                eligibility_min_views=500,
            )
            self.assertFalse(rows[0].eligible_for_leaderboard)
            self.assertEqual(rows[0].reason, REASON_LOW_VIEWS)


class IneligibleNoHoldDataTests(unittest.TestCase):
    """View count is fine but hold_at_3s is empty -> no_hold_data."""

    def test_no_hold_data_reason(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "x", "topic_id": "2026-05-10_001",
                 "video_id": "vidAAA", "url": "u", "privacy": "public",
                 "title": "t"},
            ])
            _write_analytics(analytics, [
                # hold_at_3s deliberately empty.
                _analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                               published_at="2026-05-10", views=500,
                               hold_at_3s="", shorts_pct="0.5000"),
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].eligible_for_leaderboard)
            self.assertEqual(rows[0].reason, REASON_NO_HOLD_DATA)
            self.assertIsNone(rows[0].hold_at_3s)
            self.assertEqual(rows[0].views, 500)


class NeverUploadedTests(unittest.TestCase):
    """Hook logged but topic never made it to upload_log -> not_uploaded."""

    def test_never_uploaded_blanks_metrics(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001",
                                                  letter="B",
                                                  formula="Number-Lead")])
            _write_upload_log(upload_log, [])  # empty
            _write_analytics(analytics, [])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertIsNone(r.video_id)
            self.assertIsNone(r.views)
            self.assertIsNone(r.hold_at_3s)
            self.assertIsNone(r.avg_view_pct)
            self.assertIsNone(r.days_live)
            self.assertFalse(r.eligible_for_leaderboard)
            self.assertEqual(r.reason, REASON_NOT_UPLOADED)
            # Hook fields still populated.
            self.assertEqual(r.hook_letter, "B")
            self.assertEqual(r.formula, "Number-Lead")


class UploadedPrivateNoAnalyticsTests(unittest.TestCase):
    """Uploaded but never appears in analytics CSV -> no_analytics_row."""

    def test_uploaded_no_analytics(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-06_003")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "2026-05-08T02:22:12+00:00",
                 "topic_id": "2026-05-06_003", "video_id": "KxKMpoAG8V8",
                 "url": "u", "privacy": "private", "title": "t"},
            ])
            _write_analytics(analytics, [])  # private uploads aren't pulled

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r.video_id, "KxKMpoAG8V8")
            self.assertIsNone(r.views)
            self.assertIsNone(r.hold_at_3s)
            self.assertIsNone(r.avg_view_pct)
            self.assertIsNone(r.days_live)
            self.assertFalse(r.eligible_for_leaderboard)
            self.assertEqual(r.reason, REASON_NO_ANALYTICS_ROW)


class MultiTopicJoinTests(unittest.TestCase):
    """Mix of states across topics — eligible, low views, never uploaded."""

    def test_multi_topic(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [
                _hook_row("2026-05-08_001", letter="A", formula="Named-Actor"),
                _hook_row("2026-05-09_001", letter="B", formula="Number-Lead"),
                _hook_row("2026-05-10_001", letter="C", formula="Contradiction"),
                _hook_row("2026-05-11_001", letter="A", formula="Named-Actor"),
            ])
            _write_upload_log(upload_log, [
                # 2026-05-08_001 -> uploaded, eligible
                {"uploaded_at": "x", "topic_id": "2026-05-08_001",
                 "video_id": "vidAAA", "url": "u", "privacy": "public", "title": "t"},
                # 2026-05-09_001 -> uploaded, low views
                {"uploaded_at": "x", "topic_id": "2026-05-09_001",
                 "video_id": "vidBBB", "url": "u", "privacy": "public", "title": "t"},
                # 2026-05-10_001 -> uploaded private, no analytics
                {"uploaded_at": "x", "topic_id": "2026-05-10_001",
                 "video_id": "vidCCC", "url": "u", "privacy": "private", "title": "t"},
                # 2026-05-11_001 -> never uploaded
            ])
            _write_analytics(analytics, [
                _analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                               published_at="2026-05-08", views=1000,
                               hold_at_3s="0.9000"),
                _analytics_row(pull_date="2026-05-12", video_id="vidBBB",
                               published_at="2026-05-09", views=10,
                               hold_at_3s="0.5000"),
                # vidCCC absent on purpose.
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 4)
            by_topic = _row_by_topic(rows)

            self.assertTrue(by_topic["2026-05-08_001"].eligible_for_leaderboard)
            self.assertEqual(by_topic["2026-05-08_001"].days_live, 4)

            self.assertFalse(by_topic["2026-05-09_001"].eligible_for_leaderboard)
            self.assertEqual(by_topic["2026-05-09_001"].reason, REASON_LOW_VIEWS)

            self.assertEqual(by_topic["2026-05-10_001"].reason, REASON_NO_ANALYTICS_ROW)
            self.assertEqual(by_topic["2026-05-10_001"].video_id, "vidCCC")

            self.assertEqual(by_topic["2026-05-11_001"].reason, REASON_NOT_UPLOADED)
            self.assertIsNone(by_topic["2026-05-11_001"].video_id)


class TodayInjectionTests(unittest.TestCase):
    """`today` override controls days_live so tests aren't time-flaky."""

    def test_days_live_uses_injected_today(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-01_001")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "x", "topic_id": "2026-05-01_001",
                 "video_id": "vidZZZ", "url": "u", "privacy": "public", "title": "t"},
            ])
            _write_analytics(analytics, [
                _analytics_row(pull_date="2026-05-15", video_id="vidZZZ",
                               published_at="2026-05-01", views=200,
                               hold_at_3s="0.6000"),
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 20))
            self.assertEqual(rows[0].days_live, 19)

            rows = join_hooks_to_analytics(channel, today=date(2026, 6, 1))
            self.assertEqual(rows[0].days_live, 31)


class OrphanUploadIgnoredTests(unittest.TestCase):
    """Topic in upload_log but not in JSONL -> no row (Slice 6/8 owns it)."""

    def test_orphan_upload_skipped(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            # Only one topic in JSONL.
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
            # Upload log mentions a *second* topic too.
            _write_upload_log(upload_log, [
                {"uploaded_at": "x", "topic_id": "2026-05-10_001",
                 "video_id": "vidAAA", "url": "u", "privacy": "public", "title": "t"},
                {"uploaded_at": "x", "topic_id": "2026-04-30_999",
                 "video_id": "vidORPHAN", "url": "u", "privacy": "public", "title": "t"},
            ])
            _write_analytics(analytics, [
                _analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                               published_at="2026-05-10", views=200,
                               hold_at_3s="0.6000"),
                _analytics_row(pull_date="2026-05-12", video_id="vidORPHAN",
                               published_at="2026-04-30", views=5000,
                               hold_at_3s="0.9000"),
            ])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].topic_id, "2026-05-10_001")


class NewHeaderCsvTests(unittest.TestCase):
    """When CSV header *includes* the additive columns, don't double-count."""

    def test_new_14col_header(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            _write_hook_log(hook_log, [_hook_row("2026-05-10_001")])
            _write_upload_log(upload_log, [
                {"uploaded_at": "x", "topic_id": "2026-05-10_001",
                 "video_id": "vidAAA", "url": "u", "privacy": "public", "title": "t"},
            ])
            new_header = (
                "pull_date,platform,video_id,title,published_at,views,"
                "avg_view_pct,avg_view_duration_sec,likes,shares,comments,"
                "follower_delta,hold_at_3s,traffic_source_shorts_pct"
            )
            _write_analytics(
                analytics,
                [_analytics_row(pull_date="2026-05-12", video_id="vidAAA",
                                published_at="2026-05-10", views=300,
                                hold_at_3s="0.4200")],
                header=new_header,
            )

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0].hold_at_3s, 0.42, places=4)
            self.assertEqual(rows[0].views, 300)


class MissingFilesTests(unittest.TestCase):
    """All sources missing -> empty list, not an exception."""

    def test_missing_jsonl_returns_empty(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel = tmp_path / "channel"
            (channel / "01_research").mkdir(parents=True)
            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))
            self.assertEqual(rows, [])


class FormulaFallbackTests(unittest.TestCase):
    """Missing formula in JSONL falls back to FORMULA_UNTAGGED."""

    def test_missing_formula_falls_back(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            channel, hook_log, upload_log, analytics = _build_paths(tmp_path)
            row = _hook_row("2026-05-10_001")
            del row["formula"]
            _write_hook_log(hook_log, [row])
            _write_upload_log(upload_log, [])
            _write_analytics(analytics, [])

            rows = join_hooks_to_analytics(channel, today=date(2026, 5, 12))
            self.assertEqual(rows[0].formula, FORMULA_UNTAGGED)


if __name__ == "__main__":
    unittest.main()
