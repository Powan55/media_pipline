"""Tests for trend_pull's all-sources-failed error surfacing (L5).

`pull_all` must distinguish a fully-degraded pull (every real source raised — an
infrastructure problem the operator must act on) from a benign empty day
(sources ran fine but matched nothing). The former escalates to log.error; the
latter stays a log.warning. Stub sources (NotImplementedError) are SKIPs and
must never count toward the all-failed signal.

All tests run with dry_run=True so no artifact is written, and monkeypatch the
wired source functions so the real network is never touched.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Ensure the pipeline repo root is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import trend_pull as tp  # noqa: E402
from trend_pull import TrendCandidate, pull_all  # noqa: E402


def _candidate(url: str = "https://example.com/x") -> TrendCandidate:
    """A minimal valid TrendCandidate for the happy-path source stubs."""
    return TrendCandidate(
        source="test",
        url=url,
        title="Test candidate",
        summary="A surfaced topic.",
        surfaced_at="2026-05-31T00:00:00+00:00",
        published_at=None,
        score=None,
        tag="test",
    )


def _patch_sources(monkeypatch, *, github, cursor, hn) -> None:
    """Patch the three wired source fns. Each arg is a callable taking the fn's
    own args and returning a list (or raising). github is called per-repo with
    (repo, tag); cursor and hn take no args."""
    monkeypatch.setattr(tp, "pull_github_releases", github)
    monkeypatch.setattr(tp, "pull_cursor_changelog", cursor)
    monkeypatch.setattr(tp, "pull_hacker_news", hn)
    # Make sure the credentialed Reddit stub stays a SKIP (no env creds).
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)


def test_all_sources_failed_logs_error(tmp_path, monkeypatch, caplog):
    """Every wired source raises -> a single log.error naming ALL + failed sources."""

    def boom_github(repo, tag):
        raise RuntimeError(f"github down for {repo}")

    def boom_cursor():
        raise RuntimeError("cursor changelog 503")

    def boom_hn():
        raise RuntimeError("hn firebase timeout")

    _patch_sources(monkeypatch, github=boom_github, cursor=boom_cursor, hn=boom_hn)

    with caplog.at_level(logging.ERROR, logger="trend_pull"):
        out_path, candidates = pull_all(tmp_path, dry_run=True)

    assert out_path is None  # dry-run writes nothing
    assert candidates == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected exactly one ERROR, got {len(errors)}"
    msg = errors[0].getMessage()
    assert "ALL" in msg
    # The failed source names must appear in the escalation message.
    assert "github:anthropics/claude-code" in msg
    assert "cursor:changelog" in msg
    assert "hn" in msg


def test_sources_ok_but_empty_logs_warning_not_error(tmp_path, monkeypatch, caplog):
    """Sources run cleanly but return nothing -> WARNING (benign empty), not ERROR."""

    def empty_github(repo, tag):
        return []

    def empty_cursor():
        return []

    def empty_hn():
        return []

    _patch_sources(monkeypatch, github=empty_github, cursor=empty_cursor, hn=empty_hn)

    with caplog.at_level(logging.WARNING, logger="trend_pull"):
        out_path, candidates = pull_all(tmp_path, dry_run=True)

    assert candidates == []
    assert not [r for r in caplog.records if r.levelno == logging.ERROR], (
        "a clean-but-empty pull must NOT emit an all-failed ERROR"
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("0 candidates" in r.getMessage() for r in warnings), (
        "expected the benign '0 candidates' WARNING"
    )


def test_partial_failure_no_error(tmp_path, monkeypatch, caplog):
    """One source raises, another returns a candidate -> no all-failed ERROR."""

    def ok_github(repo, tag):
        # Only the first repo yields; others return empty so we still have data.
        return [_candidate(f"https://example.com/{repo}")] if repo == "anthropics/claude-code" else []

    def boom_cursor():
        raise RuntimeError("cursor changelog 503")

    def empty_hn():
        return []

    _patch_sources(monkeypatch, github=ok_github, cursor=boom_cursor, hn=empty_hn)

    with caplog.at_level(logging.WARNING, logger="trend_pull"):
        out_path, candidates = pull_all(tmp_path, dry_run=True)

    assert len(candidates) == 1  # the one github candidate survived
    assert not [r for r in caplog.records if r.levelno == logging.ERROR], (
        "a partial failure (some source succeeded) must NOT emit an all-failed ERROR"
    )
    # The per-source FAILED warning for cursor should still be there.
    assert any(
        "cursor:changelog" in r.getMessage() and "FAILED" in r.getMessage()
        for r in caplog.records
    )
