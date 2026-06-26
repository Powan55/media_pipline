"""Pre-publish QA gate for ShadowVerse — 11 hard checks + 1 opt-in cited-obs check.

Runs against a finished video (master OR per-platform variant) and validates
that every property which has historically slipped through to YouTube is
within spec. Fails loud — operator runs the gate, fixes upstream, re-runs.

The 11 hard checks (per spec T7, with R1/R7 amendments to Agent I §6):

    1. file exists + non-empty + container is structurally sound (T2)
    2. duration >= 1.0s and <= 180.0s
    3. resolution = 1080 x 1920
    4. framerate in {29.97, 30.0}
    5. video codec = h264
    6. AV duration parity (|audio_dur - video_dur| <= 0.2s)
    7. audio channels = 2 (stereo per R7 — current 9 masters are mono and WILL fail)
    8. measured LUFS within +/- 1.0 of -14.0 (R1 — uniform across yt/tt/ig,
       overrides Agent I's per-platform -11/-14 split)
    9. true-peak <= -0.5 dBTP
    10. caption sidecar exists alongside the master AND is parseable ASS
    11. caption density >= 1.0 Dialogue events / video_duration_s
    12. (kept as the silent-intro / black-frame combo — Agent I's #11+#12 fused)
        first 1.0s audio mean_volume >= -50 dB AND first 1.0s video has no
        all-black region detected by ffmpeg blackdetect

Opt-in check #13 (R6 cited-observation, off by default — pass --check-cited-observation):

    13. script_FINAL.txt contains >= 1 URL match AND >= 1 named-source match
        (Reddit u/<handle>, X @<handle>, vendor-blog domain, or ISO-ish date)

Sprint 5 added check #14 (script-side template-artifact scan, Layer 2 of the
three-layer defense against the `_12_002` template-leak class of bug):

    14. script_FINAL.txt is free of template / internal-name / stage-instruction
        artifacts as defined in `tools.script_artifact_patterns`. Run via
        `check_script(script_path)` or `check_topic_script(topic_id, channel_root)`,
        NOT via `check_variant` — this check scans the script body, not the
        rendered MEDIA, so it must run BEFORE TTS (pre-stage 4). The Sprint 5
        brief originally called this "#13" but #13 was already used for the
        opt-in cited-observation check (commit 817bc13); rather than renumber
        the existing check and bleed into other call-sites, we take the next
        free integer (#14) and document the deviation here.

Two later script-side checks share the same pre-render gate (`check_script` /
`_scan_script_for_artifacts_or_halt`, BEFORE TTS), each taking the next free id:

    15. script_FINAL.txt sourcing hygiene (PU-7b, 2026-05-29; broadened by PU-4,
        2026-06-19): no residual `[VERIFY: ...]` tag and no anonymous citation.
        Anonymous shapes covered: "a Reddit/X user", "a/some Redditor(s)",
        "a user on r/… / Reddit / Twitter / HN / a forum", "a developer" (bare
        or "+ verb"), bare "Researchers / scientists <verb>", and "someone
        <verb>". A named handle (`u/`/`@`) or a name-adjacent role word
        ("Anthropic researcher Tom Brown") on the line suppresses the finding.
        Run via `check_script_sourcing(script_path)`.
    16. script_FINAL.txt pre-render lint: no unresolved placeholder token
        (`[VERIFY` / `[NEEDS` / `[TODO` / `[FIXME`) and no retired/forbidden CTA.
        The banned-CTA list is pulled from the style guide's retired/forbidden
        sections so it stays in sync. Run via `check_script_prerender(script_path)`.
        Forward-looking guard for the `_08_001` ([VERIFY] spoken aloud) and
        `_11_002` (retired comment-bait) published defects.

Note on numbering: Agent I §6.3 listed 12 checks where #11 was silent-intro and
#12 was no-black-frame-opener. The spec asked us to drop standalone #12 and fuse
its black-frame test into the silent-intro check, so externally we expose
11 always-on checks plus the optional #13 and the script-side #14. The IDs in
`CheckResult` follow the spec's numbering (1-11 for hard checks, 13 for
cited-obs, 14 for template-artifact). #12 is intentionally unused so an audit
can grep for "drop #12 per T7".

Library use:

    from tools.prepublish_qa import (
        check_variant, check_topic, check_script, check_topic_script,
        QAReport, PipelineQAFailed,
    )

    report = check_variant(Path("...mp4"))
    if not report.ok:
        raise PipelineQAFailed(failures=report.failures_dict())

    reports = check_topic("2026-05-07_006", channel_root=Path("..."))
    # reports = {"yt": QAReport, "tt": QAReport, "ig": QAReport}

    # Script-side check (Sprint 5 L2 — call BEFORE TTS, after gate-2 resolution):
    script_result = check_script(Path("...script_FINAL.txt"))
    if not script_result.ok:
        raise PipelineQAFailed(failures={script_result.check_id: {...}})

CLI:

    python tools/prepublish_qa.py --video <path.mp4>
    python tools/prepublish_qa.py --video <path.mp4> --json
    python tools/prepublish_qa.py --topic 2026-05-07_006 --channel-root <path>
    python tools/prepublish_qa.py --topic 2026-05-07_006 --channel-root <path> \\
        --check-cited-observation
    python tools/prepublish_qa.py --script <path.txt>

Exit 0 = all checks passed. Exit 1 = at least one FAIL. Exit 2 = malformed
inputs (no video given, video missing, ffmpeg/ffprobe missing, etc.).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, NamedTuple

# Allow `from tools.audio_loudnorm import ...` to resolve when this module is
# invoked as a script (`python tools/prepublish_qa.py ...`). When invoked as
# `python -m tools.prepublish_qa` or imported, the package path is already
# registered and this no-ops.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.audio_loudnorm import _parse_loudnorm_json  # noqa: E402
from tools.media_integrity import (  # noqa: E402
    MediaIntegrityError,
    check_integrity,
)
from tools.script_artifact_patterns import (  # noqa: E402
    ArtifactMatch,
    format_matches,
    scan_for_artifacts,
)
from tools.script_prerender_patterns import (  # noqa: E402
    build_cta_matchers,
    format_lint_matches,
    scan_script_for_lint,
)

log = logging.getLogger("prepublish_qa")

# ---------------------------------------------------------------------------
# Defaults — uniform per R1 (NOT Agent I's per-platform -11/-14 split)
# ---------------------------------------------------------------------------

DEFAULT_TARGET_LUFS = -14.0
DEFAULT_LUFS_TOLERANCE = 1.0
DEFAULT_TARGET_TP = -0.5
DEFAULT_EXPECTED_RESOLUTION: tuple[int, int] = (1080, 1920)
DEFAULT_MIN_DURATION_S = 1.0
DEFAULT_MAX_DURATION_S = 180.0
DEFAULT_AV_PARITY_TOLERANCE_S = 0.2
DEFAULT_REQUIRED_CHANNELS = 2  # stereo per R7
DEFAULT_REQUIRED_VIDEO_CODEC = "h264"
DEFAULT_VALID_FRAMERATES: tuple[float, ...] = (29.97, 30.0)
DEFAULT_FRAMERATE_TOLERANCE = 0.05
DEFAULT_MIN_CAPTION_DENSITY = 1.0  # Dialogue events per second of video
DEFAULT_FIRST_SECOND_MEAN_DB = -50.0  # mean_volume floor for non-silent intro

# Per spec AC10: total runtime < 15s on a 30-50s 30 MB master.
_FFMPEG_TIMEOUT_S = 60

PLATFORMS: tuple[str, ...] = ("yt", "tt", "ig")

# CLI exit codes — match the convention used by media_integrity.
CLI_EXIT_OK = 0
CLI_EXIT_FAIL = 1
CLI_EXIT_USAGE = 2

# Cited-observation regex set (check #13).
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_NAMED_SOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bu/[A-Za-z0-9_\-]{3,}\b"),                  # Reddit handle
    re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{2,}\b"),      # X / Twitter handle
    re.compile(
        r"\b(?:anthropic|openai|deepmind|google|microsoft|meta|"
        r"mistral|cohere|huggingface|stability|midjourney|runwayml|nvidia|amazon)"
        r"\.(?:com|ai|co|org)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b202\d-\d{2}-\d{2}\b"),                      # ISO-ish date
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class CheckResult(NamedTuple):
    """Outcome of one of the 12 visible checks.

    ok=True / severity="PASS" means the check passed; ok=False / severity in
    {"FAIL","WARN"} means it failed. We don't currently use WARN — every
    failure is a hard FAIL — but the field is present so a future tweak can
    soften any one check without changing the schema.
    """

    check_id: int
    name: str
    ok: bool
    severity: Literal["PASS", "FAIL", "WARN"]
    message: str
    expected: str
    actual: str


# Legacy alias kept for the original T7 spec (pre-Wave 2 rename to CheckResult).
QAFailure = CheckResult


@dataclass(frozen=True)
class QAReport:
    """Bundle of CheckResults for one video, plus context metadata.

    `ok` is True iff every check in `results` passed. Construct via
    `check_variant()`; do not build manually unless wiring a test.
    """

    video_path: Path
    ok: bool
    results: tuple[CheckResult, ...]
    runtime_s: float
    checks_run: int
    probe: dict = field(default_factory=dict)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    def failures_dict(self) -> dict[int, dict[str, str]]:
        """Compact failures table suitable for `PipelineQAFailed(failures=...)`."""
        return {
            r.check_id: {
                "name": r.name,
                "expected": r.expected,
                "actual": r.actual,
                "message": r.message,
            }
            for r in self.failures
        }

    def to_dict(self) -> dict:
        """JSON-serialisable view (for `--json` and pipeline logging)."""
        return {
            "video_path": str(self.video_path),
            "ok": self.ok,
            "checks_run": self.checks_run,
            "runtime_s": round(self.runtime_s, 3),
            "results": [
                {
                    "check_id": r.check_id,
                    "name": r.name,
                    "ok": r.ok,
                    "severity": r.severity,
                    "message": r.message,
                    "expected": r.expected,
                    "actual": r.actual,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Pipeline-side exception (for Wave 3 wiring)
# ---------------------------------------------------------------------------


# Late-binding import: importing pipeline at module-import time would cycle
# (pipeline imports tools.* later). Resolve the base class lazily.
def _resolve_pipeline_halted_base() -> type[Exception]:
    try:
        from pipeline import PipelineHalted  # type: ignore
    except Exception:  # pragma: no cover - only triggered if pipeline.py absent
        return Exception
    return PipelineHalted


_PipelineHaltedBase = _resolve_pipeline_halted_base()


class PipelineQAFailed(_PipelineHaltedBase):  # type: ignore[misc, valid-type]
    """Raised by Wave 3 wiring when prepublish_qa rejects a master / variant.

    `check_variant` and `check_topic` themselves do NOT raise this — they
    return `QAReport` objects. Wave 3 is responsible for inspecting `.ok`
    and raising:

        report = check_variant(path)
        if not report.ok:
            raise PipelineQAFailed(
                video_path=path,
                failures=report.failures_dict(),
            )

    Inheriting from `PipelineHalted` means the existing pipeline-driver
    plumbing treats this as an idempotent halt — operator fixes upstream,
    re-runs, the gate re-checks.
    """

    def __init__(
        self,
        *,
        failures: dict[int, dict[str, str]],
        video_path: Path | None = None,
        topic_id: str | None = None,
    ) -> None:
        self.failures = failures
        self.video_path = video_path
        self.topic_id = topic_id

        header = "[QA-GATE] pre-publish QA failed"
        if topic_id:
            header += f" for topic {topic_id}"
        if video_path:
            header += f" ({Path(video_path).name})"
        body_lines = [header, ""]
        for cid in sorted(failures.keys()):
            f = failures[cid]
            body_lines.append(
                f"  #{cid:>2} {f.get('name', '?')}: "
                f"expected={f.get('expected', '?')!r}, "
                f"actual={f.get('actual', '?')!r}"
            )
            if f.get("message"):
                body_lines.append(f"        {f['message']}")
        body_lines.append("")
        body_lines.append("Fix upstream (re-render / re-master / fix captions) and re-run.")
        super().__init__("\n".join(body_lines))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _parse_framerate(rate_str: str | None) -> float | None:
    """Parse ffprobe's `r_frame_rate` (e.g. '30/1' or '30000/1001') to a float."""
    if not rate_str:
        return None
    if "/" in rate_str:
        try:
            num, den = rate_str.split("/", 1)
            num_f = float(num)
            den_f = float(den)
            if den_f == 0:
                return None
            return num_f / den_f
        except ValueError:
            return None
    try:
        return float(rate_str)
    except ValueError:
        return None


def _ffprobe_streams(path: Path) -> dict:
    """Run ffprobe -show_streams + -show_format and return the parsed JSON dict."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_FFMPEG_TIMEOUT_S, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}): {proc.stderr.strip()[-400:]}"
        )
    return json.loads(proc.stdout)


def _measure_loudness(video_path: Path, target_lufs: float, target_tp: float) -> dict:
    """Pass-1 loudnorm measurement on the video's audio track. Returns parsed JSON."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-vn",  # ignore video — measurement is audio-only
        "-af", f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_FFMPEG_TIMEOUT_S, check=False,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg loudnorm measurement failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[-400:]}"
        )
    return _parse_loudnorm_json(proc.stderr)


_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)
_BLACKDETECT_RE = re.compile(
    r"black_start:(?P<start>\d+(?:\.\d+)?)\s+black_end:(?P<end>\d+(?:\.\d+)?)\s+"
    r"black_duration:(?P<dur>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _measure_first_second_audio_mean_db(video_path: Path) -> float | None:
    """Run volumedetect on the first 1.0s of audio. Returns mean_volume in dB or None."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-t", "1",
        "-i", str(video_path),
        "-vn",
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_FFMPEG_TIMEOUT_S, check=False,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        log.warning("volumedetect rc=%d: %s", proc.returncode, proc.stderr.strip()[-200:])
        return None
    m = _MEAN_VOLUME_RE.search(proc.stderr)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _detect_first_second_black(video_path: Path) -> list[tuple[float, float]]:
    """Run blackdetect on first 1.0s of video. Returns list of (start, end) tuples."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-t", "1",
        "-i", str(video_path),
        "-an",
        "-vf", "blackdetect=d=0.05:pix_th=0.10",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=_FFMPEG_TIMEOUT_S, check=False,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        log.warning("blackdetect rc=%d: %s", proc.returncode, proc.stderr.strip()[-200:])
        return []
    out: list[tuple[float, float]] = []
    for m in _BLACKDETECT_RE.finditer(proc.stderr):
        out.append((float(m.group("start")), float(m.group("end"))))
    return out


# ---------------------------------------------------------------------------
# Caption-file parsing
# ---------------------------------------------------------------------------


def _parse_ass_dialogue_count(ass_text: str) -> int:
    """Count `Dialogue:` events in an .ass caption file. Returns 0 if file unparseable."""
    if "[Events]" not in ass_text:
        return 0
    # We accept any Dialogue line — even malformed ones — because libass does too.
    # Stricter parsing would reject this file outright but we don't need that here.
    return sum(1 for line in ass_text.splitlines() if line.startswith("Dialogue:"))


def _ass_is_parseable(ass_text: str) -> bool:
    """Minimal sanity check for an .ass file: must have [Events] + Dialogue lines."""
    return "[Events]" in ass_text and any(
        line.startswith("Dialogue:") for line in ass_text.splitlines()
    )


# ---------------------------------------------------------------------------
# Cited-observation (#13)
# ---------------------------------------------------------------------------


def _check_cited_observation(script_text: str) -> tuple[bool, list[str]]:
    """Return (ok, missing_kinds). missing_kinds is a list of human-readable misses."""
    missing: list[str] = []
    if not _URL_RE.search(script_text):
        missing.append("at-least-one URL (https?://...)")
    if not any(p.search(script_text) for p in _NAMED_SOURCE_PATTERNS):
        missing.append(
            "at-least-one named source (Reddit u/x, X @y, vendor-blog domain, or ISO date)"
        )
    return (not missing, missing)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


_TOPIC_ID_RE = re.compile(r"^(20\d{2}-\d{2}-\d{2}_\d{3})")


def _topic_id_from_video(video_path: Path) -> str | None:
    """Extract the topic_id prefix (e.g. '2026-05-07_006') from a video filename."""
    m = _TOPIC_ID_RE.match(video_path.stem)
    return m.group(1) if m else None


def _default_captions_path(video_path: Path, channel_root: Path | None) -> Path | None:
    """Resolve the canonical captions sidecar location for a given video.

    Layout per CLAUDE.md:
        <channel_root>/04_renders/_wip/<topic_id>/<topic_id>_captions.ass

    If `channel_root` is None we fall back to walking up from the video to find
    a `04_renders` directory, otherwise we return None and let the caller decide.
    """
    topic_id = _topic_id_from_video(video_path)
    if not topic_id:
        return None
    if channel_root is not None:
        return channel_root / "04_renders" / "_wip" / topic_id / f"{topic_id}_captions.ass"
    for parent in video_path.resolve().parents:
        if parent.name == "04_renders":
            return parent / "_wip" / topic_id / f"{topic_id}_captions.ass"
        candidate = parent / "04_renders" / "_wip" / topic_id / f"{topic_id}_captions.ass"
        if candidate.exists():
            return candidate
    return None


def _default_script_final_path(
    video_path: Path, channel_root: Path | None
) -> Path | None:
    """Resolve the canonical script_FINAL.txt for a given video.

    Layout per CLAUDE.md:
        <channel_root>/02_scripts/_drafts/<topic_id>/script_FINAL.txt
    """
    topic_id = _topic_id_from_video(video_path)
    if not topic_id:
        return None
    if channel_root is not None:
        return channel_root / "02_scripts" / "_drafts" / topic_id / "script_FINAL.txt"
    for parent in video_path.resolve().parents:
        candidate = parent / "02_scripts" / "_drafts" / topic_id / "script_FINAL.txt"
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Public API: check_variant
# ---------------------------------------------------------------------------


def _pass(check_id: int, name: str, expected: str, actual: str) -> CheckResult:
    return CheckResult(check_id, name, True, "PASS", "ok", expected, actual)


def _fail(
    check_id: int,
    name: str,
    expected: str,
    actual: str,
    message: str,
) -> CheckResult:
    return CheckResult(check_id, name, False, "FAIL", message, expected, actual)


def check_variant(
    video_path: Path,
    *,
    captions_path: Path | None = None,
    script_final_path: Path | None = None,
    channel_root: Path | None = None,
    check_cited_observation: bool = False,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    lufs_tolerance: float = DEFAULT_LUFS_TOLERANCE,
    target_tp: float = DEFAULT_TARGET_TP,
    expected_resolution: tuple[int, int] = DEFAULT_EXPECTED_RESOLUTION,
    required_channels: int = DEFAULT_REQUIRED_CHANNELS,
    required_video_codec: str = DEFAULT_REQUIRED_VIDEO_CODEC,
    valid_framerates: Iterable[float] = DEFAULT_VALID_FRAMERATES,
    framerate_tolerance: float = DEFAULT_FRAMERATE_TOLERANCE,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
    av_parity_tolerance_s: float = DEFAULT_AV_PARITY_TOLERANCE_S,
    min_caption_density: float = DEFAULT_MIN_CAPTION_DENSITY,
    first_second_mean_db: float = DEFAULT_FIRST_SECOND_MEAN_DB,
) -> QAReport:
    """Run all 11 mandatory checks (+ optional #13) against a single video.

    NEVER raises. A missing video file becomes a CheckResult(check_id=1, ok=False).
    A crashing ffmpeg becomes a CheckResult(ok=False) for whichever check tripped
    it. Caller MUST inspect `.ok` / `.failures` and decide whether to halt the
    pipeline (typically by raising `PipelineQAFailed`).

    Args:
        video_path: Path to the .mp4 to validate.
        captions_path: Optional explicit override; auto-resolved from
            `<channel_root>/04_renders/_wip/<topic_id>/<topic_id>_captions.ass`
            if None.
        script_final_path: Optional explicit override for the cited-observation
            check; auto-resolved from
            `<channel_root>/02_scripts/_drafts/<topic_id>/script_FINAL.txt`
            if None and `check_cited_observation=True`.
        channel_root: Channel root for default path resolution.
        check_cited_observation: If True, run check #13 (URL + named-source
            regex on script_FINAL.txt). Off by default per spec.
        target_lufs: Integrated-loudness target in LUFS (default -14.0 per R1).
        lufs_tolerance: Allowed deviation in LU (default 1.0).
        target_tp: True-peak ceiling in dBTP (default -0.5).
        expected_resolution: (width, height) (default (1080, 1920)).
        required_channels: Required audio channel count (default 2 per R7).
        required_video_codec: Expected video codec (default "h264").
        valid_framerates: Iterable of acceptable framerates (default 29.97, 30.0).
        framerate_tolerance: Allowed +/- around any of `valid_framerates`.
        min_duration_s / max_duration_s: Inclusive duration bounds.
        av_parity_tolerance_s: Max |audio_dur - video_dur| allowed.
        min_caption_density: Min Dialogue events / second of video.
        first_second_mean_db: Mean-volume floor on the 0-1s window for #12.

    Returns:
        QAReport with the full per-check results table.
    """
    t0 = time.monotonic()
    video_path = Path(video_path)
    log.info("prepublish_qa: %s", video_path)

    results: list[CheckResult] = []
    probe: dict = {}

    # Check #1: integrity (delegate to T2). On success we get a probe dict we
    # reuse for #3, #4, #5, #6, #7 to avoid re-running ffprobe.
    integrity: dict = {}
    try:
        integrity = check_integrity(
            video_path,
            min_size_bytes=10_000,         # small enough to allow lavfi test fixtures
            min_duration_s=min_duration_s,
            require_video=True,
            require_audio=True,
            deep_decode_seconds=1.0,
        )
        results.append(_pass(
            1, "integrity",
            "T2 check_integrity passes",
            f"size={integrity.get('size_bytes')}B "
            f"dur={integrity.get('duration_s', 0):.2f}s "
            f"deep_decode_ok={integrity.get('deep_decode_ok')}",
        ))
        probe = integrity
    except FileNotFoundError as e:
        results.append(_fail(
            1, "integrity",
            "video file exists on disk",
            "missing",
            str(e),
        ))
        # Without a file we can't do checks 2-12.
        return _finalize(video_path, results, t0, probe)
    except MediaIntegrityError as e:
        results.append(_fail(
            1, "integrity",
            "T2 check_integrity passes",
            f"failed: {e.reason}",
            str(e),
        ))
        # We may still have a usable probe for the rest if ffprobe ran but a
        # later sub-check tripped — but to keep behaviour simple we re-run
        # ffprobe here on a best-effort basis.
        try:
            probe_fallback = _ffprobe_streams(video_path)
            probe = _probe_to_integrity_dict(probe_fallback) if probe_fallback else {}
        except Exception:  # pragma: no cover - best-effort only
            probe = {}

    # We need an ffprobe dict that has the raw streams to read framerate (T2's
    # check_integrity doesn't expose r_frame_rate). Run ffprobe once more for that.
    raw_probe: dict = {}
    try:
        raw_probe = _ffprobe_streams(video_path)
    except Exception as e:  # noqa: BLE001 - we log and convert to FAIL below
        log.error("ffprobe failed for %s: %s", video_path, e)

    vstream = _pick_stream(raw_probe, "video")
    astream = _pick_stream(raw_probe, "audio")
    fmt = (raw_probe.get("format") or {})

    # Check #2: duration
    duration_s = probe.get("duration_s")
    if duration_s is None and fmt.get("duration"):
        try:
            duration_s = float(fmt["duration"])
        except (TypeError, ValueError):
            duration_s = None
    if duration_s is None:
        results.append(_fail(
            2, "duration",
            f"{min_duration_s}s..{max_duration_s}s",
            "unknown",
            "ffprobe did not report a duration",
        ))
    elif duration_s < min_duration_s or duration_s > max_duration_s:
        results.append(_fail(
            2, "duration",
            f"{min_duration_s}s..{max_duration_s}s",
            f"{duration_s:.2f}s",
            f"duration {duration_s:.2f}s outside [{min_duration_s}, {max_duration_s}]",
        ))
    else:
        results.append(_pass(
            2, "duration",
            f"{min_duration_s}s..{max_duration_s}s",
            f"{duration_s:.2f}s",
        ))

    # Check #3: resolution
    resolution = probe.get("video_resolution")
    if resolution is None and vstream is not None:
        w, h = vstream.get("width"), vstream.get("height")
        if isinstance(w, int) and isinstance(h, int):
            resolution = (w, h)
    expected_res_str = f"{expected_resolution[0]}x{expected_resolution[1]}"
    if resolution is None:
        results.append(_fail(
            3, "resolution",
            expected_res_str, "unknown",
            "no video resolution reported",
        ))
    elif tuple(resolution) != tuple(expected_resolution):
        results.append(_fail(
            3, "resolution",
            expected_res_str, f"{resolution[0]}x{resolution[1]}",
            f"resolution {resolution[0]}x{resolution[1]} != {expected_res_str}",
        ))
    else:
        results.append(_pass(
            3, "resolution", expected_res_str, f"{resolution[0]}x{resolution[1]}",
        ))

    # Check #4: framerate
    valid_fr = tuple(valid_framerates)
    fr_value = _parse_framerate((vstream or {}).get("r_frame_rate"))
    expected_fr_str = "/".join(f"{f:g}" for f in valid_fr)
    if fr_value is None:
        results.append(_fail(
            4, "framerate", f"in {{{expected_fr_str}}}", "unknown",
            "ffprobe did not report r_frame_rate",
        ))
    elif not any(abs(fr_value - target) <= framerate_tolerance for target in valid_fr):
        results.append(_fail(
            4, "framerate", f"in {{{expected_fr_str}}}", f"{fr_value:.3f}",
            f"framerate {fr_value:.3f} not within +/-{framerate_tolerance} of any of {valid_fr}",
        ))
    else:
        results.append(_pass(
            4, "framerate", f"in {{{expected_fr_str}}}", f"{fr_value:.3f}",
        ))

    # Check #5: video codec
    vcodec = probe.get("video_codec") or (vstream or {}).get("codec_name")
    if not vcodec:
        results.append(_fail(
            5, "video_codec", required_video_codec, "unknown",
            "no video codec reported",
        ))
    elif vcodec != required_video_codec:
        results.append(_fail(
            5, "video_codec", required_video_codec, vcodec,
            f"video codec {vcodec} != {required_video_codec}",
        ))
    else:
        results.append(_pass(5, "video_codec", required_video_codec, vcodec))

    # Check #6: AV duration parity
    vdur = _stream_duration(vstream, fmt)
    adur = _stream_duration(astream, fmt)
    if vdur is None or adur is None:
        results.append(_fail(
            6, "av_parity",
            f"|adur-vdur|<={av_parity_tolerance_s}s",
            f"vdur={vdur} adur={adur}",
            "could not read both audio and video stream durations",
        ))
    else:
        delta = abs(vdur - adur)
        if delta > av_parity_tolerance_s:
            results.append(_fail(
                6, "av_parity",
                f"|adur-vdur|<={av_parity_tolerance_s}s",
                f"|{vdur:.3f}-{adur:.3f}|={delta:.3f}s",
                f"audio/video duration drift {delta:.3f}s exceeds {av_parity_tolerance_s}s",
            ))
        else:
            results.append(_pass(
                6, "av_parity",
                f"|adur-vdur|<={av_parity_tolerance_s}s",
                f"delta={delta:.3f}s",
            ))

    # Check #7: audio channels (R7 = stereo)
    channels = probe.get("audio_channels")
    if channels is None and astream is not None:
        try:
            channels = int(astream.get("channels"))
        except (TypeError, ValueError):
            channels = None
    if channels is None:
        results.append(_fail(
            7, "audio_channels", str(required_channels), "unknown",
            "ffprobe did not report channel count",
        ))
    elif channels != required_channels:
        results.append(_fail(
            7, "audio_channels", str(required_channels), str(channels),
            f"audio_channels={channels} != {required_channels} (R7 requires stereo)",
        ))
    else:
        results.append(_pass(7, "audio_channels", str(required_channels), str(channels)))

    # Check #8: LUFS within ±tolerance of target_lufs (R1 uniform).
    # Check #9: true-peak <= target_tp.
    measured_i: float | None = None
    measured_tp: float | None = None
    try:
        loud = _measure_loudness(video_path, target_lufs, target_tp)
        measured_i = float(loud["input_i"])
        measured_tp = float(loud["input_tp"])
    except Exception as e:  # noqa: BLE001 - convert to two FAIL rows
        results.append(_fail(
            8, "lufs",
            f"{target_lufs:.1f} ±{lufs_tolerance:.1f} LU",
            "measurement failed",
            f"loudnorm measurement failed: {e}",
        ))
        results.append(_fail(
            9, "true_peak",
            f"<= {target_tp:.1f} dBTP",
            "measurement failed",
            f"loudnorm measurement failed: {e}",
        ))
    else:
        delta_lu = abs(measured_i - target_lufs)
        if delta_lu > lufs_tolerance:
            results.append(_fail(
                8, "lufs",
                f"{target_lufs:.1f} ±{lufs_tolerance:.1f} LU",
                f"{measured_i:.2f} LU",
                f"measured {measured_i:.2f} LUFS deviates {delta_lu:.2f} LU "
                f"from target {target_lufs:.1f}",
            ))
        else:
            results.append(_pass(
                8, "lufs",
                f"{target_lufs:.1f} ±{lufs_tolerance:.1f} LU",
                f"{measured_i:.2f} LU",
            ))
        if measured_tp > target_tp:
            results.append(_fail(
                9, "true_peak",
                f"<= {target_tp:.1f} dBTP",
                f"{measured_tp:.2f} dBTP",
                f"true-peak {measured_tp:.2f} dBTP exceeds ceiling {target_tp:.1f}",
            ))
        else:
            results.append(_pass(
                9, "true_peak",
                f"<= {target_tp:.1f} dBTP",
                f"{measured_tp:.2f} dBTP",
            ))

    # Check #10: caption sidecar exists + parseable
    resolved_captions = (
        Path(captions_path) if captions_path is not None
        else _default_captions_path(video_path, channel_root)
    )
    cap_text: str | None = None
    if resolved_captions is None:
        results.append(_fail(
            10, "captions",
            "<topic_id>_captions.ass exists & parseable",
            "could not resolve path",
            "topic_id not parseable from video filename and no captions_path given",
        ))
    elif not resolved_captions.exists():
        results.append(_fail(
            10, "captions",
            "<topic_id>_captions.ass exists & parseable",
            f"missing at {resolved_captions}",
            f"caption sidecar not found: {resolved_captions}",
        ))
    else:
        try:
            cap_text = resolved_captions.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            results.append(_fail(
                10, "captions",
                "<topic_id>_captions.ass exists & parseable",
                "unreadable",
                f"could not read {resolved_captions}: {e}",
            ))
            cap_text = None
        else:
            if not _ass_is_parseable(cap_text):
                results.append(_fail(
                    10, "captions",
                    "<topic_id>_captions.ass exists & parseable",
                    "missing [Events] or Dialogue lines",
                    f"{resolved_captions.name} did not look like a valid .ass file",
                ))
            else:
                results.append(_pass(
                    10, "captions",
                    "<topic_id>_captions.ass exists & parseable",
                    f"{resolved_captions.name} parseable",
                ))

    # Check #11: caption density
    if cap_text is None or duration_s is None:
        results.append(_fail(
            11, "caption_density",
            f">= {min_caption_density:.2f} events/sec",
            "n/a",
            "caption file or duration unavailable; cannot compute density",
        ))
    else:
        n_events = _parse_ass_dialogue_count(cap_text)
        density = n_events / duration_s if duration_s > 0 else 0.0
        if density < min_caption_density:
            results.append(_fail(
                11, "caption_density",
                f">= {min_caption_density:.2f} events/sec",
                f"{density:.2f} events/sec ({n_events}/{duration_s:.2f}s)",
                f"caption density {density:.2f} ev/s below floor {min_caption_density:.2f}",
            ))
        else:
            results.append(_pass(
                11, "caption_density",
                f">= {min_caption_density:.2f} events/sec",
                f"{density:.2f} events/sec ({n_events}/{duration_s:.2f}s)",
            ))

    # Check #12 (fused silent-intro + black-frame opener; we keep id=12 to keep
    # spec-side numbering even though we dropped the standalone black-frame check).
    audio_db = _measure_first_second_audio_mean_db(video_path)
    black_regions = _detect_first_second_black(video_path)
    sub_messages: list[str] = []
    sub_actuals: list[str] = []
    sub_ok = True
    if audio_db is None:
        sub_messages.append("could not measure first-second audio mean_volume")
        sub_actuals.append("audio=unknown")
        sub_ok = False
    else:
        sub_actuals.append(f"audio_mean={audio_db:.1f}dB")
        if audio_db < first_second_mean_db:
            sub_messages.append(
                f"first-second audio mean_volume {audio_db:.1f} dB < "
                f"floor {first_second_mean_db:.1f} dB (silent intro)"
            )
            sub_ok = False
    if black_regions:
        bs = ",".join(f"[{s:.2f}-{e:.2f}]" for s, e in black_regions)
        sub_actuals.append(f"black={bs}")
        sub_messages.append(f"first-second video has black region(s): {bs}")
        sub_ok = False
    else:
        sub_actuals.append("black=none")
    actual_str = " ".join(sub_actuals)
    expected_str = (
        f"audio_mean>={first_second_mean_db:.1f}dB AND no black region in [0,1]s"
    )
    if sub_ok:
        results.append(_pass(12, "first_second_intro", expected_str, actual_str))
    else:
        results.append(_fail(
            12, "first_second_intro", expected_str, actual_str,
            "; ".join(sub_messages),
        ))

    # Check #13 (opt-in)
    if check_cited_observation:
        resolved_script = (
            Path(script_final_path) if script_final_path is not None
            else _default_script_final_path(video_path, channel_root)
        )
        if resolved_script is None:
            results.append(_fail(
                13, "cited_observation",
                ">=1 URL AND >=1 named source in script_FINAL.txt",
                "script path unresolved",
                "topic_id not parseable and no script_final_path given",
            ))
        elif not resolved_script.exists():
            results.append(_fail(
                13, "cited_observation",
                ">=1 URL AND >=1 named source in script_FINAL.txt",
                f"missing at {resolved_script}",
                f"script_FINAL not found: {resolved_script}",
            ))
        else:
            try:
                stxt = resolved_script.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                results.append(_fail(
                    13, "cited_observation",
                    ">=1 URL AND >=1 named source in script_FINAL.txt",
                    "unreadable",
                    f"could not read {resolved_script}: {e}",
                ))
            else:
                ok, missing = _check_cited_observation(stxt)
                if ok:
                    results.append(_pass(
                        13, "cited_observation",
                        ">=1 URL AND >=1 named source in script_FINAL.txt",
                        f"{resolved_script.name}: ok",
                    ))
                else:
                    results.append(_fail(
                        13, "cited_observation",
                        ">=1 URL AND >=1 named source in script_FINAL.txt",
                        f"missing: {', '.join(missing)}",
                        "script_FINAL is missing one or more required source markers",
                    ))

    return _finalize(video_path, results, t0, probe)


def _finalize(
    video_path: Path,
    results: list[CheckResult],
    t0: float,
    probe: dict,
) -> QAReport:
    runtime_s = time.monotonic() - t0
    ok = all(r.ok for r in results)
    return QAReport(
        video_path=video_path,
        ok=ok,
        results=tuple(results),
        runtime_s=runtime_s,
        checks_run=len(results),
        probe=probe,
    )


def _pick_stream(probe: dict, kind: str) -> dict | None:
    for s in probe.get("streams") or []:
        if s.get("codec_type") == kind:
            return s
    return None


def _stream_duration(stream: dict | None, fmt: dict) -> float | None:
    if stream is not None and stream.get("duration") is not None:
        try:
            return float(stream["duration"])
        except (TypeError, ValueError):
            pass
    if fmt.get("duration") is not None:
        try:
            return float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    return None


def _probe_to_integrity_dict(raw_probe: dict) -> dict:
    """Compress an ffprobe dict into the same shape T2's check_integrity returns."""
    streams = raw_probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = raw_probe.get("format") or {}
    return {
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "video_codec": (video or {}).get("codec_name"),
        "video_resolution": (
            (int(video["width"]), int(video["height"]))
            if video and video.get("width") and video.get("height")
            else None
        ),
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_channels": (
            int(audio["channels"]) if audio and audio.get("channels") else None
        ),
        "audio_sample_rate": (
            int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None
        ),
        "deep_decode_ok": False,
    }


# ---------------------------------------------------------------------------
# Public API: check_topic
# ---------------------------------------------------------------------------


def check_topic(
    topic_id: str,
    channel_root: Path,
    *,
    check_cited_observation: bool = False,
    platforms: Iterable[str] = PLATFORMS,
    **variant_kwargs,
) -> dict[str, QAReport]:
    """Run `check_variant` against every platform variant for `topic_id`.

    Resolves variants from `<channel_root>/05_exports/{youtube,tiktok,instagram}/`.
    Returns a dict keyed by platform code ('yt', 'tt', 'ig') -> QAReport.

    Raises:
        FileNotFoundError: when an expected variant file is missing on disk.
            (Per the user-instruction contract: only `check_topic` may raise on
            missing files; `check_variant` always returns a QAReport.)
    """
    channel_root = Path(channel_root)
    plat_dir_map = {
        "yt": channel_root / "05_exports" / "youtube",
        "tt": channel_root / "05_exports" / "tiktok",
        "ig": channel_root / "05_exports" / "instagram",
    }

    reports: dict[str, QAReport] = {}
    for plat in platforms:
        if plat not in plat_dir_map:
            raise ValueError(f"unknown platform code: {plat!r}")
        variant_path = plat_dir_map[plat] / f"{topic_id}_{plat}.mp4"
        if not variant_path.exists():
            raise FileNotFoundError(
                f"expected variant for topic {topic_id!r} platform {plat!r} "
                f"not found at {variant_path}"
            )
        log.info("checking %s variant: %s", plat, variant_path)
        reports[plat] = check_variant(
            variant_path,
            channel_root=channel_root,
            check_cited_observation=check_cited_observation,
            **variant_kwargs,
        )
    return reports


# ---------------------------------------------------------------------------
# Public API: check_script (Sprint 5 L2 — template-artifact scan)
# ---------------------------------------------------------------------------


# Check ID for the script-side template-artifact scan. The Sprint 5 brief
# called this "#13" but #13 was already taken by the opt-in cited-observation
# check (commit 817bc13). Using the next free integer keeps both checks
# independently addressable and avoids reflowing any existing call-site.
SCRIPT_ARTIFACT_CHECK_ID: int = 14
SCRIPT_ARTIFACT_CHECK_NAME: str = "script_template_artifacts"

# Check #15 (weekly-review 2026-05-29, PU-7b): script-side sourcing-hygiene
# halt. A FINAL script must never ship with (a) a residual `[VERIFY: ...]`
# placeholder tag (gate-2 should have resolved or stripped it — `_08_001`
# shipped with an open one, R2 §7), or (b) an ANONYMOUS "a Reddit/X user…"
# citation that violates the cited-observation durable rule (named-source-only;
# R2 §4). Runs at the same pre-render gate as #14 (`check_script`), BEFORE TTS.
SCRIPT_SOURCING_CHECK_ID: int = 15
SCRIPT_SOURCING_CHECK_NAME: str = "script_sourcing_hygiene"

# Check #16 (forward-looking pre-render lint): halt render on an unresolved
# placeholder token (`[VERIFY` / `[NEEDS` / `[TODO` / `[FIXME`) or a retired /
# forbidden CTA phrase in a FINAL script. Motivated by two real published
# defects: `_08_001` (a `[VERIFY: ...]` note spoken aloud, vid Eaxrx6CVJ0s) and
# `_11_002` (the retired `Comment "deploy" and I will send you the link.`
# comment-bait). Runs at the same pre-render gate as #14/#15, BEFORE TTS. The
# banned-CTA list is pulled from the style guide's retired/forbidden sections
# (see tools.script_prerender_patterns) so it stays in sync; a built-in baseline
# is always enforced even if the style guide can't be read.
SCRIPT_PRERENDER_CHECK_ID: int = 16
SCRIPT_PRERENDER_CHECK_NAME: str = "script_prerender_lint"

# Residual placeholder tag: matches `[VERIFY]` and `[VERIFY: ...]`, case-
# insensitive — mirrors tools.script_response_parser._VERIFY_TAG_RE semantics.
_RESIDUAL_VERIFY_RE = re.compile(r"\[VERIFY\b[^\]]*\]", re.IGNORECASE)

# A named Reddit/X handle anywhere in the SAME line suppresses the anonymous-
# citation finding for that line — "u/lreeves on r/ClaudeAI" is a NAMED source
# and must pass. (`u/<handle>` or a bare `@handle`.)
_NAMED_HANDLE_RE = re.compile(
    r"(?:\bu/[A-Za-z0-9_\-]{3,}\b)|(?<![A-Za-z0-9_])@[A-Za-z0-9_]{2,}\b"
)

# Reporting verbs that mark a citation framing (used by several anon patterns).
_REPORTING_VERB = (
    r"(?:say|says|said|report|reports|reported|claim|claims|claimed|note|notes|"
    r"noted|find|finds|found|write|writes|wrote|post|posts|posted|share|shares|"
    r"shared|observe|observes|observed|mention|mentions|mentioned|build|builds|"
    r"built|discover|discovers|discovered|show|shows|showed|tried|test|tests|tested)"
)

# PU-4 (2026-06-19 review): a name-adjacency guard so a legitimately NAMED source
# that happens to use a role word is NOT flagged. Matches a role word
# (researcher(s)/scientist(s)/developer/engineer) when it is immediately preceded
# OR followed by a proper name or "named <Name>" — e.g. "Anthropic researcher Tom
# Brown", "researcher Tom Brown", "a developer named Jane". When this matches on a
# line, that line is treated as named (the anon finding is suppressed), mirroring
# how a `u/`/`@` handle suppresses it. Kept conservative: requires a Capitalized
# token (or the explicit word "named") adjacent to the role word.
_NAMED_ROLE_RE = re.compile(
    r"(?:"
    # <Proper Noun> <role>  →  "Anthropic researcher", "OpenAI engineer"
    r"\b[A-Z][A-Za-z0-9.\-]+\s+(?:researchers?|scientists?|developer|engineer)\b"
    r"|"
    # <role> named <Name>  →  "a developer named Jane"
    r"\b(?:researchers?|scientists?|developer|engineer)\s+named\s+[A-Z][A-Za-z\-]+"
    r"|"
    # <role> <First Last>  →  "researcher Tom Brown" (two capitalized tokens)
    r"\b(?:researchers?|scientists?|developer|engineer)\s+[A-Z][A-Za-z\-]+\s+[A-Z][A-Za-z\-]+"
    r")"
)

# Anonymous-citation templates. Deliberately CONSERVATIVE (this is a render-
# halting gate on a live auto-pipeline — a false positive blocks a ship), so we
# only match the specific "anonymous source" shapes the cited-observation rule
# bans, and only when no named handle/role-name co-occurs on the line (checked
# separately):
#   - "a/an/one Reddit|X|Twitter user" (+ optional said/reported/...)
#   - "a/an/one user on r/<sub>" or "a/some Redditor(s)"
#   - "a/an/one user on Reddit|Twitter|X|HN|Hacker News|Discord|a/the forum"
#     (PU-4: broadened past the r/<sub>-only shape)
#   - "a/an/one developer|user|redditor (says|said|reported|reports|claims|
#      noted|found|wrote|posted|shared|observed)"
#   - "a/an/one developer" standalone (PU-4: bare role word as sole attribution)
#   - "Researchers|Scientists <reporting-verb>" with no named lab/person (PU-4)
#   - "someone <reporting-verb>" as sole attribution (PU-4)
_ANON_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:a|an|one)\s+(?:reddit|x|twitter)\s+user\b", re.IGNORECASE),
    re.compile(r"\b(?:a|an|one|some)\s+redditors?\b", re.IGNORECASE),
    re.compile(r"\b(?:a|an|one)\s+user\s+on\s+r/", re.IGNORECASE),
    # PU-4: "a user on <surface>" beyond the r/<sub> shape.
    re.compile(
        r"\b(?:a|an|one)\s+user\s+on\s+"
        r"(?:reddit|twitter|x|hn|hacker\s+news|discord|"
        r"(?:a|an|the|some)\s+(?:forum|thread|server|subreddit))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:a|an|one)\s+(?:developer|user|redditor|commenter|poster)\s+"
        r"(?:says|said|reported|reports|claims|claimed|noted|notes|found|"
        r"wrote|posted|shared|observed|mentioned)\b",
        re.IGNORECASE,
    ),
    # PU-4: bare "a developer" as a sole attribution noun phrase. The
    # name-adjacency guard (_NAMED_ROLE_RE) suppresses "a developer named Jane".
    re.compile(r"\b(?:a|an|one)\s+developer\b", re.IGNORECASE),
    # PU-4: leading / standalone "Researchers" or "scientists" + a reporting
    # verb, with no named lab/person (suppressed by _NAMED_ROLE_RE when present).
    re.compile(
        rf"\b(?:researchers|scientists)\s+{_REPORTING_VERB}\b",
        re.IGNORECASE,
    ),
    # PU-4: "someone" as the sole attribution + a reporting/action verb.
    re.compile(
        rf"\bsomeone\s+{_REPORTING_VERB}\b",
        re.IGNORECASE,
    ),
)

# Heuristic upper bound on a sane script body. `_12_002`-class scripts are
# ~120 words / <1 KB. Anything past this is almost certainly a misrouted
# binary or full prompt-response dump, and we should fail rather than scan
# megabytes of bytes-coerced text.
_SCRIPT_MAX_SCAN_BYTES: int = 256 * 1024  # 256 KiB

# Files whose extensions we reject up-front (mostly to surface "you passed
# an .mp4 instead of the script" as a clean FAIL, not a noisy regex hit).
_SCRIPT_REJECT_SUFFIXES: frozenset[str] = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".ass",  # caption sidecars get scanned by Layer 3, not here.
})


def check_script(script_path: Path) -> CheckResult:
    """Scan ``script_FINAL.txt`` for template / internal / stage artifacts.

    Sprint 5 Layer 2 of the three-layer defense against the ``_12_002``
    template-leak failure mode (2026-05-13). The existing 12-check
    `check_variant()` pipeline scans the rendered MEDIA (post-TTS,
    post-render) — by then it is too late, edge-TTS has already spoken the
    artifact aloud. This check runs against the SCRIPT itself and must be
    invoked BEFORE Stage 4 (TTS).

    The brief named this "#13", but #13 is occupied by the existing
    cited-observation opt-in (commit 817bc13). We take the next free
    integer (#14, ``SCRIPT_ARTIFACT_CHECK_ID``); the trade-off was: rename
    the cited-obs check and risk breaking any caller that grep'd by ID, OR
    accept a small numbering inconsistency vs the brief. We chose the
    latter — see the module docstring "Sprint 5 added check #14" note.

    The patterns themselves live in :mod:`tools.script_artifact_patterns`
    (the single source of truth shared by Layers 1, 2, and 3). This
    function ONLY wires those patterns into a `CheckResult`.

    Args:
        script_path: Path to the candidate ``script_FINAL.txt``. Must
            exist, be a regular file, and have a ``.txt`` suffix (a few
            other suffixes are rejected up-front — see
            ``_SCRIPT_REJECT_SUFFIXES``).

    Returns:
        ``CheckResult`` with ``check_id=14``. PASS if the script is clean;
        FAIL if any pattern matched, with the per-line breakdown rendered
        by :func:`tools.script_artifact_patterns.format_matches` in the
        ``message`` field.

        FAIL is also returned for missing files, unreadable files,
        non-files (e.g. a directory), wrong-extension files, and files
        whose contents cannot be decoded as text — we treat every such
        case as a halt rather than a silent pass.

    Never raises; always returns a CheckResult so callers can pass the
    result through their existing FAIL-handling paths.
    """
    expected = "no template/internal/stage artifacts"

    # ----- existence / file-type guards -----
    if script_path is None:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, "no path provided",
            "check_script() called with script_path=None",
        )
    script_path = Path(script_path)
    if not script_path.exists():
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, f"missing at {script_path}",
            f"script_FINAL.txt not found: {script_path}",
        )
    if not script_path.is_file():
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, f"not a regular file: {script_path}",
            f"expected a .txt file but {script_path} is not a regular file",
        )

    suffix = script_path.suffix.lower()
    if suffix in _SCRIPT_REJECT_SUFFIXES:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, f"wrong file type: {suffix}",
            f"check_script expects a .txt script; got {script_path.name} "
            f"({suffix}). Did you mean to call check_variant() on a video?",
        )

    # ----- size / readability guards -----
    try:
        size = script_path.stat().st_size
    except OSError as e:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, "stat() failed",
            f"could not stat {script_path}: {e}",
        )
    if size == 0:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, "empty file",
            f"script_FINAL.txt is empty: {script_path}",
        )
    if size > _SCRIPT_MAX_SCAN_BYTES:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, f"{size} bytes (cap {_SCRIPT_MAX_SCAN_BYTES})",
            f"script_FINAL.txt is suspiciously large ({size} bytes > "
            f"{_SCRIPT_MAX_SCAN_BYTES} cap); refusing to scan in case "
            f"a non-text payload was misrouted as the script.",
        )

    # ----- decode the body -----
    raw_bytes: bytes
    try:
        raw_bytes = script_path.read_bytes()
    except OSError as e:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, "unreadable",
            f"could not read {script_path}: {e}",
        )

    # Reject obvious binaries (null bytes are a strong tell). A real script
    # body — even one with weird Unicode — should never contain \x00.
    if b"\x00" in raw_bytes:
        return _fail(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, "binary content (null bytes present)",
            f"{script_path.name} contains null bytes; cannot scan as a "
            f"text script. Did you point check_script at a binary file?",
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to latin-1 + strict so we can still report something
        # actionable, but flag it as suspect because TTS will mangle
        # whatever encoding this is anyway.
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as e:  # pragma: no cover - decode-replace is total
            return _fail(
                SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
                expected, "decode failed",
                f"could not decode {script_path} as UTF-8 even with replace: {e}",
            )

    # ----- the actual scan (single source of truth) -----
    matches: list[ArtifactMatch] = scan_for_artifacts(text)
    if not matches:
        return _pass(
            SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
            expected, f"{script_path.name}: clean ({len(text)} chars scanned)",
        )

    unique_lines = sorted({m.line_no for m in matches})
    actual = (
        f"{len(matches)} match{'es' if len(matches) != 1 else ''} "
        f"across {len(unique_lines)} line{'s' if len(unique_lines) != 1 else ''}"
    )
    message = (
        f"template / internal-name / stage-instruction artifacts found in "
        f"{script_path.name} (Sprint 5 _12_002 prevention, Layer 2):\n"
        f"{format_matches(matches)}"
    )
    return _fail(
        SCRIPT_ARTIFACT_CHECK_ID, SCRIPT_ARTIFACT_CHECK_NAME,
        expected, actual, message,
    )


def _scan_script_for_sourcing_issues(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line_no, kind, line_text)`` for each sourcing-hygiene hit.

    ``kind`` is ``"residual_verify"`` or ``"anonymous_citation"``. line_no is
    1-based. An empty list means the script is clean.

    Anonymous-citation matches are SUPPRESSED on any line that also carries a
    named Reddit/X handle (``u/x`` / ``@y``) OR a name-adjacent role word
    (``Anthropic researcher Tom Brown`` / ``a developer named Jane``, via
    :data:`_NAMED_ROLE_RE`) — that line is a NAMED citation, which the
    cited-observation rule explicitly allows.
    """
    hits: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    # Residual VERIFY tags — scan full text so a tag split oddly still trips.
    for m in _RESIDUAL_VERIFY_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        line_text = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else m.group(0)
        hits.append((line_no, "residual_verify", line_text))
    # Anonymous citations — line by line so we can check for a co-located handle
    # or a name-adjacent role word (both mark a NAMED source → allowed).
    for i, line in enumerate(lines, start=1):
        if _NAMED_HANDLE_RE.search(line) or _NAMED_ROLE_RE.search(line):
            continue  # named source on this line → allowed
        if any(p.search(line) for p in _ANON_CITATION_PATTERNS):
            hits.append((i, "anonymous_citation", line))
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits


def check_script_sourcing(script_path: Path) -> CheckResult:
    """Check #15 (PU-7b): halt render on residual ``[VERIFY: …]`` tags or
    anonymous "a Reddit/X user" citations in a FINAL script.

    Runs at the same pre-render gate as :func:`check_script` (#14), BEFORE TTS.
    Two failure classes, both motivated by `_08_001` (R2 §4 + §7 —
    a FINAL reached render carrying an open ``[VERIFY:`` tag AND an anonymous
    r/ClaudeAI citation):

      * **residual_verify** — any ``[VERIFY]`` / ``[VERIFY: …]`` placeholder
        survived gate-2 resolution into the signed-off body.
      * **anonymous_citation** — an anonymous-source framing with no named
        handle, violating the cited-observation durable rule. Covers
        "a Reddit/X user", "a/some Redditor(s)", "a user on r/… / Reddit /
        Twitter / HN / a forum", "a developer" (bare or "+ verb"), a bare
        "Researchers / scientists <reporting verb>", and "someone <verb>"
        (the last four broadened under PU-4, 2026-06-19 review).

    Conservative by design (this gate halts a live auto-pipeline ship): the
    anonymous-citation patterns match only the specific banned shapes, and are
    suppressed on any line that also carries a named ``u/`` / ``@`` handle OR a
    name-adjacent role word (``Anthropic researcher Tom Brown`` / ``a developer
    named Jane``) — so a legitimately named source never trips the gate.

    Mirrors :func:`check_script`'s file guards (missing / non-file / wrong
    suffix / empty / oversized / binary all FAIL). Never raises; always
    returns a ``CheckResult`` with ``check_id=15``.
    """
    expected = "no residual [VERIFY:] tags and no anonymous 'a Reddit/X user' citations"

    if script_path is None:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, "no path provided",
            "check_script_sourcing() called with script_path=None",
        )
    script_path = Path(script_path)
    if not script_path.exists():
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, f"missing at {script_path}",
            f"script_FINAL.txt not found: {script_path}",
        )
    if not script_path.is_file():
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, f"not a regular file: {script_path}",
            f"expected a .txt file but {script_path} is not a regular file",
        )
    suffix = script_path.suffix.lower()
    if suffix in _SCRIPT_REJECT_SUFFIXES:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, f"wrong file type: {suffix}",
            f"check_script_sourcing expects a .txt script; got {script_path.name}",
        )
    try:
        size = script_path.stat().st_size
    except OSError as e:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, "stat() failed", f"could not stat {script_path}: {e}",
        )
    if size == 0:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, "empty file", f"script_FINAL.txt is empty: {script_path}",
        )
    if size > _SCRIPT_MAX_SCAN_BYTES:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, f"{size} bytes (cap {_SCRIPT_MAX_SCAN_BYTES})",
            f"script_FINAL.txt is suspiciously large ({size} bytes); refusing to scan.",
        )
    try:
        raw_bytes = script_path.read_bytes()
    except OSError as e:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, "unreadable", f"could not read {script_path}: {e}",
        )
    if b"\x00" in raw_bytes:
        return _fail(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, "binary content (null bytes present)",
            f"{script_path.name} contains null bytes; cannot scan as a text script.",
        )
    text = raw_bytes.decode("utf-8", errors="replace")

    hits = _scan_script_for_sourcing_issues(text)
    if not hits:
        return _pass(
            SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
            expected, f"{script_path.name}: clean ({len(text)} chars scanned)",
        )

    n_verify = sum(1 for _, kind, _ in hits if kind == "residual_verify")
    n_anon = sum(1 for _, kind, _ in hits if kind == "anonymous_citation")
    actual = (
        f"{len(hits)} sourcing issue{'s' if len(hits) != 1 else ''} "
        f"({n_verify} residual [VERIFY:], {n_anon} anonymous citation)"
    )
    detail = "\n".join(
        f"  line {ln} [{kind}]: {lt.strip()[:90]!r}" for ln, kind, lt in hits
    )
    message = (
        f"sourcing-hygiene issues in {script_path.name} (PU-7b check #15 — "
        f"residual VERIFY tag and/or anonymous citation):\n{detail}"
    )
    return _fail(
        SCRIPT_SOURCING_CHECK_ID, SCRIPT_SOURCING_CHECK_NAME,
        expected, actual, message,
    )


def _guard_script_path(
    script_path: Path | None,
    check_id: int,
    check_name: str,
) -> tuple[str | None, CheckResult | None]:
    """Shared existence / type / size / binary guards for the script-side checks.

    Returns ``(text, None)`` once the file is read and decoded, or
    ``(None, CheckResult-FAIL)`` on any guard trip. Mirrors the inline guards in
    :func:`check_script` and :func:`check_script_sourcing` so #16 rejects the
    same misrouted-input cases (missing / non-file / wrong-suffix / empty /
    oversized / binary) rather than silently passing.
    """
    expected = "readable .txt script body"
    if script_path is None:
        return None, _fail(
            check_id, check_name, expected, "no path provided",
            f"{check_name} called with script_path=None",
        )
    script_path = Path(script_path)
    if not script_path.exists():
        return None, _fail(
            check_id, check_name, expected, f"missing at {script_path}",
            f"script_FINAL.txt not found: {script_path}",
        )
    if not script_path.is_file():
        return None, _fail(
            check_id, check_name, expected, f"not a regular file: {script_path}",
            f"expected a .txt file but {script_path} is not a regular file",
        )
    suffix = script_path.suffix.lower()
    if suffix in _SCRIPT_REJECT_SUFFIXES:
        return None, _fail(
            check_id, check_name, expected, f"wrong file type: {suffix}",
            f"{check_name} expects a .txt script; got {script_path.name}",
        )
    try:
        size = script_path.stat().st_size
    except OSError as e:
        return None, _fail(
            check_id, check_name, expected, "stat() failed",
            f"could not stat {script_path}: {e}",
        )
    if size == 0:
        return None, _fail(
            check_id, check_name, expected, "empty file",
            f"script_FINAL.txt is empty: {script_path}",
        )
    if size > _SCRIPT_MAX_SCAN_BYTES:
        return None, _fail(
            check_id, check_name, expected, f"{size} bytes (cap {_SCRIPT_MAX_SCAN_BYTES})",
            f"script_FINAL.txt is suspiciously large ({size} bytes); refusing to scan.",
        )
    try:
        raw_bytes = script_path.read_bytes()
    except OSError as e:
        return None, _fail(
            check_id, check_name, expected, "unreadable",
            f"could not read {script_path}: {e}",
        )
    if b"\x00" in raw_bytes:
        return None, _fail(
            check_id, check_name, expected, "binary content (null bytes present)",
            f"{script_path.name} contains null bytes; cannot scan as a text script.",
        )
    return raw_bytes.decode("utf-8", errors="replace"), None


def check_script_prerender(
    script_path: Path,
    *,
    style_guide_path: str | Path | None = None,
) -> CheckResult:
    """Check #16: pre-render lint — halt render on an unresolved placeholder or a
    retired/forbidden CTA in a FINAL script.

    Forward-looking guard for two failure modes that each shipped to YouTube and
    cost views:

      * **placeholder** — a residual editor / fact-check marker (``[VERIFY``,
        ``[NEEDS``, ``[TODO``, ``[FIXME``) survived gate-2 into the signed-off
        body and was spoken aloud by edge-TTS (`_08_001`: a ``[VERIFY: ...]``
        note rendered mid-VO, vid Eaxrx6CVJ0s).
      * **banned_cta** — a retired transactional CTA / engagement-beg
        (`_11_002`: ``Comment "deploy" and I will send you the link.``), which
        the style guide explicitly retired and whose promised payload is not a
        live funnel.

    The banned-CTA list is pulled from the style guide's retired/forbidden
    sections (:func:`tools.script_prerender_patterns.build_cta_matchers`) so it
    stays in sync with the operator's single source of truth, on top of a
    built-in baseline that is always enforced even when the style guide can't be
    read.

    Runs at the same pre-render gate as #14 / #15, BEFORE TTS. Mirrors their
    file guards; never raises; always returns a ``CheckResult`` with
    ``check_id=16``.

    Args:
        script_path: Path to the candidate ``script_FINAL.txt``.
        style_guide_path: Optional override for the style guide whose
            retired/forbidden CTA phrases augment the baseline. Defaults to
            :data:`tools.script_prerender_patterns.DEFAULT_STYLE_GUIDE_PATH`.
    """
    expected = (
        "no unresolved placeholders ([VERIFY|NEEDS|TODO|FIXME]) and no retired CTA"
    )
    text, guard_fail = _guard_script_path(
        script_path, SCRIPT_PRERENDER_CHECK_ID, SCRIPT_PRERENDER_CHECK_NAME
    )
    if guard_fail is not None:
        return guard_fail

    matchers = build_cta_matchers(style_guide_path)
    matches = scan_script_for_lint(text, matchers)
    if not matches:
        return _pass(
            SCRIPT_PRERENDER_CHECK_ID, SCRIPT_PRERENDER_CHECK_NAME,
            expected, f"{Path(script_path).name}: clean ({len(text)} chars scanned)",
        )

    n_placeholder = sum(1 for m in matches if m.kind == "placeholder")
    n_cta = sum(1 for m in matches if m.kind == "banned_cta")
    actual = (
        f"{len(matches)} issue{'s' if len(matches) != 1 else ''} "
        f"({n_placeholder} placeholder, {n_cta} banned CTA)"
    )
    message = (
        f"pre-render lint failures in {Path(script_path).name} "
        f"(#16 — unresolved placeholder and/or retired CTA):\n"
        f"{format_lint_matches(matches)}"
    )
    return _fail(
        SCRIPT_PRERENDER_CHECK_ID, SCRIPT_PRERENDER_CHECK_NAME,
        expected, actual, message,
    )


def check_topic_script(topic_id: str, channel_root: Path) -> CheckResult:
    """Convenience wrapper: resolve ``script_FINAL.txt`` from a topic_id and run :func:`check_script`.

    Uses the same channel-root layout as the rest of the QA gate:

        ``<channel_root>/02_scripts/_drafts/<topic_id>/script_FINAL.txt``

    Args:
        topic_id: e.g. ``"2026-05-13_002"``. Not validated against the
            ``_TOPIC_ID_RE`` regex here because we want test fixtures to be
            able to use synthetic IDs; the wrapper just builds the path.
        channel_root: Absolute path to the channel root (the directory
            that contains ``02_scripts``, ``04_renders``, etc.).

    Returns:
        CheckResult — never raises. If the resolved script does not exist,
        the underlying :func:`check_script` returns a FAIL with the
        missing-file message.
    """
    script_path = (
        Path(channel_root) / "02_scripts" / "_drafts" / topic_id / "script_FINAL.txt"
    )
    return check_script(script_path)


# ---------------------------------------------------------------------------
# Spec-compatibility shim: run_qa()
# ---------------------------------------------------------------------------


def run_qa(
    video_path: Path,
    *,
    captions_path: Path | None = None,
    script_final_path: Path | None = None,
    check_cited_observation: bool = False,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    lufs_tolerance: float = DEFAULT_LUFS_TOLERANCE,
    target_tp: float = DEFAULT_TARGET_TP,
    expected_resolution: tuple[int, int] = DEFAULT_EXPECTED_RESOLUTION,
    min_caption_density: float = DEFAULT_MIN_CAPTION_DENSITY,
) -> tuple[bool, list[CheckResult], dict]:
    """Original T7 spec signature — returns (passed, failures, report_dict).

    Provided for backward compatibility with anyone wired against the spec
    rather than the Wave 2 `check_variant` API. Internally just calls
    `check_variant`.
    """
    report = check_variant(
        video_path,
        captions_path=captions_path,
        script_final_path=script_final_path,
        check_cited_observation=check_cited_observation,
        target_lufs=target_lufs,
        lufs_tolerance=lufs_tolerance,
        target_tp=target_tp,
        expected_resolution=expected_resolution,
        min_caption_density=min_caption_density,
    )
    return (report.ok, list(report.failures), report.to_dict())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Pre-publish QA gate for ShadowVerse. Validates a single video "
            "(--video) or a topic's three platform variants (--topic)."
        ),
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--video", type=Path, default=None,
        help="Path to a single .mp4 to validate.",
    )
    src.add_argument(
        "--topic", type=str, default=None,
        help="Topic ID (e.g. 2026-05-07_006) — validates yt/tt/ig variants.",
    )
    src.add_argument(
        "--script", type=Path, default=None,
        help=(
            "Path to a script_FINAL.txt to scan for template / internal / stage "
            "artifacts (Sprint 5 L2 check #14). No ffmpeg needed; exits 0 if "
            "clean, 1 if any artifact pattern matched."
        ),
    )
    ap.add_argument(
        "--channel-root", type=Path, default=None,
        help="Channel root (default: %%CONTENTOPS%%\\channels\\ShadowVerse).",
    )
    ap.add_argument("--captions", type=Path, default=None,
                    help="Override caption sidecar path.")
    ap.add_argument("--script-final", type=Path, default=None,
                    help="Override script_FINAL.txt path.")
    ap.add_argument("--check-cited-observation", action="store_true",
                    help="Enable optional check #13 (URL + named source in script).")
    ap.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS)
    ap.add_argument("--lufs-tolerance", type=float, default=DEFAULT_LUFS_TOLERANCE)
    ap.add_argument("--target-tp", type=float, default=DEFAULT_TARGET_TP)
    ap.add_argument("--min-caption-density", type=float,
                    default=DEFAULT_MIN_CAPTION_DENSITY)
    ap.add_argument("--json", action="store_true",
                    help="Emit one-line JSON to stdout (else: tabular report).")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Enable DEBUG logging.")
    return ap


def _print_tabular(report: QAReport, label: str | None = None) -> None:
    header = f"== {label} ==" if label else f"== {report.video_path.name} =="
    print(header)
    width = max((len(r.name) for r in report.results), default=10)
    for r in report.results:
        marker = "PASS" if r.ok else "FAIL"
        print(f"  [{marker}] #{r.check_id:>2} {r.name.ljust(width)} "
              f"expected={r.expected!s} actual={r.actual!s}")
    print(f"  -> overall: {'PASS' if report.ok else 'FAIL'} "
          f"({report.checks_run} checks, {report.runtime_s:.2f}s)")


def main(argv: list[str] | None = None) -> int:
    ap = _build_arg_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # --script mode is text-only — no ffmpeg required. Handle it before the
    # ffmpeg gate so a fresh-clone / minimal-CI environment can still validate
    # the script side of the gate without installing ffmpeg.
    if args.script is not None:
        result = check_script(args.script)
        if args.json:
            print(json.dumps({
                "script_path": str(args.script),
                "ok": result.ok,
                "result": {
                    "check_id": result.check_id,
                    "name": result.name,
                    "ok": result.ok,
                    "severity": result.severity,
                    "message": result.message,
                    "expected": result.expected,
                    "actual": result.actual,
                },
            }))
        else:
            marker = "PASS" if result.ok else "FAIL"
            print(f"== {Path(args.script).name} ==")
            print(f"  [{marker}] #{result.check_id} {result.name}")
            print(f"        expected={result.expected!s}")
            print(f"        actual={result.actual!s}")
            if not result.ok and result.message:
                print(f"        message:\n{result.message}")
        return CLI_EXIT_OK if result.ok else CLI_EXIT_FAIL

    if not _ffmpeg_on_path():
        print("error: ffmpeg / ffprobe not on PATH", file=sys.stderr)
        return CLI_EXIT_USAGE

    overall_ok = True

    if args.video is not None:
        if not args.video.exists():
            print(f"error: video not found: {args.video}", file=sys.stderr)
            return CLI_EXIT_USAGE
        report = check_variant(
            args.video,
            captions_path=args.captions,
            script_final_path=args.script_final,
            channel_root=args.channel_root,
            check_cited_observation=args.check_cited_observation,
            target_lufs=args.target_lufs,
            lufs_tolerance=args.lufs_tolerance,
            target_tp=args.target_tp,
            min_caption_density=args.min_caption_density,
        )
        overall_ok = report.ok
        if args.json:
            print(json.dumps(report.to_dict()))
        else:
            _print_tabular(report)
    else:
        # --topic mode
        if args.channel_root is None:
            print("error: --channel-root is required with --topic", file=sys.stderr)
            return CLI_EXIT_USAGE
        try:
            reports = check_topic(
                args.topic,
                args.channel_root,
                check_cited_observation=args.check_cited_observation,
                target_lufs=args.target_lufs,
                lufs_tolerance=args.lufs_tolerance,
                target_tp=args.target_tp,
                min_caption_density=args.min_caption_density,
            )
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return CLI_EXIT_USAGE
        if args.json:
            print(json.dumps({
                plat: r.to_dict() for plat, r in reports.items()
            }))
        else:
            for plat, r in reports.items():
                _print_tabular(r, label=f"{args.topic} :: {plat}")
        overall_ok = all(r.ok for r in reports.values())

    return CLI_EXIT_OK if overall_ok else CLI_EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
