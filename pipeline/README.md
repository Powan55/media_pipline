# ShadowVerse pipeline (`_pipeline`)

The Python automation behind the ShadowVerse faceless short-form channel.
Plain functions, no agent framework, three sacred human gates baked in.

This README is the local dev guide. Strategy lives in
`C:\Users\laxmi\Documents\Project\` — start with `01_Operating_Guide.md` if
you haven't read it.

---

## Project layout

```
_pipeline\
├── pipeline.py             ← stage functions + orchestrator (entry: `python -m pipeline ...`)
├── trend_pull.py           ← Monday cron: YouTube + Reddit + Google Trends → topic inbox
├── analytics_pull.py       ← Sunday cron: per-platform metrics → weekly CSV
├── prompts\                ← 10 prompts extracted from 03_Prompt_Library.md
│   ├── README.md           ← how the pipeline loads and substitutes them
│   └── 01_*.md ... 10_*.md
├── logs\                   ← per-run logs (gitignored contents)
├── config.yaml.template    ← copy → config.yaml, edit; no secrets in here
├── .env.template           ← copy → .env, fill in; never commit
├── requirements.txt        ← pinned deps; PyTorch installed separately
└── .gitignore
```

---

## First-time setup (Windows, ~10 min once you have the keys)

> All commands assume PowerShell from `C:\ContentOps\_pipeline\`. Bash works equivalently;
> swap `Activate.ps1` for `activate` if you're in Git Bash.

### 1. Create the venv (Python 3.11+; you have 3.12 already)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Install PyTorch with CUDA support (separate from requirements.txt)

PyTorch wheels are CUDA-version-specific and **must come from the official PyTorch index URL**
to get GPU acceleration. Mixing the generic PyPI build with a CUDA build silently breaks
GPU detection.

Pick the line matching your installed CUDA version:

```powershell
# CUDA 12.1
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# CUDA 11.8
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# CPU-only (slow — Whisper will work but image gen will not)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

Verify GPU is visible to PyTorch:

```powershell
python -c "import torch; print('CUDA:', torch.cuda.is_available(), '| device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu')"
```

### 4. Install FFmpeg

The `ffmpeg-python` library is a thin wrapper around the FFmpeg CLI; the CLI must
be on PATH separately.

```powershell
winget install --id=Gyan.FFmpeg -e
# Restart PowerShell after install so PATH refreshes, then verify:
ffmpeg -version
```

If `winget` isn't available, download a release build from https://www.gyan.dev/ffmpeg/builds/
and add `<extracted>\bin\` to PATH manually.

### 5. Create your local `config.yaml` and `.env`

```powershell
Copy-Item config.yaml.template config.yaml
Copy-Item .env.template .env
```

Then edit both. `config.yaml` is gitignored (per-machine paths and tweaks live there);
`.env` is gitignored (secrets). Anything tagged `<<TODO>>` in the templates needs your input.

### 6. Smoke-test the imports

```powershell
python -c "import pytrends, pandas, faster_whisper, yaml, dotenv; print('imports ok')"
```

If that prints `imports ok`, the dependency stack is wired up correctly.

(Note: `anthropic` and `openai` are intentionally NOT in `requirements.txt` —
the pipeline runs in manual LLM mode by default. See "Manual LLM mode" below.)

---

## Manual LLM mode (the current default)

The pipeline runs in **`llm.primary_provider: manual`**. There are no LLM API calls.
Every stage that needs an LLM (script gen, fact-check, metadata, weekly review):

1. Writes the filled-in prompt to `<manual_io_dir>/<topic_id>/<stage>_PROMPT.txt`.
2. Halts with a clear message identifying which file to feed to your chat LLM.
3. You paste that prompt into Claude Code (or Claude.ai, ChatGPT, etc.), get the response.
4. Save the response as `<stage>_RESPONSE.txt` in the same per-topic dir.
5. Re-run the pipeline; it picks up the response and continues to the next stage.

This is a deliberate choice — it costs $0 and keeps the human in the loop on every LLM
output. To flip to fully-automated API mode later: set `llm.primary_provider` to
`anthropic` or `openai` in `config.yaml`, add the matching package to `requirements.txt`,
and put the corresponding key in `.env`. The code will be able to dispatch to either path
once Phase 2 implementation lands.

## API keys you need to obtain

> Pasted directly into `.env` by **you** — never into chat. The default config requires
> only the two keys in **Required**; everything else activates a specific automation.

### Required (default config)

| Key                  | Where to obtain                                          | Notes |
|----------------------|----------------------------------------------------------|-------|
| `PEXELS_API_KEY`     | https://www.pexels.com/api/new/                          | Free, generous limits. Used by asset fetcher. |
| `PIXABAY_API_KEY`    | https://pixabay.com/api/docs/                            | Sign in, scroll to "Your API key". Fallback / alternate stock source. |

### Optional (only when you activate the matching feature)

| Key                                            | Activates                                                              | Where |
|------------------------------------------------|------------------------------------------------------------------------|-------|
| `ANTHROPIC_API_KEY`                            | Set `llm.primary_provider: anthropic` (flip OUT of manual mode)        | https://console.anthropic.com/settings/keys |
| `OPENAI_API_KEY`                               | Set `llm.primary_provider: openai`                                     | https://platform.openai.com/api-keys |
| `PERPLEXITY_API_KEY`                           | Set `fact_check.provider: perplexity`                                  | https://www.perplexity.ai/settings/api |
| `YOUTUBE_API_KEY`                              | Trend pull (competitor video data) + analytics pull (own-channel metrics). **NEVER for uploads** — those stay manual via Studio | https://console.cloud.google.com/apis/credentials  (enable "YouTube Data API v3" first) |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`    | Trend pull (Reddit subreddit top posts)                                | https://www.reddit.com/prefs/apps  → type `script` |
| `ELEVENLABS_API_KEY`                           | Set `tts.provider: elevenlabs`                                         | https://elevenlabs.io/app/settings/api-keys |

### NOT API keys, but accounts you also need to create

- **YouTube channel** (Google account dedicated to ShadowVerse) — **manual posting via Studio web/mobile, always**
- **TikTok account** (creator/business)
- **Instagram account** (creator or business — required for Graph API access later)
- **Beehiiv account** (free tier) — newsletter + lead-magnet landing page
- **Cloudflare or Namecheap account** — for the eventual landing-page domain

These are *your* job, not the pipeline's. The compliance checklist
(`02_Compliance_Checklist.md`) requires AI-disclosure language in each platform's bio
and a per-channel "About" note before publishing.

### What is NOT supported (and why)

- **Cookie / session-based YouTube uploads** (e.g. `yt-upload-cli`, browser-cookie hijacking) —
  ToS violation, channel-level termination risk, and reach-suppression even if undetected.
  Operating Guide §14 explicitly forbids this. YouTube Shorts uploads are manual forever.

---

## Running the modules

### Trend pull (Monday morning)

```powershell
python trend_pull.py --once
```

Reads `config.trend_pull.*`, hits the three data sources, writes the ranked topic inbox to
`config.trend_pull.topic_inbox_path`. Schedule via Windows Task Scheduler for 8:00 Mon.

### Analytics pull (Sunday evening)

```powershell
python analytics_pull.py --once --days 7
```

Reads `config.analytics_pull.*`, pulls last-7-day metrics, appends to the weekly CSV at
`config.analytics_pull.output_path`. Schedule for 19:00 Sun.

### Pipeline (per topic)

```powershell
python -m pipeline `
  --topic-id 2026-05-05_001 `
  --topic "Cursor 0.46's new background agents" `
  --angle "the one workflow that actually justifies the price bump" `
  --hook "Cursor just shipped agents. Most demos are wrong."
```

Halts at each sacred gate (fact-check resolution, final QA). Resume by re-invoking
the next stage's function directly with the cached inputs.

---

## Three sacred manual gates (do not bypass)

1. **Idea selection** (Mondays) — operator picks 5–8 of the 15 LLM-generated angles.
   Lives in Notion / a sheet, OUTSIDE this pipeline by design.
2. **Fact-check resolution** (Tuesdays) — `pipeline.await_fact_check_resolution()` halts
   the pipeline until the operator clicks through every `UNCLEAR` / `LIKELY_WRONG` claim.
   Backed by `config.fact_check.require_human_resolution` (must stay `true`).
3. **Final video QA** (Wednesdays) — `pipeline.await_final_qa()` halts the pipeline until
   the operator has watched the master end-to-end on a phone-sized window.
   Backed by `config.publishing.human_qa_required` (must stay `true`).

Skipping any of these is the failure mode that ends the channel. The code refuses to
run with the corresponding config flag set to `false`.

---

## Kill switch

```yaml
publishing:
  kill_switch: true   # halts schedule_publishing() with a clear error
```

Flip this in `config.yaml` if you go on vacation, notice a quality regression, or
get a policy notification. Cheaper than recovering from a channel-level demonetization.

---

## Troubleshooting

- **`ImportError: faster_whisper`** — reinstall: `pip install --force-reinstall faster-whisper`.
- **`torch.cuda.is_available()` returns `False`** — you installed the CPU build by accident.
  `pip uninstall torch torchvision torchaudio` then re-run the CUDA install line.
- **`ffmpeg: command not found`** — restart PowerShell after `winget install`. PATH only
  refreshes for new shells.
- **`FileNotFoundError: config.yaml`** — you skipped Setup step 5. Copy the templates.
- **Microsoft Store python.exe stub intercepts `python`** — the real Python 3.12 is at
  `C:\Users\laxmi\AppData\Local\Programs\Python\Python312\python.exe`. Use `py -3.12` to
  guarantee you're hitting the right one.

---

## Code style

- `pathlib.Path` everywhere; no hardcoded `/` or `\` in business logic
- `logging` module, never `print` in pipeline code (debug scripts are fine)
- Stubs raise `NotImplementedError("Phase 2: ...")` with concrete next-step guidance
- Idempotent stages — TTS failure on item 4 must not re-run script gen for items 1–3
- No `LangChain`, `CrewAI`, `AutoGen`, or `LangGraph` — explicit Python calls only
  (see `feedback_engineering_principles.md` for the durable rationale)
