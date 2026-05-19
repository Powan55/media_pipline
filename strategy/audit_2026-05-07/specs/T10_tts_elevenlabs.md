# T10 — `tools/tts_elevenlabs.py` (ElevenLabs Brian voice — code-only ship)

**Derives from:** RESOLUTIONS R2, 30_60_90_PLAN Day 4.3, Agent F §5.4
**Severity:** HIGH (R2 = HIGH item)
**Expected LoC:** ~120
**File:** `C:\ContentOps\_pipeline\tools\tts_elevenlabs.py`

---

## Goal

Ship a working ElevenLabs TTS adapter that the pipeline CAN call once the operator activates it — but do NOT activate it autonomously. Per `feedback_llm_api_policy.md`, each API-enabled feature requires per-feature operator approval before adding to `requirements.txt`. This sprint ships the code; the operator activates by:

1. Subscribing to ElevenLabs Starter $5/mo
2. Adding `ELEVENLABS_API_KEY=...` to `.env`
3. `pip install elevenlabs python-dotenv` (note: `python-dotenv` already pinned)
4. Adding `elevenlabs>=1.0,<2.0` to `requirements.txt`
5. Flipping `config.tts.provider: elevenlabs` in `config.yaml`

Until step 4-5 are done, this module is dormant. Therefore: **import `elevenlabs` lazily inside `synthesize()`** so the rest of the pipeline doesn't ImportError at startup when the dep isn't installed.

## Inputs / Outputs

```python
DEFAULT_VOICE_ID = "nPczCjzI2devNBz1zQrb"     # "Brian" — Daily Dose register, American baritone
DEFAULT_MODEL = "eleven_multilingual_v2"       # body-only; v3 is Creator-tier and not in scope


class ElevenLabsError(RuntimeError):
    """Raised when synthesis fails (auth, quota, network, decode)."""


def synthesize(
    text: str,
    out_path: Path,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL,
    api_key: str | None = None,                # falls back to os.environ["ELEVENLABS_API_KEY"]
    stability: float = 0.45,
    similarity_boost: float = 0.85,
    style: float = 0.30,
    speed: float = 1.05,
    output_format: str = "mp3_44100_128",      # ElevenLabs SDK output format string
    sample_rate_hz: int = 48000,               # WAV resample target — match pipeline contract
    max_retries: int = 3,
    backoff_base_s: float = 1.5,
) -> Path:
    """Synthesize a 48kHz WAV (matches pipeline.generate_voiceover contract).

    Workflow:
      1. POST /v1/text-to-speech via the elevenlabs SDK; receive MP3 bytes
      2. Write MP3 alongside as <out_path.stem>.mp3 (cache + debug)
      3. Convert MP3 → 48kHz mono WAV via ffmpeg subprocess
      4. Return WAV path

    Retry policy: exponential backoff on 429 / 5xx / network errors.
    Fail loud on auth / quota / unparseable response.

    Lazy-imports `elevenlabs` so callers without the dep installed don't crash at startup.
    """
```

CLI:

```
python tools/tts_elevenlabs.py --text "Hello world test" --out test_brian.wav
                                [--voice-id ...] [--model-id ...]
                                [--stability 0.45] [--similarity 0.85] [--style 0.30] [--speed 1.05]
                                [--no-wav]   # MP3-only, skip WAV decode (debug)
```

## Acceptance criteria (testable)

1. `synthesize()` does NOT import `elevenlabs` at module-load time. Importing `tts_elevenlabs` succeeds even on a system where `elevenlabs` is not installed. (Verify: `from tools import tts_elevenlabs` in a Python session that has not pip-installed `elevenlabs`.)
2. `synthesize()` raises `ElevenLabsError` (NOT raw `KeyError` or `ImportError`) when:
   - `elevenlabs` is not importable (informative message: "ElevenLabs SDK not installed; pip install elevenlabs to activate")
   - `api_key` is None and `ELEVENLABS_API_KEY` env var is missing
3. Retry policy: on `httpx.HTTPStatusError` 429/5xx OR network errors, retries up to `max_retries` with exponential backoff (`backoff_base_s * 2**attempt`). Final failure raises `ElevenLabsError` with the underlying exception chained.
4. Returns the WAV path (not MP3) by default. With `--no-wav` (CLI) or `output_format="mp3_only"` (kwarg, optional), returns the MP3 path.
5. WAV output is 48kHz, mono, PCM s16le — matches `pipeline._vo_edge_tts` contract.
6. CLI `--text` runs a smoke synthesis; exits 0 success, non-zero failure. **Test agent must NOT actually call the ElevenLabs API in CI** — instead, mock the SDK's `text_to_speech.convert` method.
7. Type hints throughout. No mutable defaults. Docstrings on `synthesize()` and `ElevenLabsError`.
8. Module docstring at top includes the operator-activation steps verbatim (5-step list above) so the reader knows it's dormant by default.

## Unit tests (`tests/test_tts_elevenlabs.py`)

- `test_module_imports_without_elevenlabs_installed` — patch `sys.modules['elevenlabs']` to raise ImportError when imported; assert `import tools.tts_elevenlabs` still succeeds (lazy import).
- `test_missing_api_key_raises_elevenlabserror` — call `synthesize(...)` with no `api_key` arg and unset env; assert `ElevenLabsError` with "API key" in message.
- `test_synthesize_calls_sdk_and_writes_wav` — mock the `elevenlabs` SDK at the function level (patch import inside synthesize); mock `text_to_speech.convert` to return fake MP3 bytes; mock the ffmpeg MP3→WAV step; assert returned path is a WAV.
- `test_retry_on_429` — mock SDK to raise a 429-like exception twice then succeed; assert ≥2 sleep calls and final success.
- `test_retry_exhausts_then_raises` — mock SDK to always raise; assert `ElevenLabsError` after `max_retries` attempts.
- `test_default_voice_is_brian` — assert `DEFAULT_VOICE_ID == "nPczCjzI2devNBz1zQrb"`.
- `test_default_model_is_v2_not_v3` — assert `DEFAULT_MODEL == "eleven_multilingual_v2"` (R2 says v3 is Creator-tier, out of scope).

## Dependencies

- `elevenlabs` SDK — **NOT added to requirements.txt by this sprint** per the activation flow; lazy-imported
- stdlib `pathlib`, `os`, `time`, `subprocess`, `argparse`, `logging`
- `httpx` already pinned (used for HTTPStatusError type hints if needed)
- FFmpeg on PATH for MP3→WAV decode

## References — match this code style

- `pipeline._vo_edge_tts` lines 1352-1392 — the existing TTS contract this module mirrors (48kHz mono WAV, MP3 alongside)
- `tools/youtube_upload.py` — module docstring style, argparse, logger init, exception class style
- Memory `feedback_llm_api_policy.md` — the policy that gates activation

## Wiring to pipeline.py (DOCUMENTATION ONLY — actual wiring lands in T8/Wave 3)

For Wave 3 reference, the eventual switch in `pipeline.generate_voiceover` looks like:

```python
provider = config["tts"]["provider"]
if provider == "edge-tts":
    return _vo_edge_tts(...)
if provider == "elevenlabs":
    from tools.tts_elevenlabs import synthesize
    return synthesize(text, audio_dir / f"{topic_id}_vo.wav", ...)
raise NotImplementedError(...)
```

T8 will add the dispatch branch but **leaves `tts.provider: edge-tts` as default** in `config.yaml.template`. Operator flips config when ready.

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class — purpose, args, returns, raises
- [ ] Unit tests covering: happy path (mocked), ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones (logger name `"tts_elevenlabs"`)
- [ ] Idempotent (synthesizing same text twice produces equivalent WAV)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths — out_path via arg, api_key via env-or-arg
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 success, non-zero failure
- [ ] Module docstring at top with the 5-step operator activation list
- [ ] `elevenlabs` lazy-imported inside `synthesize()`, NOT at module top
- [ ] **Do NOT modify `requirements.txt`** — operator activation step
- [ ] No agent frameworks
