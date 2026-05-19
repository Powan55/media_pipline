"""Media-integrity gate — catch broken video files before they ship.

The corrupt `2026-05-06_002_tt.mp4` (13 MB vs ~38 MB peers, missing `moov`
atom) almost shipped without anyone noticing — there was no integrity check
between render and export. This module fills that gap.

`check_integrity()` runs three stacked checks:
  1. file exists and is at least `min_size_bytes` (default 1 MB)
  2. ffprobe parses the container, returns at least one stream, and reports
     a duration >= `min_duration_s`
  3. a deep-decode pass (`ffmpeg -v error -ss 0 -t N -i PATH -f null -`)
     actually decodes frames — catches files that ffprobe accepts but that
     blow up on a real read

Any failure raises `MediaIntegrityError` with a specific message. On success,
returns a diagnostic dict suitable for logging.

Importable as:
    from tools.media_integrity import check_integrity, MediaIntegrityError

CLI:
    python tools/media_integrity.py PATH
    python tools/media_integrity.py PATH --json
    python tools/media_integrity.py PATH --no-audio --min-duration 0.5

Exits 0 on PASS, non-zero on FAIL. Different non-zero codes for missing
file vs. integrity failure (see CLI_EXIT_* constants below).

Dependencies: stdlib only. Requires `ffmpeg` and `ffprobe` on PATH. Does NOT
use the `ffmpeg-python` wrapper here — we want the exact ffprobe JSON output
and the exact ffmpeg stderr lines, both of which are easier to consume via
`subprocess.run` directly.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("media_integrity")

# CLI exit codes — distinct so callers / CI can branch on the failure class.
CLI_EXIT_OK = 0
CLI_EXIT_INTEGRITY_FAIL = 1
CLI_EXIT_FILE_NOT_FOUND = 2
CLI_EXIT_USAGE = 3

# Per spec AC6: runtime <2s per file for 30-50 MB masters. Both subprocesses
# get a generous timeout so a hung ffmpeg doesn't wedge the pipeline.
_FFPROBE_TIMEOUT_S = 30
_FFMPEG_DEEP_DECODE_TIMEOUT_S = 30


class MediaIntegrityError(RuntimeError):
    """Raised when a media file fails any integrity check.

    The message is intentionally specific (mentions `moov`, `size`, `duration`,
    `video stream`, etc.) so callers can surface a clear failure reason to the
    operator without re-running diagnostics.
    """

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"media integrity failed for {path}: {reason}")


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg shell-outs
# ---------------------------------------------------------------------------


def _run_ffprobe(path: Path) -> tuple[int, str, str]:
    """Invoke ffprobe and return (returncode, stdout, stderr).

    Uses the exact form the spec calls for (AC4):
        ffprobe -v error -show_format -show_streams -of json PATH
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    log.debug("ffprobe cmd: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FFPROBE_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_deep_decode(path: Path, seconds: float) -> tuple[int, str]:
    """Invoke ffmpeg in null-mux mode to actually decode frames; returns (rc, stderr).

    Per spec AC3: `ffmpeg -v error -ss 0 -t N -i PATH -f null -` and consider
    exit-code=0 with `frame=` lines in stderr as PASS. We add `-stats` so the
    `frame=` progress lines appear on stderr regardless of log level — without
    `-stats`, `-v error` suppresses them.
    """
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-stats",
        "-nostdin",
        "-ss", "0",
        "-t", f"{seconds}",
        "-i", str(path),
        "-f", "null",
        "-",
    ]
    log.debug("ffmpeg deep-decode cmd: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FFMPEG_DEEP_DECODE_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stderr


# Matches both `frame=  123` and `frame=123` styles emitted by ffmpeg's `-stats`.
_FRAME_RE = re.compile(r"\bframe=\s*(\d+)\b")
_TIME_RE = re.compile(r"\btime=\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)\b")


def _parse_frame_count(ffmpeg_stderr: str) -> int:
    """Pull the highest `frame=` number out of ffmpeg `-stats` output. Returns 0 if none."""
    matches = _FRAME_RE.findall(ffmpeg_stderr)
    if not matches:
        return 0
    return max(int(m) for m in matches)


def _parse_decode_time(ffmpeg_stderr: str) -> float:
    """Pull the largest `time=HH:MM:SS.ms` value out of ffmpeg `-stats` output, in seconds.

    Used for audio-only files where there's no `frame=` counter to inspect.
    Returns 0.0 if no `time=` line was emitted.
    """
    matches = _TIME_RE.findall(ffmpeg_stderr)
    if not matches:
        return 0.0
    secs = [int(h) * 3600 + int(m) * 60 + float(s) for h, m, s in matches]
    return max(secs)


# ---------------------------------------------------------------------------
# Stream-info extraction
# ---------------------------------------------------------------------------


def _pick_video_stream(streams: list[dict]) -> dict | None:
    for s in streams:
        if s.get("codec_type") == "video":
            return s
    return None


def _pick_audio_stream(streams: list[dict]) -> dict | None:
    for s in streams:
        if s.get("codec_type") == "audio":
            return s
    return None


def _resolution_or_none(video_stream: dict | None) -> tuple[int, int] | None:
    if not video_stream:
        return None
    w = video_stream.get("width")
    h = video_stream.get("height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return (w, h)
    return None


def _duration_seconds(probe: dict, video_stream: dict | None) -> float | None:
    """Best-effort duration in seconds from format → video stream fallback."""
    fmt = probe.get("format") or {}
    dur_raw = fmt.get("duration")
    if dur_raw is not None:
        try:
            return float(dur_raw)
        except (TypeError, ValueError):
            pass
    if video_stream is not None:
        v_dur = video_stream.get("duration")
        if v_dur is not None:
            try:
                return float(v_dur)
            except (TypeError, ValueError):
                pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_integrity(
    path: Path,
    *,
    min_size_bytes: int = 1_000_000,
    min_duration_s: float = 1.0,
    require_video: bool = True,
    require_audio: bool = True,
    deep_decode_seconds: float = 1.0,
) -> dict:
    """Verify a media file is structurally sound and decodes cleanly.

    Args:
        path: Media file to verify.
        min_size_bytes: Reject files under this size. Default 1 MB — anything
            smaller is almost certainly truncated for our 30-50s shorts.
        min_duration_s: Reject files whose ffprobe duration is below this.
        require_video: If True, fail when there's no video stream.
        require_audio: If True, fail when there's no audio stream.
        deep_decode_seconds: Decode this many seconds from t=0 to confirm
            frames actually come out. 1.0s is enough to catch a missing
            `moov` or a corrupt header.

    Returns:
        Dict with keys: `path`, `size_bytes`, `duration_s`, `video_codec`,
        `video_resolution` (tuple[int, int] or None), `audio_codec`,
        `audio_channels`, `audio_sample_rate`, `deep_decode_ok` (bool).

    Raises:
        FileNotFoundError: file does not exist on disk.
        MediaIntegrityError: any other integrity failure (empty / under
            min_size / ffprobe failed / no streams / missing required
            stream / duration too short / deep-decode failed).
    """
    if not isinstance(path, Path):
        path = Path(path)

    log.info("checking integrity: %s", path)

    if not path.exists():
        raise FileNotFoundError(f"media file does not exist: {path}")
    if not path.is_file():
        raise MediaIntegrityError(path, f"path exists but is not a regular file")

    size_bytes = path.stat().st_size
    if size_bytes == 0:
        raise MediaIntegrityError(path, "file is empty (size=0)")
    if size_bytes < min_size_bytes:
        raise MediaIntegrityError(
            path,
            f"size {size_bytes} bytes is under min_size_bytes={min_size_bytes}",
        )

    # ---- ffprobe ----------------------------------------------------------
    try:
        rc, stdout, stderr = _run_ffprobe(path)
    except FileNotFoundError as e:
        # ffprobe binary itself missing on PATH.
        raise MediaIntegrityError(path, f"ffprobe binary not found on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise MediaIntegrityError(path, f"ffprobe timed out after {e.timeout}s") from e

    if rc != 0:
        # ffprobe stderr typically mentions "moov atom not found" for the
        # truncated-MP4 case we care about. Surface its full text.
        stderr_clean = stderr.strip() or "(no stderr)"
        raise MediaIntegrityError(
            path,
            f"ffprobe failed (rc={rc}): {stderr_clean}",
        )

    try:
        probe = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as e:
        raise MediaIntegrityError(
            path, f"ffprobe stdout was not valid JSON: {e}"
        ) from e

    streams = probe.get("streams") or []
    if not streams:
        raise MediaIntegrityError(path, "ffprobe returned no streams")

    video = _pick_video_stream(streams)
    audio = _pick_audio_stream(streams)

    if require_video and video is None:
        raise MediaIntegrityError(path, "no video stream found (require_video=True)")
    if require_audio and audio is None:
        raise MediaIntegrityError(path, "no audio stream found (require_audio=True)")

    duration_s = _duration_seconds(probe, video)
    if duration_s is None:
        raise MediaIntegrityError(path, "ffprobe did not report a duration")
    if duration_s < min_duration_s:
        raise MediaIntegrityError(
            path,
            f"duration {duration_s:.3f}s is under min_duration_s={min_duration_s}",
        )

    # ---- deep decode ------------------------------------------------------
    # We run ffmpeg in null-mux mode and look at exit code + stderr. ffmpeg's
    # `-stats` output reports `frame=N` for video and `time=HH:MM:SS.ms` for
    # any stream (including audio-only). For files with a video stream we
    # require `frame>0` to confirm the decoder actually produced frames; for
    # audio-only files we require non-zero `time=` since there's no `frame=`
    # counter.
    deep_decode_ok = False
    if deep_decode_seconds > 0:
        try:
            dec_rc, dec_stderr = _run_deep_decode(path, deep_decode_seconds)
        except FileNotFoundError as e:
            raise MediaIntegrityError(path, f"ffmpeg binary not found on PATH: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise MediaIntegrityError(
                path, f"ffmpeg deep-decode timed out after {e.timeout}s"
            ) from e

        if dec_rc != 0:
            stderr_clean = dec_stderr.strip() or "(no stderr)"
            raise MediaIntegrityError(
                path,
                f"deep-decode failed (rc={dec_rc}): {stderr_clean}",
            )

        if video is not None:
            frames = _parse_frame_count(dec_stderr)
            if frames <= 0:
                stderr_clean = dec_stderr.strip() or "(no stderr)"
                raise MediaIntegrityError(
                    path,
                    f"deep-decode produced 0 video frames (rc=0): {stderr_clean}",
                )
            log.debug("deep-decode ok: %d video frame(s) decoded in %.2fs window",
                      frames, deep_decode_seconds)
        else:
            # Audio-only path — `time=` is the only signal. ffmpeg always prints
            # a final `time=` in its `-stats` output if it actually moved data.
            decoded_time = _parse_decode_time(dec_stderr)
            if decoded_time <= 0.0:
                stderr_clean = dec_stderr.strip() or "(no stderr)"
                raise MediaIntegrityError(
                    path,
                    f"deep-decode produced 0 seconds of audio (rc=0): {stderr_clean}",
                )
            log.debug("deep-decode ok: %.2fs of audio decoded", decoded_time)
        deep_decode_ok = True

    result: dict = {
        "path": str(path),
        "size_bytes": size_bytes,
        "duration_s": duration_s,
        "video_codec": (video or {}).get("codec_name"),
        "video_resolution": _resolution_or_none(video),
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_channels": (audio or {}).get("channels"),
        "audio_sample_rate": (
            int(audio["sample_rate"])
            if audio and audio.get("sample_rate") is not None
            else None
        ),
        "deep_decode_ok": deep_decode_ok,
    }
    log.info(
        "integrity ok: %s (%.1f MB, %.2fs, %s %s, %s %sch %sHz)",
        path.name,
        size_bytes / 1e6,
        duration_s,
        result["video_codec"],
        result["video_resolution"],
        result["audio_codec"],
        result["audio_channels"],
        result["audio_sample_rate"],
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a media file is structurally sound and decodes cleanly. "
            "Exits 0 on PASS; non-zero on FAIL."
        ),
    )
    parser.add_argument("path", help="Path to the media file to verify.")
    parser.add_argument(
        "--min-size", type=int, default=1_000_000,
        help="Reject files smaller than N bytes (default: 1_000_000).",
    )
    parser.add_argument(
        "--min-duration", type=float, default=1.0,
        help="Reject files shorter than N seconds (default: 1.0).",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Allow audio-only files (do not require a video stream).",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Allow silent files (do not require an audio stream).",
    )
    parser.add_argument(
        "--deep", type=float, default=1.0,
        help=(
            "Seconds of deep-decode to run from t=0 (default: 1.0). "
            "Set to 0 to skip the deep-decode test (ffprobe only)."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the diagnostic dict to stdout as one-line JSON.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging (ffprobe / ffmpeg cmds, frame counts).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    target = Path(args.path)

    try:
        result = check_integrity(
            target,
            min_size_bytes=args.min_size,
            min_duration_s=args.min_duration,
            require_video=not args.no_video,
            require_audio=not args.no_audio,
            deep_decode_seconds=args.deep,
        )
    except FileNotFoundError as e:
        log.error("%s", e)
        if args.json:
            print(json.dumps({"path": str(target), "ok": False, "error": str(e)}))
        else:
            print(f"FAIL (file not found): {target}")
        return CLI_EXIT_FILE_NOT_FOUND
    except MediaIntegrityError as e:
        log.error("%s", e)
        if args.json:
            print(json.dumps({
                "path": str(target),
                "ok": False,
                "error": str(e),
                "reason": e.reason,
            }))
        else:
            print(f"FAIL: {e}")
        return CLI_EXIT_INTEGRITY_FAIL

    if args.json:
        # tuple → list so it round-trips through json.
        out = dict(result)
        if isinstance(out.get("video_resolution"), tuple):
            out["video_resolution"] = list(out["video_resolution"])
        out["ok"] = True
        print(json.dumps(out))
    else:
        res = result["video_resolution"]
        res_str = f"{res[0]}x{res[1]}" if res else "n/a"
        print(
            f"PASS: {target.name} — {result['size_bytes'] / 1e6:.1f} MB, "
            f"{result['duration_s']:.2f}s, "
            f"{result['video_codec']} {res_str}, "
            f"{result['audio_codec']} {result['audio_channels']}ch "
            f"{result['audio_sample_rate']}Hz, deep_decode_ok="
            f"{result['deep_decode_ok']}"
        )
    return CLI_EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
