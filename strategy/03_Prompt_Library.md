# Prompt Library

> Copy-paste-ready prompts for every stage of the pipeline. Replace `{NICHE}`, `{TOPIC}`, `{SCRIPT}`, etc. with actual values. Tested with Claude and GPT-class models.

---

## 1. Niche style guide (fill out once per channel)

Paste this filled-out version into every script-generation prompt as context.

```
NICHE: {e.g., "AI tools for software developers"}
TARGET VIEWER: {e.g., "mid-career developer who already uses ChatGPT/Claude and wants tactical workflow improvements"}
TONE: {e.g., "direct, opinionated, slightly contrarian, no fluff"}
VOCABULARY LEVEL: {e.g., "assumes technical literacy; uses CLI, API, prompt as everyday words"}
FORBIDDEN PATTERNS:
  - Generic "Did you know" openers
  - "Top 10" framing
  - "AI is changing everything" filler
  - Listicles without strong opinions
  - Em-dashes (use commas or periods)
SIGNATURE PATTERNS:
  - {your unique element, e.g., "always end with a one-line trade-off statement"}
  - {your unique element, e.g., "show the keyboard shortcut, don't just name the feature"}
LENGTH: 100–150 words, ~30–55 seconds spoken
CTA STYLE: {e.g., "save this, comment {keyword} for the prompt template, link in bio for the cheat sheet"}
```

---

## 2. Idea generation (Mondays — produce next week's topics)

```
You are a content strategist for a faceless short-form channel in this niche:

{paste niche style guide}

Here is recent data:
- Top 10 competitor videos in the last 7 days (title + view count + days since publish):
{paste from your trend pull}
- Top Reddit threads in {3–5 subreddits} in the last 7 days:
{paste titles}
- Google Trends rising terms for the niche:
{paste}

Produce 15 candidate angles for short-form videos.

For each, give me:
1. Angle (one sentence)
2. Hook concept (one sentence)
3. Why now (what makes this timely)
4. Who watches this (audience precision)
5. Demand evidence (which data point above supports this)
6. Originality score 1–5 (5 = nobody else is doing this take)
7. Saturation risk 1–5 (5 = highly saturated)

Avoid generic top-X formats. Avoid angles that any AI channel could post with the same data. Prefer angles that require an opinion or a specific perspective.

Output as a markdown table.
```

---

## 3. Script generation

```
You are a script writer for a faceless short-form video channel.

NICHE & STYLE GUIDE:
{paste niche style guide}

TOPIC: {topic}
ANGLE: {chosen angle}
HOOK CONCEPT: {hook concept}

Constraints:
- 100–150 words total
- Spoken duration: 30–55 seconds at 175 wpm
- The first 1.5 seconds (about 4 words) must hook hard
- Every 1–2 sentences, include a [B-ROLL: short visual cue] tag
- Include exactly ONE moment of original opinion, contrarian framing, or personal observation
- End with the channel's standard CTA pattern: {paste CTA style from style guide}
- DO NOT use any specific numbers, dates, names, or quotes unless they are common knowledge or you are highly confident; mark uncertain items with [VERIFY]

Generate 3 hook variants (the first sentence) before the script body. Mark them HOOK_A, HOOK_B, HOOK_C.

Then write the full script with [B-ROLL] cues.

After the script, list every factual claim that should be fact-checked, as a bulleted list under FACT_CHECK_QUEUE.
```

---

## 4. Hook optimization (when you want more options)

```
Here is a script for a 30-55 second faceless short:

{paste script}

Generate 8 alternative opening lines (the first 4–8 words only) optimized for:
- Stopping a thumb mid-scroll on TikTok/Reels/Shorts
- Setting up the rest of the video without spoiling the payoff
- Avoiding the patterns: "Did you know", "Most people don't realize", "Here's why", "The #1"

For each, label the dominant hook style: question, claim, contradiction, sensory, scene, callout, stat, prediction.

Rank them 1–8 by your estimated 3-second retention.
```

---

## 5. Fact-checking (CRITICAL — never skip)

Best run with a model that has web search (Perplexity, GPT with browsing, Claude with web search).

```
You are a fact-checker for an educational short-form video. The script below contains factual claims that must be verified against authoritative sources.

SCRIPT:
{paste script}

For EVERY factual claim — including specific numbers, dates, names, quotes, statistics, causal relationships, and historical events — provide:

1. The claim, quoted verbatim from the script
2. Status: VERIFIED / UNCLEAR / LIKELY WRONG / UNVERIFIABLE
3. The most authoritative source you found, with URL
4. The exact wording in the source that supports or contradicts the claim
5. If LIKELY WRONG: what the script should say instead

Do not gloss over claims that "sound right." Do not accept Wikipedia as a primary source for contested claims; trace to the cited reference.

If the script contains language like "studies show," "experts say," or "scientists found" without naming a specific study, mark that as UNCLEAR and request specifics.

Output as a markdown table.
```

---

## 6. Metadata generation

```
Generate publishing metadata for this video.

NICHE & STYLE GUIDE:
{paste style guide}

SCRIPT:
{paste final script}

Produce:

YOUTUBE SHORTS:
- Title (max 60 chars, specific not clickbait, includes one searchable keyword)
- Description (3–5 sentences, includes: hook line, 1-sentence summary, sources URL, lead-magnet CTA, AI disclosure line)
- 5 tags
- 3 hashtags

TIKTOK:
- Caption (max 150 chars, conversational, 1 hook line + CTA)
- 4 hashtags (mix of broad and niche)

INSTAGRAM REELS:
- Caption (3–5 sentences, can be longer than TikTok, slightly more polished tone)
- 5 hashtags

COVER / THUMBNAIL CONCEPT:
- 3–5 word text overlay (large, bold)
- Background image description
- One color accent

Output each section clearly labeled. No emoji unless the niche style guide explicitly allows them.
```

---

## 7. Niche validation (when considering a new topic cluster)

```
You are evaluating whether a new topic cluster should be added to a faceless short-form channel.

CHANNEL NICHE:
{paste style guide}

PROPOSED CLUSTER: {e.g., "vector databases comparison"}

Assess on these axes (1–5 each):
1. Audience demand (search volume, recent view counts on competitor videos)
2. Saturation level (how many channels already cover this)
3. Originality opportunity (can I bring an angle nobody else has?)
4. Monetization fit (does this map to my affiliate stack and lead magnets?)
5. Content longevity (will this be relevant in 6 months?)
6. Production difficulty (do I have the assets, knowledge, and tools?)
7. Policy risk (any compliance concerns?)

Total score out of 35. Recommend GO if >25, MAYBE if 18–25, NO-GO if <18.

Then list the 3 strongest sub-angles within this cluster and the 3 angles I should specifically avoid.
```

---

## 8. Weekly analytics review

```
You are a content strategist reviewing the past week's performance.

DATA (last 7 days):
{paste a CSV or table of: video ID, title, platform, views, retention %, 3-second hold %, likes, saves, shares, comments, follower change}

CHANNEL CONTEXT:
- Niche: {your niche}
- Median retention over the last 4 weeks: {X%}
- Median views over the last 4 weeks: {N}

Produce:

1. TOP 3 winners — what made them work? Be specific (hook, format, topic, timing). Avoid generic answers.
2. BOTTOM 3 losers — what likely caused the underperformance?
3. TOPIC CLUSTERS to expand vs. retire (based on retention, not raw views)
4. FORMAT experiments to try next week
5. PATTERNS that suggest "inauthentic content" or templating risk — be brutal here, this is a self-audit
6. ONE specific, testable hypothesis for next week (single variable change)

Be specific. Don't tell me to "post more consistently" or "improve hooks." Tell me what to literally change.
```

---

## 9. Repurposing decision (when re-cutting old content)

```
This video performed {above/below median} for our channel:

ORIGINAL VIDEO:
- Title: {title}
- Hook: {first sentence}
- Topic: {topic}
- Angle: {angle}
- Retention: {X%}
- Views: {N}
- Best moments by retention curve: {seconds when retention dipped least}

I want to repurpose this. Recommend:

1. Should I re-cut a NEW video using this topic? (Y/N + reason)
2. If yes: what's a fundamentally different angle that doesn't make the new version look like a duplicate to the algorithm?
3. What's one element I should KEEP from the original (the part that worked)?
4. What's one element I should COMPLETELY CHANGE?

Critical constraint: the new video must NOT look like a templated copy of the original. It must read as a separate piece of work to a human reviewer and to platform classifiers.
```

---

## 10. Pre-publish self-check (run on EVERY script before rendering)

```
You are a strict content reviewer auditing this script for risks before production.

SCRIPT:
{paste script}

Flag every instance of:
1. A factual claim not yet verified (mark line numbers)
2. A pattern that resembles other AI-generated short-form content (generic openers, listicle phrasing, stock-sounding transitions)
3. A claim that could be construed as financial, medical, or legal advice
4. Any reference to a real person, brand, or copyrighted property
5. Any phrase that sounds like a hallucinated statistic ("studies show," "research finds," "experts say" without a named source)
6. Any signal that this script could be produced verbatim by another channel using the same prompt

For each flag, suggest a specific revision.

If the script has FEWER than 3 flags, also point out the WEAKEST sentence and recommend a stronger alternative.
```
