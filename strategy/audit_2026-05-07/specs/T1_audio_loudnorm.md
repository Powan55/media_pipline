# T1 — `tools/audio_loudnorm.py` (two-pass FFmpeg loudnorm)

**Derives from:** RESOLUTIONS R1, 30_60_90_PLAN Day 1.3, WORKFLOW_AUDIT Phase 5 T1.1
**Severity:** HIGH
**Expected LoC:** ~80
**File:** `C:\ContentOps\_pipeline\tools\audio_loudnorm.py`

---

## Goal

Replace the existing single-pass `loudnorm=I=-14:LRA=11:TP=-1.5` filter (lives inline inside `pipeline._vo_edge_tts`) with a proper two-pass EBU R128 normalization tool that lands within ±0.5 LU of the -14 LUFS target. This is the largest measured production defect: all 9 existing masters drift to -15.1 to -15.6 LUFS.

The two-pass approach is non-negotiable per FFmpeg docs — single-pass `loudnorm` is dynamic-range-compressed and only approximates the target. Two-pass measures first, then applies the linear correction.

## Inputs / Outputs

```python
def normalize_vo(
    src_wav: Path,
    dst_wav: Path,
    *,
    target_lufs: float = -14.0,
    target_tp: float = -1.0,
    target_lra: float = 11.0,
    sample_rate_hz: int = 48000,
) -> dict:
    """Run two-pass FFmpeg loudnorm on src_wav, write dst_wav, return measurement dict.

    Returns: dict with keys {input_i, input_tp, input_lra, input_thresh,
             output_i, output_tp, output_lra, output_thresh, normalization_type,
             target_offset}. All values floats parsed from FFmpeg's pass-1 JSON output.
    """
```

CLI:

```
python tools/audio_loudnorm.py --src <path.wav> --dst <path.wav>
                               [--target-lufs -14.0] [--target-tp -1.0]
                               [--target-lra 11.0]
```

Stdout on success: one-line JSON with the measurement dict + `output_lufs_measured` (a separate ffprobe pass on dst_wav). Exit 0 success, non-zero failure.

## Acceptance criteria (testable)

1. `normalize_vo()` runs `ffmpeg ... -af loudnorm=I=...:TP=...:LRA=...:print_format=json` for pass 1 and parses the trailing JSON object from stderr (FFmpeg prints loudnorm json to stderr, not stdout).
2. Pass 2 runs `ffmpeg ... -af loudnorm=I=...:TP=...:LRA=...:measured_I=...:measured_TP=...:measured_LRA=...:measured_thresh=...:offset=...:linear=true:print_format=summary` using the parsed measurements.
3. Output WAV measures within ±0.5 LU of the configured target (verified via a separate `ffmpeg ... -af loudnorm=I=...:print_format=json -f null -` measurement pass on the output file). Test against `2026-05-07_001_vo.wav` if available, otherwise generate a synthetic test signal.
4. Returns the measurement dict; raises `LoudnormError` (subclass of `RuntimeError`) on FFmpeg failure or unparseable JSON.
5. CLI exits 0 on success and prints the JSON measurement; exits non-zero with stderr error on failure.
6. Idempotent — calling on an already-normalized WAV must produce another normalized WAV within the same tolerance.
7. No mutable default arguments; type hints on all signatures; docstrings on every public function.
8. Uses `pathlib.Path`, `logging` (logger name `"audio_loudnorm"`), explicit exceptions. No bare `except:`.

## Unit tests (`tests/test_audio_loudnorm.py`)

- `test_parse_pass1_json_extracts_measurements` — feed a known FFmpeg loudnorm JSON blob to a `_parse_loudnorm_json()` helper, assert all 7 expected keys are present and float-typed.
- `test_normalize_synthetic_signal_lands_within_tolerance` — generate a 5-second sine via `ffmpeg -f lavfi -i "sine=frequency=440:duration=5"` to a temp WAV, run `normalize_vo()`, ffprobe the output, assert measured LUFS within ±0.5 of -14.0. Mark `@pytest.mark.skipif(not _ffmpeg_on_path())`.
- `test_missing_src_raises` — `normalize_vo(Path("nonexistent.wav"), ...)` raises `FileNotFoundError`.
- `test_invalid_json_raises_loudnormerror` — mock subprocess return that omits the JSON block; assert `LoudnormError`.
- (Optional) `test_cli_smoke` — invoke via subprocess against synthetic WAV, parse stdout JSON.

## Dependencies

- FFmpeg binary on PATH (existing pipeline assumption)
- `ffmpeg-python` already in `requirements.txt`
- stdlib `subprocess`, `json`, `logging`, `pathlib`, `argparse`
- pytest (already used for any future tests; if no pytest dep yet, use `unittest` from stdlib — preferred fallback to avoid touching requirements.txt)

## References — match this code style

- `pipeline._vo_edge_tts` (lines 1352-1392 of `pipeline.py`) — current loudnorm caller, idiomatic style for ffmpeg-python invocation
- `tools/youtube_upload.py` — current tools/ module style (argparse, logging, return-code conventions)
- `pipeline.py` module docstring format (line 1-13) — match this docstring style at top of new module

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class — purpose, args, returns, raises
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path. Use `pytest` if available, else stdlib `unittest` with no new deps
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones, DEBUG for diagnostics, ERROR for failures (logger name `"audio_loudnorm"`)
- [ ] Idempotent where applicable
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 success, non-zero failure, errors to stderr
- [ ] Module docstring at top with usage example
- [ ] Match existing pipeline.py code style (pathlib, logging, type hints, no print)
- [ ] No agent frameworks; no LLM API calls; no hardcoded paths
