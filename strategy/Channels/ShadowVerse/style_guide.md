# Style Guide: ShadowVerse

> Pre-filled from `_CHANNEL_TEMPLATE\` on 2026-05-04. **Audience pivoted 2026-05-07 evening from mid-career devs to general consumer.** This is the SINGLE SOURCE OF TRUTH for ShadowVerse voice, format, and constraints.
>
> The pipeline injects this verbatim into every script-generation prompt as the niche style guide. Edits here propagate automatically.
>
> Anything tagged `<<TODO>>` needs your input before launch.

---

## Niche

NICHE: AI tools, AI agents, consumer-facing AI products (ChatGPT, Claude, Gemini, etc.), AI-related news. Faceless short-form videos that anyone curious about AI can enjoy.

TARGET VIEWER: General consumer who is curious about AI but does NOT code. The "Joe Schmo who uses ChatGPT to write a wedding speech" persona — they've heard of AI agents but don't know the difference between an LLM and an API. They watch Shorts to be entertained and informed in 30–55 seconds. **A regular viewer must be able to understand each video without any technical background.**

## Voice

TONE: A friend telling you a surprising story they heard about AI. Curious, slightly contrarian, fun. Not a senior engineer giving a tactical demo. If it's a hot take, own it. If something's wild or counter-intuitive, let the script feel that way.

VOCABULARY LEVEL: **Laymen vocabulary only.** Assume the viewer knows what ChatGPT is and that AI can write/draw/code, but assume nothing else. Words a regular ChatGPT-using consumer wouldn't recognize must either be (a) cut, (b) replaced with a plain-English synonym, or (c) briefly explained the first time they appear (≤6 words of explanation).

**Banned without explanation** (cut or rephrase): CLI, API, repo, env var, IDE, refactor, lint, MCP, agent framework, prompt engineering, fine-tune, embedding, regex, runtime, dependency, package manager, frontend, backend, middleware, deployment, container, kernel, syntax, compiler.

**Plain-English swaps:**
- "CLI" → "command" / "the chat box"
- "refactor" → "rewrite cleanly"
- "MCP" → "the connection it uses to fetch data"
- "agent" → "AI assistant that acts on its own"
- "model" (LLM sense) → "AI"
- "prompt" → "the instruction you give the AI"
- "API" → "behind-the-scenes connection" (or just cut it)

**Test:** read the draft to a non-coder. If they say "what does that word mean?" — rewrite that line.

## Forbidden patterns

- Generic "Did you know..." or "Most people don't realize..." openers
- "Top 10 / Top 5 / 7 things" listicle framing
- "AI is changing everything" / "the future of X" / "in 2026 you need to..." filler
- Listicles without a strong opinion attached
- Em-dashes ( — ) — use commas or periods instead (em-dashes are the #1 LLM-prose tell)
- Phrases that any other AI/tech channel could publish verbatim with the same prompt
- Vague claims like "studies show," "experts say," "research finds" without naming the source
- Hyperbole that requires backing data ("game-changing," "revolutionary," "10x faster") unless followed by the actual measurement
- **Any beat that requires the viewer to already know dev concepts to land.** If a non-coder can't follow it as audio-only, rewrite.

## Signature patterns (your unique elements)

- **One specific concrete detail** in every video — a name, a number, a date, a result. ("Anthropic shipped this Tuesday." "It cost five dollars per run." "It wrote the code in twelve seconds.") Specificity beats vagueness even for general audiences.
- End with a **one-line surprise or trade-off** when describing a tool. ("Faster than the old way. Costs more in tokens." "It works — but only if you ask it the weird way.")
- When citing a tool / product, **name the version and the date.** ("Claude 4.7, tested this week.") Even general audiences trust specificity.
- Show a **before-state for at least 1 second** before the after-state on demo beats. The contrast IS the story.
- One **cited observation per video**: a specific person describing what they saw / tried / measured / had happen, with a retrievable URL or named handle. Always attributable. Source can be a Reddit post, forum thread, vendor blog, X post from a known account, or news article. Never "in theory" and never an anonymous "a developer says" or "a user reports".
- **Named-human-in-the-first-8-words is now mandatory** for any topic where a third-party observation exists (set 2026-05-12 post-`/start -auto` cycle 3 audit). Confirmed across 4 of the top 5 highest-volume videos: `_07_004` (lreeves on HN), `_07_005` (Mark Gurman / Bloomberg), `_10_001` (Tim Gowers / Fields medalist), `_10_002` (Givi Beridze / Klipy CEO) — all carry a named human in the title or first beat. The day-of-release first-party vendor exception still applies, but it's a floor for views, not a ceiling: schedule a fresh third-party observation pull within 24h of any day-of vendor topic to upgrade the citation from first-party to third-party.

## Audio-first principle

The script must work as audio-only. A viewer listening with the screen off should be able to follow the entire video. Visual is reinforcement, not load-bearing.

**Test:** read the script aloud without looking at the B-roll cues. Does it still make sense as a story? If a beat REQUIRES the visual to land, rewrite the line so the verbal carries the meaning and the visual just illustrates it.

This is why scripts must be **complete narratives**, not "watch what happens here" voice-overs.

## Storytelling shape

The narrative arc is **setup → unexpected twist → reveal/punchline**, not "claim → evidence → CTA."

- **Setup (0:00–0:08):** what's the situation? Name the AI tool / product / event in a way a non-coder grasps in 2 seconds.
- **Twist (0:08–0:20):** something surprising happened. The reveal. The "wait, what?" moment. This is also the second-hook landing zone.
- **Payoff (0:20–end):** the consequence, the implication, or the takeaway for the viewer. End on a one-line surprise or trade-off, then CTA.

Tone: feel like a friend retelling a wild AI story they read this morning. Not a technical explainer.

## Format constraints

- **Length:** 95–125 words, ~38–50 seconds spoken at ~151 wpm via edge-tts `AndrewMultilingualNeural` at `rate: +10%` (current config; calibrated 2026-05-07 after the gate-3 review of `2026-05-07_003`/`_004` flagged +0% pacing as slightly slow — +10% lifts the effective wpm from the prior 137 baseline to ~151, mid-tier Shorts cadence). Re-measure and update this line if the TTS provider, voice, or rate changes.
- **Hook duration:** first 1.5 seconds (about 4 words) must hook hard, AND must be understandable to a non-coder
- **B-roll cue cadence:** every 1–2 sentences include a `[B-ROLL: short visual cue]` tag. Target one cue per 6–8 spoken words so the rendered video has visual change every 1.5–2.5 seconds (tighter than the prior 2–3s — operator feedback 2026-05-07 evening: "B-roll cuts; not long story with same stock image").
- **CTA style:** rotate across these three to avoid templating —
  1. "Save this — share it with the AI-curious friend in your group chat."
  2. "Comment [keyword] and I'll send you the link."
  3. "Follow for one wild AI story a day."

## First 10 seconds

The opening hook + a second hook at 0:08–0:10. 24-hour analytics from the 2026-05-05 batch showed an attention cliff at 13–15s on both videos — viewers are deciding whether to stay JUST before that mark. The opening hook gets them in; the second hook bridges them across the cliff.

### Opening hook (0:00–0:03)

Research-backed: a layered hook that aligns visual + on-screen text + verbal narration in the first 0.5–1 second produces ~3x better 3-second hold than a single-element hook. Every video must hit all three layers.

**The 3-part stack:**
1. **Verbal hook** — 5–10 spoken words. The first 4 words must contain the punch (a negation, number, mid-action verb, or named handle) AND must be plain-English (a non-coder gets it instantly). This is the line annotated `[formula: <Name>]` in `script_FINAL.txt`.
2. **Visual hook** — the very first `[B-ROLL: ...]` cue. A result, mid-action, comparison split, or unexpected screen state. Not a generic shot of a tool's logo or homepage.
3. **Text overlay** — 3–7 words on-screen for 1–2 seconds, echoing the verbal claim. The captions stage produces this from the verbal hook automatically.

All three layers must reinforce the SAME claim, not three different claims.

### Second hook (0:08–0:10)

Lands BEFORE the 13–15s attention cliff observed in the 2026-05-05 batch. Bridges viewers across the drop point. Also serves as the **twist** in the storytelling arc.

- **Hard visual change** — cut to a different B-roll context. Not a zoom or pan; a genuinely new scene (different setting, different tool, different perspective).
- **Verbal twist** — curiosity gap, contradiction to the opening claim, or a "but here's the thing" pivot. Reframes what the viewer thought the video was about. Must still be plain-English.
- **Text overlay** — 1–2s on-screen, 3–7 words, reinforcing the twist.

**Why:** 24-hour analytics from the 2026-05-05 batch showed both videos cliffed at 13–15s. The second hook must land before that to bridge the drop.

### First-10-seconds checklist

Opening hook (0:00–0:03):
- [ ] First 4 words contain the punch (negation, number, mid-action verb, or named handle)
- [ ] First 4 words are plain-English; a non-coder grasps the topic in 2 seconds
- [ ] First `[B-ROLL: ...]` cue is the visual hook (a result, mid-action, or split-comparison frame — not a generic shot)
- [ ] Text overlay 1–2s, 3–7 words, echoes the verbal line
- [ ] Hook formula name annotated `[formula: <Name>]` per `prompts/library/viral_hooks.md`
- [ ] None of the forbidden hook openers from §"Forbidden patterns" present

Second hook (0:08–0:10):
- [ ] Lands at 0:08–0:10, before the 13–15s attention cliff
- [ ] Hard visual change (new B-roll context, not zoom/pan)
- [ ] Verbal twist: curiosity gap, contradiction, or "but here's the thing" pivot — plain-English
- [ ] Text overlay 1–2s, 3–7 words, reinforces the twist

For formula choices and "first 4 words" patterns, see `prompts/library/viral_hooks.md` (formulas annotated for general-audience fit).

## Visual cadence

Visual change every 1.5–2.5 seconds when rendered. Tightened from the prior 2–3s target on operator feedback 2026-05-07 evening: "B-roll cuts; not long story with same stock image. Moving parts (image/visual)."

**Targets:**
- **B-roll cue density:** 16–24 cues per 130-word script (one cue per 6–8 spoken words). Stage 1.5's `broll_cadence` rubric should reflect this density.
- **Cue spacing:** every 1–2 sentences must include a `[B-ROLL: short visual cue]` tag.
- **Motion bias:** prefer cues with movement — typing hands, screen scrolling, video playback, person reacting — over static screenshots. The operator's directive: visuals should have moving parts.
- **Workflow / before-after demos:** show the before-state for at least 1 second before cutting to the after-state (cross-references §"Signature patterns").

**Anti-patterns:**
- Long static screen with no visual change for 5+ seconds — kills retention even when the audio is dense.
- Cuts every <1 second across the whole video — feels frantic and burns the cadence reserve, leaving no contrast for the climax.
- Generic cues ("screenshot of the editor") instead of specific ones ("phone screen with ChatGPT writing a wedding speech, hands typing").
- Using the same stock clip more than twice in a 50-second video.

## Title hygiene

Data-driven rules from the 2026-05-05 analytics batch. Title formatting is a major reach lever — same channel, same day, a hashtag-stuffed title got ~5× less reach than a clean one (1108v vs 229v at 24h). Reinforced 2026-05-07 evening pivot: titles must also be understandable to a non-coder.

**Rules:**
- **No hashtags in the visible title.** Hashtags go in the description only. Hashtag-stuffed titles in the 2026-05-05 batch ran ~5× behind clean titles on identical-day publishes.
- **Default title format: catchy single-sentence narrative.** Subject-led declarative sentence with a verb of motion or intrigue, no colon, no name-prefix. Reads like a curious friend's discovery, not a journalist's lede. Set 2026-05-18 by operator feedback on cycle-11 — "Luke Lanchester: AI's internet is rude to humans" was rejected as boring; "AI quietly built itself a parallel internet" was the operator-supplied better version (catchy, interesting, sounds fun, click-worthy). Verbs that work: quietly built / just shipped / secretly added / accidentally taught / outright refused / quietly told / just dropped / now refuses / just learned. The named subject is still the grammatical subject of the sentence (e.g., "AI", "Anthropic", "Claude", "ChatGPT") — just without the colon-template scaffolding.
- **Colon shape `[Subject]: [contrarian payoff]` is OPTIONAL fallback**, not the default. Use it only when the colon genuinely sharpens the contrast (e.g., named human as the surprising payoff: "ChatGPT 5.5 Pro shocked a Fields medalist." at 1163v worked because the medalist IS the payoff). When in doubt, prefer the catchy single-sentence form.
- **Named subject must still appear in the title's first half** (the grammatical subject for catchy-narrative form, or left of the colon for the colon fallback; set 2026-05-12 post-cycle-3 audit, restated 2026-05-18). Examples that worked: "Cursor's background agents: the win nobody mentions" (named tool, 1124v), "ChatGPT 5.5 Pro shocked a Fields medalist." (named tool + named expert, 1163v), "Aider 0.86: three frontier models, one CLI" (named tool + specific number, 510v). Examples that flopped: "Your AI got downgraded" (no named subject, 73v confirmed flop). Rule confirmed across all winners in the 2026-05-12 cohort audit.
- **Plain-English check:** if a regular ChatGPT-using consumer can't tell what the video is about by reading the title in 2 seconds, rewrite. Replace dev-jargon (CLI, refactor, MCP, lint, etc.) with consumer-facing language.

**Working examples:**
- "AI quietly built itself a parallel internet" — operator-supplied 2026-05-18 as the canonical example of the catchy-narrative default. Subject ("AI") + motion verb ("quietly built") + surprising payoff ("a parallel internet"). 47 chars. Plain-English clear, no colon, reads like a curious friend's discovery.
- "Cursor's background agents: the win nobody mentions" — 1108v at 24h. ✅ Colon shape worked here because "background agents" is the named subject and "the win nobody mentions" is a strong contrarian payoff. Clean title, curiosity gap, intriguing without dev background.
- "ChatGPT 5.5 Pro shocked a Fields medalist." — 1163v. ✅ Colon-shape variant — named tool + named human as the payoff itself; the human IS the surprise.

**Anti-patterns (forbidden):**
- `[X] did [Y]. The [jargon] delta. #tag #tag` — cost ~5× reach in the 2026-05-05 batch (229v vs 1108v same day).
- Dev-jargon in the title — `Aider 0.86 model arbitrage`, `ruff #ruff:file-ignore`, `.cursor/rules folder` — these tank discovery for general audience.
- **Formal `[Named-Human]: [payoff]` colon template when the human is just the source, not the surprise.** Set 2026-05-18 post-cycle-11 operator rejection. "Luke Lanchester: AI's internet is rude to humans" reads as a journalistic byline, not a friend's discovery — boring per operator. The fix is to drop the human's name from the visible title (they still go in the spoken hook for the named-human-in-first-8-words audio rule) and lead with the AI / tool / event in a catchy single sentence: "AI quietly built itself a parallel internet."

This section is the post-launch-data refinement of § "Title constraints" below — that section gives broad style rules; this one gives the data-driven hygiene that overrides anything in conflict.

## Title constraints

- **Max length:** 60 chars (YouTube Shorts truncation point on mobile)
- **Pattern preferences:**
  - Specific tool / product names ("Claude 4.7's hidden mode")
  - Strong-claim format ("Stop pasting essays into ChatGPT. Do this instead.")
  - Outcome-led ("The AI that wrote a 10-page report in 30 seconds.")
- **Patterns to avoid:**
  - "Top N" or "N things" formats
  - "Did you know" / "You won't believe" / "This will blow your mind"
  - Generic ALL-CAPS clickbait
  - Emojis in the title (looks like AI-template output)
  - Dev-jargon (see § Title hygiene plain-English check above)

## Hashtag strategy

**Updated 2026-05-07 evening:** since titles are hashtag-free per § Title hygiene, descriptions are now the channel's primary hashtag discovery surface. Bulked the YouTube hashtag count from 3–5 up to 10–12.

- **Title:** zero hashtags (per § Title hygiene). Anything in the visible title beyond plain text tanks reach.
- **YouTube description: 10–12 hashtags at the bottom.** YouTube ignores ALL hashtags if you exceed 15 — keep under 13 to be safe.
- **Order matters.** The first 3 hashtags from the description render above the title as clickable chips on YouTube Shorts. Treat slots 1–3 as discovery slots, not afterthoughts.
- **YouTube Shorts stack (general-audience):**
  - **Slot 1 (mandatory):** `#Shorts` — required so YouTube categorizes it for the Shorts shelf
  - **Slot 2 (mandatory):** `#AI` — broad-discovery anchor
  - **Slot 3 (per-video, the strongest match):** the most search-relevant consumer-AI tag for THIS video — `#ChatGPT` / `#Claude` / `#Gemini` / `#AIagents` / `#AInews` / `#OpenAI` / `#Anthropic`
  - **Slots 4–12:** mix-and-match from the canonical buzz-word pool (only relevant ones; don't pad off-topic):
    - Vendor: `#ChatGPT` `#Claude` `#Gemini` `#OpenAI` `#Anthropic` `#GoogleAI` `#GPT5`
    - Product: `#AIagents` `#AItools` `#AIapps` `#AppleIntelligence` `#iOS27` (when topical)
    - Broad: `#AInews` `#ArtificialIntelligence` `#FutureTech` `#TechNews` `#Tech`
- **No URLs in description.** YouTube blocks clickable links in descriptions until a channel hits the verification threshold (typically driven by sub count + watch hours + channel age — ShadowVerse hasn't hit it as of 2026-05-07). Cite sources by NAME, not URL: "Source: Bloomberg via 9to5Mac" rather than `https://9to5mac.com/...`. Re-evaluate this rule once the channel passes verification.
- **Per-platform tweaks:**
  - TikTok: 4 hashtags total, mix one broad (`#TechTok`) with three niche consumer-AI
  - Instagram: 5 hashtags total, mix product-specific + broad-tech
- **Avoid:** `#fyp` `#viral` `#trending` `#explorepage` `#shortsfeed` — these signal slop and don't help reach in 2026.
- **Avoid dev-only tags** (`#DevTools` `#Python` `#PromptEngineering` `#Linting`) — they pull discovery into a small audience and conflict with the general-consumer pivot.

## Thumbnail brand constants

Locked across all thumbnail patterns so the channel reads as ONE brand at a glance. These match `tools/make_channel_art.py` and the existing metadata bundles. Stage 8.5's `generate_thumbnail` stage renders thumbnails automatically when the metadata's COVER section names a `Pattern:`.

**Locked constants:**
- **Background:** dark slate `#0B0F1A`
- **Accent:** violet `#7C5CFF`
- **Text color:** near-white `#F5F5FA`
- **Primary font:** Segoe UI Bold (`segoeuib.ttf`, bundled on Windows)
- **No emojis** in thumbnails (style-guide ban applies here too)
- **0–3 words of text maximum**, top-left placement (87% of top-CTR thumbnails in 2025–2026 do this)
- **9:16 dimensions** (1080×1920) with important content inside the center 720×1280 for cross-surface crop safety
- **Plain-English text overlay** — same rule as titles. A non-coder must grasp the thumbnail text instantly.

**Pattern selection:**
- 8 named patterns defined in `prompts/library/thumbnail_patterns.md`
- Default for the channel is `big_text_claim` (Fireship-flavored: dark-slate + 1–3 words massive bold)
- Hook-formula → thumbnail-pattern default pairings live in that library's "Pattern selection by hook formula" table
- The metadata-gen prompt's COVER section produces the `Pattern:` field; if absent the renderer falls back to `big_text_claim`

## Sources & citations

The pivot to general audience broadens the acceptable source list — we still need retrievable named sources, but the "named developer on a dev forum" rule relaxes to "any retrievable named source on a public surface."

- **Always cite when discussing:**
  - Specific benchmark numbers (latency, accuracy, token cost)
  - Version numbers and release dates
  - Security or privacy claims about a tool
  - Pricing claims (these change, always link to vendor pricing page)
- **Acceptable primary sources for technical claims:**
  - Official vendor docs, blog posts, GitHub releases
  - The tool's own release notes / changelog
  - First-party benchmarks (vendor or model-card)
  - Peer-reviewed papers (arXiv preprints OK if labelled as such)
  - Mainstream tech press (The Verge, Ars Technica, TechCrunch — fine as primary for AI news; not for technical benchmark claims)
- **Acceptable sources for the cited observation per video:**
  - Reddit posts on AI / consumer subreddits (r/ChatGPT, r/ClaudeAI, r/Bard, r/OpenAI, r/singularity, r/artificial, r/LocalLLaMA, r/AIart, etc.) with the post URL and username
  - Forum posts on official tool forums (community.openai.com, forum.cursor.com, GitHub Discussions, etc.) with the post URL
  - Hacker News comments and submissions, linked by item URL
  - X (Twitter) posts from named accounts with verified context (a known builder, journalist, vendor employee, or researcher) — link the post; quote verbatim
  - Public blog posts, dev-blog posts, or news articles where the author shares a measured / tried / observed result, linked
  - Quotes must be attributable to a named handle or human, not "a user" or "a developer"
- **Day-of-release exception:** When a topic covers a feature shipped within the last 24h and no third-party observation exists yet, first-party release notes / official changelog / vendor blog are acceptable as the cited observation, provided the script credits the vendor explicitly ("Anthropic's release notes say…", "OpenAI's blog post notes…") rather than implying a person tried it. Operator must flag the exception in `GATE_3_PREP_NOTES.md` for that topic. Precedent: `2026-05-06_003` (Claude Code `/mcp` zero-tool detection).
- **Sources to avoid as primary:**
  - Random Twitter/X threads (acceptable as secondary if the author is a known person with verified context)
  - AI-summarized blog posts
  - Aggregator sites used for technical benchmark claims (fine for AI-news topics)
  - Wikipedia for contested or recent claims
  - Quotes that are no longer publicly retrievable (deleted comments, gated content)

## Disclaimers required

- [x] AI-assisted production note (always — channel-level "About" + per-video description footer)
- [ ] Educational, not advice (only if a video drifts into financial / legal / medical territory — should be rare for ShadowVerse; if a video does, check this and add the verbal disclaimer)
- [x] Affiliate disclosure (every video that includes an affiliate link in description or bio — short form: "Some links are affiliate. Doesn't change my recommendation.")
- [ ] Other: <<TODO: add anything niche-specific you discover during compliance review>>
