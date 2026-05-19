# Viral hook library — ShadowVerse

A reference deck of hook formulas extracted from the 2026-05-06 competitor audit and the 2026 hook-research literature. Use when generating HOOK_A / HOOK_B / HOOK_C in `prompts/03_script_generation.md`.

**Rule of thumb:** the three hook variants for any topic should use THREE DIFFERENT formulas from this library, so we can test which one wins for that topic. Don't write three contradiction hooks; write one contradiction + one number-promise + one mid-action.

> **Audience pivot (set 2026-05-07 evening):** the channel target viewer is now a general consumer curious about AI, not a developer. Every formula below has been re-annotated for general-audience fit. Examples have been refreshed where the dev-flavored ones would land flat for non-coders. Use the "General-audience fit" line under each formula to decide whether to use it for a given topic.

---

## The 3-part stack rule (applies to every hook below)

Research-backed: a layered hook that aligns visual + on-screen text + verbal narration in the first 0.5–1 second produces ~3x better 3-second hold than a single-element hook. So for every hook, also produce:

- **Visual hook:** what the very first frame shows (a B-ROLL cue — a result, an action mid-stride, a comparison split, an unexpected screen state). Prefer general-audience visuals (person on phone, ChatGPT on screen, AI-generated image) over dev-themed visuals (terminals, code editors).
- **Text overlay:** 3–7 words echoing the verbal claim, on-screen for at least 1–2 seconds. Plain English.
- **Verbal hook:** 5–10 words spoken (this is the line we mark HOOK_X). Plain English — a non-coder grasps it in 2 seconds.

If the topic prompt only asks for the verbal line, that's fine — the script_FINAL stage's first B-ROLL cue is the visual hook, and the captions stage produces the text overlay. But the THREE elements should reinforce the same claim, not three different claims.

---

## Named-human carry rule (set 2026-05-12 post-cycle-3 audit)

Mandatory addendum to every hook: **a named third-party human must appear in the first 6–8 words** whenever a third-party observation exists for the topic. Confirmed across 4 of the top 5 highest-volume videos on the channel (Aider 510v / lreeves on HN, iOS 27 341v / Mark Gurman / Bloomberg, ChatGPT 5.5 Pro 1163v / Tim Gowers / Fields medalist, Gemini multimodal 258v / Givi Beridze / Klipy CEO). Vendor-only sourcing correlates with mid-tier views; anonymous "a developer says" / "a user reports" tanks discovery.

**Of the 11 formulas below, only formula #8 (The Cited-Observation Lead) natively carries a named human in its structure.** For the other 10, the writer must explicitly inject a named human into the first 6–8 words even though the formula doesn't require it. Examples:

- Contradiction (#1) — **without** named human: "Most people use Claude wrong." → **with**: "Reddit user u/foo: most people use Claude wrong."
- Specific-Number Promise (#2) — **without**: "ChatGPT wrote a 10-page report in 30 seconds." → **with**: "Karpathy: ChatGPT wrote my report in 30 seconds."
- Result-First Mid-Action (#3 or #11) — **without**: "Claude wrote a wedding speech that made the bride cry." → **with**: "Bride's brother Adam: Claude wrote a speech that made her cry."
- Comparison Frame (#4) — **without**: "ChatGPT vs Claude vs Gemini, one gave a wrong answer." → **with**: "a16z's Justine Moore tested all three. Only Sora 2 nailed it."

Day-of-release exception (per `style_guide.md` § Sources & citations) still applies — first-party vendor sources are acceptable for releases <24h old, but a third-party observation upgrade should be queued for the next render pass. The `scoring.py` `named_human_bonus` regex pattern adds +0.05 to a candidate's weighted_total when any of the topic / angle / hook / cited-observation source_handle text matches a named-human signal (Reddit handle, X handle, "named <Person>", titled CEO/researcher reference, or known-figure allowlist).

---

## Hook formulas

### 1. The Contradiction Hook (Theo / Fireship signature)

**Pattern:** State the conventional wisdom, then immediately negate it. Or claim something everyone "knows" is true is wrong.

**First 4 words:** must contain the negation or surprise.

**General-audience fit:** ✅ Strong, when the contradiction is in plain English (no dev jargon).

**Examples (general-audience):**
- "ChatGPT is great at writing. Bad at the one thing you'd expect."
- "Most people use Claude wrong. The fix takes one sentence."
- "Gemini is supposed to know everything. It still gets THIS wrong."
- "Stop pasting essays into ChatGPT. There's a setting for that."

**When to use:** any AI topic where the audience holds a default assumption you can puncture. Especially good for vendor news where "most people miss this" is true.

**When NOT to use:** when the contradiction is forced or fake — viewers detect bait-and-switch and the algo punishes (high CTR + immediate swipe). Also avoid when the contradiction requires dev knowledge to land.

**Source channels:** Theo (t3.gg), Fireship's "Code This Not That", Itssssss_Jack.

---

### 2. The Specific-Number Promise (Nate Herk signature)

**Pattern:** Lead with a specific, surprising measured number. Bonus if the number is contrarian to expectation.

**First 4 words:** a number, a unit, and a verb.

**General-audience fit:** ✅ Strong. Numbers are universal — work for any audience.

**Examples (general-audience):**
- "ChatGPT wrote a 10-page report in 30 seconds."
- "Claude can read 200,000 words in one go. Most people use 200."
- "GPT-5 takes 3 seconds to do what GPT-4 took 30."
- "An AI agent answered 87 emails before I had coffee."

**When to use:** when you have an actual measurable claim (yours or a cited source's) with a real number. Cited-observation rule applies — link the source.

**When NOT to use:** if you have to invent the number or hedge it ("about 3x faster maybe"). Vague numbers underperform vague language.

**Source channels:** Nate Herk, Itssssss_Jack, Fireship's measured benchmarks.

---

### 3. The Result-First Mid-Action (Fireship signature)

**Pattern:** Cut directly to the WIN. No "in this video", no setup. The first sentence describes what happened, not what the video is about.

**First 4 words:** an action verb in past or present tense, applied to a specific AI product / outcome.

**General-audience fit:** ✅ Strong, when the action is in plain English. This is the formula that powered `_05_001` Cursor agents (1114v).

**Examples (general-audience):**
- "An AI agent finished my Tuesday work while I made coffee."
- "Claude wrote a wedding speech that made the bride cry."
- "ChatGPT booked a flight, hotel, and rental car in one prompt."
- "The new AI just answered a 200-page legal contract in plain English."

**When to use:** when there's a single dramatic outcome the video is about. Especially good for "I tried X" style or vendor demo write-ups.

**When NOT to use:** for topics that are conceptual rather than action-bound (e.g., "should you use Claude or ChatGPT" doesn't have a single mid-action).

**Source channels:** Fireship's 100 Seconds of Code, AI Luke's build-alongs.

---

### 4. The Comparison Frame (Skill Leap AI signature)

**Pattern:** Two or three named AI tools, head-to-head, with a winner declared in the first beat.

**First 4 words:** "X vs Y vs Z" or "X beats Y at Z."

**General-audience fit:** ✅ Strong — Claude vs ChatGPT vs Gemini is a topic every casual AI user is curious about.

**Examples (general-audience):**
- "ChatGPT vs Claude vs Gemini. One of them gave us a wrong answer with full confidence."
- "I asked Claude and ChatGPT the same question. Only one apologized."
- "GPT-5 vs Gemini at writing. Winner is the one nobody expected."
- "Free ChatGPT vs paid ChatGPT. The gap is bigger than they tell you."

**When to use:** when a topic has natural competitors and you can declare a winner with evidence. Especially viral when a non-obvious tool wins.

**When NOT to use:** when the comparison is unfair (different categories), or when "it depends" is the actual answer (audience smells the cop-out).

**Source channels:** Skill Leap AI, Fireship's framework comparisons, Theo's tool tier-lists.

---

### 5. The Anti-Pattern Setup (Fireship "Code This Not That" signature)

**Pattern:** "Most people do X. Here's why it's wrong, and what to do instead." Lead with the bad pattern; the rest of the video is the correction.

**First 4 words:** "Most people do X" or "Stop doing X." **NOTE post-pivot:** the original Fireship phrasing was "Most devs do X" — for ShadowVerse's general-audience pivot, use "Most people," "Most ChatGPT users," "Most of you," etc. Avoid "Most devs."

**General-audience fit:** ✅ Strong with the language pivot above.

**Examples (general-audience):**
- "Most people ask ChatGPT for the answer. There's a smarter way."
- "Most ChatGPT users still copy-paste essays. Try this instead."
- "Stop telling AI to 'write better.' Here's what actually works."
- "Most people skip Claude's projects feature. Your chats regret it."

**When to use:** when you have a fix that the audience is doing wrong. The CTA pattern "Most do X / I do Y / here's why" is a strong narrative spine.

**When NOT to use:** when you can't actually back the "most do X" claim — viewers will test it. Cite a forum post or named source.

**Source channels:** Fireship's "Code This Not That", Theo's hot takes, Corbin Brown's "common mistakes" framing.

---

### 6. The Specific-Question Hook

**Pattern:** A pointed question the target viewer has but hasn't articulated. The video answers it.

**First 4 words:** "Why does X Y" or "What if X."

**General-audience fit:** ✅ Strong, when the question is something a casual ChatGPT user would naturally ask.

**Examples (general-audience):**
- "Why does ChatGPT lie about things it knows?"
- "What if you let an AI run your inbox for a week?"
- "Why is Claude so much better at long documents?"
- "What does the new AI agent feature actually do?"

**When to use:** when the topic is an explainer (mechanism / why-it-works) and the audience is curious enough to have noticed the question themselves.

**When NOT to use:** if the question is so basic it patronizes the audience ("What is AI?") OR so technical it loses them ("What is the context window?"). Pick a question a non-coder genuinely asks.

**Source channels:** Two Minute Papers, Kurzgesagt, Beyond Fireship.

---

### 7. The Measured-Claim Hook

**Pattern:** A specific tool / version stamp + measurable result + one-line surprise compressed into the first sentence.

**First 4 words:** named AI product + state-change verb.

**General-audience fit:** ✅ Strong — version names (Claude 4.7, GPT-5) are recognizable to general audiences as "the new one."

**Examples (general-audience):**
- "Claude 4.7 just got a feature that reads your screen. The catch is in the privacy fine print."
- "GPT-5 added a memory feature this week. It already remembers things you wish it didn't."
- "OpenAI shipped Sora 2 this morning. The first thing it generated was a viral fake."
- "Gemini 2.5 added voice mode. The voice is creepy in a specific way."

**When to use:** when the topic is version-specific and the audience cares about the exact stamp ("the new one"). Strong on credibility — signals "this is fresh news, not a re-hash."

**When NOT to use:** when you don't have a date-stamped test. Style guide requires version + date stamps for technical claims.

**Source channels:** Fireship's release reports, Skill Leap AI's day-of reviews.

---

### 8. The Cited-Observation Lead (ShadowVerse-specific brand pattern)

**Pattern:** Open by paraphrasing what a named person said about an AI tool, with the source on screen. The body of the video then explains why they're right (or wrong, with evidence).

**First 4 words:** named handle / forum / community + verb.

**General-audience fit:** ✅ Strong — broaden source pool to general AI subs (r/ChatGPT, r/ClaudeAI, r/Bard, r/OpenAI), AI X/Twitter accounts, news bylines, and vendor blog authors.

**Examples (general-audience):**
- "On r/ChatGPT, u/airider said GPT-5 wrote his vows in 30 seconds. They're better than the ones he wrote himself."
- "An OpenAI engineer admitted on X that the new memory feature surprised even them."
- "An Anthropic blog post quietly notes Claude can now summarize a 200-page PDF in one shot."
- "A Verge reporter spent a week with the new AI agent. She let it cancel her gym membership."

**When to use:** the opinionated path — channel-borrow strong claims from real people without first-person fabrication. Higher retention than neutral framing because it's a real human's experience.

**When NOT to use:** when the source is anonymous or unverifiable. Style guide forbids "a user says..." framings.

**Source channels:** none of the audited channels do this verbatim — this is our brand-differentiator.

---

### 9. The Format-Branded Hook (Fireship's 100-Seconds derivative)

**Pattern:** A consistent format-branded opening that audiences come to expect. "60 seconds on X." "AI Daily #N." Builds predictable-value pattern recognition the algorithm rewards.

**First 4 words:** the format brand + the topic.

**General-audience fit:** ✅ Strong — format branding works for any audience.

**Examples (proposed brand options for ShadowVerse — pick ONE if we adopt this):**
- "60 seconds on [AI product / news event]." *(closest to Fireship's 100 seconds)*
- "AI Daily #N: [topic]."
- "The [AI tool] thing nobody told you about."

**When to use:** as a SECONDARY hook style for ~30-50% of videos to build channel recognition. Don't use on every video — variety matters too.

**When NOT to use:** if the topic is too big for the format claim (don't promise "60 seconds on AGI safety").

**Source channels:** Fireship's 100 Seconds of Code, Two Minute Papers' fixed cadence.

---

### 10. The "You're Doing It Wrong" Hook

**Pattern:** Tell the viewer that the way they're already using an AI tool is the inferior way. Implies a fix is coming. Counter-conventional pull — viewers stay because they want to know if they're the one doing it wrong.

**First 4 words:** "You're using X wrong" or "Stop X-ing your AI" — second-person, accusatory verb.

**General-audience fit:** ✅ Strong. Plain-English, audio-first compatible (works eyes-closed — the accusation lands on tone alone), and it's a natural setup → twist → payoff: setup is the accusation, twist is the reason, payoff is the fix.

**Examples (general-audience):**
- "You're asking ChatGPT the wrong way. One word changes the whole answer."
- "You're using Claude like a search engine. It can do something way weirder."

**When to use:** when the topic teaches a tip, trick, or smarter pattern that most casual AI users haven't tried yet (cited-observation rule applies — if the script wants to quote a specific adoption percentage, it must trace to a retrievable source, not be invented). Pairs especially well with the second-hook landing zone — drop the accusation at 0:00, drop the reason at 0:08–0:10.

**When NOT to use:** when you can't actually back the claim that the audience is doing it wrong (cited-observation rule still applies). Also avoid when the "right way" is too niche to apply to most viewers — the accusation has to feel personal.

**Cadence note:** the second hook lands at 0:08–0:10 with the reveal of WHY the viewer's current approach is wrong (the twist that bridges the 13–15s attention cliff). Verbal twist example: "Here's the catch — Claude actually wants you to give it a job title first."

**Visual-pairing note:** open on a visual of the WRONG behavior (someone typing a generic prompt into ChatGPT, hands hovering, blank-ish answer on screen) for 1.5–2.5 seconds, then hard-cut at the second hook to the RIGHT behavior (same person, same screen, but with a visibly better output). Visual change every 1.5–2.5s after that — keep B-roll cuts tight on the comparison frames.

**Source channels:** Fireship's "Code This Not That" tonal cousin — but flipped from third-person ("Most devs do X") to second-person ("You're doing X"). Skill Leap AI uses a softer version of this pattern.

---

### 11. The Result-First / Mid-Action Hook

**Pattern:** Open with the OUTCOME of what an AI did, not the setup. No "I tried" framing, no "in this video." The first sentence describes a finished result the audience has to back-fill. Distinct from #3 (Result-First Mid-Action, which is action-bound and verb-led) — this one leads with a noun-first OUTCOME or artifact, and the verb arrives after.

**First 4 words:** the result itself (a number, a finished artifact, an outcome quantity), then the AI tool that produced it.

**General-audience fit:** ✅ Strong. Pure laymen-vocab compatible — "50 emails," "a wedding speech," "a 5-minute video" are all things any consumer recognizes. Audio-first works because the result IS the sentence; no visual is required to understand what happened.

**Examples (general-audience):**
- "50 emails written, sent, and sorted — I asked Claude to clear my inbox while I made coffee."
- "A 30-second voice clone of my dad. Gemini did it from one Christmas voicemail."

**When to use:** when the topic centers on a tangible artifact or quantity an AI produced (emails, images, a contract review, a generated video, a cloned voice, a finished trip plan). Especially strong for vendor-demo write-ups and "I let an AI do my X" angles.

**When NOT to use:** when the result is abstract or hard to picture in one phrase ("better answers," "more accurate") — those need formula #2 (Specific-Number Promise) for the number-anchor instead. Also avoid when the result requires a setup sentence to make sense (then it's a #3 mid-action, not a #11 result-first).

**Cadence note:** the second hook lands at 0:08–0:10 with the SURPRISE about the result — the catch, the wrong-thing-it-did, or the "but here's what shocked me" pivot. Verbal twist example: "But three of those emails were to people I'd never met."

**Visual-pairing note:** open on the FINISHED result (the inbox at zero, the generated image, the cloned-voice waveform playing) for 1.5–2 seconds — visual hook IS the artifact. Then cut at the second hook to the moment of the surprise (the weird email, the off-detail in the image, the uncanny moment in the voice clone). Visual change every 1.5–2.5s; prefer moving B-roll (typing, scrolling, audio waveforms) over static screenshots.

**Source channels:** Fireship's release reports lead with the result; Marques Brownlee's tech reveals open on the finished artifact. Distinct from #3 in word order: #3 leads with a verb ("Claude wrote a wedding speech…"), #11 leads with the noun-result ("A wedding speech that made the bride cry — Claude wrote it…").

---

## How `prompts/03_script_generation.md` uses this library

The script-gen prompt should:

1. Read the topic + angle + hook concept from the CLI
2. Pick THREE distinct formulas from this library that fit the topic. **All three must be plain-English (general-audience fit).**
3. Generate one HOOK_X line per formula, each in its formula's "first 4 words" pattern
4. Pass the formula NAME alongside each HOOK so the operator/Claude can compare which formula won post-publish

Implementation note: the script-gen prompt template should reference this file by relative path (`prompts/library/viral_hooks.md`) and the pipeline's `load_prompt` helper should be extended in Phase 3+ to inline-substitute referenced library docs. For now, the human-LLM (us, in chat) reads this library directly when responding to the script-gen prompt.

---

## Forbidden hooks (style-guide echo)

These are explicitly NOT allowed regardless of formula:

- "In this video..."
- "Hey guys / what's up..."
- "Did you know..."
- "Most people don't realize..."
- "AI is changing everything..."
- Generic "Top 10 / 7 things" listicle openers
- Em-dashes in the spoken text
- Anonymous "a user says" / "a developer says" framings
- Hyperbole without backing data ("game-changing", "10x faster" without the actual measurement)
- **Dev-jargon hooks** — "Most devs do X", "the CLI flag", "in the repo", "the API call", etc. The pivot to general audience makes these brand-mismatched and tanks reach.

---

## Sources

Same as `competitor_audit_2026-05-06.md` § Sources, plus:
- [The 3-Second Hook — Terra Market Group](https://www.terramarketgroup.com/digital-marketing-2/short-form-video-hooks-7-formulas-for-70-retention/)
- [18 Viral Hook Ideas — vidIQ](https://vidiq.com/blog/post/viral-video-hooks-youtube-shorts/)
- [Hook Formulas — OpusClip](https://www.opus.pro/blog/youtube-shorts-hook-formulas)
