"""Two-pass FFmpeg EBU R128 loudness normalization for ShadowVerse VO.

Replaces the inline single-pass `loudnorm=I=-14:LRA=11:TP=-1.5` filter that
lived in `pipeline._vo_edge_tts`. Single-pass loudnorm is dynamic-range-
compressed and only approximates the target — it drifted all 9 prior masters
to -15.1 .. -15.6 LUFS instead of -14.0. The two-pass approach measures
first, then applies the linear correction so the output lands within ±0.5 LU
of the target.

CLI:
    python tools/audio_loudnorm.py --src in.wav --dst out.wav
    python tools/audio_loudnorm.py --src in.wav --dst out.wav \\
        --target-lufs -14.0 --target-tp -1.0 --target-lra 11.0

Stdout on success: a single-line JSON object containing the pass-1 measurement
dict plus `output_lufs_measured` (a separate ffprobe-style measurement of the
normalized output). Exit 0 on success, non-zero on failure.

Library use:
    from tools.audio_loudnorm import normalize_vo
    measurements = normalize_vo(src_wav, dst_wav)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("audio_loudnorm")

# Default EBU R128 targets. -14 LUFS is YouTube/Spotify's normalization target;
# TP=-1.0 dBTP gives 1 dB headroom for true-peak; LRA=11 LU is a reasonable
# loudness range for spoken-word VO.
DEFAULT_TARGET_LUFS = -14.0
DEFAULT_TARGET_TP = -1.0
DEFAULT_TARGET_LRA = 11.0
DEFAULT_SAMPLE_RATE_HZ = 48000

# Keys we expect from the FFmpeg loudnorm pass-1 JSON object. All are floats
# in the FFmpeg output (string-encoded), parsed to Python floats by us.
_EXPECTED_FLOAT_KEYS = (
    "input_i",
    "input_tp",
    "input_lra",
    "input_thresh",
    "output_i",
    "output_tp",
    "output_lra",
    "output_thresh",
    "target_offset",
)
# `normalization_type` is a string ("linear" or "dynamic"); it is required
# but not coerced to float.
_EXPECTED_STR_KEYS = ("normalization_type",)


class LoudnormError(RuntimeError):
    """Raised when FFmpeg fails or its loudnorm JSON output cannot be parsed."""


def _parse_loudnorm_json(stderr_text: str) -> dict:
    """Extract the trailing JSON object from FFmpeg's loudnorm pass-1 stderr.

    FFmpeg emits a `[Parsed_loudnorm_0 @ ...]` log line followed by a
    pretty-printed JSON object. We locate the LAST `{...}` block in stderr
    and parse it — this is robust to leading log lines.

    Args:
        stderr_text: full captured stderr from `ffmpeg ... -af loudnorm=...:print_format=json`.

    Returns:
        Dict with all keys from `_EXPECTED_FLOAT_KEYS` (as floats) plus
        `normalization_type` (str).

    Raises:
        LoudnormError: no JSON block found, JSON unparseable, or expected
                       keys missing / not coercible to float.
    """
    matches = list(re.finditer(r"\{[^{}]*\}", stderr_text, flags=re.DOTALL))
    if not matches:
        raise LoudnormError(
            "no JSON object found in ffmpeg stderr; loudnorm pass-1 may have failed. "
            f"stderr tail: {stderr_text[-400:]!r}"
        )
    raw = matches[-1].group(0)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LoudnormError(f"could not parse loudnorm JSON: {e}; raw={raw!r}") from e

    out: dict = {}
    for key in _EXPECTED_FLOAT_KEYS:
        if key not in parsed:
            raise LoudnormError(f"loudnorm JSON missing expected key {key!r}: {parsed!r}")
        try:
            out[key] = float(parsed[key])
        except (TypeError, ValueError) as e:
            raise LoudnormError(
                f"loudnorm JSON key {key!r} not float-coercible: {parsed[key]!r}"
            ) from e
    for key in _EXPECTED_STR_KEYS:
        if key not in parsed:
            raise LoudnormError(f"loudnorm JSON missing expected key {key!r}: {parsed!r}")
        out[key] = str(parsed[key])
    return out


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke ffmpeg with the given arg list. Raises LoudnormError on non-zero exit."""
    log.debug("ffmpeg argv: %s", args)
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise LoudnormError(
            "ffmpeg binary not found on PATH; install ffmpeg and re-run."
        ) from e
    if proc.returncode != 0:
        raise LoudnormError(
            f"ffmpeg exited {proc.returncode}; stderr tail:\n{proc.stderr[-800:]}"
        )
    return proc


def _measure_loudness(wav: Path, target_lufs: float, target_tp: float,
                      target_lra: float) -> dict:
    """Run a measurement-only loudnorm pass against `wav` and return the parsed dict."""
    args = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(wav),
        "-af",
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ]
    proc = _run_ffmpeg(args)
    return _parse_loudnorm_json(proc.stderr)


def normalize_vo(
    src_wav: Path,
    dst_wav: Path,
    *,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    target_tp: float = DEFAULT_TARGET_TP,
    target_lra: float = DEFAULT_TARGET_LRA,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
) -> dict:
    """Run two-pass FFmpeg loudnorm on `src_wav`, write `dst_wav`, return measurements.

    Pass 1 measures input integrated loudness, true-peak, and loudness-range.
    Pass 2 applies the linear correction using those measurements, producing
    output that lands within ±0.5 LU of `target_lufs`.

    Args:
        src_wav: path to the input WAV file (must exist).
        dst_wav: path to write the normalized WAV (parent dir created if needed).
        target_lufs: integrated loudness target in LUFS (default -14.0).
        target_tp: true-peak ceiling in dBTP (default -1.0).
        target_lra: loudness-range target in LU (default 11.0).
        sample_rate_hz: output sample rate (default 48000).

    Returns:
        Dict with keys `input_i`, `input_tp`, `input_lra`, `input_thresh`,
        `output_i`, `output_tp`, `output_lra`, `output_thresh`, `target_offset`
        (all float), and `normalization_type` (str). These are the pass-1
        measurements, not the final post-pass-2 measurement (use the CLI or
        a separate `_measure_loudness` call for that).

    Raises:
        FileNotFoundError: `src_wav` does not exist.
        LoudnormError: ffmpeg fails or its JSON output cannot be parsed.
    """
    src_wav = Path(src_wav)
    dst_wav = Path(dst_wav)
    if not src_wav.exists():
        raise FileNotFoundError(f"src_wav not found: {src_wav}")
    dst_wav.parent.mkdir(parents=True, exist_ok=True)

    log.info("pass 1: measuring %s (target I=%.1f TP=%.1f LRA=%.1f)",
             src_wav.name, target_lufs, target_tp, target_lra)
    pass1 = _measure_loudness(src_wav, target_lufs, target_tp, target_lra)
    log.debug("pass-1 measurements: %s", pass1)
    log.info("input_i=%.2f LUFS  input_tp=%.2f dBTP  input_lra=%.2f LU",
             pass1["input_i"], pass1["input_tp"], pass1["input_lra"])

    # Pass 2: apply linear correction with measured values.
    pass2_filter = (
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}"
        f":measured_I={pass1['input_i']}"
        f":measured_TP={pass1['input_tp']}"
        f":measured_LRA={pass1['input_lra']}"
        f":measured_thresh={pass1['input_thresh']}"
        f":offset={pass1['target_offset']}"
        f":linear=true:print_format=summary"
    )
    log.info("pass 2: writing %s (linear=true, offset=%.2f)",
             dst_wav.name, pass1["target_offset"])
    args = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-i", str(src_wav),
        "-af", pass2_filter,
        "-ar", str(sample_rate_hz),
        "-ac", "1",
        "-acodec", "pcm_s16le",
        str(dst_wav),
    ]
    _run_ffmpeg(args)
    if not dst_wav.exists():
        raise LoudnormError(f"pass-2 ffmpeg succeeded but dst_wav not written: {dst_wav}")
    log.info("normalized %s -> %s (%.1f KB)",
             src_wav.name, dst_wav.name, dst_wav.stat().st_size / 1024)
    return pass1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Two-pass EBU R128 loudness normalization. Lands within ±0.5 LU of the "
            "configured target. Replaces single-pass loudnorm in pipeline._vo_edge_tts."
        ),
    )
    parser.add_argument("--src", required=True, type=Path,
                        help="Source WAV path.")
    parser.add_argument("--dst", required=True, type=Path,
                        help="Destination WAV path (overwritten if exists).")
    parser.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS,
                        help=f"Integrated loudness target in LUFS "
                             f"(default {DEFAULT_TARGET_LUFS}).")
    parser.add_argument("--target-tp", type=float, default=DEFAULT_TARGET_TP,
                        help=f"True-peak ceiling in dBTP (default {DEFAULT_TARGET_TP}).")
    parser.add_argument("--target-lra", type=float, default=DEFAULT_TARGET_LRA,
                        help=f"Loudness-range target in LU (default {DEFAULT_TARGET_LRA}).")
    parser.add_argument("--sample-rate-hz", type=int, default=DEFAULT_SAMPLE_RATE_HZ,
                        help=f"Output sample rate (default {DEFAULT_SAMPLE_RATE_HZ}).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        measurements = normalize_vo(
            args.src,
            args.dst,
            target_lufs=args.target_lufs,
            target_tp=args.target_tp,
            target_lra=args.target_lra,
            sample_rate_hz=args.sample_rate_hz,
        )
        # Verify with a fresh measurement pass on the output.
        post = _measure_loudness(
            args.dst, args.target_lufs, args.target_tp, args.target_lra,
        )
        report = dict(measurements)
        report["output_lufs_measured"] = post["input_i"]
        delta = abs(post["input_i"] - args.target_lufs)
        log.info("output_lufs_measured=%.2f LUFS (delta=%.2f LU vs target %.1f)",
                 post["input_i"], delta, args.target_lufs)
        if delta > 0.5:
            log.warning("output drifted %.2f LU from target — exceeds ±0.5 LU tolerance",
                        delta)
        print(json.dumps(report))
        return 0
    except FileNotFoundError as e:
        log.error("input file missing: %s", e)
        print(f"error: {e}", file=sys.stderr)
        return 2
    except LoudnormError as e:
        log.error("loudnorm failed: %s", e)
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
