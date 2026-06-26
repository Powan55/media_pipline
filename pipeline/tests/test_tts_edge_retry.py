"""Retry tests for pipeline._vo_edge_tts (WORKFLOW_AUDIT_2026-05-31 M6).

edge-tts hits Microsoft's PUBLIC neural-TTS endpoint (no SLA), so before M6 a
single transient 429 / socket drop failed the whole VO stage. M6 wraps the
``asyncio.run(synth())`` call in a bounded plain-Python retry with exponential
backoff, re-raising the last exception on final failure so a truly-down endpoint
still fails loud. RETRY-ONLY — no provider fallback.

These tests fake ``edge_tts.Communicate`` (so no real network) and ``ffmpeg`` (so
the MP3→WAV step is a stub), and patch ``time.sleep`` so the backoff doesn't slow
the suite. They assert: a transient failure is retried then succeeds (save called
N times, WAV produced); and an always-failing endpoint re-raises after the
configured attempts.

Run:
    python -m pytest tests/test_tts_edge_retry.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import aiohttp
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


def _install_fakes(monkeypatch, *, fail_times: int, total_attempts_box: dict):
    """Patch edge_tts.Communicate (fails `fail_times` then writes a stub mp3),
    ffmpeg (writes a stub wav), and time.sleep (no-op)."""
    import edge_tts
    import ffmpeg

    calls = {"n": 0}
    total_attempts_box["calls"] = calls

    class _FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            self._text = text

        async def save(self, mp3_path):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise aiohttp.ClientError(f"transient blip #{calls['n']}")
            Path(mp3_path).write_bytes(b"ID3stub-mp3-bytes")

    monkeypatch.setattr(edge_tts, "Communicate", _FakeCommunicate)

    # Fake the ffmpeg fluent chain: input(...).output(...).overwrite_output().run()
    # The final .run() writes a stub WAV so _vo_edge_tts's stat() calls succeed.
    class _Chain:
        def __init__(self, wav_path=None):
            self._wav = wav_path

        def output(self, wav_path, **_kw):
            return _Chain(wav_path)

        def overwrite_output(self):
            return self

        def run(self, **_kw):
            Path(self._wav).write_bytes(b"RIFFstub-wav-bytes")

    monkeypatch.setattr(ffmpeg, "input", lambda _mp3: _Chain())

    # Don't actually sleep during backoff.
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)


def _config(retries: int = 3) -> dict:
    return {"tts": {"edge_tts_voice": "en-US-AndrewMultilingualNeural",
                    "rate": "+0%", "sample_rate_hz": 48000,
                    "edge_tts_retries": retries}}


def test_edge_tts_retries_then_succeeds(monkeypatch, tmp_path, caplog) -> None:
    """One transient failure then success: save called twice, WAV produced."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=1, total_attempts_box=box)

    wav = pipeline._vo_edge_tts("2026-05-31_001", "hello world", tmp_path, _config(retries=3))

    assert wav.exists(), "WAV must be produced after a successful retry"
    assert box["calls"]["n"] == 2, "save should be called twice (fail once, then succeed)"


def test_edge_tts_reraises_after_exhausting_retries(monkeypatch, tmp_path) -> None:
    """An always-failing endpoint re-raises after the configured attempts."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=99, total_attempts_box=box)

    with pytest.raises(aiohttp.ClientError):
        pipeline._vo_edge_tts("2026-05-31_002", "hello world", tmp_path, _config(retries=3))
    # Exactly `retries` attempts were made before giving up.
    assert box["calls"]["n"] == 3


def test_edge_tts_happy_path_single_attempt(monkeypatch, tmp_path) -> None:
    """No failure: save is called exactly once (happy path unchanged)."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=0, total_attempts_box=box)

    wav = pipeline._vo_edge_tts("2026-05-31_003", "hello", tmp_path, _config(retries=3))
    assert wav.exists()
    assert box["calls"]["n"] == 1


def test_edge_tts_retries_config_tunable(monkeypatch, tmp_path) -> None:
    """The retry count is read from config.tts (edge_tts_retries)."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=99, total_attempts_box=box)

    with pytest.raises(aiohttp.ClientError):
        pipeline._vo_edge_tts("2026-05-31_004", "hello", tmp_path, _config(retries=2))
    assert box["calls"]["n"] == 2  # honored the config-supplied attempt count


# ---------------------------------------------------------------------------
# L9: config.tts.rate format validation at the synth load site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rate", ["10%", "+10", "fast", "+10 %", "%10", "10"])
def test_malformed_tts_rate_raises(monkeypatch, tmp_path, bad_rate) -> None:
    """A malformed tts.rate fails loud with a clear ValueError BEFORE any synth —
    naming tts.rate and config.yaml — instead of erroring deep in edge-tts."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=0, total_attempts_box=box)
    cfg = _config()
    cfg["tts"]["rate"] = bad_rate

    with pytest.raises(ValueError) as ei:
        pipeline._vo_edge_tts("2026-05-31_005", "hello", tmp_path, cfg)
    msg = str(ei.value)
    assert "tts.rate" in msg
    assert "config.yaml" in msg
    # The raise is at the load site — synth (Communicate.save) is never reached.
    assert box["calls"]["n"] == 0


@pytest.mark.parametrize("good_rate", ["+10%", "-5%", "+0%", "-0%", "+100%"])
def test_valid_tts_rate_ok(monkeypatch, tmp_path, good_rate) -> None:
    """Well-formed signed-percent rates pass validation and produce a WAV."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=0, total_attempts_box=box)
    cfg = _config()
    cfg["tts"]["rate"] = good_rate

    wav = pipeline._vo_edge_tts("2026-05-31_006", "hello", tmp_path, cfg)
    assert wav.exists()
    assert box["calls"]["n"] == 1


def test_default_tts_rate_ok(monkeypatch, tmp_path) -> None:
    """An absent tts.rate falls back to '+0%', which satisfies the regex."""
    box: dict = {}
    _install_fakes(monkeypatch, fail_times=0, total_attempts_box=box)
    cfg = _config()
    del cfg["tts"]["rate"]  # exercise the .get(..., "+0%") default path

    wav = pipeline._vo_edge_tts("2026-05-31_007", "hello", tmp_path, cfg)
    assert wav.exists()
    assert box["calls"]["n"] == 1
