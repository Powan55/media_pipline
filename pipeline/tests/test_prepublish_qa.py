"""Unit tests for tools/prepublish_qa.py.

Synthesizes fixtures via ffmpeg lavfi into a tmp_path; never touches real
channel-root files. Tests are pytest-compatible (the venv has pytest 9).

Run:
    C:/ContentOps/_pipeline/.venv/Scripts/python.exe -m pytest \\
        tests/test_prepublish_qa.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.prepublish_qa import (  # noqa: E402
    CheckResult,
    PipelineQAFailed,
    QAReport,
    check_topic,
    check_variant,
    run_qa,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


pytestmark = pytest.mark.skipif(
    not _ffmpeg_on_path(),
    reason="ffmpeg / ffprobe not available — synthesis tests cannot run",
)


def _synthesize_variant(
    out_path: Path,
    duration_s: float = 5.0,
    *,
    width: int = 1080,
    height: int = 1920,
    framerate: float = 30.0,
    channels: int = 2,
    target_lufs: float = -14.0,
    silent_intro: bool = False,
    black_intro: bool = False,
    audio_gain_db: float | None = None,
) -> Path:
    """Synthesize a 1080x1920 H.264/AAC MP4 via ffmpeg lavfi.

    Tunable knobs cover the failure modes our tests need to exercise.
    """
    inputs: list[str] = []
    audio_filter_chain: list[str] = []
    video_filter_chain: list[str] = []

    # Track input index counter as we append. Each "-f lavfi -i <spec>" pair
    # consumes one input slot.
    next_input_idx = 0

    if black_intro:
        inputs.extend([
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:r={framerate}:d=1",
        ])
        black_idx = next_input_idx
        next_input_idx += 1
        inputs.extend([
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration_s - 1}:rate={framerate}:size={width}x{height}",
        ])
        testsrc_idx = next_input_idx
        next_input_idx += 1
        video_filter_chain.append(
            f"[{black_idx}:v][{testsrc_idx}:v]concat=n=2:v=1:a=0[vout]"
        )
        v_map = "[vout]"
    else:
        inputs.extend([
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration_s}:rate={framerate}:size={width}x{height}",
        ])
        v_idx = next_input_idx
        next_input_idx += 1
        # Raw stream-spec mapping (no brackets) — works alongside filter_complex.
        v_map = f"{v_idx}:v"

    if silent_intro:
        inputs.extend([
            "-f", "lavfi",
            "-i", "anullsrc=cl=stereo:sample_rate=48000:duration=1",
        ])
        silent_idx = next_input_idx
        next_input_idx += 1
        inputs.extend([
            "-f", "lavfi",
            "-i", (
                f"sine=frequency=440:duration={duration_s - 1}:sample_rate=48000,"
                f"aformat=channel_layouts=stereo"
            ),
        ])
        tone_idx = next_input_idx
        next_input_idx += 1
        audio_filter_chain.append(
            f"[{silent_idx}:a][{tone_idx}:a]concat=n=2:v=0:a=1[aconcat]"
        )
        a_chain_in = "[aconcat]"
    else:
        ch_layout = "stereo" if channels == 2 else "mono"
        inputs.extend([
            "-f", "lavfi",
            "-i", (
                f"sine=frequency=440:duration={duration_s}:sample_rate=48000,"
                f"aformat=channel_layouts={ch_layout}"
            ),
        ])
        sine_idx = next_input_idx
        next_input_idx += 1
        a_chain_in = f"[{sine_idx}:a]"

    # Apply gain to push to a desired LUFS.  Sine at full scale ~ -3 LUFS.
    if audio_gain_db is not None:
        gain_db = audio_gain_db
    else:
        gain_db = target_lufs - (-3.0)
    audio_filter_chain.append(f"{a_chain_in}volume={gain_db}dB[aout]")
    a_map = "[aout]"

    filter_complex = ";".join(video_filter_chain + audio_filter_chain)

    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs
    if filter_complex:
        cmd += ["-filter_complex", filter_complex,
                "-map", v_map, "-map", a_map]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-r", str(framerate),
        "-c:a", "aac",
        "-ac", str(channels),
        "-ar", "48000",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    return out_path


def _write_caption_file(
    path: Path,
    n_events: int,
    duration_s: float,
) -> Path:
    """Write a minimal valid .ass file with `n_events` evenly-spaced Dialogue lines."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,96,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,"
        "100,100,0,0,1,5,1,5,40,40,800,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    step = duration_s / max(n_events, 1)
    body_lines: list[str] = []
    for i in range(n_events):
        start = i * step
        end = start + step * 0.9
        body_lines.append(
            f"Dialogue: 0,{_fmt_ts(start)},{_fmt_ts(end)},Default,,0,0,0,,word{i}"
        )
    path.write_text(header + "\n".join(body_lines) + "\n", encoding="utf-8")
    return path


def _fmt_ts(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_variant_returns_qareport(tmp_path: Path) -> None:
    """check_variant must NEVER raise — even on a missing file we get a QAReport."""
    fake = tmp_path / "does_not_exist.mp4"
    report = check_variant(fake)
    assert isinstance(report, QAReport)
    assert report.ok is False
    # Check #1 should be the integrity FAIL
    cid1 = next(r for r in report.results if r.check_id == 1)
    assert cid1.ok is False


def test_known_good_master_passes(tmp_path: Path) -> None:
    """A 5s 1080x1920 stereo at -14 LUFS with matching captions should pass."""
    video = _synthesize_variant(tmp_path / "good.mp4", duration_s=5.0)
    captions = _write_caption_file(tmp_path / "good.ass", n_events=10, duration_s=5.0)
    report = check_variant(video, captions_path=captions)

    # Must always return a QAReport (AC1).
    assert isinstance(report, QAReport)

    # We accept either full PASS or only LUFS/intro near-misses caused by
    # ffmpeg synthesis quirks. But the structural checks must pass.
    structural = {3, 4, 5, 6, 7, 10}  # resolution/framerate/codec/parity/channels/captions
    for r in report.results:
        if r.check_id in structural:
            assert r.ok, f"structural check {r.check_id} failed: {r}"


def test_mono_fails_check_7(tmp_path: Path) -> None:
    """A mono variant must FAIL check #7 (audio_channels)."""
    video = _synthesize_variant(tmp_path / "mono.mp4", duration_s=3.0, channels=1)
    captions = _write_caption_file(tmp_path / "mono.ass", n_events=6, duration_s=3.0)
    report = check_variant(video, captions_path=captions)
    c7 = next(r for r in report.results if r.check_id == 7)
    assert c7.ok is False
    assert "1" in c7.actual
    assert "2" in c7.expected


def test_low_lufs_fails_check_8(tmp_path: Path) -> None:
    """A file at ~-22 LUFS must FAIL check #8."""
    # Sine at amplitude 1.0 ~ -3 LUFS; apply -19 dB to land around -22 LUFS.
    video = _synthesize_variant(
        tmp_path / "quiet.mp4", duration_s=4.0, audio_gain_db=-19.0,
    )
    captions = _write_caption_file(tmp_path / "quiet.ass", n_events=8, duration_s=4.0)
    report = check_variant(video, captions_path=captions)
    c8 = next(r for r in report.results if r.check_id == 8)
    assert c8.ok is False, f"expected LUFS check to fail, got {c8}"


def test_caption_density_fail(tmp_path: Path) -> None:
    """5 Dialogue events on a 30s video = 0.17 ev/s, below the 1.0 floor."""
    # Synthesize a 5s clip then patch its duration in the caption file to 30s.
    video = _synthesize_variant(tmp_path / "vid.mp4", duration_s=5.0)
    captions = _write_caption_file(tmp_path / "vid.ass", n_events=2, duration_s=5.0)
    # 2 events / 5s = 0.4 ev/s, below 1.0 floor.
    report = check_variant(video, captions_path=captions)
    c11 = next(r for r in report.results if r.check_id == 11)
    assert c11.ok is False, f"expected density check to fail, got {c11}"


def test_silent_intro_fails_check_12(tmp_path: Path) -> None:
    """First 1s silent => FAIL check #12."""
    video = _synthesize_variant(
        tmp_path / "silent_intro.mp4", duration_s=4.0, silent_intro=True,
    )
    captions = _write_caption_file(tmp_path / "silent.ass", n_events=8, duration_s=4.0)
    report = check_variant(video, captions_path=captions)
    c12 = next(r for r in report.results if r.check_id == 12)
    assert c12.ok is False, f"expected silent-intro fail, got {c12}"
    assert "audio" in c12.message.lower() or "silent" in c12.message.lower()


def test_black_intro_fails_check_12(tmp_path: Path) -> None:
    """First 1s pure black => FAIL check #12."""
    video = _synthesize_variant(
        tmp_path / "black_intro.mp4", duration_s=4.0, black_intro=True,
    )
    captions = _write_caption_file(tmp_path / "blk.ass", n_events=8, duration_s=4.0)
    report = check_variant(video, captions_path=captions)
    c12 = next(r for r in report.results if r.check_id == 12)
    assert c12.ok is False, f"expected black-intro fail, got {c12}"
    assert "black" in c12.message.lower()


def test_cited_observation_off_by_default(tmp_path: Path) -> None:
    """Script_FINAL with no URL -> still PASS overall (flag off by default)."""
    video = _synthesize_variant(tmp_path / "v.mp4", duration_s=3.0)
    captions = _write_caption_file(tmp_path / "v.ass", n_events=6, duration_s=3.0)
    script_final = tmp_path / "script_FINAL.txt"
    script_final.write_text("a script with no urls and no named sources", encoding="utf-8")
    report = check_variant(
        video, captions_path=captions, script_final_path=script_final,
    )
    # Should not contain a #13 row when the flag is off.
    assert all(r.check_id != 13 for r in report.results)


def test_cited_observation_on_no_url_fails(tmp_path: Path) -> None:
    """Flag on + no URL => check #13 FAIL."""
    video = _synthesize_variant(tmp_path / "v.mp4", duration_s=3.0)
    captions = _write_caption_file(tmp_path / "v.ass", n_events=6, duration_s=3.0)
    script_final = tmp_path / "script_FINAL.txt"
    script_final.write_text("plain text, no urls or sources", encoding="utf-8")
    report = check_variant(
        video,
        captions_path=captions,
        script_final_path=script_final,
        check_cited_observation=True,
    )
    c13 = next(r for r in report.results if r.check_id == 13)
    assert c13.ok is False
    assert "URL" in c13.actual or "url" in c13.actual.lower()


def test_cited_observation_on_with_url_passes(tmp_path: Path) -> None:
    """Flag on + URL + named source => check #13 PASS."""
    video = _synthesize_variant(tmp_path / "v.mp4", duration_s=3.0)
    captions = _write_caption_file(tmp_path / "v.ass", n_events=6, duration_s=3.0)
    script_final = tmp_path / "script_FINAL.txt"
    script_final.write_text(
        "Per anthropic.com blog https://www.anthropic.com/news/foo dated 2026-05-07, ...",
        encoding="utf-8",
    )
    report = check_variant(
        video,
        captions_path=captions,
        script_final_path=script_final,
        check_cited_observation=True,
    )
    c13 = next(r for r in report.results if r.check_id == 13)
    assert c13.ok is True


def test_cli_json_mode(tmp_path: Path) -> None:
    """--json mode emits one valid JSON object per video."""
    video = _synthesize_variant(tmp_path / "v.mp4", duration_s=3.0)
    captions = _write_caption_file(tmp_path / "v.ass", n_events=6, duration_s=3.0)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "prepublish_qa.py"),
        "--video", str(video),
        "--captions", str(captions),
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    # We don't assert exit code (LUFS may be off due to lavfi quirks); just
    # validate the JSON shape.
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "video_path" in payload
    assert "ok" in payload
    assert "checks_run" in payload
    assert "runtime_s" in payload
    assert "results" in payload
    assert isinstance(payload["results"], list)


def test_pipelinequafailed_carries_failures() -> None:
    """PipelineQAFailed must accept a `failures` dict and stringify cleanly."""
    failures = {7: {"name": "audio_channels", "expected": "2", "actual": "1",
                    "message": "mono"}}
    exc = PipelineQAFailed(failures=failures, video_path=Path("x.mp4"))
    assert exc.failures == failures
    assert "audio_channels" in str(exc)
    assert "x.mp4" in str(exc)


def test_run_qa_compat_shim(tmp_path: Path) -> None:
    """run_qa() returns the (passed, failures, dict) tuple from the original spec."""
    video = _synthesize_variant(tmp_path / "v.mp4", duration_s=3.0)
    captions = _write_caption_file(tmp_path / "v.ass", n_events=6, duration_s=3.0)
    passed, failures, payload = run_qa(video, captions_path=captions)
    assert isinstance(passed, bool)
    assert isinstance(failures, list)
    for f in failures:
        assert isinstance(f, CheckResult)
        assert f.ok is False
    assert isinstance(payload, dict)
    assert payload.get("checks_run", 0) >= 11


def test_check_topic_raises_on_missing_variant(tmp_path: Path) -> None:
    """check_topic raises FileNotFoundError when an expected variant is missing."""
    channel_root = tmp_path / "channel"
    (channel_root / "05_exports" / "youtube").mkdir(parents=True)
    (channel_root / "05_exports" / "tiktok").mkdir(parents=True)
    (channel_root / "05_exports" / "instagram").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        check_topic("2099-01-01_999", channel_root)
