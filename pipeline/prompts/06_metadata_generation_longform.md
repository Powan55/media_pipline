# Metadata generation — LONG-FORM deep-dive (§6-LF)

Used by `pipeline.generate_metadata()` for the long-form track.
Substitutes: `{NICHE_STYLE_GUIDE}`, `{SCRIPT}`.

**Long-form track = YouTube ONLY.** This emits ONLY the YouTube section (no TikTok / Instagram — the long-form track does not cross-post). The long-form metadata parser does NOT require TT/IG sections. The pipeline computes and injects the timestamped chapter list from the script's `[CHAPTER: …]` markers + the rendered narration timing — do NOT write timestamps yourself.

```
Generate YouTube publishing metadata for this LONG-FORM (10–12 min) deep-dive video.

NICHE & STYLE GUIDE:
{NICHE_STYLE_GUIDE}

SCRIPT:
{SCRIPT}

Audience: a general consumer curious about AI, NOT a developer. Every visible field must be understandable to a non-coder in 2 seconds. No dev jargon (CLI, refactor, MCP, repo, env var, etc.).

Produce these sections, each clearly labeled:

YOUTUBE:
- Title (50–70 chars — long-form titles can run a little longer than Shorts since there's no cover-truncation, but keep under ~70 for mobile). Rules:
  - **Single-sentence narrative** that leads with the named subject and tells a mini-story ("AI quietly built itself a parallel internet"). Use the `[Subject]: [payoff]` colon shape ONLY as a rare exception when the human IS the surprise.
  - **Recognizable anchor in the FIRST 3 WORDS:** a mainstream AI brand (Claude, ChatGPT, OpenAI, Anthropic, Gemini, Google, Apple, Microsoft, Grok, Meta), a household-name human (Musk, Altman), or a universal object (iPhone, your phone). BANNED openers: "This AI…", "Your AI…", "A new AI…", "Researchers…", "Hackers…".
  - **Modal-framing ban:** state a DATED FACTUAL EVENT, not a hypothetical. BANNED: "could", "might", "imagine if", "what if".
  - **Withhold the twist:** surface the unsettling implication / the stakes, not the full resolution. Make a promise the title doesn't fully keep — the viewer must watch for the payoff.
  - Include one searchable keyword. Specific, not clickbait.
- Description (a real long-form SEO description, ~150–300 words, NO URLs — the channel is too new to embed links):
  - Open with a 1–2 sentence hook that restates the central question the video answers.
  - 2–4 sentences summarizing what the video covers (rich with searchable keywords: the AI products, people, and concepts by name) WITHOUT spoiling the payoff.
  - One plain-text AI-disclosure line ("This video uses an AI-generated voice and AI-assisted visuals.").
  - Credit sources by NAME, not URL ("Sources include Bloomberg, Anthropic's release notes, and …").
  - Do NOT write a chapter/timestamp list — the pipeline appends it automatically from the script's chapters.
- Tags: 12–15 (comma-separated; mix consumer-AI buzzwords + topic-specific terms + the named subjects).
- Hashtags: 3–5 at the very bottom of the description (long-form, so NOT `#Shorts`). The first 2–3 are the most search-relevant consumer-AI tags for the topic; then 1–2 more. From: `#AI #ChatGPT #Claude #Gemini #OpenAI #Anthropic #AInews #AItools #ArtificialIntelligence #TechNews #GoogleAI`. Only relevant ones.

COVER / THUMBNAIL CONCEPT:
- Note: custom thumbnails are policy-OFF for now (`--no-thumbnail`); YouTube auto-selects a frame for the MVP. Still emit a concept so a landscape thumbnail can be built in Phase 2.
- Text overlay: 2–4 words, ALL CAPS, the punchiest distillation of the central question/claim — plain English (e.g., "IT KEPT GOING.", "NOBODY ASKED IT TO").
- Background image description: one sentence describing the single most striking landscape (16:9) image the script implies (a person reacting to a screen, an abstract AI visual, a named product) — used as guidance for the Phase-2 landscape thumbnail.
- Color accent (HEX): always `#7C5CFF` (channel violet). Locked.

PINNED COMMENT:
- ONE short line (no URL, ≤140 chars) in the friend-retelling voice, posing the SAME stakes-tied question the video ends on. Genuinely answerable, specific to THIS video's twist. Not keyword-bait, not a generic "what do you think?".

Output each section clearly labeled. No emoji unless the style guide allows them. Do NOT emit TikTok or Instagram sections — this is the YouTube-only long-form track.
```
