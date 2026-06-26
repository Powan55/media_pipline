# ShadowVerse — Project CLAUDE.md

> Read this every session. It's the constitution. Anything in it overrides defaults.
> For point-in-time state (what's been shipped, what's open), see `SESSION_HANDOFF.md`.
> For the live work queue, see `TODO.md`.
> For the channel's voice / audience / rules, see `Channels\ShadowVerse\style_guide.md`.

---

## What this project is

**ShadowVerse** is a one-person faceless short-form AI/tech video channel. Goal: monetization-via-views. Audience: **general consumer curious about AI** (pivoted 2026-05-07 from "mid-career devs"). Format: 30-50 sec Shorts on YouTube → TikTok → Instagram, AI-vendor / AI-news / AI-product topics. **Visual format:** AI-VO over **stock + AI-generated B-roll** (no gameplay; 0% gameplay shipped historically per Agent A scan, and Agent G recommended against it — Daily Dot brain-rot fatigue + format mismatch with consumer-AI explainer per audit D1, 2026-05-08).

**Dual-track second slot (added 2026-06-21; ENABLED by default since 2026-06-23, operator-directed).** The two daily slots run different tracks: slot 1 (11:25 AM ET) stays AI-vendor; slot 2 (12:35 PM ET) runs the **general-tech track** — broad consumer tech (iPhone/Meta/Windows/Tesla/Neuralink/gadgets) + **crazy-tech-story** human narratives ("a person did X with tech"). Active via `tracks.dual_track_enabled: true` + `news_rss.general_tech_feeds_enabled: true` in `config.yaml`; the AI-vendor track is byte-identical when off, so **one-flip rollback** (`dual_track_enabled: false`) reverts `/start -auto` to two AI-vendor picks. Do NOT flip it back off without operator direction. Crazy-stories pass a stricter truth gate (named protagonist + retrievable URL or dropped; no overclaims). Rollout + measurement gate (now a live evaluation checkpoint, not a blocker): `dual_track_plan.md`.

The pipeline that makes the videos lives at `C:\ContentOps\_pipeline\` (its own git repo). Strategy + reference docs live here at `C:\Users\laxmi\Documents\Project\` (OneDrive-syncable). See `Channels\ShadowVerse\README.md` for affiliate slots and channel branding.

---

## How sessions work

**Session start protocol:** when the operator types `start`, read `SESSION_HANDOFF.md` first to load context. When operator types `end`, update `SESSION_HANDOFF.md` with what was done + next-session pointers (only on explicit ask — see § Session handoff protocol below).

**Manager/PM mode (set 2026-05-07 evening):** for mid-to-high level tasks, operate as a manager. Delegate to sub-agents (general-purpose, Explore, gsd-* skills) for heavy reading / research / parallel verification. Synthesize, brief the operator, ask for direction. Don't burn main-context tokens on parallelizable work.

**Per-video upload approval gate:** YouTube upload is automated via `tools/youtube_upload.py` (official Data API), but **NEVER** invoke without explicit per-video operator approval after gate 3. Always ask "want me to upload `<topic_id>`? (privacy: public/unlisted/private)" — don't pre-empt, don't bundle approval across videos.

---

## Files to read by topic

| You need... | Read |
|---|---|
| Latest session state, what's open | `SESSION_HANDOFF.md` |
| Live work queue, footguns log, analytics log, active strategic pivots | `TODO.md` |
| Voice / vocabulary / hooks / B-roll cadence / titles / thumbnails | `Channels\ShadowVerse\style_guide.md` |
| Channel branding (affiliate slots, persona) | `Channels\ShadowVerse\README.md` |
| Memory index (prior context, durable rules, references) | `C:\Users\laxmi\.claude\projects\C--Users-laxmi-Documents-Project\memory\MEMORY.md` |
| Pipeline engineering rules + commands | `C:\ContentOps\_pipeline\CLAUDE.md` |

**Auto-loaded memory files** (already in scope each session, no read needed):
- `user_profile.md`, `project_shadowverse.md`, `project_topic_focus.md`, `project_hardware.md`
- `feedback_engineering_principles.md`, `feedback_git_identity.md`, `feedback_llm_api_policy.md`
- `feedback_youtube_upload_policy.md`, `feedback_audience_general.md`, `feedback_no_overpromise_videos.md`
- `feedback_tavily_mcp.md`, `feedback_session_handoff.md`, `reference_strategy_docs.md`

---

## Durable rules (non-negotiable)

These come from operator's explicit prior decisions. Don't relitigate:

1. **Sacred gates** (per `01_Operating_Guide.md` §12, amended 2026-05-06):
   - **Gate 1 (idea selection)** — delegated to Claude via `daily_batch.py` + `scoring.py`. Show ranked picks for first batch under any new strategic shift.
   - **Gate 2 (fact-check resolution)** — manual default; auto-resolve gate behind `config.fact_check.auto_resolve_gate_2: false`. `script_FINAL.txt` existing means operator-signed.
   - **Gate 3 (final video QA)** — **operator-only**, non-negotiable for manual `daily_batch.py` runs. `<topic_id>_master_QA_APPROVED.marker` is the sign-off mechanism. **Auto-mode exception (2026-05-11):** `/start -auto` is allowed to drop the marker unattended because the operator's review window moves to YouTube Studio between scheduling and the auto-flip the next day. The exception applies to `/start -auto` ONLY; all upstream gates (Stage 1.5, Stage 10.1, Stage 11) must still pass — any failure halts and surfaces to operator before the marker is created.
   - **Master safety guard:** `config.fact_check.require_human_resolution: true` must remain true regardless of gate-2 mode.

2. **YouTube upload policy** (set 2026-05-07 evening, see `feedback_youtube_upload_policy.md`):
   - Automated via official Data API (`tools/youtube_upload.py`)
   - Cookie auth + third-party uploaders (yt-dlp, yt-upload-cli) still **banned**
   - **Never** upload without explicit per-video operator approval
   - Per-video flow: gate 3 marker → variants + metadata + thumbnail done → ask operator → upload on yes

3. **LLM API policy** (revised 2026-05-07 evening, see `feedback_llm_api_policy.md`):
   - Manual mode (Claude Code in chat) is the safe default
   - APIs allowed when (a) free tier OR within $25-50/mo budget AND (b) demonstrably faster than manual
   - Each API-enabled feature requires per-feature operator approval before being added to `requirements.txt`

4. **Git identity for content repos:** `ShadowVerse <official.shadowverse@gmail.com>` (set per-repo). **Never** let work email (`Laxmi.Poudel@carrier.com`, the global config) appear in any content commit. See `feedback_git_identity.md`.

5. **Audience pivot** (2026-05-07 evening, see `feedback_audience_general.md`):
   - Hard pivot to **general consumer curious about AI**, not developers
   - Three hard rules in scripts: laymen vocabulary, audio-first (works eyes-closed), fun storytelling shape (setup → twist → payoff)
   - Topic priority: consumer AI buzzwords (ChatGPT, Claude, Gemini, AI agents, AI news); drop pure dev-infra (uv, ruff, pip, etc.)

6. **Cited observation per video:** every video needs one specific named source (Reddit handle, X account, news byline, vendor blog author, HN comment) with a retrievable URL. No anonymous "a developer says". Day-of-release exception allows first-party vendor sources.

7. **No agent frameworks in pipeline code** — plain Python with explicit calls. No LangChain, CrewAI, AutoGen, LangGraph. (Memory: `feedback_engineering_principles.md`.)

8. **Search via tavily-mcp** — operator's preferred search MCP. Use over WebSearch/WebFetch where possible. (Memory: `feedback_tavily_mcp.md`.)

9. **Manager-only access to `SESSION_HANDOFF.md` + `TODO.md`** (set 2026-05-08, operator-approved; see `feedback_session_handoff.md`):
   - The manager (main session context) is the **single reader and writer** of these two files.
   - Sub-agents (Team A execution, Team B QA, Explore, general-purpose, gsd-* skills, etc.) must **NEVER** be dispatched to read, summarize, or modify them.
   - Manager reads at session start (chunk via offset/limit if needed — never delegate the read to dodge size limits) and writes at session end. Sub-agents receive only the targeted excerpts pasted into their prompts.
   - Rule is gated: relax only with explicit per-task operator approval. **Default-deny.**

10. **Self-improving learn loop runs by default** (set 2026-06-24, operator-directed):
    - The reach-first learning loop (`_pipeline/learning/`) is the project's **standing default**, not an opt-in. `/start -auto` runs it at step 2.5 on every run (rebuilds the ledger, runs the reach-first + right-tail analysis, feeds the top reach levers into idea-gen) — **no flag needed**.
    - **`learning.apply_enabled: true` is the default** (`config.yaml` + template): the loop **auto-applies at most one bounded, reversible, non-sacred config knob per run** (word-count / `<38s` duration only), opened as a tracked experiment that **auto-reverts on regression**. It NEVER touches sacred gates or scoring-component weights (those stay PROPOSE-only via the weekly review).
    - One-flip rollback: set `learning.apply_enabled: false` for report/propose-only (the loop still runs + shapes idea-gen; it just stops self-mutating config). See `project_self_improving_loop.md` + `project_analytics_deepdive_2026-06.md`.

---

## Folder layout

```
C:\Users\laxmi\Documents\Project\         ← strategy + reference, OneDrive-syncable
├── CLAUDE.md                              ← this file
├── 00_README.md, 01_Operating_Guide.md, 02_Compliance_Checklist.md, ...
├── SESSION_HANDOFF.md                     ← point-in-time state
├── TODO.md                                ← live work queue
├── .research/                             ← sub-agent research reports land here
└── Channels\ShadowVerse\
    ├── README.md                          ← affiliate slots, persona
    └── style_guide.md                     ← voice / audience / hooks / cadence

C:\ContentOps\                             ← production scratch, NOT OneDrive-synced
├── _pipeline\                             ← git repo (Powan55/shadowverse-pipeline private)
├── _shared\, _models\                     ← whisper / TTS model weights
└── channels\ShadowVerse\
    ├── 00_brand\, 01_research\            ← profile + banner art, trends/audits/analytics
    ├── 02_scripts\_drafts\<topic_id>\     ← per-topic prompt/response files + script_FINAL
    ├── 03_assets\stock\, audio_vo\        ← downloaded clips + TTS output
    ├── 04_renders\_wip\, _final_master\, _thumbnails\
    └── 05_exports\{youtube, tiktok, instagram}\
```

---

## Critical commands

```powershell
cd C:\ContentOps\_pipeline
.\.venv\Scripts\Activate.ps1

# Daily batch (idea-gen → script → fact-check → render → gate 3 → variants → metadata → thumbnail)
python daily_batch.py                              # 10 candidates, top 2 picks (default)
python daily_batch.py --n-target 5 --n-picks 2     # tighter pool

# Analytics
python analytics_pull.py --all                     # all-time on every published video
python analytics_pull.py --days 7                  # last 7 days

# Compute publish slots for /start -auto (used internally by start.md)
python tools/compute_publish_slots.py                       # next free day, both 11:25 AM + 12:35 PM ET slots
python tools/compute_publish_slots.py --date 5/25/26        # explicit date, halts on collision
python tools/compute_publish_slots.py --date 2026-05-25     # ISO form also accepted

# YouTube upload (REQUIRES explicit per-video operator approval)
python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy public
python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy private --dry-run

# TikTok upload (official Content Posting API; added 2026-06-24). Config-gated in /start -auto
# via tiktok.upload_enabled (default OFF). Manual single-video path = /sv-upload-tiktok.
# AUDIT WALL: an unaudited app posts SELF_ONLY only; mode:inbox lands in @shadowversetec drafts.
python tools/tiktok_oauth_init.py                                  # one-time consent (then --force --with-publish post-audit)
python tools/tiktok_upload.py --topic-id 2026-06-24_002 --dry-run  # verify inputs/caption, no API call
python tools/tiktok_upload.py --topic-id 2026-06-24_002            # uses config defaults (mode/privacy)

# Approve a master at gate 3 (operator only)
New-Item C:\ContentOps\channels\ShadowVerse\04_renders\_final_master\<topic_id>_master_QA_APPROVED.marker
```

---

## Session handoff protocol

Per `feedback_session_handoff.md`:
- `start` — read `SESSION_HANDOFF.md` first, then resume
- `end` — update `SESSION_HANDOFF.md` with what was done + next-session priorities (only on explicit operator ask, never silently)

The handoff is a snapshot, not live state. The "Last updated" line at the bottom is the timestamp.

---

## Workflows (slash commands)

- **`/start`** — orientation only. Reads SESSION_HANDOFF + TODO, reports state, waits. Same as typing `start`.
- **`/start -auto`** — fully autonomous daily run (added 2026-05-11; **dual-video shape since 2026-05-20**). Ships **TWO** videos per invocation, scheduled at **11:25 AM ET (slot 1)** and **12:35 PM ET (slot 2)** (moved to the midday audience-online peak 2026-06-24; were 5:25/6:35 PM). Apex runs shared steps once (analytics → `tools/compute_publish_slots.py` for next-free-day slot resolution → idea-gen pool with `--n-picks 2`), then dispatches two sub-agents in PARALLEL — each runs script-gen → fact-check → render → gate-3 marker → upload end-to-end for its own topic_id. Apex aggregates results and is the sole writer of TODO.md + SESSION_HANDOFF.md. Failure isolation: if one sub-agent halts, the other ships; whole-run halts only on shared-stage failures. Topic-to-slot: rank-1 → 11:25 AM, rank-2 → 12:35 PM. **Date override:** `/start -auto 5/25/26` (M/D/YY) or `/start -auto 2026-05-25` (ISO) schedules both videos for that explicit date; halts if either slot is occupied. See `~/.claude/commands/start.md` for the full step-by-step. Halt conditions are mandatory: analytics fail or slot-helper non-zero exit (whole-run); Stage 1.5 / 10.1 / 11 / `youtube_upload.py` non-zero (per-video). Manual `daily_batch.py` invocations are unaffected; the gate-3 halt still fires for those. See [`feedback_dual_video_pipeline.md`](C:\Users\laxmi\.claude\projects\C--Users-laxmi-Documents-Project\memory\feedback_dual_video_pipeline.md).

---

## When to ask vs proceed

- **Ask first**: anything that touches sacred gates, identity, branding, durable rules, or risks-money. Anything irreversible. Anything beyond what the user explicitly asked for. Strategic pivots.
- **Proceed without asking**: tactical execution within an explicitly-approved scope, sub-agent dispatch for delegated research, atomic commits with clean messages, file edits within an in-flight task.
- **Always confirm before**: pushing to remote, force-pushing, deleting files, destructive git ops, uploading to YouTube, sending notifications outside the operator.

## Capability awareness (auto-run)

A SessionStart hook in `.claude/settings.local.json` refreshes the
capability-mapper inventory at the start of every new Claude Code session
in this project. The check is cheap (~50ms when nothing has changed in
`~/.claude/skills/`). To force a refresh:

    bash ~/.claude/skills/capability-mapper/discover.sh

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- ALWAYS read graphify-out/GRAPH_REPORT.md before reading any source files, running grep/glob searches, or answering codebase questions. The graph is your primary map of the codebase.
- IF graphify-out/wiki/index.md EXISTS, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
