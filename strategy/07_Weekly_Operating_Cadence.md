# Weekly Operating Cadence

> The repeating rhythm. Once you've finished setup, this is what your week looks like indefinitely.

Total time: **~6–10 hours/week** at the 30-day system level.

---

## Monday — Plan (1.5 hrs)

**Morning (auto, runs on schedule)**
- 8:00 AM: `trend_pull.py` runs automatically. Pulls YouTube Data API + Reddit + Google Trends data for your niche into `Topic_Inbox` in Notion / a sheet.

**Your work (1.5 hrs, mid-morning)**
- Review the trend pull output
- Run the **idea generation prompt** (`Prompt 03_Prompt_Library.md, section 2`) using the trend pull data
- From the 15 candidate angles, pick **5–8 winners** based on: original opinion potential, saturation level, affiliate fit
- Set their status to "approved-for-script" in your tracker
- Add any human-noticed topics from your own observations during the week

---

## Tuesday — Scripts & fact-check (2 hrs)

**Pipeline runs (30 min, mostly hands-off)**
- For each approved topic: run scriptwriting prompt → 3 hooks + draft script with [B-ROLL] cues
- Run fact-check prompt on each script → flagged claims list

**Your work (~1.5 hrs)**
- For each script, manually review every UNCLEAR or LIKELY WRONG claim. Click through the source URLs. Edit script as needed.
- Pick the strongest hook variant per video.
- Edit any line that sounds generic, AI-phrased, or templated. (If you can imagine 5 other channels publishing this exact line, rewrite it.)
- Move scripts to `_approved\` status

---

## Wednesday — Production (2 hrs)

**Pipeline runs (auto-triggered when scripts hit "approved")**
- Asset fetcher: parses [B-ROLL] cues → fetches Pexels/Pixabay → falls back to Flux generation
- TTS generation: F5-TTS local OR ElevenLabs API → WAV per script
- Whisper: generates word-level timestamps from VO → ASS subtitle file
- FFmpeg assembly: combines VO + b-roll + captions → MP4 master at 1080×1920

**Your work (~1.5 hrs)**
- Watch each rendered video end-to-end on a phone-sized window (or actually on your phone)
- Flag any: pronunciation errors, sync issues, weird visuals, robotic delivery, copyright concerns
- Re-render anything that fails QA (usually means swap a b-roll clip or regenerate one VO line)
- Run pre-publish self-check prompt on each script as a final paranoia pass
- Approve final masters

---

## Thursday — Variants & metadata (1 hr)

**Pipeline runs**
- Variant generator: produces YT/TT/IG cuts (slight differences in opening seconds, audio bed, etc.)
- Metadata generation: titles, descriptions, hashtags, cover concepts

**Your work (~45 min)**
- Edit titles — LLM titles are competent but rarely the best. Sharpen them.
- Verify descriptions include: lead magnet link, sources, AI disclosure
- Generate cover frames in Canva or via your image pipeline
- Approve variants

---

## Friday — Schedule & publish (45 min)

- Upload YouTube Shorts manually via Studio web/mobile (avoid third-party Shorts uploads)
- Schedule TikTok and Instagram Reels via Metricool or Buffer
- Stagger publish times: aim for 11am-1pm and 6-9pm local time, ~3 days of content scheduled
- Set the next week's tracker entries to "scheduled"
- Final compliance checklist (from `02_Compliance_Checklist.md`) for each video

---

## Saturday — Engagement (30–45 min)

### Flip-night touch (~10 min, HARD CAP)

*(Added 2026-06-09 weekly review, PU-1 / R3 lever 1, Manager C1 folded. Runs **each flip evening** — every night a scheduled video flips public, ~7:00–7:45 PM ET after the 6:35 slot. It supplements the Saturday block below; it does not replace it.)*

Each flip evening, for that night's flipped video(s):

1. **Pin the PINNED COMMENT.** Post the video's staged `PINNED COMMENT:` text (from its metadata bundle) in YouTube Studio and pin it — day-of, not 6 days later.
2. **Quick reply pass.** First-pass replies to whatever genuine comments have already landed on tonight's and recent videos. Quick, real, specific — not the deep Saturday pass.
3. **Citation-upgrade audit — night N−1's videos.** Check whether yesterday's published videos got their 24h third-party citation upgrade (a day-of video can't have its 24h upgrade verified at publish, so flip-night N audits night N−1). Tick the result in the log.
4. **Log a row in the treatment log** — `C:\ContentOps\channels\ShadowVerse\01_research\_engagement_log.csv` (`topic_id, video_id, flip_date, pinned, reply_pass, citation_upgrade_checked, notes`). One row per flipped video, every flip night. **Skipped steps get logged too, with the reason in `notes`** — a skipped night with a reason beats a silent gap.

Rules:
- **10 minutes per night, HARD CAP.** Pin + log + citation tick fit inside it. Reply overflow defers to the existing Saturday top-N triage below — no second SOP.
- **Pin-only fallback:** if comments stay at 0 for 4 consecutive weeks, the reply pass downgrades to pin-only until PU-6 instrumentation shows volume.

### Saturday block (unchanged)

**Per-video engagement SOP** (run for each video published this week, inside the 30–45 min block):

1. **PIN your own first comment.** Post the channel's own first comment using the `PINNED COMMENT:` text from that video's metadata bundle (the single non-URL friend-voice line posing the script's stakes-tied closing question), then pin it. This seeds the conversation with the question we *want* answered and anchors the comment section before anyone else lands.
2. **Reply — with a volume off-ramp.** While comments are sparse (under ~15/week across the channel), reply to **~100%** of genuine comments — every real reply is high-leverage for early-stage algorithmic reach. As volume grows past that, switch to **top-N triage**: reply to the highest-signal handful per video (questions, strong reactions, named regulars) and let low-signal "nice" comments go. Keep the whole pass inside the 30–45 min Saturday block; don't let it sprawl.
3. **Log genuine viewer questions to the topic inbox.** Any comment that's a real question or a latent video idea → capture it in the topic inbox (`Channel\content_calendar.md`) so it can seed a future video. The pinned closing-question is designed to manufacture exactly these.

Notes:
- Replies must be real and specific, never generic — that's what boosts reach.
- (Or, if you can't sustain Saturday work: batch all engagement into 30 min on Sunday morning.)

---

## Sunday — Review & plan (1.5 hrs)

**Auto runs (7 PM)**
- `analytics_pull.py` pulls last 7 days of metrics from YT/TT/IG APIs into your sheet

**Your work (~1.5 hrs)**
- Run the **weekly analytics review prompt** (`03_Prompt_Library.md, section 8`) on the data
- Identify: top 3 winners, bottom 3 losers, patterns to expand, patterns to retire
- Run the **weekly compliance review** from `02_Compliance_Checklist.md`
- Update `Channel\postmortems\YYYY-MM-DD.md` with the week's findings
- Adjust next Monday's idea generation prompt if needed (bias toward winning patterns)
- Set ONE single-variable experiment for the week ahead

---

## What's automated vs. manual

| Task | Mode |
|---|---|
| Trend data pulling | 🤖 Auto |
| Idea generation (LLM call) | 🤖 Auto |
| Idea SELECTION | 👤 Manual (this is your taste) |
| Script generation | 🤖 Auto |
| Fact-check claim flagging | 🤖 Auto |
| Fact-check RESOLUTION | 👤 Manual (always) |
| Asset fetching | 🤖 Auto |
| TTS generation | 🤖 Auto |
| Subtitle generation | 🤖 Auto |
| Video assembly | 🤖 Auto |
| Final video QA | 👤 Manual (always) |
| Title editing | 👤 Manual (LLM as draft only) |
| YouTube Shorts upload | 👤 Manual (avoid API risk) |
| TT/IG scheduling | 🤖 Auto via Metricool/Buffer |
| Comment replies | 👤 Manual (this is your relationship with viewers) |
| Analytics ingestion | 🤖 Auto |
| Analytics INTERPRETATION | 👤 Manual + LLM-assisted |
| Topic pivots | 👤 Manual (always, with conviction) |

**Three sacred manual gates: idea selection, fact-check resolution, final video QA.** Everything else can break and you recover. Skip these and you don't.

---

## Vacation mode

- 1 week away: pre-produce 2 weeks of content, schedule it all, disable auto-publishing on day 8
- 2+ weeks away: pause all auto-publishing entirely. Better to go quiet than to publish unsupervised.
- NEVER let auto-publishing run for more than 7 days without you checking the dashboard.

---

## Time budget summary

| Day | Hours |
|---|---|
| Monday | 1.5 |
| Tuesday | 2.0 |
| Wednesday | 2.0 |
| Thursday | 1.0 |
| Friday | 0.75 |
| Saturday | 0.5 |
| Sunday | 1.5 |
| **Total** | **~9.25 hrs/week** |

This is the realistic floor. Anyone telling you it's 1 hr/week is selling you something.
