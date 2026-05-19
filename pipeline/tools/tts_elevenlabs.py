"""ElevenLabs TTS adapter — Brian voice (code-only ship; dormant by default).

Per `feedback_llm_api_policy.md`, each API-enabled feature requires per-feature
operator approval before adding to `requirements.txt`. This module ships the code
but is dormant until the operator activates it. Activation steps:

    1. Subscribe to ElevenLabs Starter $5/mo
    2. Add `ELEVENLABS_API_KEY=...` to `.env`
    3. `pip install elevenlabs python-dotenv` (note: `python-dotenv` already pinned)
    4. Add `elevenlabs>=1.0,<2.0` to `requirements.txt`
    5. Flip `config.tts.provider: elevenlabs` in `config.yaml`

Until step 4-5 are done, this module is dormant. The `elevenlabs` SDK is
imported LAZILY inside `synthesize()` so the rest of the pipeline does not
ImportError at startup when the dep is not installed.

Output contract: matches `pipeline._vo_edge_tts` — 48 kHz mono PCM s16le WAV
written to the requested `out_path`, with the source MP3 cached alongside
as `<out_path.stem>.mp3`.

CLI:
    python tools/tts_elevenlabs.py --text "Hello world test" --out test_brian.wav
                                   [--voice-id ...] [--model-id ...]
                                   [--stability 0.45] [--similarity 0.85]
                                   [--style 0.30] [--speed 1.05]
                                   [--no-wav]   # MP3-only, skip WAV decode (debug)
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("tts_elevenlabs")

DEFAULT_VOICE_ID = "nPczCjzI2devNBz1zQrb"      # "Brian" — Daily Dose register, American baritone
DEFAULT_MODEL = "eleven_multilingual_v2"        # body-only; v3 is Creator-tier and not in scope
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
DEFAULT_SAMPLE_RATE_HZ = 48000


class ElevenLabsError(RuntimeError):
    """Raised when synthesis fails (missing SDK, auth, quota, network, decode).

    Wraps every failure mode `synthesize()` can hit so callers have a single
    exception type to catch instead of `ImportError`, `KeyError`, `httpx.*`, or
    `subprocess.CalledProcessError` leaking out.
    """


def _is_retryable(exc: BaseException) -> bool:
    """Return True if `exc` looks like a transient ElevenLabs/network failure.

    Retryable: HTTP 429 / 5xx, connect/read timeouts, generic OSError network
    errors. Non-retryable: 4xx auth/validation, decode errors, anything else.

    Detection is duck-typed (look at exception class names + `.response.status_code`)
    to avoid importing `httpx` here just for isinstance checks — the SDK may
    expose its own exception classes wrapping httpx.
    """
    # Check status_code on a wrapped httpx response, if any.
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status == 429 or 500 <= status < 600
    # Fall back to class-name heuristic for httpx network errors.
    name = type(exc).__name__
    if name in {
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
        "NetworkError", "TimeoutException",
    }:
        return True
    if isinstance(exc, OSError):
        return True
    return False


def _consume_audio_stream(audio: object) -> bytes:
    """Coalesce the SDK's audio return value into a single `bytes` blob.

    The `text_to_speech.convert` method may return `bytes` directly or an
    iterator of `bytes` chunks depending on SDK version. Handle both.
    """
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if isinstance(audio, Iterable):
        chunks: list[bytes] = []
        for chunk in audio:  # type: ignore[assignment]
            if not isinstance(chunk, (bytes, bytearray)):
                raise ElevenLabsError(
                    f"unexpected audio chunk type from ElevenLabs SDK: {type(chunk).__name__}"
                )
            chunks.append(bytes(chunk))
        return b"".join(chunks)
    raise ElevenLabsError(
        f"unexpected audio return type from ElevenLabs SDK: {type(audio).__name__}"
    )


def _decode_mp3_to_wav(mp3_path: Path, wav_path: Path, sample_rate_hz: int) -> None:
    """Run ffmpeg to convert MP3 → 48 kHz mono PCM s16le WAV. Raises on failure."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(mp3_path),
        "-ar", str(sample_rate_hz),
        "-ac", "1",
        "-acodec", "pcm_s16le",
        str(wav_path),
    ]
    log.info("ffmpeg decode: %s -> %s @ %d Hz mono", mp3_path.name, wav_path.name, sample_rate_hz)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ElevenLabsError(
            "ffmpeg not found on PATH; required for MP3 -> WAV decode"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ElevenLabsError(
            f"ffmpeg failed to decode {mp3_path.name} -> WAV (rc={exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc


def synthesize(
    text: str,
    out_path: Path,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stability: float = 0.45,
    similarity_boost: float = 0.85,
    style: float = 0.30,
    speed: float = 1.05,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    max_retries: int = 3,
    backoff_base_s: float = 1.5,
) -> Path:
    """Synthesize text to a 48 kHz mono WAV via the ElevenLabs HTTP API.

    Mirrors the contract of `pipeline._vo_edge_tts` so the eventual dispatch
    branch in `pipeline.generate_voiceover` can swap providers transparently:
    the MP3 from ElevenLabs is cached at `<out_path.stem>.mp3` alongside the
    final WAV at `out_path`.

    Args:
        text: Voiceover text to synthesize.
        out_path: Target WAV path. Parent dirs are created if missing.
        voice_id: ElevenLabs voice id. Defaults to "Brian".
        model_id: ElevenLabs model id. Defaults to `eleven_multilingual_v2`
            (Starter-tier body model; v3 is Creator-tier and out of scope).
        api_key: ElevenLabs API key. Falls back to `ELEVENLABS_API_KEY` env.
        stability: Voice setting (0.0-1.0).
        similarity_boost: Voice setting (0.0-1.0).
        style: Voice setting (0.0-1.0).
        speed: Voice setting (~0.7-1.2).
        output_format: ElevenLabs SDK output format string. Pass the special
            value `"mp3_only"` to skip the WAV decode and return the MP3 path.
        sample_rate_hz: WAV resample target. Default 48000 to match pipeline.
        max_retries: Retry count on 429/5xx/network errors. Default 3.
        backoff_base_s: Exponential backoff base. Sleep = base * 2**attempt.

    Returns:
        Path to the WAV file (or MP3 file when `output_format == "mp3_only"`).

    Raises:
        ElevenLabsError: for any failure — missing SDK, missing API key,
            exhausted retries, decode failure, or unexpected SDK return shape.
    """
    if not text or not text.strip():
        raise ElevenLabsError("synthesize: `text` must be non-empty")

    # Resolve API key first so we fail loud before importing the SDK.
    resolved_key = api_key if api_key is not None else os.environ.get("ELEVENLABS_API_KEY")
    if not resolved_key:
        raise ElevenLabsError(
            "ElevenLabs API key not provided: pass `api_key=` or set the "
            "ELEVENLABS_API_KEY environment variable."
        )

    # Lazy import — keeps the rest of the pipeline importable when `elevenlabs`
    # is not installed (e.g., before the operator activates the provider).
    try:
        from elevenlabs.client import ElevenLabs  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ElevenLabsError(
            "ElevenLabs SDK not installed; pip install elevenlabs to activate. "
            "See module docstring for the 5-step activation flow."
        ) from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_only = output_format == "mp3_only"
    sdk_output_format = DEFAULT_OUTPUT_FORMAT if mp3_only else output_format
    mp3_path = out_path.with_suffix(".mp3")

    client = ElevenLabs(api_key=resolved_key)

    # Voice settings dict — passed via the SDK's `voice_settings` kwarg. Recent
    # SDK versions accept either a dict or a `VoiceSettings` model; dict is
    # forward-compatible.
    voice_settings = {
        "stability": float(stability),
        "similarity_boost": float(similarity_boost),
        "style": float(style),
        "speed": float(speed),
    }

    last_exc: BaseException | None = None
    audio_bytes: bytes | None = None
    for attempt in range(max_retries + 1):
        try:
            log.info(
                "elevenlabs: synthesize attempt %d/%d — voice=%s model=%s chars=%d",
                attempt + 1, max_retries + 1, voice_id, model_id, len(text),
            )
            audio = client.text_to_speech.convert(
                voice_id=voice_id,
                model_id=model_id,
                text=text,
                voice_settings=voice_settings,
                output_format=sdk_output_format,
            )
            audio_bytes = _consume_audio_stream(audio)
            break
        except ElevenLabsError:
            # Don't retry our own validation errors raised inside _consume_audio_stream.
            raise
        except Exception as exc:  # noqa: BLE001 — duck-typed retry classifier below
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                raise ElevenLabsError(
                    f"ElevenLabs synthesis failed after {attempt + 1} attempt(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            sleep_s = backoff_base_s * (2 ** attempt)
            log.warning(
                "elevenlabs: retryable error on attempt %d (%s: %s) — sleeping %.2fs",
                attempt + 1, type(exc).__name__, exc, sleep_s,
            )
            time.sleep(sleep_s)

    if audio_bytes is None:  # defensive — loop should have either set or raised
        raise ElevenLabsError(
            f"ElevenLabs synthesis produced no audio (last exc: {last_exc!r})"
        )

    mp3_path.write_bytes(audio_bytes)
    log.info("elevenlabs wrote %s (%.1f KB)", mp3_path.name, mp3_path.stat().st_size / 1024)

    if mp3_only:
        return mp3_path

    _decode_mp3_to_wav(mp3_path, out_path, sample_rate_hz)
    log.info(
        "decoded to %s (%d Hz mono, %.1f KB)",
        out_path.name, sample_rate_hz, out_path.stat().st_size / 1024,
    )
    return out_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize text to a 48 kHz mono WAV via ElevenLabs (Brian voice). "
                    "Dormant by default — see module docstring for activation steps.",
    )
    parser.add_argument("--text", required=True, help="Text to synthesize.")
    parser.add_argument(
        "--out", required=True, type=Path,
        help="Output WAV path (or MP3 path when --no-wav is set).",
    )
    parser.add_argument("--voice-id", default=DEFAULT_VOICE_ID, help="ElevenLabs voice id.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL, help="ElevenLabs model id.")
    parser.add_argument("--stability", type=float, default=0.45)
    parser.add_argument("--similarity", type=float, default=0.85, dest="similarity_boost")
    parser.add_argument("--style", type=float, default=0.30)
    parser.add_argument("--speed", type=float, default=1.05)
    parser.add_argument(
        "--sample-rate-hz", type=int, default=DEFAULT_SAMPLE_RATE_HZ,
        help="WAV resample target (default 48000 — matches pipeline contract).",
    )
    parser.add_argument(
        "--no-wav", action="store_true",
        help="Skip MP3->WAV decode and return the MP3 path (debug).",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="Retry count for 429/5xx/network errors (default 3).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    output_format = "mp3_only" if args.no_wav else DEFAULT_OUTPUT_FORMAT
    try:
        result = synthesize(
            args.text,
            args.out,
            voice_id=args.voice_id,
            model_id=args.model_id,
            stability=args.stability,
            similarity_boost=args.similarity_boost,
            style=args.style,
            speed=args.speed,
            output_format=output_format,
            sample_rate_hz=args.sample_rate_hz,
            max_retries=args.max_retries,
        )
    except ElevenLabsError as exc:
        log.error("synthesis failed: %s", exc)
        return 1
    print(str(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
