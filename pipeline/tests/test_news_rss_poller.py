"""Unit tests for tools/news_rss_poller.py.

All tests use tmp_path fixtures — never touch the real channel queue/seen files.
``feedparser.parse`` is monkeypatched throughout so tests don't hit the real
RSS feeds (the A5 task brief explicitly forbids real fetches in tests).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure the pipeline repo root is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import news_rss_poller as poller  # noqa: E402
from tools.news_rss_poller import (  # noqa: E402
    NewsDrop,
    entries_to_drops,
    load_seen,
    poll_once,
    prune_seen,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _struct_now() -> time.struct_time:
    """A time.struct_time for "now in UTC" — matches what feedparser populates."""
    return datetime.now(timezone.utc).timetuple()


def _entry(
    *,
    eid: str,
    title: str = "Test entry",
    link: str | None = None,
    summary: str = "Short summary.",
    published: time.struct_time | None = None,
) -> dict:
    """Build a feedparser-shaped entry dict. Use plain dict for clarity in tests."""
    return {
        "id": eid,
        "title": title,
        "link": link or f"https://example.com/{eid}",
        "summary": summary,
        "published_parsed": published or _struct_now(),
    }


def _fake_parsed(entries: list[dict], *, bozo: int = 0):
    """Build a SimpleNamespace that mimics feedparser.parse()'s return value."""
    return SimpleNamespace(entries=entries, bozo=bozo, bozo_exception=None)


def _patch_feedparser(monkeypatch, mapping: dict[str, list[dict]]) -> dict[str, int]:
    """Patch feedparser.parse so each URL returns a fixed entries list.

    Keys of `mapping` are SUBSTRINGS to match against the requested URL — that
    way tests don't have to bake in the exact production URL string. Returns a
    dict tracking how many times each URL substring was hit (mutated by the
    patched function).
    """
    call_counts: dict[str, int] = {k: 0 for k in mapping}

    def fake_parse(url: str, **_kwargs):
        for key, entries in mapping.items():
            if key in url:
                call_counts[key] += 1
                return _fake_parsed(entries)
        # Unknown feed → empty.
        return _fake_parsed([])

    monkeypatch.setattr(poller.feedparser, "parse", fake_parse)
    return call_counts


# ---------------------------------------------------------------------------
# Test 1: dedup against seen-set works
# ---------------------------------------------------------------------------


def test_dedup_skips_already_seen_entries():
    """An entry whose id is already in `seen` must NOT produce a NewsDrop."""
    seen = {"already-saw-this": "2026-05-07T00:00:00+00:00"}
    entries = [
        _entry(eid="already-saw-this", title="Old"),
        _entry(eid="brand-new-one", title="Fresh"),
    ]
    drops = entries_to_drops("openai", entries, seen)
    assert len(drops) == 1
    assert drops[0].id == "brand-new-one"
    # Seen-set must now contain the new id.
    assert "brand-new-one" in seen
    # And the old one is still there, untouched.
    assert seen["already-saw-this"] == "2026-05-07T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Test 2: new entries get appended with all required fields
# ---------------------------------------------------------------------------


def test_new_entries_have_all_required_fields(tmp_path, monkeypatch):
    """A new entry must materialize with id/feed/title/url/published_at/summary/added_to_queue_at."""
    queue_path = tmp_path / "queue.json"
    seen_path = tmp_path / "seen.json"
    log_path = tmp_path / "poller.log"
    poller.setup_logging(log_path)

    _patch_feedparser(
        monkeypatch,
        {
            "openai.com/news/rss.xml": [
                _entry(
                    eid="https://openai.com/index/foo",
                    title="OpenAI did the thing",
                    link="https://openai.com/index/foo",
                    summary="They shipped a new feature.",
                ),
            ],
        },
    )

    drops = poll_once(
        feeds={"openai": ("OpenAI News", "https://openai.com/news/rss.xml")},
        queue_path=queue_path,
        seen_path=seen_path,
        max_age_hours=168,
    )
    assert len(drops) == 1
    d = drops[0]
    assert d.id == "https://openai.com/index/foo"
    assert d.feed == "openai"
    assert d.title == "OpenAI did the thing"
    assert d.url == "https://openai.com/index/foo"
    assert d.summary == "They shipped a new feature."
    assert d.published_at  # non-empty ISO string
    assert d.added_to_queue_at  # non-empty ISO string

    # Queue file got the JSON shape we expect.
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert isinstance(queue, list)
    assert len(queue) == 1
    for required in ("id", "feed", "title", "url", "published_at", "summary", "added_to_queue_at"):
        assert required in queue[0], f"missing field {required!r}"


# ---------------------------------------------------------------------------
# Test 3: per-feed failure doesn't crash other feeds
# ---------------------------------------------------------------------------


def test_per_feed_failure_isolated(tmp_path, monkeypatch):
    """If one feed raises, other feeds must still produce drops + a queue file."""
    queue_path = tmp_path / "queue.json"
    seen_path = tmp_path / "seen.json"
    log_path = tmp_path / "poller.log"
    poller.setup_logging(log_path)

    def fake_parse(url: str, **_kwargs):
        if "anthropic" in url:
            raise OSError("network unreachable")
        if "openai" in url:
            return _fake_parsed([_entry(eid="openai-1", title="OpenAI ok")])
        if "deepmind" in url:
            return _fake_parsed([_entry(eid="dm-1", title="DM ok")])
        return _fake_parsed([])

    monkeypatch.setattr(poller.feedparser, "parse", fake_parse)

    drops = poll_once(
        feeds={
            "anthropic": ("Anthropic", "https://example.com/anthropic.xml"),
            "openai": ("OpenAI", "https://openai.com/news/rss.xml"),
            "google": ("DeepMind", "https://deepmind.google/blog/rss.xml"),
        },
        queue_path=queue_path,
        seen_path=seen_path,
        max_age_hours=168,
    )
    feeds_seen = sorted(d.feed for d in drops)
    assert feeds_seen == ["google", "openai"], (
        f"expected openai+google to survive, got {feeds_seen!r}"
    )
    # Queue persisted.
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 2


# ---------------------------------------------------------------------------
# Test 4: atomic write of queue + seen files (write+rename pattern)
# ---------------------------------------------------------------------------


def test_atomic_write_uses_tmp_then_rename(tmp_path, monkeypatch):
    """Verify _atomic_write_json writes to <path>.tmp first then os.replace's it.

    We patch os.replace to record the (src, dst) pair so we can prove the
    write+rename pattern was used, not a direct write.
    """
    queue_path = tmp_path / "queue.json"
    seen_path = tmp_path / "seen.json"
    log_path = tmp_path / "poller.log"
    poller.setup_logging(log_path)

    captured_replaces: list[tuple[Path, Path]] = []
    real_replace = poller.os.replace

    def spy_replace(src, dst):
        captured_replaces.append((Path(str(src)), Path(str(dst))))
        return real_replace(src, dst)

    monkeypatch.setattr(poller.os, "replace", spy_replace)

    _patch_feedparser(
        monkeypatch,
        {
            "openai": [_entry(eid="op-1", title="One")],
        },
    )

    poll_once(
        feeds={"openai": ("OpenAI", "https://openai.com/news/rss.xml")},
        queue_path=queue_path,
        seen_path=seen_path,
        max_age_hours=168,
    )

    # We expect TWO replaces: one for the queue, one for the seen-set. Both
    # must rename a .tmp sibling onto the real path.
    assert len(captured_replaces) == 2, (
        f"expected 2 atomic replaces (queue + seen), got {len(captured_replaces)}"
    )
    for src, dst in captured_replaces:
        assert src.suffix == ".tmp", f"src must be a .tmp file, got {src}"
        assert dst in (queue_path, seen_path), f"unexpected dst {dst}"
        # The .tmp must have been removed by the rename — only the dest stays.
        assert not src.exists()
        assert dst.exists()


# ---------------------------------------------------------------------------
# Test 5: max-age-hours pruning works on seen-set
# ---------------------------------------------------------------------------


def test_prune_seen_drops_old_entries():
    """Entries older than max_age_hours leave; recent entries stay."""
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    stale = (now - timedelta(hours=200)).isoformat()
    seen = {
        "fresh-id": fresh,
        "stale-id": stale,
        "broken-timestamp": "not-a-real-iso",  # malformed → keep (safer than re-emitting)
    }
    pruned = prune_seen(seen, max_age_hours=168)
    assert "fresh-id" in pruned
    assert "stale-id" not in pruned
    assert "broken-timestamp" in pruned
    # Original dict not mutated.
    assert "stale-id" in seen


def test_max_age_zero_disables_pruning():
    """max_age_hours <= 0 means "never prune"."""
    now = datetime.now(timezone.utc)
    seen = {
        "ancient": (now - timedelta(days=365)).isoformat(),
    }
    pruned = prune_seen(seen, max_age_hours=0)
    assert pruned == seen
    pruned_neg = prune_seen(seen, max_age_hours=-1)
    assert pruned_neg == seen


# ---------------------------------------------------------------------------
# Test 6: re-running poll_once on the same feed doesn't re-emit (full integration)
# ---------------------------------------------------------------------------


def test_second_run_does_not_re_emit_same_entries(tmp_path, monkeypatch):
    """Two poll_once calls with the same feedparser results: only the first run produces drops."""
    queue_path = tmp_path / "queue.json"
    seen_path = tmp_path / "seen.json"
    log_path = tmp_path / "poller.log"
    poller.setup_logging(log_path)

    _patch_feedparser(
        monkeypatch,
        {
            "openai": [
                _entry(eid="op-1", title="First"),
                _entry(eid="op-2", title="Second"),
            ],
        },
    )

    feeds = {"openai": ("OpenAI", "https://openai.com/news/rss.xml")}
    first = poll_once(
        feeds=feeds, queue_path=queue_path, seen_path=seen_path, max_age_hours=168,
    )
    second = poll_once(
        feeds=feeds, queue_path=queue_path, seen_path=seen_path, max_age_hours=168,
    )
    assert len(first) == 2
    assert second == [], "second run must produce zero new drops"
    # Queue file still contains exactly the original two — no duplicates appended.
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 2

    # Seen-set persisted across runs.
    seen = load_seen(seen_path)
    assert "op-1" in seen and "op-2" in seen


# ---------------------------------------------------------------------------
# Test 7: entry.id falls back to entry.link when no id is present
# ---------------------------------------------------------------------------


def test_entry_falls_back_to_link_when_no_id():
    """Some RSS feeds omit <guid>/<id>. Use entry.link as the dedup key instead."""
    entries = [
        # No 'id' key at all — exercises the .get('id') -> None fallback.
        {
            "title": "No-id entry",
            "link": "https://example.com/no-id",
            "summary": "x",
            "published_parsed": _struct_now(),
        },
    ]
    seen: dict[str, str] = {}
    drops = entries_to_drops("anthropic", entries, seen)
    assert len(drops) == 1
    assert drops[0].id == "https://example.com/no-id"
    assert "https://example.com/no-id" in seen


# ---------------------------------------------------------------------------
# Test 8: summary is truncated to SUMMARY_MAX_CHARS
# ---------------------------------------------------------------------------


def test_summary_truncation():
    """A summary longer than SUMMARY_MAX_CHARS is truncated with an ellipsis."""
    long_summary = "x" * (poller.SUMMARY_MAX_CHARS + 200)
    entries = [_entry(eid="big-1", summary=long_summary)]
    seen: dict[str, str] = {}
    drops = entries_to_drops("openai", entries, seen)
    assert len(drops) == 1
    assert len(drops[0].summary) <= poller.SUMMARY_MAX_CHARS
    assert drops[0].summary.endswith("...")
