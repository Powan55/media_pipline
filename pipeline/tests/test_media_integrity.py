"""Unit tests for tools/media_integrity.py.

Uses stdlib `unittest` because pytest is not installed in the venv. The same
test structure is pytest-compatible if a `pytest` is dropped in later — the
@unittest.skipUnless guards on ffmpeg presence translate cleanly to
@pytest.mark.skipif.

All fixtures are SYNTHESIZED in a temp dir via ffmpeg lavfi. Tests do NOT
touch real channel-root files (per orchestrator instruction T2.3).

Note on `min_size_bytes`: the synthesized 3-second `testsrc` masters land
around 100-200 KB, well under the 1 MB production default. Tests pass
`min_size_bytes=10_000` (or `5_000`) so size checks don't mask the
ffprobe / deep-decode logic we're actually exercising. Production callers
should keep the 1 MB default.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable so `from tools.media_integrity import ...` works
# regardless of how pytest / unittest discovers this file.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.media_integrity import (  # noqa: E402
    CLI_EXIT_FILE_NOT_FOUND,
    CLI_EXIT_INTEGRITY_FAIL,
    CLI_EXIT_OK,
    MediaIntegrityError,
    check_integrity,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _synthesize_good_master(out_path: Path, duration_s: int = 3) -> Path:
    """Generate a known-good 1080x1920 H.264/AAC MP4 via ffmpeg lavfi.

    Uses `-preset ultrafast` + `-shortest` to keep the test fast while still
    producing a real fragmented MP4 with a `moov` atom.
    """
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration_s}:rate=30:size=1080x1920",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    return out_path


def _synthesize_audio_only(out_path: Path, duration_s: int = 3) -> Path:
    """Generate an audio-only M4A — no video stream."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
        "-c:a", "aac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    return out_path


def _truncate(path: Path, keep_bytes: int) -> None:
    """Truncate `path` in place to keep only the first `keep_bytes` bytes."""
    with path.open("r+b") as f:
        f.truncate(keep_bytes)


# ---------------------------------------------------------------------------
# Test base — synthesizes shared fixtures once per class.
# ---------------------------------------------------------------------------


@unittest.skipUnless(_ffmpeg_on_path(), "ffmpeg/ffprobe not on PATH")
class MediaIntegrityTests(unittest.TestCase):
    tmp_root: Path
    good_master: Path
    truncated_master: Path
    audio_only: Path
    tiny_file: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="media_integrity_test_")
        cls.tmp_root = Path(cls._tmpdir.name)

        # 1) Known-good master.
        cls.good_master = _synthesize_good_master(cls.tmp_root / "good.mp4")
        assert cls.good_master.stat().st_size > 10_000, "synthesized master too small"

        # 2) Truncated copy — keep only the first ~8 KB to wipe the moov atom.
        cls.truncated_master = cls.tmp_root / "truncated.mp4"
        shutil.copy2(cls.good_master, cls.truncated_master)
        _truncate(cls.truncated_master, 8 * 1024)

        # 3) Audio-only file (no video stream).
        cls.audio_only = _synthesize_audio_only(cls.tmp_root / "audio_only.m4a")

        # 4) Tiny 100-byte file — well under min_size_bytes default.
        cls.tiny_file = cls.tmp_root / "tiny.mp4"
        cls.tiny_file.write_bytes(b"\x00" * 100)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    # ----- happy path ------------------------------------------------------

    def test_known_good_master_passes(self) -> None:
        # `testsrc` ultrafast masters are ~100-200 KB; lower min_size so the
        # check exercises ffprobe + deep-decode rather than tripping on size.
        result = check_integrity(self.good_master, min_size_bytes=10_000)

        # All keys present per spec.
        for key in (
            "path", "size_bytes", "duration_s", "video_codec", "video_resolution",
            "audio_codec", "audio_channels", "audio_sample_rate", "deep_decode_ok",
        ):
            self.assertIn(key, result, f"missing key {key!r} in result dict")

        self.assertEqual(result["video_resolution"], (1080, 1920))
        self.assertEqual(result["video_codec"], "h264")
        self.assertEqual(result["audio_codec"], "aac")
        self.assertGreaterEqual(result["audio_channels"], 1)
        self.assertEqual(result["audio_sample_rate"], 44100)
        self.assertGreaterEqual(result["duration_s"], 1.0)
        self.assertTrue(result["deep_decode_ok"])

    # ----- error paths -----------------------------------------------------

    def test_truncated_file_raises_with_moov_or_ffprobe_message(self) -> None:
        # Lower min_size to 5_000 so the 8_192-byte truncated fixture passes
        # the size gate and reaches ffprobe — that's the failure mode we want
        # to assert on (per spec AC1).
        with self.assertRaises(MediaIntegrityError) as cm:
            check_integrity(self.truncated_master, min_size_bytes=5_000)
        msg = str(cm.exception).lower()
        # Spec AC1: message contains "moov" OR a generic ffprobe-failed message.
        self.assertTrue(
            "moov" in msg or "ffprobe" in msg,
            f"expected 'moov' or 'ffprobe' in error, got: {msg!r}",
        )

    def test_missing_file_raises_filenotfound(self) -> None:
        with self.assertRaises(FileNotFoundError):
            check_integrity(self.tmp_root / "does_not_exist.mp4")

    def test_under_min_size_raises(self) -> None:
        with self.assertRaises(MediaIntegrityError) as cm:
            check_integrity(self.tiny_file)
        self.assertIn("size", str(cm.exception).lower())

    def test_audio_only_when_require_video_raises(self) -> None:
        # Use a smaller min_size so the synthesized audio file isn't rejected
        # earlier on size alone.
        with self.assertRaises(MediaIntegrityError) as cm:
            check_integrity(
                self.audio_only,
                min_size_bytes=1_000,
                require_video=True,
                require_audio=True,
                deep_decode_seconds=0.0,
            )
        self.assertIn("video", str(cm.exception).lower())

    def test_audio_only_with_no_video_required_passes(self) -> None:
        result = check_integrity(
            self.audio_only,
            min_size_bytes=1_000,
            require_video=False,
            require_audio=True,
            deep_decode_seconds=0.5,
        )
        self.assertIsNone(result["video_resolution"])
        self.assertIsNone(result["video_codec"])
        self.assertEqual(result["audio_codec"], "aac")

    def test_empty_file_raises(self) -> None:
        empty = self.tmp_root / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(MediaIntegrityError) as cm:
            check_integrity(empty)
        self.assertIn("empty", str(cm.exception).lower())

    # ----- CLI -------------------------------------------------------------

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(REPO_ROOT / "tools" / "media_integrity.py"), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_cli_pass_exits_zero(self) -> None:
        proc = self._run_cli(str(self.good_master), "--min-size", "10000")
        self.assertEqual(
            proc.returncode, CLI_EXIT_OK,
            f"expected 0, got {proc.returncode}; stderr: {proc.stderr!r}",
        )
        self.assertIn("PASS", proc.stdout)

    def test_cli_fail_exits_nonzero(self) -> None:
        proc = self._run_cli(str(self.truncated_master), "--min-size", "5000")
        self.assertEqual(
            proc.returncode, CLI_EXIT_INTEGRITY_FAIL,
            f"expected {CLI_EXIT_INTEGRITY_FAIL}, got {proc.returncode}; "
            f"stderr: {proc.stderr!r}",
        )
        self.assertIn("FAIL", proc.stdout)

    def test_cli_missing_file_exits_distinct_code(self) -> None:
        proc = self._run_cli(str(self.tmp_root / "nope.mp4"))
        self.assertEqual(proc.returncode, CLI_EXIT_FILE_NOT_FOUND)

    def test_cli_json_mode_emits_valid_json_on_pass(self) -> None:
        proc = self._run_cli(str(self.good_master), "--min-size", "10000", "--json")
        self.assertEqual(proc.returncode, CLI_EXIT_OK, proc.stderr)
        # Last non-empty stdout line should be the JSON payload.
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertTrue(lines, "no stdout produced by --json mode")
        payload = json.loads(lines[-1])
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("video_codec"), "h264")
        # Resolution serialized as a 2-element list.
        self.assertEqual(payload.get("video_resolution"), [1080, 1920])

    def test_cli_json_mode_emits_valid_json_on_fail(self) -> None:
        proc = self._run_cli(str(self.truncated_master), "--min-size", "5000", "--json")
        self.assertEqual(proc.returncode, CLI_EXIT_INTEGRITY_FAIL)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertTrue(lines, "no stdout produced by --json mode on fail")
        payload = json.loads(lines[-1])
        self.assertFalse(payload.get("ok"))
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
