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
| `generate_script` | manual LLM halt | 3 hook variants + body + FACT_CHECK_QUEUE + QUALITY_SCORES (6 dimensions). **Word target 80–95 words (aim ~88), recut 2026-06-11** from ~95–110 (measured edge-tts cadence ~0.33–0.40 s/word breached ≤38s). Second hook must REFRAME + rotate the bridge phrase; citation ladder (only a named human earns the Cited-Observation Lead tag) |
| `evaluate_script_quality` (Stage 1.5) | enforce mode | Pure-function gate; halts if weighted_total < `script_quality.min_score` (0.50). **Hard rules now WIRED (`8229865`):** anchor gate (PU-3, first 4 words carry a named anchor), modal-ban (PU-4, no could/might/imagine-if/what-if in sentence 1), word-count halt (PU-11, bounds `word_count_min`/`word_count_max` = **75/98** as of 2026-06-11). Failure = capped regenerate-with-feedback (max 2 attempts, then escalate to operator — never auto-kills). All flags default OFF in code, ON in prod config |
| `fact_check_script` | manual LLM halt | Claude verifies via tavily-mcp; markdown table response; column-name-aware parser |
| `await_fact_check_resolution` (gate 2) | manual default | Auto-resolve eligible behind `fact_check.auto_resolve_gate_2: true`; `script_FINAL.txt` is the operator-signed artifact. **Pre-render lint check #16 WIRED here (`8229865`):** `_scan_script_for_artifacts_or_halt` runs checks #14 (template-artifacts), #15 (sourcing-hygiene), **#16 (pre-render lint — placeholder tokens `[VERIFY/NEEDS/TODO`, style-guide banned CTAs)** on both the auto-resolve and manual paths; aggregates into one `PipelineQAFailed`. Also: PU-5a hook-tag warn annotates (never rejects) a vendor-only "Cited-Observation Lead" row in the hook-selection JSONL |
| `fetch_assets` | no | Pexels then Pixabay; B-roll cues should lead with dev-friendly stock keywords |
| `generate_voiceover` | no | edge-tts AndrewMultilingualNeural at `tts.rate: +10%` (measured ~0.33–0.40 s/word ≈ 150–180 wpm gross; the old "~151 wpm" was an undercount). **Stage 7.5** two-pass loudnorm follows (`_normalize_vo_loudness`), guarded by a bounded `os.replace` retry against Windows AV/indexer locks (`6ccfb01`). **Post-Stage-7.5 duration WARN** (warn-only, `script_quality.duration_warn_s: 38.0`): logs if the normalized VO overruns the ≤38s breakout target — never halts (so `/start -auto` can't deadlock) |
| `generate_captions` | no | faster-whisper large-v3, `vad_filter=False, condition_on_previous_text=False` (don't change without testing) |
| `render_master` | no | Tries h264_nvenc; falls back to libx264 (NVENC driver mismatch on this rig). Encodes to `<master>.mp4.part`, atomic-promotes on success; held under a `RenderLock` (heartbeat) so a detached/killed render can't silently orphan — see footguns + `tools/render_lock.py` / `tools/render_reaper.py`. **Re-render skip-guard (`47b3dd0`, `render.skip_if_approved_master: true`):** on a re-run, if the final master exists AND its gate-3 marker exists AND `media_integrity.check_integrity` passes, the encode is SKIPPED (pure early-exit ABOVE the RenderLock; saves ~1–2 min). Downstream Stage 10.1/11 still re-verify; delete the marker or master to force a re-render |
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
| **Word count is a poor proxy for spoken duration** | A script inside the word bounds still renders >38s (e.g. 114w→45.0s, 119w→40.5s on 2026-06-11) | **Measured edge-tts AndrewMultilingual +10% cadence ≈ 0.33–0.40 s/word (2026-06-11):** 114w→45.0s, 119w→40.5s, 125w→41.3s, 96w→35.7s, 88w→29.8s. Prompt target recut to **80–95 words (aim ~88)**; `word_count_max` tightened to **98** (≈39s hard ceiling). Backstop: the post-Stage-7.5 **duration WARN** (`script_quality.duration_warn_s: 38.0`) surfaces the real measured overrun (warn-only). Calibrate scripts to SECONDS, not word count |
| Metadata parser missed leading `- ` bullets | `cover_text` empty → topic_id fallback on thumbnail | **Fixed 2026-05-07 evening** (commit `1679d75`); regex tolerates `[-*]?\s*` prefix |
| Sub-agent backgrounds the render + yields → detached encode killed on return, video frozen mid-flight | Cycle-24 + recurred 2026-06-05 despite an emphatic prompt-level "never background" ban (prompt guard proved insufficient) | **Structurally fixed:** `render_master` encodes to `<master>.mp4.part` and `os.replace`s to the final name only after the smoke-check, so a killed render never leaves a file that looks finished. The render runs under a `RenderLock` (`tools/render_lock.py`): heartbeat + PID, and on entry a stale/dead lock is logged loudly, its `.part` cleaned, and stolen — so the **next foreground `pipeline.py` invocation auto-resumes** (no manual rescue). A genuinely-live lock is waited-on, not double-encoded (`RenderLockBusy`). Apex runs `python tools/render_reaper.py [--resume]` after sub-agents return to surface/replay any orphan. NVENC→libx264 fallback + idempotent-resume + `_isolate_config_for_topic` unchanged. |
| Master silently corrupted/truncated IN the cold archive AFTER gate-3 (no integrity check ever re-reads `04_renders/_final_master/`) | `2026-05-22_002_master.mp4` found truncated (5.25 MB vs ~28 MB, "moov atom not found") ~10 min after approval; sat undetected ~2 weeks. `tools/media_integrity` only ran as a render→export gate. | **Fixed 2026-06-07:** `tools/integrity_sweep.py` — schedulable PASS/FAIL sweep over every master (reuses `check_integrity`, exit 1 on any failure). Wire as a Windows Scheduled Task. `--auto-recover` rebuilds a corrupt master losslessly from its byte-identical `<id>_yt.mp4` stream-copy (remux → verify `.new` → back corrupt up to `.corrupt` → atomic `os.replace`, mirroring the render `.part`-promote). The YT variant lives in `05_exports/youtube/` and is mirrored in `06_published/<YYYY-MM>/<id>/youtube/`. |

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

# Per-topic (legacy, useful for re-runs). Run FOREGROUND — never background + yield;
# the render is RenderLock-guarded so a re-run auto-resumes an orphaned render.
python pipeline.py --topic-id YYYY-MM-DD_NNN --topic "..." --angle "..." --hook "..."

# Detect / recover orphaned renders (sub-agent-backgrounding footgun). Exit 1 if any
# render is frozen; --resume replays the orphan's captured argv foreground to finish it.
python tools/render_reaper.py                 # detect (exit 1 if orphans), human report
python tools/render_reaper.py --json          # machine-readable report for apex
python tools/render_reaper.py --resume        # detect + drive each orphan to completion

# Cold-archive integrity sweep (catch silent truncation/bit-rot in the master vault).
# Exit 1 if any file fails. READ-ONLY without --auto-recover. Default decodes the
# WHOLE file; --deep N for a faster N-second probe.
# NOW WIRED as a Windows Scheduled Task: "ShadowVerse Integrity Sweep" — daily 03:30,
# READ-ONLY (`--include-published`, no --auto-recover; exit 1 is the alarm), driven by
# the wrapper C:\ContentOps\integrity_sweep_task.cmd (logs to integrity_sweep_task.log).
# The wrapper lives OUTSIDE this repo on purpose (schtasks /TR quoting limits).
python tools/integrity_sweep.py                       # sweep masters, PASS/FAIL report
python tools/integrity_sweep.py --include-published   # + 06_published cold backups
python tools/integrity_sweep.py --auto-recover        # rebuild corrupt master from its <id>_yt.mp4 stream-copy (lossless remux, backup→.corrupt, atomic promote)
python tools/integrity_sweep.py --json                # machine-readable report

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
