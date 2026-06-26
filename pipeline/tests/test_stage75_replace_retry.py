"""Unit tests for the Stage-7.5 ``os.replace`` Windows-lock retry.

Covers the bounded retry-with-backoff added to ``pipeline._normalize_vo_loudness``
around the atomic loudness-normalized-VO swap. On Windows a freshly written WAV is
briefly held by AV real-time scan / the search indexer, so a bare ``os.replace()``
can raise ``PermissionError`` [WinError 5] even though nothing of ours holds a
handle. The retry makes the swap resilient; these tests pin the contract:

    1. A transient lock that clears after N failures -> the swap eventually
       succeeds, ``os.replace`` is called exactly N+1 times, and the function
       returns the original ``vo_path``.
    2. A lock that never clears -> the function retries a BOUNDED number of times
       (8) and then re-raises the ``PermissionError`` (fail-loud, never silent).
    3. A non-``PermissionError`` ``OSError`` is NOT caught by the retry loop and
       propagates on the first attempt (we only retry the Windows AV/indexer lock).

The real ``tools.audio_loudnorm.normalize_vo`` (which shells out to ffmpeg) and
``time.sleep`` (the backoff) are monkeypatched out so the test is fast and has no
external dependencies. ``os.replace`` is monkeypatched on the shared ``os`` module
object that ``_normalize_vo_loudness`` imports, so the patch is observed inside the
function under test.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FAKE_MEASUREMENTS = {"input_i": -16.0, "input_tp": -2.0, "target_offset": 1.5}


def _patch_loudnorm_and_sleep(monkeypatch):
    """Stub out the ffmpeg loudnorm pass and the backoff sleep.

    ``normalize_vo`` is imported lazily inside ``_normalize_vo_loudness`` as
    ``from tools.audio_loudnorm import normalize_vo``; patching the attribute on
    the ``tools.audio_loudnorm`` module makes the lazy import resolve to the stub.
    Record the sleep durations so we can assert the backoff grows per attempt.
    """
    import tools.audio_loudnorm as audio_loudnorm

    def _fake_normalize_vo(src, dst, **kwargs):
        # Real impl writes ``dst``; the retry path only cares that the swap runs,
        # and os.replace is itself patched, so no real file work is needed.
        return dict(_FAKE_MEASUREMENTS)

    monkeypatch.setattr(audio_loudnorm, "normalize_vo", _fake_normalize_vo)

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleeps.append(secs))
    return sleeps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_replace_succeeds_after_transient_lock(monkeypatch, tmp_path):
    """3 PermissionErrors then success -> 4 calls total, returns vo_path."""
    sleeps = _patch_loudnorm_and_sleep(monkeypatch)
    vo_path = tmp_path / "vo.wav"

    calls = {"n": 0}
    fail_until = 3  # fail on attempts 1,2,3; succeed on attempt 4

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_until:
            raise PermissionError(5, "Access is denied")
        # success: no-op (file work is irrelevant under the stub)
        return None

    monkeypatch.setattr(os, "replace", _flaky_replace)

    result = pipeline._normalize_vo_loudness(vo_path, {})

    assert result == vo_path
    assert calls["n"] == fail_until + 1  # 4 total: 3 failures + 1 success
    # Backoff slept once per failed attempt, growing 0.5 * attempt.
    assert sleeps == [0.5, 1.0, 1.5]


def test_replace_exhausts_retries_then_raises(monkeypatch, tmp_path):
    """Lock never clears -> bounded at 8 attempts, then re-raises PermissionError."""
    sleeps = _patch_loudnorm_and_sleep(monkeypatch)
    vo_path = tmp_path / "vo.wav"

    calls = {"n": 0}

    def _always_locked(src, dst):
        calls["n"] += 1
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", _always_locked)

    with pytest.raises(PermissionError):
        pipeline._normalize_vo_loudness(vo_path, {})

    # Bounded: exactly 8 attempts (range(1, 9)), no infinite loop.
    assert calls["n"] == 8
    # One backoff per attempt: 0.5 * 1 .. 0.5 * 8.
    assert sleeps == [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]


def test_non_permission_oserror_is_not_retried(monkeypatch, tmp_path):
    """A FileNotFoundError (OSError, not PermissionError) propagates immediately."""
    sleeps = _patch_loudnorm_and_sleep(monkeypatch)
    vo_path = tmp_path / "vo.wav"

    calls = {"n": 0}

    def _wrong_error(src, dst):
        calls["n"] += 1
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "replace", _wrong_error)

    with pytest.raises(FileNotFoundError):
        pipeline._normalize_vo_loudness(vo_path, {})

    # Only the retry loop's PermissionError branch backs off; this raises on attempt 1.
    assert calls["n"] == 1
    assert sleeps == []
