# T3 — `tools/caption_word_pop.py` (faster-whisper word-pop ASS captions)

**Derives from:** RESOLUTIONS R5, 30_60_90_PLAN Days 2.3 + 3.1, WORKFLOW_AUDIT Phase 5 T1.2 (highest single retention lever), Agent I §2 + §1.8
**Severity:** HIGH (highest single retention lever per audit; +12-18% AVD vs static block)
**Expected LoC:** ~150-250
**File:** `C:\ContentOps\_pipeline\tools\caption_word_pop.py`

---

## Goal

Replace the current static-block `.ass` captions (3-word groups, 2-line static positioning) with word-by-word karaoke captions: each word pops with a scale-bounce animation as it's spoken. Active word highlights yellow; rest are white. This is the largest measured 2026 retention lever the channel hasn't pulled.

Visual spec (from Agent I §1.8):
- Font: **Montserrat Black** with fallback chain `Anton, Bebas Neue, Impact, Arial Black`
- Inactive word: white (`#FFFFFF`), 84 px, 6 px black outline
- Active word: yellow (`#FFE600`), pops to 110 px (~125% scale) over 80 ms, settles to 96 px over 60 ms
- Position: `MarginV` aligned so caption baseline lands at Y=1380 in 1080×1920 frame (clears YT Shorts UI top + TT/IG safe zones)
- Animation: libass `\rPop\t(0,80,\fscx125\fscy125)\t(80,140,\fscx100\fscy100)` overlay tags per Agent I §1.7

## Inputs / Outputs

```python
def transcribe_words(
    audio_path: Path,
    *,
    model_name: str = "large-v3",
    compute_type: str = "float16",
    language: str = "en",
) -> list[Word]:
    """Run faster-whisper with word_timestamps=True.

    Returns flat list of Word(start, end, text) tuples, sorted by start time.
    """

def pack_into_lines(
    words: list[Word],
    *,
    max_words_per_line: int = 4,
    max_line_duration_s: float = 2.5,
    max_chars_per_line: int = 22,
) -> list[Line]:
    """Group word timestamps into screen-fitting lines.

    A new line starts when (a) max_words reached, (b) max_chars reached,
    (c) max_duration reached, or (d) the inter-word gap > 0.6s.
    """

def render_ass(
    lines: list[Line],
    out_path: Path,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    font_primary: str = "Montserrat Black",
    font_fallbacks: tuple[str, ...] = ("Anton", "Bebas Neue", "Impact", "Arial Black"),
    font_size_inactive: int = 84,
    font_size_active_peak: int = 110,
    font_size_active_settle: int = 96,
    color_inactive: str = "&H00FFFFFF",   # ASS BGR + alpha format
    color_active: str = "&H0000E6FF",     # yellow #FFE600 in BGR
    outline_px: int = 6,
    margin_v: int = 540,                  # places baseline at Y=1380
    pop_attack_ms: int = 80,
    pop_settle_ms: int = 60,
    enable_bounce: bool = True,           # when False, settle-only (for A/B "without bounce")
) -> Path:
    """Emit a libass-renderable .ass file with word-pop animation.

    Each visible word pops with scale-bounce on its (start, end) interval;
    surrounding words on the same line are visible-but-inactive.
    """
```

`Word` and `Line` are simple dataclasses or NamedTuples (your call — match pipeline.py's pydantic-BaseModel style if reasonable, or use `@dataclass` if pydantic feels heavy here).

CLI:

```
python tools/caption_word_pop.py --audio PATH --out PATH.ass
                                  [--model large-v3] [--language en]
                                  [--no-bounce]   # disable pop animation (A/B comparison)
```

## Acceptance criteria (testable)

1. `transcribe_words()` returns a flat sorted list of `(start, end, text)` triples; calling on a 30-second TTS WAV (`2026-05-07_001_vo.wav` if available, else generate) returns >50 word-level timestamps.
2. `pack_into_lines()` enforces all four packing rules; never produces a Line whose total duration exceeds `max_line_duration_s` or whose char count exceeds `max_chars_per_line`.
3. `render_ass()` produces a valid ASS file that:
   - Has `[Script Info]`, `[V4+ Styles]`, and `[Events]` sections
   - Declares the font with the fallback chain joined by commas in the Style line
   - Each `Dialogue:` event covers one (line, active-word-index) pairing — for each word in a line, one dialogue event marking that word as active and the rest as inactive
   - Animation tags use libass `\t(time1,time2,\fscx...\fscy...)` per Agent I §1.7
   - PlayResX = video_width, PlayResY = video_height
4. `--no-bounce` mode emits ASS without the `\t(...)` scale-tween (settle size only) — used for the Day 23-24 A/B test cycle.
5. Generated ASS file plays correctly in mpv (libass renderer): visible word-pop, yellow active word, MarginV=540 places caption baseline near Y=1380. **This is verified manually by the QA agent if time permits; otherwise verified by parsing the .ass and checking the structural/content rules above.**
6. Module is importable as `from tools.caption_word_pop import transcribe_words, pack_into_lines, render_ass` so Stage 09 wiring in Wave 3 can call them.
7. No mutable default arguments. Tuples, not lists, for default font_fallbacks. Type hints throughout.
8. Whisper init uses `condition_on_previous_text=False, vad_filter=False` per the existing `pipeline.generate_captions` (don't revert that fix — see footgun in `_pipeline/CLAUDE.md`).

## Unit tests (`tests/test_caption_word_pop.py`)

- `test_pack_respects_max_words` — feed 20 fake words at 0.3s each, max_words=4 → expect ≥5 lines, no line >4 words.
- `test_pack_breaks_on_long_gap` — 4 words at 0,0.5,1.0 then a 1s-gap then 4 more words at 2.5,3.0 → expect a line break at the gap regardless of max_words.
- `test_render_ass_structure` — call `render_ass()` with a 6-word two-line corpus; parse the output; assert `[Script Info]`, `[V4+ Styles]`, `[Events]` sections all present and non-empty.
- `test_render_ass_no_bounce_omits_scale_tween` — call with `enable_bounce=False`; assert no `\fscx125` or `\fscy125` substrings in output.
- `test_render_ass_bounce_includes_pop_attack` — call with `enable_bounce=True`; assert `\fscx125\fscy125` substring present.
- `test_transcribe_words_smoke` (skip if no faster-whisper or no model available) — feed `2026-05-07_001_vo.wav` if present, else `ffmpeg`-generated synthetic speech-shaped audio; assert `>0` words returned with strictly-increasing start times.
- `test_render_ass_active_color_yellow` — assert the active-word color hex appears in output Style/Dialogue.

If `faster_whisper` isn't importable, mark the transcription tests skip and keep the structural tests pure-Python.

## Dependencies

- `faster-whisper` already in `requirements.txt`
- stdlib for the rest
- Fonts on the operator's machine — check `C:\Windows\Fonts\Montserrat-Black.ttf` and `C:\Windows\Fonts\Anton-Regular.ttf`. If missing, log a WARNING but emit ASS anyway (libass will fall through the fallback chain). Day 3.2 of the 30/60/90 plan is the operator-task font install.

## References — match this code style

- `pipeline.generate_captions` (lines 1490-1554) — current ASS emitter, format we're replacing. Match its variable-naming and helper-function style.
- `pipeline._chunk_words_for_captions` (lines 1441-1488) — chunking helper; `pack_into_lines` is the replacement, similar shape.
- Agent I §1.7-§1.8 in `audit_2026-05-07/phase2_finishing.md` — the visual spec source of truth (font sizes, colors, MarginV, animation timings).

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones (logger name `"caption_word_pop"`)
- [ ] Idempotent
- [ ] No mutable default arguments — use tuples for default sequences
- [ ] No hardcoded absolute paths
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 success, non-zero failure
- [ ] Module docstring at top with usage example
- [ ] No agent frameworks; no LLM API calls
