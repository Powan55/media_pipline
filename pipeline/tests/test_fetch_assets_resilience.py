"""Resilience tests for the stock-asset search/download paths in pipeline.py.

Pins WORKFLOW_AUDIT_2026-05-31 M1: the Pexels/Pixabay search helpers and
`_download_clip` previously caught a bare ``Exception``, which swallowed
EVERYTHING — including programming errors — and made a 401-bad-key
indistinguishable from a transient timeout. The fix narrows the clause to
``requests.RequestException`` while KEEPING the existing return contract
(None / False) that ``fetch_assets`` relies on for its Pexels→Pixabay fallback.

These tests assert:
  - a transient request error returns None / False (fallback still works);
  - a failed mid-stream download cleans up its partial and returns False;
  - a genuinely unexpected (non-request) error now PROPAGATES loud instead of
    being silently absorbed.

The helpers ``import requests`` locally, so monkeypatching ``requests.get`` on
the real module is picked up by the functions under test.

Run:
    python -m pytest tests/test_fetch_assets_resilience.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Search helpers — transient error returns None, not a raise
# ---------------------------------------------------------------------------


def test_pexels_search_returns_none_on_request_exception(monkeypatch) -> None:
    """A requests.Timeout in the Pexels search returns None (so fetch_assets can
    fall back to Pixabay), it does NOT raise."""
    def _boom(*_a, **_kw):
        raise requests.Timeout("connect timed out")

    monkeypatch.setattr(requests, "get", _boom)
    assert pipeline._search_pexels_video("ai robot", "fake-key") is None


def test_pixabay_search_returns_none_on_request_exception(monkeypatch) -> None:
    """Symmetric Pixabay guard — ConnectionError returns None, not a raise."""
    def _boom(*_a, **_kw):
        raise requests.ConnectionError("name resolution failed")

    monkeypatch.setattr(requests, "get", _boom)
    assert pipeline._search_pixabay_video("ai robot", "fake-key") is None


def test_pexels_http_error_returns_none_and_keeps_fallback(monkeypatch) -> None:
    """A 401 (bad key) is an HTTPError ⊂ RequestException — must STILL return None
    so the Pexels→Pixabay fallback chain is preserved (M1 must not re-raise 4xx)."""
    class _Resp:
        status_code = 401

        def raise_for_status(self):
            raise requests.HTTPError("401 Unauthorized", response=self)

    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: _Resp())
    assert pipeline._search_pexels_video("ai robot", "bad-key") is None


# ---------------------------------------------------------------------------
# _download_clip — partial cleanup + None/False contract
# ---------------------------------------------------------------------------


def test_download_clip_unlinks_partial_on_request_exception(monkeypatch, tmp_path) -> None:
    """A request error during download returns False and leaves no final dest."""
    dest = tmp_path / "clip.mp4"

    def _boom(*_a, **_kw):
        raise requests.ConnectionError("reset by peer")

    monkeypatch.setattr(requests, "get", _boom)
    assert pipeline._download_clip("https://example.com/x.mp4", dest) is False
    assert not dest.exists()


# ---------------------------------------------------------------------------
# The narrowing must FAIL LOUD on a non-request error (no longer swallowed)
# ---------------------------------------------------------------------------


def test_unexpected_non_request_error_propagates_in_search(monkeypatch) -> None:
    """A ValueError (e.g. a bug, not a network failure) must now propagate from
    the search helper, proving the clause no longer swallows everything."""
    def _boom(*_a, **_kw):
        raise ValueError("unexpected programming error")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(ValueError):
        pipeline._search_pexels_video("ai robot", "fake-key")


def test_unexpected_non_request_error_propagates_in_download(monkeypatch, tmp_path) -> None:
    """Same fail-loud guarantee for _download_clip — a non-request error raises."""
    def _boom(*_a, **_kw):
        raise ValueError("unexpected programming error")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(ValueError):
        pipeline._download_clip("https://example.com/x.mp4", tmp_path / "clip.mp4")


# ---------------------------------------------------------------------------
# L12: the end-of-loop summary must name which cues failed (partial b-roll miss)
# ---------------------------------------------------------------------------


def _script_with_cues(cues: list[str]) -> pipeline.ScriptDraft:
    """A minimal ScriptDraft carrying the given B-ROLL cues for fetch_assets."""
    return pipeline.ScriptDraft(
        topic_id="2026-05-31_900",
        hook_variants=["h1", "h2", "h3"],
        body="body text",
        broll_cues=cues,
        fact_check_queue=[],
        word_count=100,
    )


def _match(query: str, source: str = "pexels") -> dict:
    return {
        "url": f"https://example.com/{query}.mp4",
        "source": source,
        "license": "Pexels License",
        "page_url": f"https://example.com/{query}",
        "query": query,
    }


def test_partial_failure_logs_cue_summary(monkeypatch, tmp_path, caplog) -> None:
    """3 cues, only the first matches: fetch_assets succeeds with 1 clip AND a
    single WARNING summary names the 2 failed cue indices/queries."""
    monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    # Cue 0 matches; cues 1 and 2 return no match from either provider.
    def fake_pexels(query, key):
        # _cue_to_query reduces the cue text; the first cue's query starts with "first".
        return _match(query) if query.startswith("first") else None

    monkeypatch.setattr(pipeline, "_search_pexels_video", fake_pexels)
    monkeypatch.setattr(pipeline, "_search_pixabay_video", lambda q, k: None)
    monkeypatch.setattr(pipeline, "_download_clip", lambda url, dest, **_kw: Path(dest).write_bytes(b"x") or True)

    config = {
        "paths": {"channel_root": str(tmp_path)},
        "assets": {"preferred_stock_provider": "pexels"},
    }
    script = _script_with_cues(["first robot scene", "second city skyline", "third data center"])

    with caplog.at_level("WARNING", logger="pipeline"):
        bundle = pipeline.fetch_assets(script, config)

    # Run succeeded with the one match (no raise).
    assert len(bundle.clips) == 1

    summaries = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "cues failed" in r.getMessage()
    ]
    assert len(summaries) == 1, "expected exactly one failed-cue summary line"
    msg = summaries[0].getMessage()
    assert "2/3" in msg
    # Both failed cue indices appear (idx 1 and 2), not idx 0.
    assert "#1:" in msg and "#2:" in msg
    assert "#0:" not in msg


def test_all_cues_fail_still_raises(monkeypatch, tmp_path) -> None:
    """When no cue produces a clip, the existing all-failed RuntimeError fires
    (the summary line is logging-only and does not suppress the hard stop)."""
    monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    monkeypatch.setattr(pipeline, "_search_pexels_video", lambda q, k: None)
    monkeypatch.setattr(pipeline, "_search_pixabay_video", lambda q, k: None)

    config = {
        "paths": {"channel_root": str(tmp_path)},
        "assets": {"preferred_stock_provider": "pexels"},
    }
    script = _script_with_cues(["alpha scene", "beta scene"])

    with pytest.raises(RuntimeError, match="No b-roll clips successfully fetched"):
        pipeline.fetch_assets(script, config)


def test_download_failure_counts_as_failed_cue(monkeypatch, tmp_path, caplog) -> None:
    """A cue that matches but whose download returns False is recorded as failed
    in the summary (the new else-branch on _download_clip)."""
    monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    # Both cues match, but cue 1's download fails (returns False).
    def fake_pexels(query, key):
        return _match(query)

    def fake_download(url, dest, **_kw):
        if "second" in url:
            return False  # simulate a download miss for the second cue
        Path(dest).write_bytes(b"x")
        return True

    monkeypatch.setattr(pipeline, "_search_pexels_video", fake_pexels)
    monkeypatch.setattr(pipeline, "_search_pixabay_video", lambda q, k: None)
    monkeypatch.setattr(pipeline, "_download_clip", fake_download)

    config = {
        "paths": {"channel_root": str(tmp_path)},
        "assets": {"preferred_stock_provider": "pexels"},
    }
    script = _script_with_cues(["first robot scene", "second skyline scene"])

    with caplog.at_level("WARNING", logger="pipeline"):
        bundle = pipeline.fetch_assets(script, config)

    assert len(bundle.clips) == 1  # only the first cue's clip survived
    summaries = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "cues failed" in r.getMessage()
    ]
    assert len(summaries) == 1
    assert "1/2" in summaries[0].getMessage()
    assert "#1:" in summaries[0].getMessage()
