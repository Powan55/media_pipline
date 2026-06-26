"""Unit tests for the post-Stage-7.5 VO duration WARN (warn-only).

Covers ``pipeline._probe_wav_duration_seconds`` and
``pipeline._warn_if_vo_over_duration``, added 2026-06-11. After Stage 7.5
loudnorm the normalized VO .wav is the final spoken artifact, so the pipeline
measures its real duration and logs a WARNING when it overruns the <=38s
breakout target. The check is WARN-ONLY by design: an unattended ``/start
-auto`` run must NEVER deadlock on a fresh duration gate, so it must not halt
and must not raise on a measurement failure.

Contract pinned here:
    1. A synthetic PCM WAV longer than ``duration_warn_s`` -> exactly one WARNING
       naming the measured seconds, the target, and the word count.
    2. A synthetic PCM WAV shorter than the threshold -> no WARNING (INFO only).
    3. ``duration_warn_s: 0`` (or absent-and-overridden to 0) DISABLES the check:
       no probe, no warning, returns None.
    4. ``_probe_wav_duration_seconds`` reads a real PCM WAV via stdlib ``wave``
       to within a frame of the true duration.
    5. A measurement failure (probe raises) does NOT propagate — it logs and the
       warn helper returns None, so the render path is never broken.

Synthetic WAVs are generated with the stdlib ``wave`` module in ``tmp_path`` — no
ffmpeg, no edge-tts, no network.
"""

from __future__ import annotations

import logging
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_RATE_HZ = 8000  # tiny rate keeps the synthetic file small


def _write_pcm_wav(path: Path, seconds: float, rate: int = _SAMPLE_RATE_HZ) -> Path:
    """Write a silent mono pcm_s16le WAV of ``seconds`` length to ``path``."""
    n_frames = int(round(seconds * rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return path


def _make_script(word_count: int = 112) -> "pipeline.ScriptDraft":
    return pipeline.ScriptDraft(
        topic_id="2026-06-11_999",
        hook_variants=["a", "b", "c"],
        body="word " * word_count,
        broll_cues=[],
        fact_check_queue=[],
        word_count=word_count,
    )


# ---------------------------------------------------------------------------
# _probe_wav_duration_seconds
# ---------------------------------------------------------------------------


def test_probe_reads_pcm_wav_duration(tmp_path):
    """stdlib wave path: a 2.0s PCM WAV measures ~2.0s (within one frame)."""
    wav = _write_pcm_wav(tmp_path / "vo.wav", seconds=2.0)
    dur = pipeline._probe_wav_duration_seconds(wav)
    assert abs(dur - 2.0) < (1.0 / _SAMPLE_RATE_HZ) + 1e-6


def test_probe_raises_runtimeerror_when_unreadable(monkeypatch, tmp_path):
    """A non-WAV file with no ffprobe available -> RuntimeError (fail-loud at probe level)."""
    bogus = tmp_path / "vo.wav"
    bogus.write_bytes(b"not a wav at all")

    # Force the ffprobe fallback to look unavailable.
    import subprocess

    def _no_ffprobe(*args, **kwargs):
        raise FileNotFoundError(2, "ffprobe not found")

    monkeypatch.setattr(subprocess, "run", _no_ffprobe)

    with pytest.raises(RuntimeError):
        pipeline._probe_wav_duration_seconds(bogus)


# ---------------------------------------------------------------------------
# _warn_if_vo_over_duration
# ---------------------------------------------------------------------------


def test_warns_when_over_threshold(monkeypatch, tmp_path, caplog):
    """Duration over duration_warn_s -> exactly one WARNING with the numbers."""
    wav = tmp_path / "vo.wav"
    # Avoid writing a 41s file: stub the probe to report 41.2s.
    monkeypatch.setattr(pipeline, "_probe_wav_duration_seconds", lambda p: 41.2)
    config = {"script_quality": {"duration_warn_s": 38.0}}
    script = _make_script(word_count=112)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, script, config)

    assert result == pytest.approx(41.2)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "41.2s" in msg
    assert "38.0s" in msg
    assert "112" in msg  # word count surfaced for the next-cycle tighten


def test_no_warning_when_under_threshold(monkeypatch, tmp_path, caplog):
    """Duration under the target -> no WARNING (INFO only), returns the duration."""
    wav = tmp_path / "vo.wav"
    monkeypatch.setattr(pipeline, "_probe_wav_duration_seconds", lambda p: 30.0)
    config = {"script_quality": {"duration_warn_s": 38.0}}

    with caplog.at_level(logging.INFO, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, _make_script(), config)

    assert result == pytest.approx(30.0)
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_disabled_when_warn_s_zero(monkeypatch, tmp_path, caplog):
    """duration_warn_s: 0 disables the check — no probe, no warning, returns None."""
    wav = tmp_path / "vo.wav"

    probed = {"n": 0}

    def _should_not_run(p):
        probed["n"] += 1
        return 99.0

    monkeypatch.setattr(pipeline, "_probe_wav_duration_seconds", _should_not_run)
    config = {"script_quality": {"duration_warn_s": 0}}

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, _make_script(), config)

    assert result is None
    assert probed["n"] == 0  # the probe is never invoked when disabled
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_measurement_failure_does_not_raise(monkeypatch, tmp_path, caplog):
    """A probe RuntimeError must NOT break the render path — log + return None."""
    wav = tmp_path / "vo.wav"

    def _boom(p):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(pipeline, "_probe_wav_duration_seconds", _boom)
    config = {"script_quality": {"duration_warn_s": 38.0}}

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, _make_script(), config)

    assert result is None
    # It warned about the measurement failure (advisory), but did not raise.
    assert any("could not measure VO duration" in r.getMessage() for r in caplog.records)


def test_default_threshold_is_38_when_key_absent(monkeypatch, tmp_path, caplog):
    """With no duration_warn_s key, the default 38.0 applies (49s -> WARN)."""
    wav = tmp_path / "vo.wav"
    monkeypatch.setattr(pipeline, "_probe_wav_duration_seconds", lambda p: 49.0)
    config = {"script_quality": {}}  # key absent -> default 38.0

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, _make_script(), config)

    assert result == pytest.approx(49.0)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_real_pcm_wav_end_to_end_warn(tmp_path, caplog):
    """End-to-end with a real (tiny) PCM WAV over threshold via a low warn_s."""
    wav = _write_pcm_wav(tmp_path / "vo.wav", seconds=2.0)
    # Use a 1.0s target so the real 2.0s file trips the warn without a big file.
    config = {"script_quality": {"duration_warn_s": 1.0}}

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = pipeline._warn_if_vo_over_duration(wav, _make_script(88), config)

    assert result == pytest.approx(2.0, abs=0.01)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "exceeds" in warnings[0].getMessage()
