# News-drop script template — ShadowVerse

> 12h-ship template for "[VENDOR] just did X" Shorts. Pairs with `tools/news_rss_poller.py`
> and the `news_drops_queue.json` produced by it. Use when the topic source is a vendor
> announcement (model release, pricing change, feature drop, controversy/policy, partnership/launch,
> capability surprise) rather than a synthesized angle from `daily_batch.py`.
>
> **Why this template exists:** competitor-audit data shows news velocity is the differentiator at
> sub-100 subs (Cursor 1114v vs `.cursor/rules` 5v on the same channel, same day). Target ship time
> from vendor news drop to YouTube upload: 12 hours.
>
> **Audience:** general consumer curious about AI, NOT developers. Pivot set 2026-05-07. Every
> beat in this template assumes a non-coder can follow it as audio-only.

---

## Voice rules (compressed from `Channels\ShadowVerse\style_guide.md`)

These are NON-NEGOTIABLE and override anything that "sounds better" but breaks them:

1. **Laymen vocabulary only.** A regular ChatGPT-using consumer must follow every sentence. Banned
   without explanation: CLI, API, repo, env var, IDE, refactor, lint, MCP, agent framework, fine-tune,
   embedding, regex, runtime, dependency, container, kernel, syntax, compiler. Plain-English swaps:
   "agent" -> "AI assistant that acts on its own", "model" (LLM) -> "AI", "prompt" -> "the
   instruction you give the AI", "API" -> "behind-the-scenes connection" or just cut it.
2. **Audio-first.** Read the draft aloud with the screen off. If a beat REQUIRES the visual to
   land, rewrite the line so the verbal carries the meaning and visuals are reinforcement.
3. **Setup -> twist -> payoff** (NOT "claim -> evidence -> CTA"). Setup names the vendor + what
   they did in 2 seconds. Twist at 0:08-0:10 — the surprising part, the catch, what most people
   missed. Payoff is the consequence for the regular viewer.
4. **No em-dashes** in spoken text. Use commas or periods. Em-dashes are the #1 LLM-prose tell.
5. **One specific concrete detail.** A name, a number, a date, a dollar amount, a duration.
6. **One cited observation per video.** A named source — Reddit handle, X account, news byline,
   vendor blog author, HN comment. Never anonymous "a user says". Day-of-release exception:
   first-party vendor blog/release notes are acceptable when the script credits them explicitly
   ("Anthropic's release notes say...", "OpenAI's blog post notes...").
7. **End on a one-line surprise or trade-off**, then CTA. Rotate CTAs across the three style-guide
   options to avoid templating.

## Pairing with hook formulas

The two newest hook formulas in `prompts/library/viral_hooks.md` were designed to pair with news
drops:

- **#10 "You're Doing It Wrong"** — pairs with news that exposes a smarter way to use a tool the
  audience already uses. Example: a vendor ships a feature 80% of users haven't found yet. Open
  with the accusation, drop the reason at the second hook.
- **#11 "Result-First / Mid-Action"** — pairs with capability-surprise news ("an AI just did X").
  Open on the finished result, save the catch for the second hook.

Both formulas slot naturally into setup -> twist -> payoff: the accusation/result IS the setup,
the reason/catch IS the twist, the implication IS the payoff. Default to one of these for news
drops unless the angle is comparative (then formula #4) or version-stamped (then formula #7).

---

## Slot-fill template

Replace each bracket with a concrete value before generating. Keep total spoken length
**95-125 words** (~38-50s at the channel's calibrated +10% TTS rate).

```
[B-ROLL: visual hook — see Visual hook line below, 1.5-2.5s]

[OPENING_HOOK — 5-10 spoken words, first 4 words contain the punch, plain-English]

(SETUP, 0:03-0:08)
[VENDOR] just [WHAT_THEY_DID]. [B-ROLL: vendor logo or product screen, 1.5-2s]
Here's what that means for [WHO_THE_REGULAR_VIEWER_IS]. [B-ROLL: relatable consumer scene]

(SECOND HOOK / TWIST, 0:08-0:10)
[B-ROLL: hard visual change — different setting/tool/perspective, NOT a zoom]
[TWIST_LINE — curiosity gap, contradiction, or "but here's the thing" pivot, plain-English]

(PAYOFF / EXPLAINER, 0:10-0:35)
[WHY_IT_MATTERS_TO_REGULAR_PEOPLE — 2-3 sentences max, every sentence with a B-ROLL cue]
[CONCRETE_EXAMPLE_FOR_LAYMEN — one specific scenario a non-coder pictures instantly]
[CITED_OBSERVATION — "On r/[sub], u/[handle] said..." OR "[Vendor]'s blog post notes..."]

(LANDING, 0:35-end)
[ONE_LINE_TWIST_OR_REVEAL — the surprise, the catch, the unexpected trade-off]
[CTA — rotate across the three style-guide options]
```

### Required annotations in `script_FINAL.txt`

- `[formula: <Name>]` next to each hook line (per `viral_hooks.md`)
- One `[B-ROLL: ...]` cue per 6-8 spoken words (16-24 cues per 130-word script)
- `[SOURCE: <named handle / blog URL>]` next to the cited observation line
- `[VENDOR_DROP: <feed_key>:<news_drops_queue id>]` somewhere in the header so the manager
  can match the script back to the queue entry it came from

---

## Five news-type categories with example openers

Each example uses a real consumer-AI vendor so the LLM has a concrete pattern to imitate. Pick
the category that matches the queue entry, then write 3 hook variants per the standard
script-gen prompt.

### (a) Model release

Topic: a vendor ships a new flagship model.

Example openers (formula -> verbal hook):
- **#7 Measured-Claim:** "Claude 4.7 dropped this morning. The new mode reads your screen, and the privacy fine print is wild."
- **#11 Result-First:** "A 30-page legal contract, summarized in plain English in 11 seconds. GPT-5 just did that on a free account."
- **#3 Result-First Mid-Action:** "ChatGPT wrote a wedding speech good enough to make the bride cry. The new memory feature is why."

Setup pattern: "[VENDOR] just shipped [MODEL_NAME] on [DATE]." Twist: the one capability that's
counter to expectation. Payoff: what a regular ChatGPT user can do with it tomorrow.

### (b) Pricing change

Topic: a vendor raises, drops, or restructures pricing.

Example openers:
- **#1 Contradiction:** "ChatGPT Plus is supposed to be the value tier. OpenAI just made the free tier better."
- **#10 You're Doing It Wrong:** "You're paying for Claude Pro for the wrong reason. The thing that actually unlocks is in a different setting."
- **#2 Specific-Number:** "Twenty dollars to two hundred dollars overnight. Gemini just split into a tier most people didn't see coming."

Setup pattern: "[VENDOR] just changed what [PLAN_NAME] costs." Twist: the hidden winner or
loser. Payoff: what to do about it before the change locks in.

### (c) Feature drop

Topic: a vendor adds a single feature without a full model release.

Example openers:
- **#10 You're Doing It Wrong:** "You're using Claude like a chatbot. It just got a feature that turns it into something weirder."
- **#11 Result-First:** "Fifty emails written, sent, and sorted. Claude's new agent feature did it while I made coffee."
- **#3 Result-First Mid-Action:** "ChatGPT just learned to remember things you actually wanted it to remember. Here's the one setting."

Setup pattern: "[VENDOR] quietly added [FEATURE_NAME] this week." Twist: the use case nobody
saw coming. Payoff: a 15-second how-to in plain English.

### (d) Controversy / policy

Topic: a vendor changes terms, faces backlash, or ships something users protest.

Example openers:
- **#8 Cited-Observation:** "An OpenAI engineer admitted on X that the new memory feature surprised even them. Then the team locked the thread."
- **#1 Contradiction:** "Anthropic said your chats were private. The new policy quietly carved out an exception."
- **#6 Specific-Question:** "Why did Gemini just block a question that ChatGPT answers fine? The reason is in the fine print."

Setup pattern: "[VENDOR]'s [POLICY/STATEMENT] this week." Twist: the part that's not in the
press release. Payoff: what changes for the viewer's daily use.

### (e) Partnership / launch

Topic: a vendor partners with a non-AI brand or launches in a surprising channel.

Example openers:
- **#7 Measured-Claim:** "Anthropic just shipped Claude inside Excel and Outlook. The first person who tried it in PowerPoint regretted it."
- **#11 Result-First:** "A full week's grocery order, delivered, decided by AI. Perplexity just partnered with someone you wouldn't expect."
- **#4 Comparison Frame:** "ChatGPT vs Claude vs Gemini, but the question is which one you can now buy a flight through."

Setup pattern: "[VENDOR] just announced [PARTNER]." Twist: the thing the partnership unlocks
that nobody asked for. Payoff: which everyday workflow this changes.

### (f) Capability surprise

Topic: a vendor demos a result that's qualitatively new (a category jump, not an iteration).

Example openers:
- **#11 Result-First:** "A 30-second voice clone of my dad. Suno did it from one Christmas voicemail."
- **#3 Result-First Mid-Action:** "Midjourney just generated a 5-minute video that fooled three people I showed it to."
- **#2 Specific-Number:** "200,000 words in one go. Claude can now read a whole novel in the time it takes you to make tea."

Setup pattern: "[VENDOR] just showed [CAPABILITY] for the first time." Twist: the limit they
didn't mention in the demo. Payoff: the everyday use case this enables this year.

---

## How the LLM should fill the slots

When invoked with a queue entry, the LLM should:

1. Read `feed`, `title`, `url`, `summary` from the queue entry.
2. Pick the news-type category (a-f above) that fits the title + summary.
3. Choose THREE distinct hook formulas from `prompts/library/viral_hooks.md`. At least one
   should be #10 or #11 (the news-paired formulas) unless the topic is clearly comparative
   (#4) or version-stamped only (#7).
4. Fill `[VENDOR]`, `[WHAT_THEY_DID]`, `[WHY_IT_MATTERS_TO_REGULAR_PEOPLE]`,
   `[CONCRETE_EXAMPLE_FOR_LAYMEN]`, `[ONE_LINE_TWIST_OR_REVEAL]` from the queue entry's
   summary plus a tavily-search-verified detail when the summary is thin.
5. Output one `script_FINAL.txt` candidate per hook variant. Annotate each with `[formula: <Name>]`,
   `[VENDOR_DROP: <feed_key>:<id>]`, and inline `[B-ROLL: ...]` cues at the cadence specified
   above.
6. **Run the laymen-vocab test mentally** — if any sentence uses a banned word without a
   plain-English swap, rewrite before output.

Operator gate: gate-2 fact-check still runs. Day-of-release exception (vendor blog as the cited
observation) must be flagged in the gate-3 prep notes.
