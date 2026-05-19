# Local Production Folder Structure

This is a SEPARATE folder from your `Project\` reference folder. Production files (large media, renders, working assets) should NOT live alongside your strategy docs — they have totally different read/write patterns and backup needs.

## Recommended location

```
C:\ContentOps\
```

(Or `D:\ContentOps\` if you have a second drive — strongly preferred for media work. SSD if possible.)

## Why a separate folder

| `Documents\Project\` | `C:\ContentOps\` |
|---|---|
| Small files (markdown, CSV) | Large files (MP4, WAV, PNG bundles) |
| Read-mostly | Write-heavy, churns constantly |
| Sync to OneDrive / Drive | DO NOT sync — will hammer the sync client |
| Git-friendly | Git-hostile (binary churn) |
| Cross-channel | Per-channel subfolders |

## Full structure to create

```
C:\ContentOps\
│
├── _shared\
│   ├── stock_cache\              ← downloaded Pexels/Pixabay assets keyed by hash
│   ├── music_library\            ← platform-approved music you've vetted
│   ├── voice_models\             ← F5-TTS / XTTS reference voices
│   └── ffmpeg_templates\         ← reusable FFmpeg command templates
│
├── _pipeline\                    ← your Python automation lives here
│   ├── pipeline.py
│   ├── trend_pull.py
│   ├── analytics_pull.py
│   ├── prompts\                  ← prompt files referenced by pipeline.py
│   ├── config.yaml               ← API keys via env vars, NOT in this file
│   ├── logs\                     ← per-run logs
│   └── requirements.txt
│
├── _models\                      ← local AI models
│   ├── whisper\                  ← Whisper large-v3
│   ├── flux\                     ← ComfyUI / Flux checkpoints
│   └── tts\                      ← F5-TTS or XTTS weights
│
└── channels\
    │
    └── {channel-name}\
        ├── 01_research\          ← raw research per topic
        │   └── {YYYY-MM-DD}_{NNN}_{slug}\
        │       ├── notes.md
        │       └── sources.md
        │
        ├── 02_scripts\
        │   ├── _drafts\          ← LLM output before review
        │   └── _approved\        ← after fact-check + your edits
        │
        ├── 03_assets\
        │   ├── stock\            ← Pexels/Pixabay clips for this video
        │   ├── generated\        ← Flux output for this video
        │   └── audio_vo\         ← TTS WAVs
        │
        ├── 04_renders\
        │   ├── _wip\             ← intermediate renders
        │   └── _final_master\    ← the 1080×1920 master per video
        │
        ├── 05_exports\
        │   ├── youtube\          ← variants pushed to YT
        │   ├── tiktok\           ← variants pushed to TT
        │   └── instagram\        ← variants pushed to IG
        │
        └── 06_published\         ← archived final files post-publish
            └── {YYYY-MM}\        ← grouped by month for archival sanity
```

## Naming convention (use everywhere)

```
{YYYY-MM-DD}_{NNN}_{slug}.{ext}
```

Examples:
```
2026-05-04_007_kalman-filter-explained.mp4
2026-05-04_007_kalman-filter-explained_yt.mp4
2026-05-04_007_kalman-filter-explained_tt.mp4
2026-05-04_007_kalman-filter-explained.srt
2026-05-04_007_kalman-filter-explained_vo.wav
```

The 3-digit sequence number is GOLD. It lets you sort by creation order, join to the tracker, and instantly find a video's files.

## Disk space planning

| Asset type | Size estimate per video | Monthly at 20 vids/wk |
|---|---|---|
| Stock cache | ~50 MB shared | ~2 GB total |
| Generated images | ~10–30 MB | ~2–3 GB |
| TTS audio | ~1–3 MB | ~0.3 GB |
| Working renders | ~200–500 MB | ~40 GB |
| Final masters | ~30–80 MB | ~6 GB |
| Platform exports | ~30 MB × 3 | ~7 GB |
| **Total monthly** | | **~50–60 GB** |

Plan for at least **500 GB free** if you want a 6-month buffer before archival. SSD strongly preferred — your render times will be 3–5x faster than on HDD.

## What NOT to put in this folder

- Strategy docs (those go in `Documents\Project\`)
- API keys in plain text (use `.env` file or Windows env vars; add `.env` to `.gitignore` if you use Git)
- Anything with PII or financial info
- Backups of your published videos (those should go to cold storage — Backblaze B2 is ~$0.005/GB/mo)

## Backup strategy

- **`Documents\Project\`**: backed up via OneDrive/Drive automatically; weekly Git commit if using Git
- **`C:\ContentOps\_pipeline\`**: backed up via Git push to GitHub private repo (code only)
- **`C:\ContentOps\channels\*\06_published\`**: monthly cold backup to external drive or Backblaze B2
- **Everything else in ContentOps**: ephemeral; do NOT back up working files, you'll thrash storage and budgets
