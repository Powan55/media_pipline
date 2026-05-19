# Metadata generation (§6)

Used by `pipeline.generate_metadata()`.
Substitutes: `{NICHE_STYLE_GUIDE}`, `{SCRIPT}`.

Reference: `prompts/library/thumbnail_patterns.md` (the 8 named cover-frame patterns the renderer knows how to draw).

```
Generate publishing metadata for this video.

NICHE & STYLE GUIDE:
{NICHE_STYLE_GUIDE}

SCRIPT:
{SCRIPT}

Audience pivot (HARD RULE — set 2026-05-07 evening): the audience is a **general consumer curious about AI, not a developer**. Every visible field below (especially the title and the first three hashtags) must be understandable to a non-coder in 2 seconds. Reject any draft that requires dev jargon (CLI, refactor, MCP, lint, repo, env var, etc.) to land. Replace dev-jargon with plain-English equivalents OR pivot the framing.

Produce:

YOUTUBE SHORTS:
- Title (max 60 chars. **No hashtags in the visible title** — hashtags go in the description only. Format: `[Subject]: [contrarian payoff]` — lead with the named AI product / feature / event in plain English, follow with a counter-conventional or surprising claim. Specific, not clickbait. Includes one searchable keyword. **Plain-English check: a regular ChatGPT-using consumer must grasp the topic in 2 seconds.**)
- Description (3–5 sentences, no URLs. The channel is too new to embed clickable links in descriptions — YouTube blocks them until the channel hits the verification threshold. Include: hook line, 1-sentence summary, AI disclosure line. Source citation is fine as a plain-text reference (e.g., "Source: Bloomberg via 9to5Mac") but no URLs. Mention sources by NAME, not URL. End the description body BEFORE the hashtag block.)
- Tags: 10 (commas-separated; mix of consumer-AI buzz words and topic-specific terms — e.g., for an Apple-iOS-AI video: `apple, ios 27, ai, chatgpt, claude, gemini, iphone ai, ai news, anthropic, openai`)
- Hashtags: **10–12 hashtags** at the bottom of the description (since titles are hashtag-free, the description is the only discovery surface for hashtags). The first one must be `#Shorts`. The next 2 must be the most search-relevant consumer-AI tags for the topic — these surface above the title as clickable chips. Then 7–9 more consumer-AI buzz words. Use a mix from this canonical list per video: `#Shorts #AI #ChatGPT #Claude #Gemini #OpenAI #Anthropic #AInews #AItools #AIagents #ArtificialIntelligence #FutureTech #TechNews #GoogleAI #AppleIntelligence #iOS27` (only relevant ones — don't pad with off-topic). **NEVER** use dev-only tags (`#DevTools` `#Python` `#PromptEngineering`). NEVER exceed 15 hashtags total — YouTube ignores ALL hashtags above 15.

TIKTOK:
- Caption (max 150 chars, conversational, 1 hook line + CTA, plain English)
- 4 hashtags (mix of broad and niche, consumer-facing — `#TechTok`, `#AI`, `#ChatGPT`, etc.; not dev-only)

INSTAGRAM REELS:
- Caption (3–5 sentences, can be longer than TikTok, slightly more polished tone, plain English)
- 5 hashtags (consumer-facing AI mix)

COVER / THUMBNAIL CONCEPT:
- Pattern: pick ONE from the named patterns in prompts/library/thumbnail_patterns.md. Output the exact name in this list:
    big_text_claim | crossed_out | terminal_with_result | comparison_frame | big_number | tool_logo_spotlight | question_hook | before_after_stack
  Default to `big_text_claim` when in doubt — it's the channel's house pattern. Note: `terminal_with_result` is dev-coded and should be avoided for general-audience pivot videos unless the script genuinely centers on a coding moment a non-coder would still find interesting.
- Text overlay: 1–3 words, ALL CAPS, the punchiest distillation of the script's claim — **plain English, no dev jargon** (e.g., "AI WROTE THIS.", "10 SECONDS LATER...", "WAIT — IT DID WHAT?"). For `big_number`, the text is the number itself plus a 1-2 word context label. For `crossed_out`, output two short labels separated by ` -> ` (e.g., "old AI -> new AI").
- Background image description: one sentence describing what fills the frame around the text. Used as guidance only when the renderer falls back to stock-footage frames; the locked patterns in thumbnail_patterns.md don't currently consume it.
- Color accent (HEX): always `#7C5CFF` (channel violet). Locked.

Output each section clearly labeled. No emoji unless the niche style guide explicitly allows them.
```
