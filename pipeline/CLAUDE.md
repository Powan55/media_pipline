# ShadowVerse Pipeline — Engineering CLAUDE.md

> Engineering-side rules for the pipeline repo. Strategy + workflow rules live at
> `C:\Users\laxmi\Documents\Project\CLAUDE.md`. Memory files at
> `C:\Users\laxmi\.claude\projects\C--Users-laxmi-Documents-Project\memory\` are auto-loaded.

---

## What this repo is

The production pipeline for the ShadowVerse YouTube Shorts channel. Plain Python orchestrator.
- Git remote: `Powan55/shadowverse-pipeline` (private)
- Python: 3.12 in `.venv\` (Windows path)
- Entry points: `daily_batch.py` (full flow), `pipeline.py` (per-topic), `analytics_pull.py`, `tools/youtube_upload.py`

The pipeline runs in **manual LLM mode** by default — every stage that needs an LLM writes a prompt to a file and halts; Claude Code in a chat session reads the prompt and writes the response back; the pipeline resumes. (Recently relaxed: API mode allowed per `feedback_llm_api_policy.md` when free or within budget AND demonstrably faster.)

---

## Engineering rules

1. **No agent frameworks.** Plain Python + explicit function calls. No LangChain, CrewAI, AutoGen, LangGraph.
2. **Fail loud, never silent.** Stubs raise `NotImplementedError` with the phase guidance. Don't swallow errors.
3. **`pathlib.Path`, not `os.path`.**
4. **`logging`, not `print`** for runtime info. Print only for CLI output meant for humans.
5. **Manual LLM mode is the default.** Stages write `<stage>_PROMPT.txt` and halt; resume on `<stage>_RESPONSE.txt`. Don't add API mode without explicit operator approval per feature.
6. **Atomic commits.** One logical change per commit. Detailed message with rationale + validation note. Never `--no-verify` or `--no-gpg-sign` unless operator explicitly asks.
7. **Git identity is per-repo.** `ShadowVerse <official.shadowverse@gmail.com>`. **Never** let `Laxmi.Poudel@carrier.com` (global) appear in this repo. Verify after each commit:
   ```bash
   git log -1 --format='%an <%ae>'
   ```
8. **Sacred gates stay sacred.** Never propose disabling gate 3. Gates 1 and 2 are delegated/auto-eligible per the operator's amendments — don't relitigate.
9. **`config.fact_check.require_human_resolution: true`** — never set to false.

---

## Pipeline stages (current)

| Stage | Halts? | Notes |
|---|---|---|
| `trend_pull` | no | GH releases (8 dev-AI repos) + HN top-60 word-boundary filter; Cursor changelog stub; Reddit/YT/Trends stubs |
| `idea_generation` | manual LLM halt | Claude writes JSON candidates; pipeline scores via `scoring.py`, picks top N, spawns topic dirs |
| `generate_script` | manual LLM halt | 3 hook variants + body + FACT_CHECK_QUEUE + QUALITY_SCORES (6 dimensions) |
| `evaluate_script_quality` (Stage 1.5) | enforce mode | Pure-function gate; halts if weighted_total < `script_quality.min_score` (0.50) |
| `fact_check_script` | manual LLM halt | Claude verifies via tavily-mcp; markdown table response; column-name-aware parser |
| `await_fact_check_resolution` (gate 2) | manual default | Auto-resolve eligible behind `fact_check.auto_resolve_gate_2: true`; `script_FINAL.txt` is the operator-signed artifact |
| `fetch_assets` | no | Pexels then Pixabay; B-roll cues should lead with dev-friendly stock keywords |
| `generate_voiceover` | no | edge-tts AndrewMultilingualNeural at `tts.rate: +10%` (~151 wpm) |
| `generate_captions` | no | faster-whisper large-v3, `vad_filter=False, condition_on_previous_text=False` (don't change without testing) |
| `render_master` | no | Tries h264_nvenc; falls back to libx264 (NVENC driver mismatch on this rig) |
| `await_final_qa` (gate 3) | operator-only | Marker file approval; **operator-only, non-negotiable** |
| `generate_variants` | no | YT (stream-copy) / TT (0.3s fade) / IG (0.5s fade) |
| `generate_metadata` | manual LLM halt | Claude writes title/description/tags/hashtags/cover; parser tolerates `- ` bullets |
| `generate_thumbnail` (Stage 8.5) | no | Pillow-rendered 1080×1920 PNG; 3 of 8 patterns implemented |
| `tools/youtube_upload.py` | requires operator approval | NOT auto-invoked. Operator says "upload `<topic_id>` privacy=X" → script runs |

---

## Common footguns

| Footgun | Symptom | Fix / Workaround |
|---|---|---|
| Bash cwd resets between calls | `python daily_batch.py` — "no such file" | Use absolute path or `cd /c/ContentOps/_pipeline && python ...` in single Bash call |
| Microsoft Store python.exe stub on PATH | `python` runs the stub | Use `py -3.12` or the venv interpreter directly |
| FFmpeg ASS filter on Windows colon paths | "fopen failed" `C\\:/...` | Already handled in `render_master` — subprocess + cwd + relative basename |
| YAML colon-space in TODO markers | `yaml.safe_load` fails on `<<TODO: text>>` | Move TODO markers to trailing `# <<TODO ...>>` comments |
| Whisper `condition_on_previous_text` skips speech | Caption gap mid-script (5+ sec) | Already fixed: `condition_on_previous_text=False, vad_filter=False` |
| LF→CRLF warnings on git add | core.autocrlf doing its job | Ignore |
| F5-TTS install would downgrade tokenizers | Breaks faster-whisper | Defer F5-TTS until operator schedules a focused install session |
| Trend keyword substring matching | "zed" matched inside "authoriZED" | Already fixed: word-boundary regex |
| Cursor changelog HTML scrape returns 0 | Page is JS-only | Pipeline-side warning only; fall back to GH releases |
| `_try_apply_fix` matcher fails on `[VERIFY]`-interrupted text | LIKELY_WRONG fix marked "no_match" | Auto-resolve mode strips `[VERIFY]` tags first |
| TT variant ~1/3 the size of YT/IG | Possibly bitrate quirk in 0.3s fade-in branch | Investigate only if it recurs across batches |
| NVENC driver mismatch | Required: 13.0 Found: 12.2 | Already handled via libx264 fallback; ~10s wasted per render attempt |
| Metadata parser missed leading `- ` bullets | `cover_text` empty → topic_id fallback on thumbnail | **Fixed 2026-05-07 evening** (commit `1679d75`); regex tolerates `[-*]?\s*` prefix |

---

## Critical commands

```bash
# Activate venv (Bash)
cd /c/ContentOps/_pipeline && source .venv/Scripts/activate

# Or PowerShell
cd C:\ContentOps\_pipeline
.\.venv\Scripts\Activate.ps1

# Daily batch (full flow, defaults: 10 candidates / 2 picks)
python daily_batch.py
python daily_batch.py --n-target 5 --n-picks 2
python daily_batch.py --refresh-trends      # force trend_pull re-run

# Per-topic (legacy, useful for re-runs)
python pipeline.py --topic-id YYYY-MM-DD_NNN --topic "..." --angle "..." --hook "..."

# Standalone trend pull
python trend_pull.py
python trend_pull.py --dry-run

# Score candidates offline
python scoring.py --candidates path/to/candidates.json --top 5

# Render channel art / test thumbnails
python tools/make_channel_art.py
python tools/make_thumbnail.py

# Approve a master at gate 3
New-Item C:\ContentOps\channels\ShadowVerse\04_renders\_final_master\<topic_id>_master_QA_APPROVED.marker

# YouTube upload (REQUIRES per-video operator approval)
python tools/youtube_upload.py --topic-id <id> --privacy {public,unlisted,private}
python tools/youtube_upload.py --topic-id <id> --privacy unlisted --dry-run
```

---

## Style-guide injection

The pipeline reads `C:\Users\laxmi\Documents\Project\Channels\ShadowVerse\style_guide.md` verbatim and substitutes it for `{NICHE_STYLE_GUIDE}` in script-gen, idea-gen, and metadata-gen prompts. Edits to that file propagate to the next prompt build automatically.

---

## When you're not sure

- Read `SESSION_HANDOFF.md` for current state
- Read `TODO.md` for active work queue and footguns log
- Read memory files for durable rules (auto-loaded each session)
- For substantial new features, consider running through `/gsd-plan-phase` and `/gsd-verify-work` instead of ad-hoc execution
- For research-heavy tasks, dispatch sub-agents (general-purpose, Explore) rather than burning main-context tokens
