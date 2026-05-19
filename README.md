# media_pipline

A working faceless short-form video pipeline plus the strategy / operating
docs that drive it. Originally built for one channel (ShadowVerse, AI/tech
Shorts on YouTube), now shared so we can collaborate, compare notes, and
figure out what actually works.

This is **active operational software**, not a polished open-source kit.
Expect Windows-isms, hardcoded paths, in-flight decisions, and code that
gets rewritten when a hypothesis dies. Read the `CLAUDE.md` files for the
mental model before changing anything load-bearing.

---

## Repo layout

```
media_pipline/
├── CLAUDE.md            ← project constitution (read first)
├── strategy/            ← what we're making and why
│   ├── 00_README.md ... 07_*.md     ← operating guide, prompt library, etc.
│   ├── Channels/
│   │   ├── _CHANNEL_TEMPLATE/       ← blank template for a new channel
│   │   └── ShadowVerse/             ← current channel's voice + branding
│   └── audit_2026-05-07/            ← design decisions, hook library, specs
└── pipeline/            ← the Python that makes the videos
    ├── README.md                    ← local dev setup, command reference
    ├── CLAUDE.md                    ← pipeline engineering rules
    ├── *.py                         ← orchestrator + stage modules
    ├── prompts/                     ← 10 LLM prompts the pipeline loads
    ├── tools/                       ← stage helpers (loudnorm, captions, OAuth, upload)
    ├── tests/                       ← pytest suite
    ├── .env.template                ← copy to .env, fill in keys
    └── config.yaml.template         ← copy to config.yaml, edit
```

---

## What's not in here (on purpose)

- Real `.env`, `config.yaml`, OAuth tokens — secrets live on the operator's
  machine and are `.gitignored`.
- `SESSION_HANDOFF.md` / `TODO.md` — point-in-time operational state.
- Per-video postmortems and analytics CSVs — channel-specific performance
  data.
- Competitive intel and analytics rollups from the audit folder — these
  reveal too much about specific channel performance.

If something looks missing and you think we need it to collaborate, open an
issue.

---

## Quick start (pipeline)

```powershell
cd pipeline
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.template .env        # then edit — at minimum, set Pexels + Pixabay
copy config.yaml.template config.yaml
```

Then read `pipeline/README.md` for the full setup, command list, and how
the three sacred human gates work.

For the strategy side, start with `strategy/01_Operating_Guide.md`.

---

## How we want to collaborate

The point of this repo is to find out what works. Concretely:

- **Try things and write down what happened** — postmortems in your own
  channel folder, or a shared `LEARNINGS.md` if it helps.
- **Don't be precious about the existing decisions** — the `DECISION_LOG.md`
  is a snapshot, not a contract. If something's wrong, push back.
- **Branch for anything non-trivial** — main should stay green-ish.
- **PRs over direct pushes to main** — easier to leave comments.

---

## Heritage

The pipeline started life in a private repo (`Powan55/shadowverse-pipeline`).
This public mirror is a fresh fork for collaboration; the original may
diverge or stay behind.
