# T7 — `tools/prepublish_qa.py` (12-check pre-publish QA gate)

**Derives from:** 30_60_90_PLAN Days 4.6 + 5.1, WORKFLOW_AUDIT Phase 5 T1.6, Agent I §6
**Severity:** HIGH
**Expected LoC:** ~250
**File:** `C:\ContentOps\_pipeline\tools\prepublish_qa.py`
**Wave:** 2 (depends on T1 + T2)

---

## Goal

A single-pass QA gate that runs against a video file (master OR per-platform variant) and validates 12 hard checks. Fails loud — operator runs once, fixes upstream, re-runs. Stage 11 of the pipeline (between variants and gate-3 marker drop) raises `PipelineHalted` on any failure so the bad file never reaches operator review.

The 12 checks: (1) file exists + non-empty, (2) duration ≥1s ≤180s, (3) resolution = 1080×1920, (4) framerate ∈ [29.97, 30.0], (5) video codec h264, (6) AV duration parity (audio_duration within ±0.2s of video_duration), (7) audio channels = 2 (stereo per R7), (8) measured LUFS within ±1.0 of -14 (R1), (9) true-peak ≤ -0.5 dBTP, (10) caption file exists alongside the master AND is parseable ASS, (11) caption density ≥ 1.0 chunks/sec averaged, (12) first-second is not pure black AND audio RMS ≥ -50 dB (no silent intro).

Optional check #13 (R6 cited-observation): script_FINAL.txt contains ≥1 URL match AND ≥1 named-source match (Reddit handle / X handle / vendor blog author / specific date). Wired but **off by default**, opt-in via `--check-cited-observation`.

## Inputs / Outputs

```python
class QAFailure(NamedTuple):
    check_id: int
    name: str
    severity: Literal["FAIL", "WARN"]
    message: str
    expected: str
    actual: str

def run_qa(
    video_path: Path,
    *,
    captions_path: Path | None = None,        # auto-resolve if None
    script_final_path: Path | None = None,    # auto-resolve if None
    check_cited_observation: bool = False,
    target_lufs: float = -14.0,
    lufs_tolerance: float = 1.0,
    target_tp: float = -0.5,
    expected_resolution: tuple[int, int] = (1080, 1920),
    min_caption_density: float = 1.0,
) -> tuple[bool, list[QAFailure], dict]:
    """Run all 12 (or 13) checks against video_path. Returns (passed, failures, report_dict)."""
```

Auto-resolve: when `captions_path is None`, look at `<channel_root>/04_renders/_wip/<topic_id>/<topic_id>_captions.ass` (where `topic_id` is parsed from the video filename's stem prefix). When `script_final_path is None`, similar lookup under `02_scripts/_drafts/<topic_id>/script_FINAL.txt`.

**CLI:**
```
python tools/prepublish_qa.py --video <path.mp4>
                             [--captions <path.ass>]
                             [--script-final <path.txt>]
                             [--check-cited-observation]
                             [--target-lufs -14.0] [--lufs-tolerance 1.0]
                             [--json]
                             [--config <config.yaml>]
```

`--json` emits the full report dict to stdout. Non-json mode prints a tabular pass/fail summary. Exit 0 = all checks passed; exit 1 = at least one FAIL; exit 2 = malformed inputs (no video, etc.).

## Acceptance criteria (testable)

1. `run_qa()` against a known-good fixture (use `2026-05-07_004_master.mp4` if present, else synthesize) returns `(True, [], {...})`.
2. `run_qa()` against a deliberately-mono fixture returns `(False, [QAFailure(check_id=7, ...)], {...})`.
3. `run_qa()` against a file at -16 LUFS returns a FAIL on check #8 with `expected="-14.0 ±1.0 LU"` and `actual="-16.0 LU"`.
4. AV-parity check (#6) passes when `|audio_duration - video_duration| ≤ 0.2s`, fails otherwise.
5. Caption density check (#11) computes `len(Dialogue events) / video_duration_s` and fails if < `min_caption_density`.
6. First-second silent-intro check (#12) extracts 0–1s audio via ffmpeg `-t 1 -af volumedetect` and fails if `mean_volume < -50 dB`.
7. First-second black-frame check (#12) extracts 0–1s video via ffmpeg `-t 1 -vf blackdetect` and fails if any black region detected.
8. Cited-observation check (#13, off by default) reads `script_final_path`, regex-matches at least one URL (`https?://\S+`) AND at least one named-source pattern (Reddit `u/...`, X `@...`, vendor-blog domain like `anthropic.com`, OpenAI etc., or a date `\b202\d-\d{2}-\d{2}\b`). Failure if either is absent.
9. CLI `--json` prints valid one-line JSON to stdout; the dict has keys `{video_path, passed, failures: [], checks_run: int, runtime_s: float}`.
10. Total runtime < 15s on a 30-50s 30 MB master (per Phase A 5.1 spec).
11. Module is importable as `from tools.prepublish_qa import run_qa, QAFailure` for pipeline.py wiring.
12. Internally calls `tools.media_integrity.check_integrity()` for checks #1-#6, and reuses `tools.audio_loudnorm` measurement helper if exposed (or re-implements pass-1 measure inline).
13. Cleanly imports both T1 and T2 modules (verifies the wave-1 dependency wiring).

## Dependencies

- T1 (`audio_loudnorm.py`) — for LUFS/TP measurement (pass-1 measure, no normalization)
- T2 (`media_integrity.py`) — for checks #1-#6 (file integrity, codec, AV parity)
- ffmpeg + ffprobe binaries
- stdlib `subprocess`, `json`, `re`, `logging`, `pathlib`, `argparse`
- No new pip deps

## Unit tests (`tests/test_prepublish_qa.py`)

- `test_known_good_master_passes` — synthesize a 5s 1080×1920 stereo file at -14 LUFS via ffmpeg lavfi; assert all 12 checks pass.
- `test_mono_fails_check_7` — synthesize a mono variant; assert FAIL with `check_id=7`.
- `test_low_lufs_fails_check_8` — synthesize a file at -22 LUFS; assert FAIL with `check_id=8`.
- `test_silent_intro_fails_check_12` — synthesize a file with first 1s silent (lavfi `anullsrc=cl=stereo:duration=1` + sine 1-5s, concat); assert FAIL with `check_id=12`.
- `test_black_intro_fails_check_12` — synthesize a file with first 1s black (`color=c=black:s=1080x1920:d=1` + testsrc, concat); assert FAIL with `check_id=12`.
- `test_caption_density_fail` — pass an ASS file with only 5 events on a 30s video; assert FAIL with `check_id=11`.
- `test_cited_observation_off_by_default` — pass a script_FINAL with no URL; assert PASS overall (because flag off).
- `test_cited_observation_on_no_url_fails` — pass a script_FINAL with no URL + flag on; assert FAIL with `check_id=13`.
- `test_cli_json_mode` — subprocess invocation with `--json`, parse stdout, validate schema.

All synthesis tests should `@pytest.mark.skipif(not _ffmpeg_on_path())` (or unittest equivalent).

## References — match this code style

- T1 `tools/audio_loudnorm.py` — LUFS measurement helper invocation
- T2 `tools/media_integrity.py` — exception class + dict-return idiom
- `tools/youtube_upload.py` — argparse + logger style + JSON stdout dual-output

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class — purpose, args, returns, raises
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`
- [ ] Logging at INFO/DEBUG/ERROR appropriately (logger name `"prepublish_qa"`)
- [ ] Idempotent (read-only)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths (use config or args)
- [ ] f-strings, PEP 8 line length 100
- [ ] CLI: argparse with --help, exit 0/1/2 codes
- [ ] Module docstring with usage example
- [ ] No agent frameworks; no LLM API calls
- [ ] Imports T1 and T2 from `tools.audio_loudnorm` / `tools.media_integrity` (use `sys.path` insert idiom from `tools/youtube_upload.py` if needed)
