"""Unit tests for the analytics_pull enumeration-dedupe fix (2026-06-09).

Regression context: the 2026-06-09 `--all` pull returned xf9HknOP4xs twice
(byte-identical CSV rows) and dropped SVx9vjIp278 entirely. Root cause:
playlistItems.list pagination is not snapshot-consistent — if the uploads
playlist shifts between page fetches, an item can repeat across a page
boundary while another is skipped.

Covers:
- `_list_uploaded_videos` dedupes ids (first occurrence wins) and logs a
  WARNING when a duplicate crosses a page boundary.
- `_log_enumeration_diff` warns on ids that were in the previous pull_date
  (and inside the current lookback window) but missing from this pull, and
  stays quiet for aged-out videos, first pulls, and pure additions.

The YouTube Data API client is faked — no network, no OAuth.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import analytics_pull as ap  # noqa: E402


# --- fakes for the Data API surface _list_uploaded_videos touches -----------------


class _FakeRequest:
    def __init__(self, resp: dict):
        self._resp = resp

    def execute(self, **kwargs) -> dict:
        # Tolerate num_retries= (added to analytics_pull execute calls in the
        # qa-campaign merge) the same way the real googleapiclient request does.
        return self._resp


class _FakeChannels:
    def list(self, part: str, id: str) -> _FakeRequest:
        return _FakeRequest({
            "items": [{
                "contentDetails": {"relatedPlaylists": {"uploads": "UUfake"}}
            }]
        })


class _FakePlaylistItems:
    """Pages keyed by pageToken; the first call (pageToken=None) gets key None."""

    def __init__(self, pages: dict):
        self._pages = pages

    def list(self, playlistId: str, part: str, maxResults: int,
             pageToken: str | None = None) -> _FakeRequest:
        return _FakeRequest(self._pages[pageToken])


class _FakeYtData:
    def __init__(self, pages: dict):
        self._pl = _FakePlaylistItems(pages)

    def channels(self) -> _FakeChannels:
        return _FakeChannels()

    def playlistItems(self) -> _FakePlaylistItems:
        return self._pl


def _pl_item(vid: str, published: str = "2026-06-01T21:25:00Z") -> dict:
    return {"contentDetails": {"videoId": vid, "videoPublishedAt": published}}


def _page(items: list[dict], next_token: str | None = None) -> dict:
    resp: dict = {"items": items}
    if next_token is not None:
        resp["nextPageToken"] = next_token
    return resp


SINCE = date(2026, 1, 1)


# --- _list_uploaded_videos dedupe --------------------------------------------------


def test_duplicate_across_page_boundary_is_deduped_first_wins(caplog):
    """The 2026-06-09 shape: last item of page 1 repeats as first item of
    page 2 after the playlist shifted mid-walk. Exactly one copy survives,
    in first-seen order, and the duplicate is WARNINGed."""
    pages = {
        None: _page([_pl_item("aaa"), _pl_item("xf9HknOP4xs")], next_token="p2"),
        "p2": _page([_pl_item("xf9HknOP4xs"), _pl_item("ccc")]),
    }
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        out = ap._list_uploaded_videos(_FakeYtData(pages), "UCfake", SINCE)

    assert [vid for vid, _ in out] == ["aaa", "xf9HknOP4xs", "ccc"]
    dup_warnings = [r for r in caplog.records
                    if "duplicate video id xf9HknOP4xs" in r.message]
    assert len(dup_warnings) == 1
    assert dup_warnings[0].levelno == logging.WARNING


def test_clean_pages_produce_no_duplicate_warning(caplog):
    pages = {
        None: _page([_pl_item("aaa"), _pl_item("bbb")], next_token="p2"),
        "p2": _page([_pl_item("ccc")]),
    }
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        out = ap._list_uploaded_videos(_FakeYtData(pages), "UCfake", SINCE)

    assert [vid for vid, _ in out] == ["aaa", "bbb", "ccc"]
    assert not [r for r in caplog.records if "duplicate video id" in r.message]


def test_dedupe_still_applies_date_filter(caplog):
    """Dedupe must not weaken the `since` filter — old videos stay excluded."""
    pages = {
        None: _page([
            _pl_item("old", published="2025-12-31T12:00:00Z"),
            _pl_item("new", published="2026-06-01T12:00:00Z"),
        ]),
    }
    out = ap._list_uploaded_videos(_FakeYtData(pages), "UCfake", SINCE)
    assert [vid for vid, _ in out] == ["new"]


# --- _log_enumeration_diff ----------------------------------------------------------


def _metrics(vid: str, pub: date = date(2026, 6, 1)) -> ap.VideoMetrics:
    return ap.VideoMetrics(
        platform="youtube", video_id=vid, title="t", published_at=pub,
        views=0, avg_view_pct=0.0, avg_view_duration_sec=0.0,
        likes=0, shares=0, comments=0, follower_delta=0,
    )


def _csv_row(vid: str, pull_date: str, published_at: str = "2026-06-01") -> dict:
    return {
        "pull_date": pull_date, "platform": "youtube", "video_id": vid,
        "published_at": published_at,
    }


def test_diff_warns_on_dropout(caplog):
    """SVx9vjIp278 was in the 2026-06-08 pull but missing on 2026-06-09 —
    that must produce a WARNING naming the id."""
    existing = [
        _csv_row("xf9HknOP4xs", "2026-06-08"),
        _csv_row("SVx9vjIp278", "2026-06-08"),
    ]
    new = [_metrics("xf9HknOP4xs")]
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        ap._log_enumeration_diff(existing, new, SINCE)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SVx9vjIp278" in warnings[0].message
    assert "dropout" in warnings[0].message


def test_diff_only_compares_latest_pull_date(caplog):
    """An id absent since an OLDER pull must not re-warn on every run —
    only the most recent pull_date is the baseline."""
    existing = [
        _csv_row("gone_long_ago", "2026-06-01"),
        _csv_row("xf9HknOP4xs", "2026-06-08"),
    ]
    new = [_metrics("xf9HknOP4xs")]
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        ap._log_enumeration_diff(existing, new, SINCE)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_diff_ignores_videos_aged_out_of_lookback_window(caplog):
    """--days mode: a video whose published_at predates `since` leaving the
    pull is expected aging-out, not a dropout."""
    existing = [
        _csv_row("aged_out", "2026-06-08", published_at="2026-05-01"),
        _csv_row("in_window", "2026-06-08", published_at="2026-06-05"),
    ]
    new = [_metrics("in_window", pub=date(2026, 6, 5))]
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        ap._log_enumeration_diff(existing, new, since=date(2026, 6, 2))
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_diff_unparseable_published_at_treated_as_in_window(caplog):
    """A malformed historical published_at must not silently exempt the id —
    the loud choice is to keep it in the comparison."""
    existing = [_csv_row("bad_date", "2026-06-08", published_at="not-a-date")]
    new: list[ap.VideoMetrics] = []
    with caplog.at_level(logging.WARNING, logger="analytics_pull"):
        ap._log_enumeration_diff(existing, new, since=date(2026, 6, 2))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bad_date" in warnings[0].message


def test_diff_silent_on_first_pull(caplog):
    with caplog.at_level(logging.INFO, logger="analytics_pull"):
        ap._log_enumeration_diff([], [_metrics("aaa")], SINCE)
    assert not caplog.records


def test_diff_logs_additions_at_info_not_warning(caplog):
    existing = [_csv_row("xf9HknOP4xs", "2026-06-08")]
    new = [_metrics("xf9HknOP4xs"), _metrics("brand_new")]
    with caplog.at_level(logging.INFO, logger="analytics_pull"):
        ap._log_enumeration_diff(existing, new, SINCE)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "brand_new" in infos[0].message
