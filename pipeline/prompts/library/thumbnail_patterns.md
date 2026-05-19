# Thumbnail patterns library — ShadowVerse

A reference deck of cover-frame patterns that work for tech/AI Shorts, extracted from the 2026-05-06 competitor audit and 2026 thumbnail-CTR research. Will be consumed by the `generate_thumbnail` pipeline stage (Session 2 of the quality build) and by `prompts/06_metadata_generation.md` for the COVER section.

**Where thumbnails matter for Shorts:** they DO NOT control feed delivery (the Shorts feed picks the first frame algorithmically). They DO control click-through from search results, the channel page, playlists, and external embeds. So thumbnails compound long-tail discovery, not initial distribution.

---

## Universal design rules (apply to every pattern below)

Pulled verbatim from 2026 thumbnail-CTR research:

1. **0–3 words of text.** More than 3 creates cognitive friction; A/B testing across niches consistently picks shorter every time.
2. **Top-left text placement.** 87% of top-CTR thumbnails in 2025-2026 use top-left for the primary text element. Mobile viewers scan left-to-right; the top-left lands first.
3. **Bold sans-serif font only.** Recommended: Impact, Bebas Neue, Montserrat Extra Bold, Oswald Bold. Thin / decorative / italic fonts become unreadable at the 96–144px mobile thumbnail size.
4. **2–3 colors maximum** per thumbnail.
5. **High-contrast color pairs.** Research-validated combos: yellow on violet, red on cyan, blue on orange. White text on dark slate is also high-contrast and matches our brand.
6. **One dominant subject.** Either text OR a visual element, not three competing focal points.
7. **Mobile-first sizing.** 68% of mobile viewers decide to click within 1 second; the thumbnail must read instantly at 96×170 pixels.
8. **No exaggerated facial expressions** for tech audiences. Research shows tech/dev/finance audiences increasingly find mouth-open shock-faces off-putting. ShadowVerse is faceless anyway, so this is automatic.
9. **9:16 dimensions for Shorts** (1080×1920 recommended) — but ensure the center 720×1280 area holds all important content, since YouTube crops differently on different surfaces.

---

## ShadowVerse brand constants (set in stone)

These match what's already committed in `tools/make_channel_art.py` and the existing metadata bundles:

- **Background:** dark slate `#0B0F1A`
- **Accent:** violet `#7C5CFF`
- **Text color:** near-white `#F5F5FA`
- **Primary font:** Segoe UI Bold (`segoeuib.ttf`) — already bundled on Windows; matches the channel art
- **No emojis in thumbnails.** Style guide ban applies.

These constants stay locked across all patterns below so the channel reads as ONE brand at a glance, even when thumbnail patterns vary.

---

## Thumbnail patterns

### 1. Big-Text-Claim (Fireship-flavored default)

**Description:** Dark-slate background, 1–3 words of MASSIVE bold text dominating the frame. No imagery. The claim is the entire visual.

**Layout:**
- Top-left third: brand mark (small "S" in violet, ~80px) + tiny `@ShadowVerseTec` handle
- Center 60% of frame: the claim, white text, font size auto-fit to width (typically 200–280pt)
- Optional accent: violet underline beneath the claim, 60% width

**Example claims:**
- "PROMPTS BELONG IN FILES." *(2026-05-06_001 cover, in metadata)*
- "PLAN BEFORE YOU EDIT." *(2026-05-06_002 cover)*
- "PIP IS SLOW. UV." *(2026-05-05_002 cover)*
- "AGENTS RUN. YOU TYPE." *(2026-05-05_001 cover)*

**When to use:** the default for our channel until further notice. Strong-claim hooks pair naturally with strong-claim thumbnails. Our existing 4 metadata bundles all specify this style.

**When NOT to use:** when the topic is fundamentally about a comparison (use pattern 4) or a measured result (use pattern 5).

**Source channels:** Fireship's 100 Seconds of Code thumbnails, Theo's hot-take thumbnails.

---

### 2. Crossed-Out / Negation

**Description:** Big text in two layers — the rejected term with a violet strikethrough, then the recommendation underneath.

**Layout:**
- Top half: `pip` in white, with a violet diagonal strike running through it
- Bottom half: `uv` in white, ~30% larger, with a green check or a violet underline

**Example claims:**
- ~~`pip`~~ → `uv`
- ~~"paste context"~~ → `.cursor/rules/`
- ~~"auto-accept"~~ → `plan mode`

**When to use:** for anti-pattern videos and tool-replacement videos. Maps directly to the "Stop using X / Do Y" hook formula.

**When NOT to use:** when there's no clear "X is bad, Y is good" frame — feels forced if the comparison isn't real.

**Source channels:** Theo's tier-list thumbnails, Fireship's "Code This Not That".

---

### 3. Terminal-with-Result

**Description:** The thumbnail IS a terminal screenshot, slightly stylized. Shows the dramatic command output (the win).

**Layout:**
- Full frame: dark terminal at 80% opacity over dark-slate background
- Bottom 30%: a subtle violet glow / vignette to anchor the brand color
- Top-left: 3-word annotation in white (e.g., "11x FASTER")

**Example annotations:**
- "11x FASTER" over a `uv pip install` finishing instantly
- "PLAN APPROVED" over Claude Code's numbered plan
- "RULES ATTACHED" over Cursor's chat panel showing the auto-attached rule

**When to use:** when the topic has a single dramatic terminal moment that captures the win. Pairs with the Result-First Mid-Action hook.

**When NOT to use:** when the topic is conceptual (no single screen captures it). Don't fake the terminal output — viewers will smell it.

**Source channels:** Fireship's command-line thumbnails, AI Luke's build-along stills.

---

### 4. Comparison Frame

**Description:** Split-screen with two or three named tools, head-to-head, with a winner indicated.

**Layout:**
- Vertical split: left half = tool A, right half = tool B (or three-way thirds)
- Each side: tool name in white bold, ~20-30% transparent screenshot underneath
- Winner side: violet outline / highlight; loser side: muted (50% opacity)

**Example splits:**
- `Cursor` vs `Cline` vs `Continue`, with one violet-outlined
- `pip` vs `uv` vs `poetry`, with `uv` highlighted
- `auto-accept` vs `plan mode`, with `plan mode` violet-outlined

**When to use:** Comparison Frame hook videos. Directly maps to the hook formula.

**When NOT to use:** when the comparison is unfair or you can't pick a clear winner. "It depends" thumbnails don't get clicks.

**Source channels:** Skill Leap AI's comparison thumbnails, Theo's tool tier-lists.

---

### 5. Big Number

**Description:** A single enormous number IS the thumbnail. Caption tells you what the number means.

**Layout:**
- Center: a number in violet, font size 400-500pt, occupying 50% of vertical space
- Below the number: 2-3 words of context in white (e.g., "X FASTER", "TOKEN COST", "MISREADS CAUGHT")
- Top-left: small brand mark

**Example numbers:**
- `11x` with `FASTER` below (uv vs pip)
- `5` with `FILES SAVED` below (plan mode catching a misread)
- `$200` with `TOKEN BILL AVOIDED` below (Cursor spend limit)

**When to use:** when the video has a real measurable number you can stake the claim on. Pairs with Specific-Number Promise hook. Powerful — numbers stop the scroll faster than text.

**When NOT to use:** when the number is invented, vague, or "about". Vague numbers in thumbnails kill credibility.

**Source channels:** Nate Herk's automation-savings thumbnails, Itssssss_Jack's growth-numbers stills.

---

### 6. Tool-Logo Spotlight

**Description:** Recognizable tool logo (Cursor, Claude, OpenAI, Astral, etc.) center-frame, with a 1-2 word reaction.

**Layout:**
- Center: tool logo, ~40% of frame, on dark-slate background
- Above or below: 1-2 word reaction in white bold
- Optional: violet accent ring around the logo

**Example reactions:**
- Cursor logo + "BROKEN."
- Claude Code logo + "PLAN MODE."
- Astral logo + "WHY UV WINS."

**When to use:** when the channel has built up brand recognition (we haven't yet — defer this for ~30+ videos in). Logo-driven thumbnails work when viewers ALREADY associate your channel with takes on those tools.

**When NOT to use:** for our first 30 videos. Without recognition, the logo feels like a sponsored ad rather than an opinion.

**Source channels:** Theo's tool-reaction thumbnails, Skill Leap AI's tool-of-the-day thumbnails.

---

### 7. Question Hook

**Description:** A pointed question fills the frame, no answer shown.

**Layout:**
- Top half: the question in white bold, 2 lines max
- Bottom half: dark slate with a faint violet glow + subtle hint (a glyph, partial screenshot)

**Example questions:**
- "WHY IS uv install FASTER?"
- "WHAT DOES --plugin-url DO?"
- "WHY DOES PLAN MODE WIN?"

**When to use:** for explainer content where the audience genuinely has the question. Pairs with the Specific-Question hook.

**When NOT to use:** if the question is patronizing or so basic it loses the mid-career audience.

**Source channels:** Two Minute Papers, Beyond Fireship's deep-dive thumbnails.

---

### 8. Before / After Stack

**Description:** Vertical stack: top half = before state (bad), bottom half = after state (good).

**Layout:**
- Top half: small terminal/screenshot of the bad state, with a subtle red tint
- Bottom half: same view in the good state, violet-tinted
- Right edge: a tiny "→" or arrow connecting them

**Example stacks:**
- Top: chat panel full of pasted context. Bottom: empty chat panel, `.cursor/rules/` directory in sidebar.
- Top: 5-file diff applied wrongly. Bottom: numbered plan caught the misread.
- Top: `pip install...` half-finished. Bottom: `uv pip install` completed.

**When to use:** for transformational topics where there's a clear before/after. Pairs with the Anti-Pattern Setup hook.

**When NOT to use:** when the "before" state is hard to capture in a single frame.

**Source channels:** Itssssss_Jack's growth-trajectory thumbnails, Nate Herk's workflow-improvement stills.

---

## Pattern selection by hook formula

Default pairings between viral_hooks.md formulas and the patterns above:

| Hook formula | Default thumbnail pattern |
|---|---|
| 1. Contradiction | 1. Big-Text-Claim |
| 2. Specific-Number Promise | 5. Big Number |
| 3. Result-First Mid-Action | 3. Terminal-with-Result |
| 4. Comparison Frame | 4. Comparison Frame |
| 5. Anti-Pattern Setup | 2. Crossed-Out / Negation OR 8. Before / After Stack |
| 6. Specific-Question | 7. Question Hook |
| 7. Measured-Claim | 5. Big Number OR 3. Terminal-with-Result |
| 8. Cited-Observation Lead | 1. Big-Text-Claim with the cited handle as the claim |
| 9. Format-Branded | 1. Big-Text-Claim with the format brand visible |

Override when the topic suggests something stronger. Don't follow this table robotically.

---

## Implementation note for the `generate_thumbnail` pipeline stage (Session 2)

The future stage will use Pillow (same as `tools/make_channel_art.py`) to render these patterns programmatically. Each pattern becomes a function:

```python
def thumbnail_big_text_claim(text: str, accent_color: str, out_path: Path) -> Path: ...
def thumbnail_crossed_out(rejected: str, recommended: str, out_path: Path) -> Path: ...
def thumbnail_terminal_with_result(screenshot_path: Path, annotation: str, out_path: Path) -> Path: ...
# etc.
```

The metadata-gen prompt's COVER section already produces `text_overlay`, `background_desc`, and `accent_color` — extend it to also produce a `pattern_name` field (one of the 8 above) so the renderer picks the right function automatically.

For now (pre-Session-2), this library is operator-facing reference: when uploading via Studio, generate the thumbnail in Canva using one of these patterns + the brand constants.

---

## Sources

Same as `competitor_audit_2026-05-06.md` § Sources, plus:
- [YouTube Thumbnail Design Principles 2026 — ThumbMagic](https://www.thumbmagic.co/blog/thumbnail-design-principles)
- [YouTube Shorts Thumbnail Strategy 2026 — Miraflow](https://miraflow.ai/blog/youtube-shorts-thumbnail-strategy-2026)
- [Thumbnail Best Practices CTR — Awisee](https://awisee.com/blog/youtube-thumbnail-best-practices/)
- [Thumbnail Mobile Secrets — Banana Thumbnail](https://blog.bananathumbnail.com/youtube-thumbnail-design-2026/)
