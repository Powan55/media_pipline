# Project: Faceless Short-Form Content Operations

This folder is the **strategy and reference layer** of your operation. The actual production work (scripts, renders, exports) happens in a separate working directory on your PC — see `05_Local_Production_Folder_Structure.md` for that setup.

## Folder map

```
C:\Users\laxmi\Documents\Project\
│
├── 00_README.md                            ← you are here
├── 01_Operating_Guide.md                   ← the master strategic guide
├── 02_Compliance_Checklist.md              ← pre-publish + weekly + monthly checks
├── 03_Prompt_Library.md                    ← all LLM prompts (copy-paste ready)
├── 04_Tracker_Template.csv                 ← import into Sheets/Excel/Notion
├── 05_Local_Production_Folder_Structure.md ← how to set up C:\ContentOps\
├── 06_Channel_Decision_Worksheet.md        ← fill out before launching each channel
├── 07_Weekly_Operating_Cadence.md          ← what to do each day of the week
│
└── Channels\
    ├── _CHANNEL_TEMPLATE\                  ← copy this folder for each new channel
    │   ├── README.md                       ← fill out for this specific channel
    │   ├── style_guide.md                  ← niche-specific voice and format rules
    │   ├── content_calendar.md             ← rolling backlog
    │   └── postmortems\                    ← weekly/monthly review notes
    │
    └── (your real channel folders go here, one per niche)
```

## How to use this

1. **First time setup:** read `01_Operating_Guide.md` end-to-end. It's long. Skim once, then re-read sections 6 (phased setup) and 15 (final recommendation) carefully.
2. **Pick your niche:** fill out `06_Channel_Decision_Worksheet.md` before doing anything else.
3. **Create your first channel folder:** copy `Channels\_CHANNEL_TEMPLATE\` and rename to your channel name. Fill in its README and style guide.
4. **Set up the production folder:** follow `05_Local_Production_Folder_Structure.md` to create `C:\ContentOps\` (separate from this Project folder, intentionally).
5. **Set up the tracker:** import `04_Tracker_Template.csv` into Google Sheets or Notion.
6. **Run the weekly cadence:** `07_Weekly_Operating_Cadence.md` is your repeating playbook.

## Why two folders (Project vs. ContentOps)

This `Project\` folder is reference material — small files, mostly markdown, gets backed up to cloud, shared between channels. Read-and-edit, not write-heavy.

`C:\ContentOps\` (or wherever you put it) is the production scratch space — large media files, render outputs, working assets. High write volume, doesn't need to be in OneDrive/Drive sync (and shouldn't be — it'll thrash the sync client).

## Versioning

If you use Git: this `Project\` folder is a great Git repo. The `C:\ContentOps\` folder should NOT be a Git repo (binary churn). If you don't use Git: snapshot this folder once a month to a zip backup.

---

*Last updated when this scaffold was created. Update the date below whenever you make material changes.*

**Last reviewed:** _________________
