"""Tests for tools/audio_loudnorm.py.

Uses stdlib `unittest` to avoid adding pytest as a dependency. Run via:
    python -m unittest tests.test_audio_loudnorm -v

The synthetic-signal test invokes the real ffmpeg binary; it is auto-skipped
if ffmpeg is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable so `tools.audio_loudnorm` resolves regardless
# of the cwd the test runner uses.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.audio_loudnorm import (  # noqa: E402
    LoudnormError,
    _measure_loudness,
    _parse_loudnorm_json,
    normalize_vo,
)


def _ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None


# A representative pass-1 stderr block from FFmpeg 7.x. Includes preceding log
# lines so we can verify the parser locates the trailing JSON object.
SAMPLE_PASS1_STDERR = """\
ffmpeg version 7.1.1
  built with gcc 14.2.0
  configuration: --enable-gpl
Input #0, wav, from '/tmp/x.wav':
  Duration: 00:00:05.00
Output #0, null, to 'pipe:':
[Parsed_loudnorm_0 @ 0x55d4f0a1c280]\x20
{
\t"input_i" : "-21.75",
\t"input_tp" : "-18.06",
\t"input_lra" : "0.00",
\t"input_thresh" : "-31.75",
\t"output_i" : "-14.05",
\t"output_tp" : "-10.31",
\t"output_lra" : "0.00",
\t"output_thresh" : "-24.05",
\t"normalization_type" : "linear",
\t"target_offset" : "0.05"
}
[out#0/null @ 0x55d4f0a01540] muxing overhead: unknown
"""


class TestParseLoudnormJson(unittest.TestCase):
    """Unit-level tests for `_parse_loudnorm_json`. No ffmpeg required."""

    def test_parse_pass1_json_extracts_measurements(self) -> None:
        """All expected float keys + normalization_type are parsed correctly."""
        result = _parse_loudnorm_json(SAMPLE_PASS1_STDERR)

        expected_floats = {
            "input_i": -21.75,
            "input_tp": -18.06,
            "input_lra": 0.00,
            "input_thresh": -31.75,
            "output_i": -14.05,
            "output_tp": -10.31,
            "output_lra": 0.00,
            "output_thresh": -24.05,
            "target_offset": 0.05,
        }
        for key, expected in expected_floats.items():
            self.assertIn(key, result, f"missing key {key!r}")
            self.assertIsInstance(result[key], float, f"{key!r} not float")
            self.assertAlmostEqual(result[key], expected, places=4)

        self.assertEqual(result["normalization_type"], "linear")

    def test_invalid_json_raises_loudnormerror(self) -> None:
        """stderr without a JSON block raises LoudnormError."""
        bad_stderr = "ffmpeg version 7.1.1\nSome error happened, no JSON here.\n"
        with self.assertRaises(LoudnormError) as ctx:
            _parse_loudnorm_json(bad_stderr)
        self.assertIn("no JSON object found", str(ctx.exception))

    def test_missing_key_raises_loudnormerror(self) -> None:
        """JSON missing an expected key raises LoudnormError citing the key."""
        # Drop output_thresh from an otherwise-valid block.
        partial = """\
[Parsed_loudnorm_0 @ 0x1]
{
"input_i" : "-21.75",
"input_tp" : "-18.06",
"input_lra" : "0.00",
"input_thresh" : "-31.75",
"output_i" : "-14.05",
"output_tp" : "-10.31",
"output_lra" : "0.00",
"normalization_type" : "linear",
"target_offset" : "0.05"
}
"""
        with self.assertRaises(LoudnormError) as ctx:
            _parse_loudnorm_json(partial)
        self.assertIn("output_thresh", str(ctx.exception))

    def test_non_float_value_raises_loudnormerror(self) -> None:
        """Float-typed key with a non-numeric value raises LoudnormError."""
        garbage = """\
[Parsed_loudnorm_0 @ 0x1]
{
"input_i" : "not-a-number",
"input_tp" : "-18.06",
"input_lra" : "0.00",
"input_thresh" : "-31.75",
"output_i" : "-14.05",
"output_tp" : "-10.31",
"output_lra" : "0.00",
"output_thresh" : "-24.05",
"normalization_type" : "linear",
"target_offset" : "0.05"
}
"""
        with self.assertRaises(LoudnormError):
            _parse_loudnorm_json(garbage)


class TestNormalizeVoErrors(unittest.TestCase):
    """Error-path tests that don't require ffmpeg to run."""

    def test_missing_src_raises_filenotfounderror(self) -> None:
        """Nonexistent source path raises FileNotFoundError before invoking ffmpeg."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with self.assertRaises(FileNotFoundError):
                normalize_vo(tdp / "does_not_exist.wav", tdp / "out.wav")

    def test_normalize_vo_propagates_json_parse_failure(self) -> None:
        """When pass-1 ffmpeg returns 0 but emits no JSON block, LoudnormError surfaces.

        Mocks subprocess.run so the test is hermetic — does not require a real
        ffmpeg invocation. Exercises the JSON-parse fallback path inside
        `_measure_loudness` -> `_parse_loudnorm_json`.
        """
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "in.wav"
            src.write_bytes(b"\x00" * 16)  # placeholder; ffmpeg is mocked
            dst = tdp / "out.wav"

            fake_proc = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0, stdout="",
                stderr="ffmpeg version 7.1.1\nno json here\n",
            )
            with mock.patch(
                "tools.audio_loudnorm.subprocess.run", return_value=fake_proc
            ):
                with self.assertRaises(LoudnormError) as ctx:
                    normalize_vo(src, dst)
            self.assertIn("no JSON object found", str(ctx.exception))

    def test_normalize_vo_propagates_ffmpeg_nonzero_exit(self) -> None:
        """ffmpeg pass-1 returning non-zero is surfaced as LoudnormError.

        Mocks subprocess.run to simulate a failed ffmpeg invocation (e.g.
        unreadable input format). The error must include stderr context so
        the operator can debug — bare RuntimeError without context fails the
        production-standards "no swallowed errors" rule.
        """
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "in.wav"
            src.write_bytes(b"\x00" * 16)
            dst = tdp / "out.wav"

            fake_proc = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=1, stdout="",
                stderr="Invalid data found when processing input\n",
            )
            with mock.patch(
                "tools.audio_loudnorm.subprocess.run", return_value=fake_proc
            ):
                with self.assertRaises(LoudnormError) as ctx:
                    normalize_vo(src, dst)
            msg = str(ctx.exception)
            self.assertIn("ffmpeg exited 1", msg)
            self.assertIn("Invalid data", msg)

    def test_normalize_vo_missing_ffmpeg_binary_raises_loudnormerror(self) -> None:
        """If ffmpeg is not on PATH, a LoudnormError is raised (not bare FNF)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "in.wav"
            src.write_bytes(b"\x00" * 16)
            dst = tdp / "out.wav"

            with mock.patch(
                "tools.audio_loudnorm.subprocess.run",
                side_effect=FileNotFoundError("ffmpeg"),
            ):
                with self.assertRaises(LoudnormError) as ctx:
                    normalize_vo(src, dst)
            self.assertIn("ffmpeg binary not found", str(ctx.exception))


@unittest.skipUnless(_ffmpeg_on_path(), "ffmpeg not on PATH")
class TestNormalizeVoIntegration(unittest.TestCase):
    """End-to-end tests that invoke the real ffmpeg binary."""

    def _make_sine(self, dst: Path, freq: int = 440, duration: float = 5.0) -> None:
        """Generate a mono 48kHz sine WAV via ffmpeg lavfi."""
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"sine=frequency={freq}:duration={duration}",
                "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le",
                str(dst),
            ],
            check=True,
        )

    def test_normalize_synthetic_signal_lands_within_tolerance(self) -> None:
        """5s 440Hz sine normalizes to within ±0.5 LU of -14.0 LUFS."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "sine.wav"
            dst = tdp / "sine_norm.wav"
            self._make_sine(src)

            measurements = normalize_vo(src, dst, target_lufs=-14.0)
            self.assertTrue(dst.exists(), "destination WAV not written")
            self.assertGreater(dst.stat().st_size, 0)

            for key in ("input_i", "input_tp", "output_i", "target_offset"):
                self.assertIn(key, measurements)
                self.assertIsInstance(measurements[key], float)

            # Independent measurement pass on the output.
            post = _measure_loudness(dst, -14.0, -1.0, 11.0)
            measured = post["input_i"]
            delta = abs(measured - (-14.0))
            self.assertLess(
                delta, 0.5,
                f"output measured at {measured:.2f} LUFS, "
                f"delta {delta:.2f} LU exceeds ±0.5 tolerance",
            )

    def test_idempotent_double_normalization(self) -> None:
        """Re-normalizing an already-normalized WAV stays within tolerance."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "sine.wav"
            once = tdp / "sine_1.wav"
            twice = tdp / "sine_2.wav"
            self._make_sine(src)

            normalize_vo(src, once, target_lufs=-14.0)
            normalize_vo(once, twice, target_lufs=-14.0)

            post = _measure_loudness(twice, -14.0, -1.0, 11.0)
            delta = abs(post["input_i"] - (-14.0))
            self.assertLess(
                delta, 0.5,
                f"second-pass output measured at {post['input_i']:.2f} LUFS, "
                f"delta {delta:.2f} LU exceeds ±0.5 tolerance",
            )

    def test_cli_smoke(self) -> None:
        """CLI invocation against synthetic input prints a parseable JSON report."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "sine.wav"
            dst = tdp / "sine_norm.wav"
            self._make_sine(src)

            cli_path = REPO_ROOT / "tools" / "audio_loudnorm.py"
            proc = subprocess.run(
                [
                    sys.executable, str(cli_path),
                    "--src", str(src), "--dst", str(dst),
                    "--target-lufs", "-14.0",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"CLI exited {proc.returncode}; stderr:\n{proc.stderr}",
            )
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertIn("output_lufs_measured", payload)
            self.assertIn("input_i", payload)
            self.assertLess(abs(payload["output_lufs_measured"] - (-14.0)), 0.5)


if __name__ == "__main__":
    unittest.main()
