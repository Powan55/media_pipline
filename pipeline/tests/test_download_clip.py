"""Atomic-download tests for pipeline._download_clip (WORKFLOW_AUDIT_2026-05-31 M7).

Before M7 the function streamed directly onto ``dest``; a crash / kill / power
loss between the first chunk and completion left a non-zero-but-truncated file
that the ``st_size > 0`` cache check then treated as a valid cached clip and fed
to the render. M7 streams to a ``<dest>.part`` temp and only ``os.replace()``s it
onto ``dest`` after a fully-successful read, so an interrupted download never
produces a deceptive "complete" dest.

These tests assert:
  - an interrupted download leaves NO final dest (only the temp, which is cleaned
    up), returning False;
  - a stray pre-existing truncated dest written via the .part path is never
    promoted on failure, so the next call re-fetches instead of false-caching.

The helper ``import requests`` locally, so monkeypatching ``requests.get`` is
picked up by the function under test.

Run:
    python -m pytest tests/test_download_clip.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


class _FakeResp:
    """Minimal stand-in for the streaming requests response context manager."""

    def __init__(self, chunks, *, raise_mid=False):
        self._chunks = chunks
        self._raise_mid = raise_mid

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1 << 16):
        # Yield the first chunk, then (optionally) fail mid-stream to mimic a
        # dropped socket after some bytes already hit the .part temp.
        for i, c in enumerate(self._chunks):
            yield c
            if self._raise_mid and i == 0:
                raise requests.ConnectionError("connection reset mid-stream")


def test_atomic_rename_only_after_complete(monkeypatch, tmp_path) -> None:
    """An interrupted (mid-stream) download must NOT leave a final dest — only
    the .part temp, which is cleaned up — and the call returns False."""
    dest = tmp_path / "clip.mp4"

    def _get(*_a, **_kw):
        return _FakeResp([b"x" * 1024, b"y" * 1024], raise_mid=True)

    monkeypatch.setattr(requests, "get", _get)
    assert pipeline._download_clip("https://example.com/x.mp4", dest) is False
    # No deceptive complete file, and the temp was cleaned up.
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_complete_download_promotes_to_dest(monkeypatch, tmp_path) -> None:
    """A clean download promotes the temp to dest atomically and returns True;
    no .part remains afterward."""
    dest = tmp_path / "clip.mp4"

    def _get(*_a, **_kw):
        return _FakeResp([b"a" * 2048, b"b" * 2048], raise_mid=False)

    monkeypatch.setattr(requests, "get", _get)
    assert pipeline._download_clip("https://example.com/x.mp4", dest) is True
    assert dest.exists() and dest.stat().st_size == 4096
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_partial_download_not_treated_as_cache(monkeypatch, tmp_path) -> None:
    """A leftover truncated artifact from a prior interrupted run must NOT be
    served as a cache hit. With the atomic-write design, an interrupted run
    leaves only a `.part` (not a non-zero `dest`), so the cache fast-path
    (st_size>0 on `dest`) does not fire and the next call re-fetches."""
    dest = tmp_path / "clip.mp4"

    # First call: interrupted mid-stream -> leaves no usable dest.
    monkeypatch.setattr(
        requests, "get",
        lambda *_a, **_kw: _FakeResp([b"z" * 512, b"z" * 512], raise_mid=True),
    )
    assert pipeline._download_clip("https://example.com/x.mp4", dest) is False
    assert not dest.exists(), "interrupted download must not leave a cacheable dest"

    # Second call: succeeds and is the one that produces the real file.
    calls = {"n": 0}

    def _get_ok(*_a, **_kw):
        calls["n"] += 1
        return _FakeResp([b"q" * 4096], raise_mid=False)

    monkeypatch.setattr(requests, "get", _get_ok)
    assert pipeline._download_clip("https://example.com/x.mp4", dest) is True
    # Proves a real re-fetch happened (the stub was never served as a cache hit).
    assert calls["n"] == 1
    assert dest.exists() and dest.stat().st_size == 4096
