"""Tests for trend_pull dual-track support (2026-06-21).

Covers GENERAL_TECH_KEYWORDS HN filtering, the news-RSS queue ingestion, and the
track-branched pull_all (general-tech skips the dev-AI GitHub/Cursor sources and
reads the news-RSS queue; the artifact is track-suffixed; the ai-vendor track is
byte-identical and never reads the queue). No network: source fns are
monkeypatched and the queue is a tmp_path file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure the pipeline repo root is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import trend_pull as tp  # noqa: E402
from trend_pull import (  # noqa: E402
    GENERAL_TECH_KEYWORDS,
    TrendCandidate,
    pull_all,
    pull_news_rss_queue,
)


def _drop(url: str, *, track: str, title: str = "A tech thing", feed: str = "theverge") -> dict:
    return {
        "id": url,
        "feed": feed,
        "title": title,
        "url": url,
        "published_at": "2026-06-21T00:00:00+00:00",
        "summary": "x",
        "added_to_queue_at": "2026-06-21T00:00:00+00:00",
        "track": track,
    }


def _queue_file(tmp_path: Path, drops: list[dict]) -> Path:
    p = tmp_path / tp.DEFAULT_NEWS_QUEUE_NAME
    p.write_text(json.dumps(drops), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# pull_news_rss_queue
# ---------------------------------------------------------------------------


class TestPullNewsRssQueue:
    def test_missing_file_returns_empty(self, tmp_path):
        assert pull_news_rss_queue(tmp_path / "nope.json", track="general-tech") == []

    def test_filters_by_track(self, tmp_path):
        qp = _queue_file(tmp_path, [
            _drop("https://a/1", track="general-tech", title="New iPhone"),
            _drop("https://a/2", track="ai-vendor", title="Claude update", feed="anthropic"),
        ])
        gt = pull_news_rss_queue(qp, track="general-tech")
        assert [c.url for c in gt] == ["https://a/1"]
        assert gt[0].source == "news_rss:theverge"
        assert gt[0].extra["track"] == "general-tech"
        av = pull_news_rss_queue(qp, track="ai-vendor")
        assert [c.url for c in av] == ["https://a/2"]

    def test_missing_track_field_defaults_ai_vendor(self, tmp_path):
        drop = _drop("https://a/3", track="general-tech")
        del drop["track"]
        qp = _queue_file(tmp_path, [drop])
        # A drop with no track is treated as ai-vendor (back-compat with pre-field rows).
        assert len(pull_news_rss_queue(qp, track="ai-vendor")) == 1
        assert pull_news_rss_queue(qp, track="general-tech") == []

    def test_malformed_file_returns_empty(self, tmp_path):
        p = tmp_path / tp.DEFAULT_NEWS_QUEUE_NAME
        p.write_text("{not json", encoding="utf-8")
        assert pull_news_rss_queue(p, track="general-tech") == []

    def test_skips_entries_missing_url_or_title(self, tmp_path):
        qp = _queue_file(tmp_path, [_drop("", track="general-tech")])  # empty url
        assert pull_news_rss_queue(qp, track="general-tech") == []


# ---------------------------------------------------------------------------
# pull_hacker_news with general-tech keywords
# ---------------------------------------------------------------------------


def test_hacker_news_general_tech_keywords(monkeypatch):
    top_ids = [1, 2, 3]
    stories = {
        1: {"title": "Apple unveils new iPhone camera", "time": 1, "score": 200, "by": "x"},
        2: {"title": "A new JavaScript framework drops", "time": 1, "score": 50, "by": "y"},
        3: {"title": "Tesla robotaxi hits the road", "time": 1, "score": 300, "by": "z"},
    }

    def fake_get(url, *, accept="text/html,application/json"):
        if "topstories" in url:
            return SimpleNamespace(json=lambda: top_ids)
        sid = int(url.rsplit("/", 1)[1].split(".")[0])
        return SimpleNamespace(json=lambda: stories[sid])

    monkeypatch.setattr(tp, "_http_get", fake_get)
    titles = [c.title for c in tp.pull_hacker_news(keywords=GENERAL_TECH_KEYWORDS)]
    assert "Apple unveils new iPhone camera" in titles
    assert "Tesla robotaxi hits the road" in titles
    assert "A new JavaScript framework drops" not in titles  # no general-tech keyword


# ---------------------------------------------------------------------------
# pull_all track branching
# ---------------------------------------------------------------------------


def test_pull_all_general_tech_uses_hn_and_queue_not_github(tmp_path, monkeypatch):
    def boom_github(repo, tag):
        raise AssertionError("github must not run on general-tech track")

    def boom_cursor():
        raise AssertionError("cursor must not run on general-tech track")

    monkeypatch.setattr(tp, "pull_github_releases", boom_github)
    monkeypatch.setattr(tp, "pull_cursor_changelog", boom_cursor)
    monkeypatch.setattr(tp, "pull_hacker_news", lambda **kw: [TrendCandidate(
        source="hn", url="https://hn/1", title="iPhone thing", summary="",
        surfaced_at="2026-06-21T00:00:00+00:00", published_at=None, score=10.0, tag="iphone",
    )])
    _queue_file(tmp_path, [
        _drop("https://a/gt", track="general-tech", title="Meta glasses"),
        _drop("https://a/av", track="ai-vendor", title="Claude", feed="anthropic"),
    ])

    _out, candidates = pull_all(tmp_path, dry_run=True, track="general-tech")
    urls = {c.url for c in candidates}
    assert "https://hn/1" in urls            # HN general-tech candidate
    assert "https://a/gt" in urls            # general-tech queue drop
    assert "https://a/av" not in urls        # ai-vendor queue drop filtered out


def test_general_tech_artifact_is_track_suffixed(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "pull_hacker_news", lambda **kw: [])
    out_path, _ = pull_all(tmp_path, track="general-tech")  # not dry-run → writes artifact
    assert out_path is not None
    assert "trends_general-tech_" in out_path.name


def test_unknown_track_raises(tmp_path):
    with pytest.raises(ValueError):
        pull_all(tmp_path, track="bogus", dry_run=True)


def test_ai_vendor_track_ignores_news_rss_queue(tmp_path, monkeypatch):
    """Byte-identical guard: the ai-vendor track never reads the news-RSS queue."""
    monkeypatch.setattr(tp, "pull_github_releases", lambda repo, tag: [])
    monkeypatch.setattr(tp, "pull_cursor_changelog", lambda: [])
    monkeypatch.setattr(tp, "pull_hacker_news", lambda **kw: [])
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    _queue_file(tmp_path, [_drop("https://a/av", track="ai-vendor", title="Claude", feed="anthropic")])

    _out, candidates = pull_all(tmp_path, dry_run=True, track="ai-vendor")
    assert candidates == []  # queue is NOT a source for ai-vendor
