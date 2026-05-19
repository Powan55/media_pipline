# T2 — `tools/media_integrity.py` (ffprobe + deep-decode wrapper)

**Derives from:** WORKFLOW_AUDIT Phase 5 T1.5, 30_60_90_PLAN Day 1.4, defect: corrupt `2026-05-06_002_tt.mp4` (moov atom missing)
**Severity:** HIGH
**Expected LoC:** ~120
**File:** `C:\ContentOps\_pipeline\tools\media_integrity.py`

---

## Goal

Catch broken video files before they ship. The corrupt `2026-05-06_002_tt.mp4` (13 MB vs ~38 MB peers, missing `moov` atom) almost shipped without anyone knowing — there's currently no integrity gate. This module checks: (a) file exists + min-size, (b) ffprobe parses container + reports streams + duration, (c) a 1-second deep-decode test actually decodes frames.

## Inputs / Outputs

```python
class MediaIntegrityError(RuntimeError):
    """Raised when a media file fails any integrity check."""

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

    Returns a dict with keys: {path, size_bytes, duration_s, video_codec,
        video_resolution (tuple[int,int] or None), audio_codec, audio_channels,
        audio_sample_rate, deep_decode_ok (bool)}.

    Raises MediaIntegrityError with a specific message on any failure:
        - file missing / empty / under min_size_bytes
        - ffprobe fails or returns no streams
        - require_video but no video stream
        - require_audio but no audio stream
        - duration < min_duration_s
        - deep-decode fails (ffmpeg non-zero or zero frames decoded)
    """
```

CLI:

```
python tools/media_integrity.py PATH [--min-size 1000000] [--min-duration 1.0]
                                     [--no-video] [--no-audio] [--deep 1.0]
                                     [--json]
```

`--json` mode prints the result dict to stdout as one-line JSON. Default mode prints a human-readable PASS / FAIL line. Exit 0 PASS, non-zero FAIL (different non-zero codes for different failure classes if practical).

## Acceptance criteria (testable)

1. `check_integrity()` raises `MediaIntegrityError` with a message containing "moov" when given a deliberately-truncated MP4 (the `_06_002_tt.mp4` symptom). If the corrupt file is unavailable, generate one by `dd`-truncating a known-good master in the unit test setup.
2. `check_integrity()` returns a complete dict (all keys non-None where applicable) when given a known-good master like `2026-05-07_001_master.mp4`.
3. Deep-decode test runs `ffmpeg -v error -ss 0 -t {deep_decode_seconds} -i PATH -f null -` and considers exit-code=0 with `frame=` lines in stderr as PASS. Pure-error cases like `Invalid data` → FAIL.
4. ffprobe call uses `ffprobe -v error -show_format -show_streams -of json PATH`.
5. CLI exits 0 PASS, non-zero FAIL; prints clear PASS/FAIL line plus the diagnostic dict in `--json` mode.
6. Runtime <2s per file for masters in the 30-60s / 30-50 MB range.
7. Module is importable as `from tools.media_integrity import check_integrity, MediaIntegrityError` from elsewhere in the pipeline (so Stage 10.1 + 11.2 wiring in Wave 3 can use it directly).

## Unit tests (`tests/test_media_integrity.py`)

- `test_known_good_master_passes` — point at a known-good masters fixture (synthesize one via `ffmpeg -f lavfi -i testsrc -f lavfi -i sine -t 3 ...`); assert returns dict with expected keys.
- `test_truncated_file_raises_with_moov_message` — take a synthesized known-good MP4 and `truncate` it to first 8 KB; assert `MediaIntegrityError` with "moov" in the message OR a generic ffprobe-failed message.
- `test_missing_file_raises_filenotfound` — point at non-existent path; assert `FileNotFoundError`.
- `test_under_min_size_raises` — point at a 100-byte file; assert `MediaIntegrityError` mentioning "size".
- `test_audio_only_when_require_video_raises` — synthesize an audio-only file; pass `require_video=True`; assert raises.
- `test_cli_pass_exits_zero` — subprocess invocation against good fixture; assert returncode 0.
- `test_cli_fail_exits_nonzero` — subprocess invocation against truncated fixture; assert non-zero.

All synthetic-fixture tests should `@pytest.mark.skipif(not _ffmpeg_on_path())` (or unittest equivalent). Fixtures can be created in a temp dir and torn down.

## Dependencies

- FFmpeg + ffprobe binaries on PATH
- stdlib `subprocess`, `json`, `logging`, `pathlib`, `argparse`
- No new pip deps (do NOT add `ffmpeg-python` here — call CLI directly because we want exact ffprobe output)

## References — match this code style

- `tools/youtube_upload.py` — argparse style, exit codes, logger initialization
- `pipeline.py` lines 57-130 (`PipelineHalted` and friends) — exception hierarchy style: subclass with descriptive `__init__`
- `pipeline._vo_edge_tts` lines 1374-1389 — ffmpeg subprocess invocation style

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class — purpose, args, returns, raises
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones, DEBUG for diagnostics, ERROR for failures (logger name `"media_integrity"`)
- [ ] Idempotent (read-only)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 PASS, non-zero FAIL
- [ ] Module docstring at top with usage example
- [ ] Match existing pipeline.py code style
- [ ] No agent frameworks; no LLM API calls; no hardcoded paths
