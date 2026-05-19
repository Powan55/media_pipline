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

- Reply to comments on this week's videos (real replies, not generic — these massively boost algorithmic reach)
- Reply to the top 5 comments on your most-recent video on each platform
- Note any audience question that could become a future video topic
- Add to `Channel\content_calendar.md`

(Or, if you can't sustain Saturday work: batch all engagement into 30 min on Sunday morning.)

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
