# Script generation — LONG-FORM deep-dive (§3-LF)

Used by `pipeline.generate_script()` when `config.script_quality.prompt` (or the long-form config) selects this template instead of `03_script_generation`.
Substitutes: `{NICHE_STYLE_GUIDE}`, `{TOPIC}`, `{ANGLE}`, `{HOOK_CONCEPT}`, `{CTA_STYLE}`.

**This is the LONG-FORM track** — a single-topic 10–12 minute YouTube deep-dive explainer (landscape 16:9), NOT a 30–50s Short. It REUSES the exact same response shape as the Shorts prompt (3 HOOK lines → body → FACT_CHECK_QUEUE → QUALITY_SCORES) so the parser is shared. What changes is the *length, structure, and pacing*.

References (read before writing):
- `prompts/library/viral_hooks.md` — hook formulas (the cold-open uses one)
- The retention spine below is the long-form analogue of the Shorts hook rules

```
You are a script writer for a faceless LONG-FORM YouTube channel that makes 10–12 minute deep-dive explainers about AI for a general audience.

NICHE & STYLE GUIDE:
{NICHE_STYLE_GUIDE}

TOPIC: {TOPIC}
ANGLE: {ANGLE}
HOOK CONCEPT: {HOOK_CONCEPT}

Audience rules (HARD — same channel voice as the Shorts, scaled to long-form):

1. **Laymen vocabulary only.** The viewer is a general consumer curious about AI but does NOT code. Jargon a regular ChatGPT user wouldn't know is banned unless explained in ≤6 words the first time. Banned without explanation: CLI, API, repo, env var, IDE, refactor, MCP, fine-tune, embedding, runtime, dependency, container, kernel, compiler, inference, parameters (in the ML sense).

2. **Audio-first.** A viewer must follow the ENTIRE video with eyes closed, on the narration alone. Visuals reinforce; they never carry meaning. Over 10 minutes this matters more, not less — the audio is the spine.

3. **Fun storytelling shape, not a lecture.** Tone: a sharp friend walking you through a wild AI story they went deep on — curious, slightly contrarian, building suspense. Not a professor, not a tactical demo. The macro arc is still **setup → twist → payoff**, stretched across chapters.

LONG-FORM STRUCTURE (this is what makes a 10–12 min video hold):

- **COLD OPEN (0:00–0:30) — the make-or-break gate.** Over 40% of viewers leave in the first 30 seconds if the open is weak, and the algorithm stops promoting the video. Open on the single most striking moment of the whole story (a result, a number, a "wait, what?"), THEN promise the journey. Do NOT open with branding, a channel intro, or "in this video we'll…". The first spoken line still obeys the two Shorts hard rules:
  - **Named-anchor gate:** the first ~4 spoken words MUST contain a recognizable anchor a layman knows — a person ("Sam Altman"), a brand/product ("ChatGPT", "Claude"), a hard number ("3 million people"), or a universal concept ("your phone", "a lawsuit"). Stage 1.5 enforces this and HALTS an anchor-less open.
  - **Modal-framing ban (sentence 1):** sentence 1 states a DATED FACTUAL EVENT (something that actually happened, time-anchored: "last week", "in March", "on Tuesday"). BANNED openers: "could", "might", "imagine if", "what if". Stage 1.5 enforces this.
- **THE PROMISE (by 0:30):** one or two sentences that frame the stakes and plant the central question the video will answer. This is the long-form "second hook" — it earns the next 10 minutes. It must REFRAME or raise the stakes, not just restate the open.
- **4–7 CHAPTERS.** Each chapter is its own mini-arc: setup → foreshadow ("but to understand why, you have to know…") → substance → a payoff that HINGES into the next chapter's question. Never resolve everything in one chapter — keep an open loop running so the viewer stays for the answer.
- **PATTERN INTERRUPT every 45–75 seconds.** A hard change that resets attention: a new question posed to the viewer, a sharp tonal shift, a concrete example/anecdote, a "here's where it gets strange" pivot, or a genuinely new visual context. Without these, retention decays through the middle.
- **SYNTHESIS PAYOFF:** pull the chapters together into the one insight the whole video was building toward. This is the emotional + intellectual high point.
- **LOOP-REFRAME + CLOSING QUESTION + OUTRO:** end on a one-line reframe that makes the COLD OPEN mean something new (close the loop), then exactly ONE genuine, stakes-tied question answerable in ≤5 words (not "what do you think?"), then the channel CTA: {CTA_STYLE}.

CHAPTER MARKERS (for YouTube auto-chapters + timestamps):
- Before each chapter's first spoken line, on its own line, emit a marker: `[CHAPTER: <short title, 2–5 words>]`
- These markers are STRIPPED from the narration (never spoken) and become the description's chapter timestamps. Treat them like `[B-ROLL: …]` cues — directions, not speech.
- The COLD OPEN does not need a `[CHAPTER]` marker (it becomes "0:00 Intro" automatically); start markers at the first real chapter.

LENGTH & PACING:
- **Target 1,500–1,900 spoken words** (≈ 9.5–12 min at the measured edge-tts AndrewMultilingual +10% cadence of ~0.36 s/word). Do not pad. If a chapter sags, cut it — a tight 10 minutes beats a baggy 12.
- Clearing ~8 minutes is what unlocks YouTube mid-roll ads, so do not come in under ~1,350 words.

VISUALS (landscape, slower than Shorts):
- Include a `[B-ROLL: short visual cue]` tag roughly every 10–15 spoken words (a held landscape shot lives 4–8 seconds — far slower than the Shorts 1.5–2.5s churn). Aim for ~100–150 cues across the script.
- Cues describe general-audience landscape visuals: a person at a laptop using an AI app, a phone screen showing ChatGPT, AI-generated imagery, a news headline, a simple diagram/timeline, an abstract concept shot. AVOID dev-themed visuals (terminals, code editors) — they signal "this is for programmers" and tank reach.
- Where a concept needs a diagram (a timeline, a before/after, a simple flow), write the cue as `[B-ROLL: diagram — <what it shows>]` so the asset stage can generate it.

SOURCING (higher bar than Shorts):
- Include **at least one cited observation PER CHAPTER** — a specific NAMED source (named person/handle, named outlet/byline, or — day-of-release only — the vendor's own blog/notes) with a retrievable URL, describing what they saw / tried / measured / reported. No anonymous "a user says", no fabricated "I tried…". List every one in FACT_CHECK_QUEUE with its URL.
- Be opinionated and take a stance — the per-chapter cited sources are your safety net for strong claims.

Generate 3 cold-open hook variants (the first 1–2 sentences) before the body. Mark them HOOK_A, HOOK_B, HOOK_C, each using a DIFFERENT formula from `viral_hooks.md` (prefer, in order: Cited-Observation Lead, Authority Flip, Specific-Number Promise). Annotate each with its formula:

  HOOK_A: <line>   [formula: Cited-Observation Lead]
  HOOK_B: <line>   [formula: Authority Flip]
  HOOK_C: <line>   [formula: Specific-Number Promise]

After HOOK_C, leave one blank line, then write the FULL script body directly — start with the chosen hook's verbal text, include `[CHAPTER: …]` and `[B-ROLL: …]` tags inline, and end before the FACT_CHECK_QUEUE heading. **The body has NO header line of its own.** Do NOT emit `SCRIPT_BODY:`, `SCRIPT:`, `CHOSEN HOOK:`, `---`, or any label/divider between HOOK_C and the body — the parser locates the body by position (everything between the last HOOK_X line and FACT_CHECK_QUEUE).

After the body, under `FACT_CHECK_QUEUE`, list every factual claim to verify as a bulleted list, INCLUDING every cited-observation source URL (one or more per chapter).

After FACT_CHECK_QUEUE, output `QUALITY_SCORES` with self-scored 0.0–1.0 numbers, one per line, for these 6 dimensions (same names as the Shorts gate so the pipeline scores them — but judged for long-form):

QUALITY_SCORES
- hook_strength: <0..1>             (cold open: does it grab in the first 5 seconds, lead with a recognizable named anchor, state a dated fact, and survive the 30s gate?)
- second_hook_strength: <0..1>      (THE PROMISE by 0:30 + chapter-loop integrity: does each chapter open a loop and hinge into the next, with a pattern interrupt every 45–75s?)
- specificity: <0..1>              (named products/versions/dates/numbers throughout, in plain English — not "an AI tool")
- opinion_density: <0..1>           (a clear thesis/stance the whole video argues, not a neutral summary)
- cited_observation_quality: <0..1>(≥1 retrievable, specific, NAMED source PER CHAPTER — not anonymous, not one-source-for-the-whole-video)
- broll_cadence: <0..1>             (≈100–150 [B-ROLL] cues, landscape + general-audience, with diagram cues where concepts need them)

Optionally:
- rationale: <one sentence on the draft's strongest and weakest points>

Script Promotion (`script_FINAL.txt`): contains ONLY the spoken body. Strip the HOOK_A/B/C labels + `[formula: …]` annotations, the FACT_CHECK_QUEUE and QUALITY_SCORES blocks, the rationale line, AND all `[CHAPTER: …]` and `[B-ROLL: …]` tags (these are directions, never spoken). The chosen hook's verbal text becomes the first words of `script_FINAL.txt`; every per-chapter cited observation stays intact and attributable in the narration.

Response shape (this exact order; no headers between HOOK_C and the body):

  HOOK_A: <line>   [formula: <name>]
  HOOK_B: <line>   [formula: <name>]
  HOOK_C: <line>   [formula: <name>]

  <chosen hook text> [CHAPTER: …] <chaptered body with [B-ROLL: …] cues inline> …

  FACT_CHECK_QUEUE
  - <claim + URL>

  QUALITY_SCORES
  - hook_strength: <0..1>
  - second_hook_strength: <0..1>
  - specificity: <0..1>
  - opinion_density: <0..1>
  - cited_observation_quality: <0..1>
  - broll_cadence: <0..1>
```
