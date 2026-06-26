"""Error-surfacing tests for analytics_pull.pull_youtube_analytics (M5).

Before M5 a per-video Analytics query failure was swallowed to ``an_resp = {}``,
so that video's CSV row was written with ZERO metrics — indistinguishable from a
video that genuinely had zero views. M5 keeps the swallow (one bad video must not
kill the whole pull) but (a) tags the affected VideoMetrics with
``analytics_error=True`` and (b) emits a loud run-level ``log.error`` summary
counting the failures.

These tests drive pull_youtube_analytics with fake Data/Analytics services
(monkeypatching ``analytics_pull.build``) so no network is touched.

Run:
    python -m pytest tests/test_analytics_pull_error_surfacing.py -v
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import analytics_pull  # noqa: E402


class _Req:
    """A request whose .execute returns a fixed payload (or raises)."""

    def __init__(self, result=None, *, exc: Exception | None = None):
        self._result = result if result is not None else {}
        self._exc = exc

    def execute(self, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result


def _make_fake_services(*, fail_video_id: str):
    """Build (yt_data, yt_an) fakes for two fixed videos (vidGOOD, vidBAD); the
    main per-video analytics query raises ONLY for `fail_video_id` (pass a
    sentinel that matches neither, e.g. '__none__', for the all-succeed case)."""
    good_id, bad_id = "vidGOOD", "vidBAD"

    # --- Data API (yt_data) -------------------------------------------------
    yt_data = mock.MagicMock()
    # channels().list().execute() -> uploads playlist id
    yt_data.channels.return_value.list.return_value = _Req(
        {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UP1"}}}]}
    )
    # playlistItems().list().execute() -> one page with both videos, no nextToken
    yt_data.playlistItems.return_value.list.return_value = _Req(
        {
            "items": [
                {"contentDetails": {"videoId": good_id, "videoPublishedAt": "2026-05-10T00:00:00Z"}},
                {"contentDetails": {"videoId": bad_id, "videoPublishedAt": "2026-05-11T00:00:00Z"}},
            ]
        }
    )
    # videos().list().execute() -> snippet/statistics/contentDetails for both
    yt_data.videos.return_value.list.return_value = _Req(
        {
            "items": [
                {
                    "id": good_id,
                    "snippet": {"title": "Good video", "publishedAt": "2026-05-10T00:00:00Z"},
                    "statistics": {"viewCount": "500", "likeCount": "10", "commentCount": "2"},
                    "contentDetails": {"duration": "PT45S"},
                },
                {
                    "id": bad_id,
                    "snippet": {"title": "Bad video", "publishedAt": "2026-05-11T00:00:00Z"},
                    "statistics": {"viewCount": "0", "likeCount": "0", "commentCount": "0"},
                    "contentDetails": {"duration": "PT40S"},
                },
            ]
        }
    )

    # --- Analytics API (yt_an) ---------------------------------------------
    yt_an = mock.MagicMock()

    def _query(**kwargs):
        # The main per-video metrics query keys off `filters=video==<id>` and the
        # `averageViewPercentage` metric; the hold/traffic sub-queries use other
        # metrics and just return empty-but-valid payloads.
        filt = kwargs.get("filters", "")
        metrics = str(kwargs.get("metrics", ""))
        is_main_metrics = "averageViewPercentage" in metrics
        if is_main_metrics and f"video=={fail_video_id}" in filt:
            return _Req(exc=RuntimeError("503 backend error"))
        if is_main_metrics:
            # Any non-failing video gets a full, valid metrics payload.
            return _Req(
                {
                    "columnHeaders": [
                        {"name": "views"}, {"name": "likes"}, {"name": "comments"},
                        {"name": "shares"}, {"name": "estimatedMinutesWatched"},
                        {"name": "averageViewDuration"}, {"name": "averageViewPercentage"},
                        {"name": "subscribersGained"},
                    ],
                    "rows": [[500, 10, 2, 1, 30, 20, 80.0, 3]],
                }
            )
        return _Req({"rows": []})

    yt_an.reports.return_value.query.side_effect = _query
    return yt_data, yt_an


def test_failed_video_query_is_flagged_and_summarized(caplog) -> None:
    """The video whose analytics query raises is flagged analytics_error=True;
    the good video is not; and a run-level log.error summary fires."""
    yt_data, yt_an = _make_fake_services(fail_video_id="vidBAD")

    def _fake_build(serviceName, version, **_kw):
        return yt_data if serviceName == "youtube" else yt_an

    creds = mock.MagicMock()
    with mock.patch.object(analytics_pull, "build", _fake_build):
        with caplog.at_level(logging.ERROR, logger="analytics_pull"):
            rows = analytics_pull.pull_youtube_analytics(creds, "CHAN", date(2026, 1, 1))

    by_id = {r.video_id: r for r in rows}
    assert by_id["vidBAD"].analytics_error is True
    assert by_id["vidGOOD"].analytics_error is False
    # The good row still carries its real metrics.
    assert by_id["vidGOOD"].views == 500

    # Run-level summary fired at ERROR and names the failure count.
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("analytics unavailable for 1/2 videos" in m for m in errors), errors


def test_no_error_summary_when_all_succeed(caplog) -> None:
    """When no video query fails, no run-level error summary is emitted and no
    row is flagged."""
    yt_data, yt_an = _make_fake_services(fail_video_id="__none__")  # nothing matches

    def _fake_build(serviceName, version, **_kw):
        return yt_data if serviceName == "youtube" else yt_an

    creds = mock.MagicMock()
    with mock.patch.object(analytics_pull, "build", _fake_build):
        with caplog.at_level(logging.ERROR, logger="analytics_pull"):
            rows = analytics_pull.pull_youtube_analytics(creds, "CHAN", date(2026, 1, 1))

    assert all(r.analytics_error is False for r in rows)
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert not any("analytics unavailable" in m for m in errors), errors


def test_csv_round_trips_analytics_error_column(tmp_path) -> None:
    """merge_into_tracker emits the additive analytics_error column and the
    existing column order is preserved (backward-compatible)."""
    from analytics_pull import VideoMetrics, merge_into_tracker

    out = tmp_path / "_weekly_analytics.csv"
    rows = [
        VideoMetrics(
            platform="youtube", video_id="vidA", title="A", published_at=date(2026, 5, 10),
            views=500, avg_view_pct=80.0, avg_view_duration_sec=20.0, likes=10, shares=1,
            comments=2, follower_delta=3, hold_at_3s=0.75, traffic_source_shorts_pct=0.9,
            analytics_error=False,
        ),
        VideoMetrics(
            platform="youtube", video_id="vidB", title="B", published_at=date(2026, 5, 11),
            views=0, avg_view_pct=0.0, avg_view_duration_sec=0.0, likes=0, shares=0,
            comments=0, follower_delta=0, analytics_error=True,
        ),
    ]
    merge_into_tracker(rows, out)
    header = out.read_text(encoding="utf-8").splitlines()[0]
    # New column appended at the END (append-only contract).
    assert header.split(",")[-1] == "analytics_error"
    # Pre-existing leading columns unchanged.
    assert header.startswith("pull_date,platform,video_id,title,published_at,views,")

    import csv as _csv
    with out.open(encoding="utf-8", newline="") as f:
        read = {r["video_id"]: r for r in _csv.DictReader(f)}
    assert read["vidA"]["analytics_error"] == "False"
    assert read["vidB"]["analytics_error"] == "True"
