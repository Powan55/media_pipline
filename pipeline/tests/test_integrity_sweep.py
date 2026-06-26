"""Unit + integration tests for tools/integrity_sweep.py.

Uses stdlib `unittest` because pytest is not installed in the venv (same
convention as test_media_integrity.py).

Two layers:

* **Pure-logic tests** (always run) inject a fake `checker` and fake `runner`
  so the sweep / recovery ORCHESTRATION (exit codes, candidate selection,
  backup-then-promote ordering, fail-loud paths) is exercised deterministically
  with NO ffmpeg and NO real media. The filesystem moves (os.replace backup +
  promote) are real — only ffprobe/ffmpeg are stubbed.

* **Integration tests** (@skipUnless ffmpeg) synthesize real fixtures via
  ffmpeg lavfi in a temp dir and drive the REAL remux + REAL check_integrity
  end-to-end, plus the CLI exit codes via subprocess. They pass a lowered
  `min_size_bytes` because synthesized `testsrc` clips are ~100-200 KB (the
  same accommodation test_media_integrity.py makes).

No test touches real channel-root files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable so `from tools.* import ...` works regardless
# of how unittest / pytest discovers this file.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.integrity_sweep import (  # noqa: E402
    SWEEP_EXIT_FAIL,
    SWEEP_EXIT_OK,
    RecoveryError,
    _exit_code,
    _make_checker,
    _topic_id_from_master,
    recover_master_from_variant,
    sweep,
    variant_candidates,
)
from tools.media_integrity import MediaIntegrityError, check_integrity  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes — a "media file" is GOOD iff its bytes start with b"GOOD".
# ---------------------------------------------------------------------------

GOOD = b"GOOD-MEDIA" + b"\x00" * 4096
BAD = b"CORRUPTED-" + b"\x00" * 4096
REMUXED = b"GOOD-REMUXED" + b"\x00" * 4096


def fake_checker(path: Path) -> dict:
    """Pass iff the file exists and its content starts with b"GOOD".

    Mimics check_integrity's exception contract: FileNotFoundError when absent,
    MediaIntegrityError when "corrupt".
    """
    path = Path(path)
    data = path.read_bytes()  # FileNotFoundError if missing — same as check_integrity
    if data.startswith(b"GOOD"):
        return {
            "path": str(path),
            "size_bytes": len(data),
            "duration_s": 30.0,
            "video_codec": "h264",
            "video_resolution": (1080, 1920),
            "audio_codec": "aac",
            "audio_channels": 2,
            "audio_sample_rate": 44100,
            "deep_decode_ok": True,
        }
    raise MediaIntegrityError(path, "fake: content does not start with GOOD")


def make_runner(output_bytes: bytes = REMUXED, *, returncode: int = 0, write: bool = True):
    """Build a fake ffmpeg runner that writes `output_bytes` to the cmd's output path."""

    def _runner(cmd, *args, **kwargs):  # noqa: ANN001
        out = Path(cmd[-1])  # _remux_cmd puts the output path last
        if write and returncode == 0:
            out.write_bytes(output_bytes)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    return _runner


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_channel_root(tmp: Path) -> Path:
    for sub in ("04_renders/_final_master", "05_exports/youtube", "06_published"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    return tmp


# ===========================================================================
# Pure-logic: path helpers
# ===========================================================================


class PathHelperTests(unittest.TestCase):
    def test_topic_id_from_master(self) -> None:
        self.assertEqual(
            _topic_id_from_master(Path("/x/04_renders/_final_master/2026-05-22_002_master.mp4")),
            "2026-05-22_002",
        )

    def test_topic_id_only_strips_trailing_master(self) -> None:
        # removesuffix, not replace — an id that happened to contain "master"
        # mid-string must not be mangled.
        self.assertEqual(
            _topic_id_from_master(Path("2026-05-22_master_002_master.mp4")),
            "2026-05-22_master_002",
        )

    def test_variant_candidates_order_and_paths(self) -> None:
        root = Path("C:/x/ShadowVerse")
        master = root / "04_renders" / "_final_master" / "2026-05-22_002_master.mp4"
        cands = variant_candidates(master, root)
        self.assertEqual(len(cands), 2)
        # Hot export first, cold published mirror second.
        self.assertEqual(cands[0], root / "05_exports" / "youtube" / "2026-05-22_002_yt.mp4")
        self.assertEqual(
            cands[1],
            root / "06_published" / "2026-05" / "2026-05-22_002" / "youtube" / "2026-05-22_002_yt.mp4",
        )

    def test_variant_candidates_skips_published_when_id_not_dated(self) -> None:
        root = Path("C:/x/ShadowVerse")
        master = root / "04_renders" / "_final_master" / "weird-id_master.mp4"
        cands = variant_candidates(master, root)
        # No YYYY-MM bucket derivable → only the hot export candidate.
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].name, "weird-id_yt.mp4")


# ===========================================================================
# Pure-logic: recover_master_from_variant
# ===========================================================================


class RecoverUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="recover_unit_")
        self.tmp = Path(self._tmp.name)
        self.master = self.tmp / "2026-05-22_002_master.mp4"
        self.variant = self.tmp / "2026-05-22_002_yt.mp4"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recover_backs_up_corrupt_then_promotes(self) -> None:
        _write(self.master, BAD)
        _write(self.variant, GOOD)

        detail = recover_master_from_variant(
            self.master, self.variant, checker=fake_checker, runner=make_runner(REMUXED)
        )

        self.assertTrue(detail["ok"])
        # Master now holds the remuxed content; corrupt original preserved.
        self.assertEqual(self.master.read_bytes(), REMUXED)
        corrupt = self.master.with_name(self.master.name + ".corrupt")
        self.assertTrue(corrupt.exists())
        self.assertEqual(corrupt.read_bytes(), BAD)
        self.assertEqual(detail["corrupt_backup"], str(corrupt))
        # `.new` temp consumed by the promote.
        self.assertFalse(self.master.with_name(self.master.name + ".new").exists())

    def test_recover_missing_master_promotes_without_backup(self) -> None:
        # No master on disk (e.g. interrupted prior recovery). Recover anyway.
        _write(self.variant, GOOD)
        detail = recover_master_from_variant(
            self.master, self.variant, checker=fake_checker, runner=make_runner(REMUXED)
        )
        self.assertTrue(detail["ok"])
        self.assertIsNone(detail["corrupt_backup"])
        self.assertEqual(self.master.read_bytes(), REMUXED)
        self.assertFalse(self.master.with_name(self.master.name + ".corrupt").exists())

    def test_recover_raises_when_variant_missing(self) -> None:
        _write(self.master, BAD)
        with self.assertRaises(RecoveryError) as cm:
            recover_master_from_variant(
                self.master, self.variant, checker=fake_checker, runner=make_runner()
            )
        self.assertIn("variant not found", str(cm.exception))
        # Corrupt master untouched.
        self.assertEqual(self.master.read_bytes(), BAD)

    def test_recover_raises_when_variant_also_bad(self) -> None:
        _write(self.master, BAD)
        _write(self.variant, BAD)  # variant is itself corrupt
        with self.assertRaises(RecoveryError) as cm:
            recover_master_from_variant(
                self.master, self.variant, checker=fake_checker, runner=make_runner()
            )
        self.assertIn("also failed integrity", str(cm.exception))
        self.assertEqual(self.master.read_bytes(), BAD)  # untouched

    def test_recover_raises_on_remux_failure_and_cleans_temp(self) -> None:
        _write(self.master, BAD)
        _write(self.variant, GOOD)
        with self.assertRaises(RecoveryError) as cm:
            recover_master_from_variant(
                self.master, self.variant, checker=fake_checker,
                runner=make_runner(returncode=1, write=False),
            )
        self.assertIn("remux failed", str(cm.exception))
        self.assertEqual(self.master.read_bytes(), BAD)  # untouched
        self.assertFalse(self.master.with_name(self.master.name + ".new").exists())

    def test_recover_raises_when_remux_output_corrupt(self) -> None:
        _write(self.master, BAD)
        _write(self.variant, GOOD)
        # Remux "succeeds" (rc=0) but produces a bad file.
        with self.assertRaises(RecoveryError) as cm:
            recover_master_from_variant(
                self.master, self.variant, checker=fake_checker, runner=make_runner(BAD)
            )
        self.assertIn("remuxed output failed", str(cm.exception))
        self.assertEqual(self.master.read_bytes(), BAD)  # untouched
        self.assertFalse(self.master.with_name(self.master.name + ".new").exists())


# ===========================================================================
# Pure-logic: sweep orchestration
# ===========================================================================


class SweepOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sweep_orch_")
        self.root = _make_channel_root(Path(self._tmp.name))
        self.masters = self.root / "04_renders" / "_final_master"
        self.yt = self.root / "05_exports" / "youtube"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_pass_exits_zero(self) -> None:
        _write(self.masters / "2026-05-01_001_master.mp4", GOOD)
        _write(self.masters / "2026-05-01_002_master.mp4", GOOD)
        report = sweep(self.masters, channel_root=self.root, checker=fake_checker)
        self.assertTrue(report["ok"])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(_exit_code(report), SWEEP_EXIT_OK)
        # PASS info is JSON-safe (tuple resolution -> list).
        self.assertEqual(report["entries"][0]["info"]["video_resolution"], [1080, 1920])

    def test_corrupt_master_without_recover_exits_nonzero(self) -> None:
        _write(self.masters / "2026-05-01_001_master.mp4", GOOD)
        _write(self.masters / "2026-05-01_002_master.mp4", BAD)
        report = sweep(self.masters, channel_root=self.root, checker=fake_checker)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["recovered"], 0)
        self.assertEqual(_exit_code(report), SWEEP_EXIT_FAIL)
        bad = next(e for e in report["entries"] if not e["ok"])
        self.assertFalse(bad["recovered"])
        self.assertIsNone(bad["recovery"])  # recovery not attempted

    def test_auto_recover_from_export_variant(self) -> None:
        master = _write(self.masters / "2026-05-22_002_master.mp4", BAD)
        _write(self.yt / "2026-05-22_002_yt.mp4", GOOD)
        report = sweep(
            self.masters, channel_root=self.root, auto_recover=True,
            checker=fake_checker, runner=make_runner(REMUXED),
        )
        self.assertTrue(report["ok"])  # recovered does not count as a failure
        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(_exit_code(report), SWEEP_EXIT_OK)
        self.assertEqual(master.read_bytes(), REMUXED)
        self.assertEqual(master.with_name(master.name + ".corrupt").read_bytes(), BAD)

    def test_auto_recover_falls_back_to_published_mirror(self) -> None:
        master = _write(self.masters / "2026-05-22_002_master.mp4", BAD)
        # No export variant; only the cold 06_published mirror exists.
        pub = (self.root / "06_published" / "2026-05" / "2026-05-22_002"
               / "youtube" / "2026-05-22_002_yt.mp4")
        _write(pub, GOOD)
        report = sweep(
            self.masters, channel_root=self.root, auto_recover=True,
            checker=fake_checker, runner=make_runner(REMUXED),
        )
        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(master.read_bytes(), REMUXED)

    def test_auto_recover_unrecoverable_when_no_variant(self) -> None:
        _write(self.masters / "2026-05-22_002_master.mp4", BAD)
        report = sweep(
            self.masters, channel_root=self.root, auto_recover=True,
            checker=fake_checker, runner=make_runner(REMUXED),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["recovered"], 0)
        bad = next(e for e in report["entries"] if not e["ok"])
        self.assertFalse(bad["recovery"]["ok"])
        self.assertIn("no recovery variant", bad["recovery"]["reason"])

    def test_auto_recover_unrecoverable_when_variant_also_bad(self) -> None:
        _write(self.masters / "2026-05-22_002_master.mp4", BAD)
        _write(self.yt / "2026-05-22_002_yt.mp4", BAD)  # variant corrupt too
        report = sweep(
            self.masters, channel_root=self.root, auto_recover=True,
            checker=fake_checker, runner=make_runner(REMUXED),
        )
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["recovered"], 0)

    def test_published_failures_counted_but_not_recovered(self) -> None:
        # Masters all good; a published backup is corrupt → exit non-zero, and
        # no recovery is attempted for published files even with --auto-recover.
        _write(self.masters / "2026-05-08_001_master.mp4", GOOD)
        published = self.root / "06_published"
        _write(published / "2026-05" / "2026-05-08_001" / "youtube" / "2026-05-08_001_yt.mp4", GOOD)
        _write(published / "2026-05" / "2026-05-08_001" / "tiktok" / "2026-05-08_001_tt.mp4", BAD)
        report = sweep(
            self.masters, published_dir=published, channel_root=self.root,
            auto_recover=True, checker=fake_checker, runner=make_runner(REMUXED),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["recovered"], 0)
        bad = next(e for e in report["entries"] if not e["ok"])
        self.assertEqual(bad["kind"], "published")
        self.assertIsNone(bad["recovery"])  # published never auto-recovered

    def test_filenotfound_from_checker_is_a_failure_not_a_crash(self) -> None:
        # If a file vanishes under the sweep, check_integrity raises
        # FileNotFoundError. The sweep must record it as a FAIL and keep going,
        # never propagate and abort the whole run.
        _write(self.masters / "2026-05-01_001_master.mp4", GOOD)

        def fnf_checker(path: Path) -> dict:
            if Path(path).name == "2026-05-01_001_master.mp4":
                raise FileNotFoundError(f"vanished: {path}")
            return fake_checker(path)

        _write(self.masters / "2026-05-01_002_master.mp4", GOOD)
        report = sweep(self.masters, channel_root=self.root, checker=fnf_checker)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["passed"], 1)  # the other master still checked
        self.assertEqual(_exit_code(report), SWEEP_EXIT_FAIL)


# ===========================================================================
# Integration — real ffmpeg + real check_integrity + real remux
# ===========================================================================


def _ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _synthesize_good_master(out_path: Path, duration_s: int = 3) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration_s}:rate=30:size=1080x1920",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    return out_path


def _stream_copy(src: Path, dst: Path) -> Path:
    """Make a byte-faithful YT-style stream-copy of `src` (no re-encode)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-c", "copy", "-map", "0", "-movflags", "+faststart", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    return dst


def _truncate(path: Path, keep_bytes: int) -> None:
    with path.open("r+b") as f:
        f.truncate(keep_bytes)


# Synthesized testsrc clips are ~100-200 KB; lower the size gate so the check
# exercises ffprobe / deep-decode rather than tripping on size.
_TEST_MIN_SIZE = 10_000


@unittest.skipUnless(_ffmpeg_on_path(), "ffmpeg/ffprobe not on PATH")
class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sweep_integration_")
        self.root = _make_channel_root(Path(self._tmp.name))
        self.masters = self.root / "04_renders" / "_final_master"
        self.yt = self.root / "05_exports" / "youtube"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _checker(self):
        return _make_checker(deep_decode_seconds=1.0, min_size_bytes=_TEST_MIN_SIZE)

    def test_real_sweep_recovers_truncated_master(self) -> None:
        tid = "2026-05-22_002"
        master = _synthesize_good_master(self.masters / f"{tid}_master.mp4")
        # The YT variant is a real stream-copy of the (good) master.
        _stream_copy(master, self.yt / f"{tid}_yt.mp4")
        # Corrupt the master in place — wipe the moov atom.
        _truncate(master, 8 * 1024)

        # Sanity: the master really is broken now.
        with self.assertRaises(MediaIntegrityError):
            check_integrity(master, min_size_bytes=_TEST_MIN_SIZE, deep_decode_seconds=1.0)

        report = sweep(
            self.masters, channel_root=self.root, auto_recover=True,
            checker=self._checker(), runner=subprocess.run,
        )

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["failed"], 0)
        # Master decodes again, and the corrupt original was preserved.
        info = check_integrity(master, min_size_bytes=_TEST_MIN_SIZE, deep_decode_seconds=1.0)
        self.assertTrue(info["deep_decode_ok"])
        self.assertEqual(info["video_resolution"], (1080, 1920))
        self.assertTrue(master.with_name(master.name + ".corrupt").exists())

    def test_real_sweep_clean_archive_passes(self) -> None:
        _synthesize_good_master(self.masters / "2026-05-01_001_master.mp4")
        _synthesize_good_master(self.masters / "2026-05-01_002_master.mp4")
        report = sweep(self.masters, channel_root=self.root, checker=self._checker())
        self.assertTrue(report["ok"])
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 0)


# ===========================================================================
# Integration — CLI exit codes via subprocess
# ===========================================================================


@unittest.skipUnless(_ffmpeg_on_path(), "ffmpeg/ffprobe not on PATH")
class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sweep_cli_")
        self.root = _make_channel_root(Path(self._tmp.name))
        self.masters = self.root / "04_renders" / "_final_master"
        self.yt = self.root / "05_exports" / "youtube"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable, str(REPO_ROOT / "tools" / "integrity_sweep.py"),
            "--channel-root", str(self.root),
            "--min-size", str(_TEST_MIN_SIZE),
            "--deep", "1",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def test_cli_clean_archive_exits_zero(self) -> None:
        _synthesize_good_master(self.masters / "2026-05-01_001_master.mp4")
        proc = self._run_cli("--json")
        self.assertEqual(proc.returncode, SWEEP_EXIT_OK, proc.stderr)
        report = json.loads(proc.stdout)  # stdout is pure JSON; logs go to stderr
        self.assertTrue(report["ok"])
        self.assertEqual(report["passed"], 1)

    def test_cli_corrupt_master_without_recover_exits_one(self) -> None:
        master = _synthesize_good_master(self.masters / "2026-05-01_002_master.mp4")
        _truncate(master, 8 * 1024)
        proc = self._run_cli("--json")
        self.assertEqual(proc.returncode, SWEEP_EXIT_FAIL, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)

    def test_cli_auto_recover_exits_zero(self) -> None:
        tid = "2026-05-22_002"
        master = _synthesize_good_master(self.masters / f"{tid}_master.mp4")
        _stream_copy(master, self.yt / f"{tid}_yt.mp4")
        _truncate(master, 8 * 1024)
        proc = self._run_cli("--auto-recover", "--json")
        self.assertEqual(proc.returncode, SWEEP_EXIT_OK, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["recovered"], 1)


if __name__ == "__main__":
    unittest.main()
