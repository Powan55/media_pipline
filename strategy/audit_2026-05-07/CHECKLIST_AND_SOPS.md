# Pre-Publish Checklist & SOPs — ShadowVerse Operations Manual (2026-05-08)

> Agent M, Wave 4 synthesis. Operator-facing runbook that consolidates Agent H (workflow), Agent I (`prepublish_qa.py`), Agent F (TTS), Agent G (visuals), Agent D (distribution), and the canonical `RESOLUTIONS.md` decisions into one printable manual. Builds on `02_Compliance_Checklist.md` and `07_Weekly_Operating_Cadence.md` — does NOT duplicate them; cross-references where relevant.
>
> **Where this file disagrees with any phase report, RESOLUTIONS.md and this file win.** Specifically: this file uses **uniform -14 LUFS / -1.0 dBTP** across all platforms (R1), **stereo `-ac 2`** mandatory (R7), **0% gameplay** (R3), and **narrow AI-disclosure** (R6). Earlier phase reports that show -11 LUFS for TT/IG are superseded.
>
> Sacred gates G1 / G2 / G3 + G4 per-video upload-approval are immutable per CLAUDE.md durable rule #1 and #2. Nothing in this document automates or compresses them.

---

## Table of contents

1. [Pre-Publish Checklist (Gate 3, Operator-Only)](#1-pre-publish-checklist-gate-3-operator-only)
2. [Per-Video SOP (idea → published)](#2-per-video-sop-idea--published)
3. [Weekly Batch SOP (5 Shorts in 3 hours, Sunday 09:00–12:00)](#3-weekly-batch-sop-5-shorts-in-3-hours-sunday-090012-00)
4. [Monthly Review SOP (~90 min, 1st Sunday)](#4-monthly-review-sop-90-min-1st-sunday)
5. [Daily Routine Anchor (printable single-page)](#5-daily-routine-anchor-printable-single-page)
6. [Failure-Recovery Playbooks (top 5)](#6-failure-recovery-playbooks-top-5)

---

## 1. Pre-Publish Checklist (Gate 3, Operator-Only)

**Purpose.** Final operator-only QA before the `_QA_APPROVED.marker` drops. Total budget: **<5 minutes per video.** 22 items grouped by category; all auto-checks live in `tools/prepublish_qa.py` (Agent I §6) which exits 0/1 + JSON. Manual checks happen while the master plays end-to-end on a phone-sized screen.

**The gate is sacred.** Per CLAUDE.md durable rule #1 and `01_Operating_Guide.md` §12, only the operator clears Gate 3. Never auto-clear. The marker file is the de facto sign-off. Builds on `02_Compliance_Checklist.md` § Final human review — extends, does not replace.

**Run command:**

```powershell
# PowerShell (operator default)
python tools\prepublish_qa.py `
  --video C:\ContentOps\channels\ShadowVerse\04_renders\_final_master\<topic_id>_master.mp4 `
  --platform yt `
  --caption C:\ContentOps\channels\ShadowVerse\04_renders\_wip\<topic_id>\<topic_id>_captions.ass `
  --script-words <count_from_script_FINAL>
```

```bash
# bash / Git Bash / WSL equivalent
python tools/prepublish_qa.py \
  --video "C:/ContentOps/channels/ShadowVerse/04_renders/_final_master/<topic_id>_master.mp4" \
  --platform yt \
  --caption "C:/ContentOps/channels/ShadowVerse/04_renders/_wip/<topic_id>/<topic_id>_captions.ass" \
  --script-words <count_from_script_FINAL>
```

### 1.1 Checklist table (22 items, <5 min total)

| # | Item | Auto-check | Manual check | Fail action |
|---|---|---|---|---|
| **HOOK (3 items, ~30s manual)** ||||
| 1 | First verbal claim audible by 0:00.8 | `astats=metadata=1:reset=1` over [0.0–0.8s] confirms RMS > -45 dBFS | Operator hears the punch word in the first half-second on phone speaker | Re-render with hook trimmed; do not ship a hook with >0.3s leading silence |
| 2 | On-screen text overlay visible by 0:00.8 | First Dialogue line in `.ass` has Start ≤ 00:00:00.80 | Caption visible in first frame on phone screen | Re-emit `.ass` with caption_word_pop.py forcing first-word start at 0.0s |
| 3 | Visual change occurs within first 0.8s | First B-roll cut timestamp from manifest ≤ 0.8s | Frame-1 vs frame-24 luma differs noticeably (no static intro) | Re-cut variant: trim leading hold; re-render |
| **AUDIO INTEGRITY (4 items, ~20s, fully auto)** ||||
| 4 | Integrated loudness -14 LUFS ±0.5 (uniform per R1) | `loudnorm=print_format=json` measures within ±0.5 LU of -14 | n/a | Re-run `audio_loudnorm.normalize_for_platform()` with corrected target |
| 5 | True peak ≤ -1.0 dBTP | Same loudnorm pass measures `input_tp` ≤ -1.0 | n/a | Re-render with TP=-1.0 ceiling; check for inter-sample peaks from re-encode |
| 6 | No silence >0.3s in first 1.5s of audio | `astats` over [0.0–1.5s] confirms no contiguous block >300ms below -50 dBFS | n/a | Trim VO leading silence with FFmpeg `silenceremove` filter |
| 7 | Stereo `-ac 2` (R7 mandatory) | `ffprobe` audio stream `channels == 2` | n/a | Re-mux with `-ac 2` (dual-mono upmix from mono VO is fine) |
| **CAPTION LEGIBILITY (3 items, ~30s manual)** ||||
| 8 | Caption Y position ≤ 1380 (UI safe zone) | `.ass` `MarginV` from bottom is ≥ 540 OR Alignment 2 with PlayResY-540 anchor | Eyeball: caption sits above TikTok like/comment chrome on phone-sized preview | Edit `.ass` style block; re-burn |
| 9 | Caption font size ≥ 84 px, contrast ratio ≥ 4.5:1 | `.ass` `Fontsize` ≥ 84; outline ≥ 4px black on white | Mute the audio, can you read every word at arm's length? | Regenerate `.ass` with Default style 84/Pop style 96 |
| 10 | Caption density ≥ 80% of VO words | `prepublish_qa` checks dialogue lines ≥ 0.8 × `--script-words` | Sample-check 3 random sentences: every word highlighted | Re-run `caption_word_pop.py` with verbose word_timestamps |
| **EXPORT SPECS (4 items, fully auto)** ||||
| 11 | Resolution exactly 1080×1920 | `ffprobe` video `width×height == 1080×1920` | n/a | Re-render at correct resolution |
| 12 | H.264 high profile, 30fps CFR | `ffprobe` codec=h264, profile=high, r_frame_rate ∈ {30/1, 30000/1001} | n/a | Re-encode with `-c:v libx264 -profile:v high -r 30` |
| 13 | CRF per platform: 19 YT / 21 TT / 20 IG | Bitrate proxy: file-size ratio inside expected band per platform | n/a | Re-encode variant with platform-correct CRF (per Agent I §4) |
| 14 | Container .mp4 with `+faststart`; duration 30–50s | `ffprobe format` shows mp4, duration ∈ [30, 50] | n/a | Re-mux `-movflags +faststart`; if duration off, script length needs trim |
| **METADATA (3 items, ~30s manual)** ||||
| 15 | Title ≤ 55 chars; main keyword in first 30 chars; zero hashtags in title (style_guide § Title hygiene) | `metadata.json` field-length check; regex for `#` in title fails | Read title aloud — does a non-coder grasp the topic? | Edit title in `metadata.json`; never paste hashtags into title |
| 16 | Description has 10–12 hashtags total; slot 1=`#Shorts`, slot 2=`#AI`, slot 3=topic-specific | Hashtag count in description in [10, 12]; first three match canonical pattern | Spot-check: no `#viral` `#fyp` `#trending` `#explorepage` (style_guide forbidden) | Edit `metadata.json` description to canonical stack |
| 17 | Cited observation present in script with named source + URL/handle (durable rule #6) | grep `script_FINAL.txt` for at least one of: u/, @, [vendor]'s blog, HN comment id | Read the cited line aloud; can you point to the URL on demand? | Send back to Stage 02 (script gen) — no shipping without a named source |
| **COMPLIANCE / MONETIZATION (3 items, ~45s manual)** ||||
| 18 | AI-disclosure toggle decision per R6 (narrow): OFF unless video contains AI of real people, altered real-world events, or photoreal synthetic scene | `metadata.json` `ai_disclosure` field set; no real-person likeness in B-roll manifest | Watch the video and ask: "Could a viewer mistake any frame for real footage of a real person?" If yes → toggle ON | Set `metadata.ai_disclosure=true`; verify YT Studio toggle on upload form |
| 19 | Made-For-Kids audit per video (Disney $10M lesson, Agent D §8) | `metadata.json` `mfk_classification == "not_for_kids"` set explicitly | Watch with Disney-FTC eyes: cartoony mascots, primary colors, simple voice, simple language? If any 2 → reconsider | Toggle MFK=Yes if classifier-leaning; or rework visual palette toward adult-coded |
| 20 | Affiliate disclosure present if any affiliate link in description (`02_Compliance_Checklist.md` carryover) | grep description for affiliate URL pattern; if present, "Some links are affiliate" string present | Manual confirmation if any link present | Add disclosure line; don't ship affiliate without it |
| **INTEGRITY (2 items, ~10s, fully auto)** ||||
| 21 | `ffprobe` parses cleanly — moov atom present, V+A streams found, duration > 0 | `media_integrity.check_fast()` returns 0 | n/a | Re-render the variant; if 2nd fail, drop topic (see § 6.1 playbook) |
| 22 | Video plays end-to-end without artifact (final operator watch) | `media_integrity.check_deep()` (full decode pass, opt-in) returns 0 | Operator watches master end-to-end on phone-sized window with sound on; flag any sync drift, frame drop, audio glitch | Re-render; or open the topic dir and re-run from caption stage |

**Gate 3 sign-off.** Once all 22 pass and operator has watched the video end-to-end on a phone-sized screen:

```powershell
New-Item C:\ContentOps\channels\ShadowVerse\04_renders\_final_master\<topic_id>_master_QA_APPROVED.marker
```

```bash
# bash equivalent
touch "C:/ContentOps/channels/ShadowVerse/04_renders/_final_master/<topic_id>_master_QA_APPROVED.marker"
```

**Estimated time per video at steady state: 4–5 minutes.** Items 4–7, 11–14, 21 are fully auto and run in <10s combined. The other 13 require operator eyes/ears on a phone preview. Don't shortcut the watch.

### 1.2 Single most-likely-to-be-skipped item operators must NOT skip

**Item 17 — cited observation per video with named source + retrievable URL/handle.** This is durable rule #6 in CLAUDE.md and the single strongest defense against YouTube's inauthentic-content policy (Agent D §7) — the policy that terminated Screen Culture, KH Studio, the Bible-stories channel ($30K/mo), and ~35M subs across late-2025/early-2026. Auto-checks can flag a missing `u/` or `@` but cannot tell if the URL still resolves. Operator must click through once. **Skipping this item to ship faster is the single highest-EV mistake the channel can make.**

---

## 2. Per-Video SOP (idea → published)

**Target: ≤45 min hands-on per video at steady state.** First-time-through is closer to 90 min. Every step lists operator time vs background time, the relevant `RESOLUTIONS.md` ID where applicable, the failure mode, and the skip condition. Builds on Agent H §3.1 — extends with the breakdown columns and recovery notes.

**Convention.** "Operator time" = hands-on attention required. "Background time" = pipeline/API runs while operator does something else. "Wall clock" = how long the step blocks the next sequential step.

### Stage 1 — Trend pull (cron, unattended)
- **Owner:** Pipeline / Windows Task Scheduler 06:30
- **Operator time: 0 min. Background: ~2 min. Wall clock: 0 min (operator sees output).**
- **Input:** HN top-60, GH releases, Anthropic/OpenAI/Google AI feeds, tavily-mcp consumer-AI Reddit pulls
- **Output:** `01_research\trends_<YYYY-MM-DD>.json`
- **Tools:** `trend_pull.py`
- **Failure mode:** API rate limit, network timeout. Pipeline raises and skips quietly.
- **Recovery:** Next morning's run picks up; or manual `python trend_pull.py` from the venv.
- **Skip if:** Never. Cron handles this; operator never reads it.

### Stage 2 — Idea-gen halt → 10 candidates
- **Owner:** Operator + Claude Code chat
- **Operator time: 3 min. Background: 0 (manual LLM). Wall clock: 3 min.**
- **Input:** PROMPT generated by `idea_generation.py` from trends + style_guide
- **Output:** 10 ranked candidates (JSON pasted back into pipeline)
- **Tools:** Claude Code chat (manual default per LLM API policy)
- **RESOLUTIONS:** Per durable rule §LLM API policy — manual mode is the safe default
- **Failure mode:** Claude returns malformed JSON. Pipeline halts at JSON parse.
- **Recovery:** Re-run prompt; or hand-edit JSON.
- **Skip if:** Sunday batch covers 5 picks at once — see § 3 weekly batch.

### Stage 3 — Scoring → top-N picks (Gate 1)
- **Owner:** Pipeline (sacred gate, delegated to scoring.py)
- **Operator time: 0 min. Background: <1 min. Wall clock: <1 min.**
- **Input:** Candidate JSON
- **Output:** Top-N ranked picks; topic IDs allocated
- **Tools:** `scoring.py` with weighted 8-component rubric
- **RESOLUTIONS:** Sacred gate G1, immutable. Show ranked picks for first batch under any new strategic shift.
- **Failure mode:** All candidates fail score floor. Rare. Pipeline raises `NoPicksAboveFloor`.
- **Recovery:** Re-run idea-gen with broader prompt; or accept lower floor for one batch.
- **Skip if:** Never. This IS Gate 1.

### Stage 4 — Per-topic dispatch (×N) starts
- **Owner:** Pipeline
- **Operator time: 0 min. Background: instant. Wall clock: 0.**
- **Input:** Picks list
- **Output:** N topic dirs spawned under `02_scripts\_drafts\<topic_id>\`
- **Tools:** `pipeline.run_for_topic`
- **Failure mode:** Topic ID collision. Fixed in commit `542fc9e`; covered by test #1 in Agent H §6.7.
- **Recovery:** N/A (regression-tested).
- **Skip if:** Never.

### Stage 5 — Script LLM halt → script_RESPONSE
- **Owner:** Operator + Claude Code chat
- **Operator time: 8 min. Background: 0. Wall clock: 8 min per topic.**
- **Input:** PROMPT built from style_guide + cited-source rule
- **Output:** `script_RESPONSE.txt` in topic dir
- **Tools:** Claude Code chat
- **RESOLUTIONS:** Manual default per LLM API policy.
- **Failure mode:** Drift from style_guide voice (em-dashes, "did you know" openers, dev jargon). Stage 1.5 catches.
- **Recovery:** Edit response in place before Stage 6; or regenerate.
- **Skip if:** Never. This is the highest-leverage manual step.

### Stage 6 — Stage 1.5 script-quality review
- **Owner:** Pipeline
- **Operator time: 0 min unless any rubric item <7. Background: ~10s. Wall clock: <30s.**
- **Input:** `script_RESPONSE.txt`
- **Output:** strict-JSON 5-dim rubric scores
- **Tools:** `gsd-script-quality-review` skill
- **Failure mode:** Any dim <7 surfaces as warning. Operator skims.
- **Recovery:** Edit script; re-run review.
- **Skip if:** Never — gate is cheap.

### Stage 7 — script_FINAL.txt writeback (de facto G2.5)
- **Owner:** Operator
- **Operator time: 3 min. Background: 0. Wall clock: 3 min.**
- **Input:** RESPONSE + Stage 1.5 rubric
- **Output:** `script_FINAL.txt` (operator-signed)
- **Tools:** Editor of choice
- **Failure mode:** Operator forgets to write FINAL. Pipeline halts at Stage 8.
- **Recovery:** Write the file; re-run.
- **Skip if:** Never. The file existing is the operator sign-off mechanism.

### Stage 8 — Fact-check claim extraction
- **Owner:** Pipeline
- **Operator time: 0. Background: 1–2 min. Wall clock: 1–2 min.**
- **Input:** `script_FINAL.txt`
- **Output:** Claims list with tavily-mcp evidence URLs
- **Tools:** `fact_check.py` + tavily-mcp
- **RESOLUTIONS:** Search via tavily-mcp (operator preferred MCP).
- **Failure mode:** tavily quota exhausted; network failure. Pipeline halts loudly.
- **Recovery:** Wait for quota window; or fall back to WebFetch (one topic only, document in handoff).
- **Skip if:** Never — this feeds Gate 2.

### Stage 9 — Gate 2 fact-check resolution (sacred)
- **Owner:** Operator
- **Operator time: 5 min. Background: 0. Wall clock: 5 min.**
- **Input:** Claims + URLs
- **Output:** Resolved fact-check report; pipeline continues
- **Tools:** Browser + judgment
- **RESOLUTIONS:** Sacred gate per CLAUDE.md durable rule #1. `auto_resolve_gate_2: false` master safety guard immutable. `require_human_resolution: true` cannot be flipped silently (test #10 in Agent H §6.7).
- **Failure mode:** Operator approves a contested claim. Postmortem trace is the audit.
- **Recovery:** Re-resolve; rewrite the line in `script_FINAL.txt`; restart from Stage 8.
- **Skip if:** Never. This is Gate 2.

### Stage 10 — Metadata generation
- **Owner:** Pipeline (LLM call)
- **Operator time: 0. Background: ~30s. Wall clock: <1 min.**
- **Input:** `script_FINAL.txt`, style_guide, hook formula
- **Output:** `metadata.json` with title/desc/hashtag stack/Pattern: hint per platform
- **Tools:** Claude API (manual or auto per LLM API policy)
- **Failure mode:** Title >55 chars, hashtags include `#viral`. Pre-publish check #15 catches.
- **Recovery:** Edit `metadata.json` before Stage 16.
- **Skip if:** Never — feeds thumbnail + upload.

### Stage 11 — TTS + VO loudnorm pre-mix
- **Owner:** Pipeline
- **Operator time: 0. Background: 30–60s. Wall clock: ~1 min.**
- **Input:** `script_FINAL.txt`, voice config
- **Output:** `<topic_id>_vo.wav` at -14 LUFS / -1.0 dBTP (R1 uniform)
- **Tools:** edge-tts (current) → ElevenLabs Starter $5 (per R2 when monetization triggers); `audio_loudnorm.py` two-pass
- **RESOLUTIONS:** R1 (uniform -14 LUFS), R2 (Starter $5 now, Creator at 1k subs / YPP).
- **Failure mode:** TTS API outage; quota exceeded.
- **Recovery:** See § 6.3 playbook (failover to OpenAI gpt-4o-mini-tts).
- **Skip if:** Never.

### Stage 12 — B-roll select (stock + dedup) + AI hero shots
- **Owner:** Pipeline
- **Operator time: 0 (operator pre-approves AI prompt list at Stage 12.0 if AI cues present). Background: 1–3 min. Wall clock: 2–3 min.**
- **Input:** `script_FINAL.txt` B-roll cues + manifest
- **Output:** Stock clips in `03_assets\stock\<topic_id>\`; AI clips in `03_assets\ai_broll\<topic_id>\`
- **Tools:** Pexels + Mixkit APIs; `broll_dedup.py` (cache); `ai_broll_generate.py` via fal.ai when AI cues
- **RESOLUTIONS:** R3 — mix is Stock 55–65% + AI 20–30% + Pillow cards 15–25% + **Gameplay 0%**.
- **Failure mode:** Pexels rate limit; AI generation timeout (fal.ai charges only on success per Agent G §6e).
- **Recovery:** Stock — wait for window. AI — retry once, then fall back to stock for that cue.
- **Skip if:** Never.

### Stage 13 — Thumbnail render
- **Owner:** Pipeline
- **Operator time: 0. Background: <30s. Wall clock: <30s.**
- **Input:** `metadata.json` Pattern: field
- **Output:** `_thumbnails\<topic_id>.jpg`
- **Tools:** Pillow (3 of 8 patterns implemented; 5 pending per Agent H §6)
- **Failure mode:** Pattern not implemented. Falls back to `big_text_claim`.
- **Recovery:** Implement missing patterns over time.
- **Skip if:** Never — needed for YT upload.

### Stage 14 — Caption ASR + word-pop emit
- **Owner:** Pipeline
- **Operator time: 0. Background: 1–2 min. Wall clock: 1–2 min.**
- **Input:** `<topic_id>_vo.wav`
- **Output:** `<topic_id>_captions.ass` (karaoke word-pop with keyword highlight)
- **Tools:** faster-whisper large-v3 + `caption_word_pop.py` (Agent I §2 spec)
- **RESOLUTIONS:** R5 — in-house Python wins; skip Submagic.
- **Failure mode:** Whisper OOM (rare on 6 GB VRAM with float16); ASS emit error.
- **Recovery:** Restart caption stage with TTS .mp3/.wav cached.
- **Skip if:** Never — captions are the highest-leverage retention upgrade.

### Stage 15 — Master render
- **Owner:** Pipeline
- **Operator time: 0. Background: 1–2 min. Wall clock: 1–2 min.**
- **Input:** VO + B-roll manifest + captions + thumbnail metadata
- **Output:** `<topic_id>_master.mp4` (1080×1920, H.264 high, 30fps CFR, AAC 384kbps stereo)
- **Tools:** FFmpeg libx264 (NVENC fallback to libx264 acceptable per Agent I §1)
- **RESOLUTIONS:** R7 (`-ac 2` mandatory).
- **Failure mode:** Encoder error; corrupt output.
- **Recovery:** See § 6.1 playbook.
- **Skip if:** Never.

### Stage 16 — Master integrity probe
- **Owner:** Pipeline
- **Operator time: 0. Background: <5s. Wall clock: <5s.**
- **Input:** Master MP4
- **Output:** Pass/fail
- **Tools:** `media_integrity.check_fast()` (Agent I §5)
- **Failure mode:** moov atom missing, no audio stream, duration mismatch.
- **Recovery:** Re-render. If 2nd fail, drop topic.
- **Skip if:** Never. This is the gate that would have caught the `2026-05-06_002_tt` corruption.

### Stage 17 — Gate 3 final QA (sacred, operator-only)
- **Owner:** Operator
- **Operator time: 5 min (4–5 min checklist + watch). Background: 0. Wall clock: 5 min.**
- **Input:** Master + metadata + thumbnail
- **Output:** `<topic_id>_master_QA_APPROVED.marker`
- **Tools:** Pre-publish checklist § 1; phone preview; `prepublish_qa.py`
- **RESOLUTIONS:** Sacred gate G3 per durable rule #1. Never auto-clear.
- **Failure mode:** Any of the 22 items fails.
- **Recovery:** Per item — see § 1.1 fail-action column.
- **Skip if:** Never. The marker is the sign-off.

### Stage 18 — Variants + per-platform encode + variant probes
- **Owner:** Pipeline
- **Operator time: 0. Background: 1–2 min. Wall clock: 1–2 min.**
- **Input:** Approved master
- **Output:** `_yt.mp4` (CRF 19), `_tt.mp4` (CRF 21), `_ig.mp4` (CRF 20). All at -14 LUFS / -1.0 dBTP per R1.
- **Tools:** FFmpeg per-platform encode + `media_integrity` per variant
- **RESOLUTIONS:** R1 supersedes Agent H/I -11 LUFS for TT/IG. **All three variants land at -14 LUFS uniform.**
- **Failure mode:** Variant probe fails (the `_06_002_tt` case).
- **Recovery:** Re-render that one variant; do not block other platforms.
- **Skip if:** Never.

### Stage 19 — Gate 4 upload approval (sacred per-video)
- **Owner:** Operator
- **Operator time: 2 min. Background: 0. Wall clock: 2 min.**
- **Input:** Topic ID, gate-3 marker present
- **Output:** Verbal/written approval; privacy choice (public / unlisted / private / scheduled)
- **Tools:** Operator brain + `sv-upload` skill prompt
- **RESOLUTIONS:** Per CLAUDE.md durable rule #2. **NEVER bundle approval across videos.**
- **Failure mode:** None — purely a gate.
- **Recovery:** N/A.
- **Skip if:** Never. This is sacred.

### Stage 20 — YouTube upload via Data API
- **Owner:** Pipeline
- **Operator time: 0. Background: 1–3 min (resumable upload). Wall clock: 1–3 min.**
- **Input:** Variant + thumbnail + metadata + privacy
- **Output:** YouTube video URL; audit row in `upload_log.csv`
- **Tools:** `tools/youtube_upload.py` (official Data API; OAuth desktop client)
- **RESOLUTIONS:** Per durable rule #2 — official Data API only; cookie auth + 3rd-party uploaders banned.
- **Failure mode:** OAuth token expired; quota.
- **Recovery:** See § 6.2 playbook.
- **Skip if:** Privacy=private and operator wants delayed `--publish-at` for batch — still uploads, just scheduled.

### Stage 21 — TikTok manual upload
- **Owner:** Operator
- **Operator time: 5 min. Background: 0. Wall clock: 5 min.**
- **Input:** `_tt.mp4`, caption (different from YT title per style_guide § Hashtag strategy: 4 hashtags), AI-disclosure decision per R6
- **Output:** Live TikTok post
- **Tools:** TikTok desktop / mobile app
- **RESOLUTIONS:** No third-party uploader allowed (durable rule #2). Manual desktop upload of watermark-free master dodges Agent D §4 watermark penalty.
- **Failure mode:** Upload fails mid-flow; TikTok rate limit on new accounts.
- **Recovery:** Retry; ≥3hr spacing on subsequent posts (Agent D §3).
- **Skip if:** Operator scheduled; just don't skip the AI-disclosure toggle.

### Stage 22 — Instagram manual upload
- **Owner:** Operator
- **Operator time: 5 min. Background: 0. Wall clock: 5 min.**
- **Input:** `_ig.mp4`, caption (different from TT — 5 hashtags per style_guide), AI-disclosure decision per R6
- **Output:** Live Reels post
- **Tools:** Instagram desktop / mobile app
- **Failure mode:** Same as TT.
- **Recovery:** Retry.
- **Skip if:** Operator chooses to skip IG for low-priority topics; not recommended at sub-100 subs (Agent D growth math).

### Stage 23 — Archive variants → 06_published
- **Owner:** Pipeline
- **Operator time: 0. Background: <30s. Wall clock: <30s.**
- **Input:** Successful upload return
- **Output:** Variants copied to `06_published\<YYYY-MM>\<topic_id>\{youtube,tiktok,instagram}\`
- **Tools:** `pipeline.archive` (Agent H §6 NEW Stage 13)
- **Failure mode:** Disk full.
- **Recovery:** Free disk; re-run archive.
- **Skip if:** Never — this is the cold backup.

### Stage 24 — Postmortem stub creation
- **Owner:** Pipeline
- **Operator time: 0 immediately; ~5 min 24–72h later when analytics arrive. Background: <5s. Wall clock: <5s.**
- **Input:** Topic metadata + upload result
- **Output:** `Channels\ShadowVerse\postmortems\<topic_id>.md` from template
- **Tools:** `postmortem_stub.py` (Agent H §6.5)
- **Failure mode:** None at create-time.
- **Recovery:** N/A.
- **Skip if:** Never — operator fills during Sunday or monthly review.

### 2.1 Hands-on time budget per video (steady state)

| Category | Operator time |
|---|---|
| Manual gates (idea-gen + script + FINAL + G2 + G3 + G4 + Stage 7 writeback) | ~29 min |
| Manual platform uploads (TT + IG) | ~10 min |
| Drift / context switching | ~6 min |
| **Total per video** | **~45 min** |

If running 2 picks/day, idea-gen is shared across both → ~75 min for two videos, not 90.

### 2.2 Wall-clock per video (operator + background)

| Phase | Wall clock |
|---|---|
| Idea-gen → script_FINAL | ~15 min |
| Fact-check + Gate 2 + metadata | ~7 min |
| TTS + B-roll + thumbnail + captions + render + integrity probe | ~7 min |
| Gate 3 | ~5 min |
| Variants + Gate 4 + YT upload | ~6 min |
| TT + IG manual | ~10 min |
| Archive + postmortem stub | ~1 min |
| **Total wall clock per video** | **~50 min** |

---

## 3. Weekly Batch SOP (5 Shorts in 3 hours, Sunday 09:00–12:00)

**Premise.** The daily-batch flow is also the weekly-batch flow when picks_per_day=5; the pipeline already loops `run_for_topic`. What changes is the operator's session shape. Concentrating the 5 sacred gates into one focused 3-hour block is faster than spreading them across 5 separate days because the operator stays in headspace.

**Anchor: Sunday 09:00–12:00 local time.** This is the canonical batch window.

### 3.1 Block-by-block schedule

| Block | Wall clock | Operator activity | Pipeline activity (parallel) | What blocks vs runs in parallel |
|---|---|---|---|---|
| 09:00–09:30 | 30 min | **Topic candidate review (Gate 1 batch)**: read trend pull from cron 06:30, run idea-gen prompt once, paste 10 candidates, accept top-5 from scoring.py. Approve any AI-broll prompt lists from `_pending.json` for the 5 picks. | Pipeline scores; spawns 5 topic dirs; halts at script gen for each | Blocks: nothing — operator must finish before scripts generate |
| 09:30–10:30 | 60 min | **Script round-robin** — 5 scripts × ~10 min each. Paste PROMPT, return RESPONSE, write FINAL. Don't skip Stage 1.5 review on any. | After each FINAL writeback: pipeline runs Stage 1.5 review, fact-check claim extraction, halts at Gate 2 | Parallel: while writing FINAL N, pipeline is fact-checking N-1 |
| 10:30–11:00 | 30 min | **Fact-check round (Gate 2)** — 5 × ~5 min. Operator reviews extracted claims + URLs; resolves contested. | After each Gate 2 unblock: metadata gen, TTS, VO loudnorm, B-roll fetch, thumbnail, caption ASR, master render, integrity probe | Parallel: heavy lifting (TTS + render) happens in background as Gate 2 unblocks topics one by one |
| 11:00–11:45 | 45 min | **Gate 3 round** — 5 × ~5 min. Watch each master end-to-end on phone. Run `prepublish_qa.py`. Drop the marker on each. **Operator email / engagement / Saturday cadence work fits here between videos** (Agent H §3.2 explicit). | Once each marker drops: variants encode, variant probes, `06_published` archive prep | Parallel: variant render runs while operator watches the next topic's master |
| 11:45–12:00 | 15 min | **Metadata + thumbnails inspect, batch-schedule uploads**: 5 × `youtube_upload.py --publish-at <RFC3339>` distributed across the next ~5 days. Privacy=private; scheduled-public via `--publish-at`. Approve each per Gate 4. | Schedules upload jobs | Sequential: uploads queue but don't fire until publish time |
| Day-of-publish | 5 min/video | **Manual TT + IG upload** at scheduled publish time using watermark-free master. Toggle AI-disclosure per R6 (narrow). | n/a | Parallel: while TT uploads, operator can prep the IG variant |

**Total Sunday wall-clock: 3 hours (09:00–12:00).** Per-video manual upload time on subsequent days adds ~10 min/video for TT+IG = ~50 min spread across the week, hands-on at scheduled times.

### 3.2 Cadence distribution — when each video publishes

Per Agent D §3 + style_guide § First 10 seconds, optimal slots: 12–2 PM and 6–9 PM viewer-local time. ≥3hr TT spacing.

Default schedule for Sunday-batched 5 videos:

| Video | YouTube publish | TikTok publish | Instagram publish |
|---|---|---|---|
| Topic 1 | Mon 11:30 | Mon 11:30 | Mon 18:00 |
| Topic 2 | Tue 11:30 | Tue 11:30 | Tue 18:00 |
| Topic 3 | Wed 11:30 | Wed 11:30 | Wed 18:00 |
| Topic 4 | Thu 11:30 | Thu 11:30 | Thu 18:00 |
| Topic 5 | Fri 11:30 | Fri 11:30 | Fri 18:00 |

YouTube uses `--publish-at` for scheduled-private→public. TT/IG manual at the publish slot. Sat/Sun are off-publish (Agent D §3 worst windows).

### 3.3 What blocks vs what runs in parallel

**Strictly sequential (cannot parallelize):**
- Script LLM rounds (Claude chat is one-at-a-time)
- Gate 2 review (operator can only resolve one fact-check at a time)
- Gate 3 watch (operator can only watch one master at a time)
- Gate 4 upload approval (per-video by policy)

**Parallel-friendly (pipeline runs while operator focuses elsewhere):**
- TTS rendering for topic N happens while operator writes script for topic N+1
- B-roll fetch for topic N happens while operator does Gate 2 for topic N-1
- Master render for topic N happens while operator does Gate 3 for topic N-1
- Variant encode for topic N happens after marker drops, can overlap with operator's Gate 3 on N+1

**Pexels API rate limit** is the only background constraint that bites at 5/session. Tolerable; `broll_dedup.py` cache softens it on repeat clips.

### 3.4 What the operator does while pipeline renders

Sunday batch is also Saturday-cadence overflow per `07_Weekly_Operating_Cadence.md`. While master renders run (~10–15 min cumulative across 5 topics during 11:00–11:45):
- Reply to comments on this week's published videos (top 5 per platform)
- Note any audience question that could become next week's topic → add to `Channel\content_calendar.md`
- Quick spot-check on YT Studio for any monetization-status changes on individual videos (per `02_Compliance_Checklist.md` weekly)

### 3.5 Don't exceed 5/session

Beyond 5, gate-3 fatigue documented in cadence research; quality of approval drops. If demand calls for more, run a second 3hr session midweek (Wednesday) — don't push to 7+ in one Sunday.

### 3.6 Failure modes specific to batch

- **One topic's script LLM RESPONSE is bad** → operator regens or skips that topic; pipeline catches the halt and runs the rest.
- **Stock fetch fails (Pexels API rate limit)** → manifest.json marks topic incomplete; pipeline halts that topic; operator re-runs with `--topic-id` after the limit window.
- **Whisper fails on TTS audio** → almost never seen; rerun the topic. Keep `<topic>_vo.mp3/wav` so re-run starts from caption stage.
- **Two topics happen to converge on same hook formula** → diversify per style_guide variation rule (Agent D §8 inauthentic-content defense). Edit one before Gate 2.

---

## 4. Monthly Review SOP (~90 min, 1st Sunday)

**Cadence: 1st Sunday of each month, after the weekly batch (so 12:00–13:30).** Net additional time over the weekly Sunday slot.

**Inputs:**
- `_weekly_analytics.csv` (4 weekly pulls accumulated)
- `upload_log.csv` (all uploads in the month)
- `06_published\<YYYY-MM>\` archived variants
- `Channels\ShadowVerse\postmortems\<topic_id>.md` (filled stubs)
- `TODO.md` § Analytics log (operator's manual notes during the month)

**Outputs:**
- `Channels\ShadowVerse\postmortems\<YYYY-MM>_monthly.md` — net delta vs last month
- `TODO.md` updated A/B test queue with next experiment selected
- Optional `style_guide.md` adjustment commit if voice/format learnings emerged

### 4.1 Block schedule

| Block | Duration | Activity |
|---|---|---|
| 0:00–0:15 | 15 min | **Pull fresh analytics**: `python analytics_pull.py --all` (all-time on every published video) AND `python analytics_pull.py --days 30`. Wait for completion. |
| 0:15–0:30 | 15 min | **Top-3 / bottom-3 list**: extract month's top-3 viewed + bottom-3. Note hook patterns + topics. Note traffic-source breakdown (when augmented per Agent H §6.8 lands). |
| 0:30–0:45 | 15 min | **Hook category leaderboard**: which of the 12 hook formulas from `prompts/library/viral_hooks.md` won this month? Tally average AVD + 3-sec retention per formula across the month's videos. |
| 0:45–1:00 | 15 min | **Cost-per-video tally vs $50/mo budget**: sum ElevenLabs (R2: $5 Starter unless triggered to $22 Creator) + Runway ($12) + fal.ai overflow + Ideogram ($7) + Backblaze B2 (~$1.50). Flag any creep above $50. |
| 1:00–1:15 | 15 min | **Cited-observation source cheatsheet refresh**: which sources fired this month (Reddit subs, X handles, vendor blogs, HN)? Which dried up? Update `prompts/library/sources.md` with promote/demote. |
| 1:15–1:30 | 15 min | **Single-variable A/B selection for next month + decision log entry**: ONE variable (caption color, hook formula, thumbnail pattern, VO speed, voice). Add to `TODO.md` A/B queue with success metric. Track YPP / Creator Rewards posture (Agent D §7 thresholds). |

### 4.2 Canonical analytics-review template

Copy this into `Channels\ShadowVerse\postmortems\<YYYY-MM>_monthly.md` each month. Operator pastes data, fills text fields.

```markdown
# ShadowVerse Monthly Review — <YYYY-MM>

> Compiled <DATE> after `analytics_pull.py --all` + `--days 30`.
> Cost-per-video tally vs $50/mo budget; A/B test queue update for next month.

---

## 1. Top-3 / Bottom-3 by views (30-day)

| Rank | Topic ID | Title | Views | AVD | 3s ret | Hook formula | Traffic source |
|------|----------|-------|-------|-----|--------|--------------|----------------|
| Top 1 | | | | | | | |
| Top 2 | | | | | | | |
| Top 3 | | | | | | | |
| Bot 1 | | | | | | | |
| Bot 2 | | | | | | | |
| Bot 3 | | | | | | | |

**Patterns to expand:** <fill>
**Patterns to retire:** <fill>

## 2. Retention curve shape (top-3 + bottom-3)

For each: classify as plateau / cliff / hump / mid-cliff (Agent B taxonomy). Note timestamp of cliff for each cliff video.

| Topic | Shape | Cliff timestamp | Hypothesis |
|-------|-------|-----------------|------------|

## 3. Hook formula leaderboard

| Formula (from viral_hooks.md) | Videos shipped | Avg AVD | Avg 3s ret | Verdict |
|-------------------------------|----------------|---------|------------|---------|
| Pattern Interrupt | | | | |
| Curiosity Gap | | | | |
| Direct Question | | | | |
| Bold Claim (number) | | | | |
| False Premise | | | | |
| You're Doing It Wrong | | | | |
| Outcome-First | | | | |
| Mid-Action | | | | |
| (etc — full 12) | | | | |

**Winner this month:** <formula>
**Loser this month:** <formula>

## 4. Inauthentic-content posture

- Videos shipped without a named source (target: 0): <count>
- Hook formula concentration on top single formula (target: <50%): <%>
- Channel-level pattern variation grade (subjective): <A–F>

## 5. Cost-per-video tally vs $50/mo budget

| Line item | Cost | Notes |
|-----------|------|-------|
| ElevenLabs Starter $5 (or Creator $22 if triggered per R2) | | |
| Runway Standard | $12 | |
| fal.ai overflow (Kling 2.5 Turbo $0.07/s) | | |
| Ideogram Basic | $7 | |
| Backblaze B2 backup | ~$1.50 | |
| Other (any new SaaS) | | |
| **Total** | | Within $50? Y/N |

## 6. Cited-observation source cheatsheet refresh

| Source | Fired times this month | Quality | Action |
|--------|-----------------------|---------|--------|
| r/ChatGPT | | | promote / demote / hold |
| r/ClaudeAI | | | |
| HN | | | |
| @<X handle> | | | |
| <vendor blog author> | | | |

## 7. YPP / Creator Rewards posture

| Metric | This month | Last month | Threshold |
|--------|------------|------------|-----------|
| Subs | | | 1,000 (YPP) |
| 90d Shorts views | | | 10M (YPP) |
| Followers (TT) | | | 10,000 (Creator Rewards) |
| 30d TT views | | | 100,000 (Creator Rewards) |

## 8. Single-variable A/B for next month

| Field | Value |
|-------|-------|
| Variable under test | |
| Control | |
| Variant | |
| Success metric | |
| Sample size target | |
| Decision deadline | |

## 9. Re-upload-flop guardrail (Agent H §3.3)

At sub-100 subs, never re-upload a flop with new hook (per `agent_d_reupload_risk.md`). Produce fresh content.

Status: <do not re-upload | considering at 1k+ subs / YPP active>
```

### 4.3 Re-upload-flop guardrail

At sub-100 subs, never re-upload a flop with a new hook (Agent D §6 + `agent_d_reupload_risk.md`). Produce fresh content. This SOP is read-only on past videos; it does NOT trigger re-uploads. Only at >1k subs and YPP active should re-upload-with-different-hook even be considered.

---

## 5. Daily Routine Anchor (printable single-page)

> Pin next to monitor. This is the rhythm.

```
SHADOWVERSE DAILY RHYTHM — print and pin
==========================================

DAILY PUBLISH WINDOWS
  11:30 local — primary slot (YT Shorts on Mon/Wed/Fri; TT every day)
  18:00 local — secondary slot (TT extra, IG Reels)
  TT spacing: ≥3 HOURS between posts (Agent D §3 — 22% engagement lift vs hourly)

WEEKLY CADENCE
  Mon/Wed/Fri 11:30 — YT Shorts publish (3/wk minimum; sweet spot)
  Tue/Thu 11:30   — TT-only or IG-priority (cross-post optional)
  Sun 09:00–12:00 — WEEKLY BATCH (5 Shorts produced; gates 1/2/3/4 all clear)
  Sat morning      — Engagement (top-5 comments per platform; ~30 min)

MONTHLY CADENCE
  1st Sunday +90 min after weekly batch (i.e., 12:00–13:30)
    — analytics pull, top/bottom 3, hook leaderboard, A/B selection

SACRED GATES (NEVER COMPRESS, NEVER AUTOMATE)
  G1  Idea selection         — scoring.py + operator review for any pivot
  G2  Fact-check resolution  — operator clicks every URL, accepts or rewrites
  G3  Final video QA         — operator watches end-to-end on phone, drops marker
  G4  Per-video YT upload    — operator approves: public/unlisted/private/scheduled

THE ONE RULE OPERATORS SKIP THAT THEY MUST NOT
  Cited observation per video — named source + retrievable URL.
  Not "a developer says". A specific Reddit handle, X account, news byline,
  HN comment id, or vendor blog author. Click the URL once before shipping.
  This is the strongest defense vs. YouTube's inauthentic-content terminations.

BUDGET CEILING — $50/mo
  Current stack: ~$25–44/mo depending on upgrades (see RESOLUTIONS R2)
  Trigger Creator $22 only at: 1k subs OR YPP active OR 80+ shorts/mo
                               OR A/B-proven >5% AVD lift from v3 audio tags

WHEN IN DOUBT — ASK FIRST
  Sacred gates. Identity. Branding. Durable rules. Strategic pivots.
  Anything irreversible. Anything that risks money.

WHEN PROCEED WITHOUT ASKING
  Tactical execution within explicitly-approved scope. Sub-agent dispatch.
  Atomic commits. File edits within an in-flight task.

CONFIRM BEFORE
  Push to remote. Force-push. Delete files. Destructive git ops.
  Upload to YouTube. Notifications outside the operator.
```

---

## 6. Failure-Recovery Playbooks (top 5)

### 6.1 Render fails / corrupt MP4

**Symptom.** `media_integrity.check_fast()` fails. Common: moov atom missing (the `2026-05-06_002_tt` case), no audio stream, video stream truncated, file <1 MB.

**Triage (60 seconds).**
1. Read the JSON failure report from `prepublish_qa.py` or `media_integrity` — exact failure mode.
2. Check disk free: `Get-PSDrive C` (PowerShell) / `df -h /c` (Git Bash). If <5 GB free, that's the cause.
3. Check FFmpeg logs at `logs\<run_ts>\ffmpeg_<topic>.log` for encoder-side error.

**Recovery.**
- Disk full → free space, re-run `pipeline.py --topic-id <id> --resume-from render`.
- Encoder error (NVENC mismatch) → already falls back to libx264 per Agent I §1; if it didn't, force `-c:v libx264` in config.yaml.
- Container truncation → re-run from caption stage; FFmpeg's atomic write should produce a clean file. **If 2nd render fails, drop the topic and reslot to next batch** (Agent H §3.2 explicit). Don't burn 3+ retries.
- Log the fail to `logs\corrupt_renders\<topic_id>_<ts>.json` for postmortem.

### 6.2 YouTube upload OAuth token expired

**Symptom.** `tools/youtube_upload.py` returns HTTP 401 / "invalid_grant" / "Token has been expired or revoked."

**Triage.**
- Check `_pipeline\credentials\token.json` mtime. Tokens auto-refresh; if mtime old + refresh failed, the refresh token is revoked.

**Recovery.**
1. Run `python tools/youtube_upload.py --reauth` (if implemented) OR manually delete `token.json` and re-run any upload command — desktop OAuth flow opens browser, signs in, regenerates token.
2. Verify `client_secrets.json` still present and unchanged (not gitignored leak).
3. Re-attempt upload.
4. **NEVER use cookie auth or 3rd-party uploaders to "fix" this** — banned per CLAUDE.md durable rule #2. The official Data API path is the only path.
5. If quota exhausted (separate from token expiry — HTTP 403 with `quotaExceeded`): wait until midnight Pacific (YouTube quota reset) or request quota increase via Google Cloud Console.

### 6.3 TTS API quota / outage (ElevenLabs or edge-tts)

**Symptom.** TTS step raises `ElevenLabsTTSError: quota exceeded` or `edge_tts.exceptions.NoAudioReceived`.

**Triage.** Check ElevenLabs dashboard for credit balance; check status.elevenlabs.io for outage.

**Recovery.**
1. **Failover to OpenAI gpt-4o-mini-tts** per Agent F §6 backup #1. Pre-built interface; ~50 LoC swap. ~$0.50/mo at scenario B volume; voice quality drops but ToS-clean.
2. If OpenAI also down: failover to edge-tts (current free baseline). Voice realism drops two tiers; document in postmortem; flag for re-render once primary returns.
3. **NEVER fall back silently.** Per fail-loud principle — operator must know which video used the backup voice so style_guide voice consistency is tracked. Add a `voice_provider` field to `metadata.json` for the affected topic.
4. Document in `Channels\ShadowVerse\postmortems\<topic_id>.md` under "deviation from primary stack."
5. If outage >24h: pause batch; don't ship voice-inconsistent videos at scale.

### 6.4 Algorithm flag (low views suddenly across multiple videos)

**Symptom.** Sustained 30%+ drop in average retention across 5+ consecutive videos (`02_Compliance_Checklist.md` red flag); OR YT Studio shows "limited" status on a video; OR multiple videos auto-flagged for AI content without disclosure.

**Triage (≤30 min).**
1. **PAUSE PUBLISHING for ≥48 hours.** Per `02_Compliance_Checklist.md` red flags — this is one. Pausing is cheap; recovery from channel-level demonetization is expensive or impossible.
2. Audit the recent 5 videos against:
   - **R6 (AI-disclosure):** Did any contain real-person likeness or photoreal synthetic? Was the toggle ON?
   - **Originality (Agent D §7):** Did any ship without a cited observation? Did template repetition emerge (same hook formula 3x in a row, same B-roll cadence)?
   - **MFK trap:** Did the visual palette drift cartoony/primary-colored?
3. Read the YT Studio "Why limited?" panel for any flagged videos.
4. Run `analytics_pull.py --days 7` and compare retention curve shape vs prior 7-day baseline.

**Recovery.**
- If disclosure issue: edit affected videos to toggle disclosure; submit appeal with note.
- If originality issue: rework next 5 videos with stronger structural variation (alternate hook formulas, alternate B-roll cadence, alternate cited-observation source types). Document the recovery batch in `style_guide.md`.
- If MFK issue: audit color palette; verify each video's MFK classification.
- Write `postmortems\<YYYY-MM-DD>_algorithm_flag.md` capturing trigger, hypothesis, recovery plan, success metric.
- Resume publishing only after 48h pause + 1 fresh video that clears revised checklist.

### 6.5 Style drift / off-voice script

**Symptom.** Stage 1.5 review flags <7 on tone/voice rubric, OR operator reads `script_RESPONSE.txt` and feels it could be any AI-tech channel, OR Gate 3 watch reveals a line that doesn't sound like the channel.

**Triage.**
- Re-read the offending line aloud; confirm vs style_guide § Voice + § Forbidden patterns.

**Recovery.**
- `script_FINAL.txt` is operator-signed — that's the contract. If drift detected pre-Gate-3, manually rewrite the line in FINAL and re-trigger render.
- If drift detected at Gate 3 (master already rendered): the marker MUST NOT drop. Choose:
  - **Quick fix:** edit `script_FINAL.txt`, re-run from Stage 11 (TTS) — adds ~5 min wall clock.
  - **Drop topic:** mark as `_dropped\<topic_id>\` and reslot for next batch. Acceptable if drift is structural.
- **Never push an off-voice video through Gate 3.** Voice consistency IS the channel skin (Agent E pattern #3) — single off-voice video diluted across 5 videos in a week is a 20% regression in channel skin signal.
- Add the drift type + line to `TODO.md § Footguns log` so the next idea-gen prompt explicitly avoids that phrasing pattern.
- If 3+ scripts in a month show drift on the same axis: update `style_guide.md` § Forbidden patterns + commit + push the strategy repo.

---

*End — Pre-Publish Checklist & SOPs. Compiled by Agent M, Wave 4 synthesis, 2026-05-08.*
