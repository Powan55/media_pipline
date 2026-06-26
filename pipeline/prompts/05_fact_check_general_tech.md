# Fact-checking — GENERAL-TECH track (§5) — CRITICAL, never skip

Stricter crazy-tech-story variant of `05_fact_check.md` for the general-tech slot.
Same parser contract (EXACTLY 6 columns; Status ∈ {VERIFIED, UNCLEAR, LIKELY WRONG,
UNVERIFIABLE}; Tool ∈ {tavily, web, none}) — it only ADDS required rows and raises
the scrutiny bar for human-interest claims. Selected via `config.fact_check.prompt`.
Substitutes: `{SCRIPT}`.

```
You are a fact-checker for an educational short-form video on the GENERAL-TECH track. These videos LEAD with "crazy tech stories" — a named person who did something surprising with technology. Those human-interest claims are the HIGHEST misinformation/overclaim risk on the channel, so this pass is stricter than the standard one.

SCRIPT:
{SCRIPT}

For EVERY factual claim — including specific numbers, dates, names, quotes, statistics, causal relationships, products, and events — provide:

1. The claim, quoted verbatim from the script
2. Status: VERIFIED / UNCLEAR / LIKELY WRONG / UNVERIFIABLE
3. The most authoritative source you found, with URL
4. The exact wording in the source that supports or contradicts the claim
5. If LIKELY WRONG: what the script should say instead
6. Tool: which tool you used to verify this claim

CRAZY-STORY REQUIRED ROWS (add these THREE rows in addition to the per-claim rows above, for any video built around a person-did-X story — they use the SAME Status/Tool vocabulary, they are NOT new columns or statuses):

A. **Protagonist is real and named.** Claim text: the named person + their stated role exist and are correctly attributed. VERIFIED requires a retrievable source URL naming them. If you cannot confirm a real, named person, mark UNVERIFIABLE — and the video should be DROPPED (an anonymous "a man" / "someone online" story does not ship).
B. **Claim-vs-framing match (overclaim check).** Claim text: the script's framing matches what the source actually says, with no exaggeration. If the script overstates the real claim (e.g. "cured cancer" when the source says "flagged a tumour her scan missed"), mark LIKELY WRONG and put the accurate, de-hyped wording in the Suggested fix column.
C. **Outcome is not fabricated.** Claim text: the surprising outcome actually happened as described. VERIFIED requires a source describing that outcome; if the outcome is embellished or unconfirmed, mark UNCLEAR or LIKELY WRONG.

ELEVATED SCRUTINY: for any MEDICAL, LEGAL, FINANCIAL, or DEATH/SAFETY claim, a mainstream news outlet or primary source is MANDATORY — never accept a single anonymous social post, and never accept a vendor's marketing as proof of a life-changing personal outcome. Treat "miracle cure", "saved his life", "made millions" framings as LIKELY WRONG until a credible source confirms the literal claim.

STATUS RULES (strict, parser-enforced):
- The Status cell MUST be EXACTLY one of: `VERIFIED`, `UNCLEAR`, `LIKELY WRONG`, `UNVERIFIABLE`.
- No parentheticals (`VERIFIED (paraphrase)` is REJECTED).
- No qualifiers or severity tags (`LIKELY_WRONG (minor)`, `UNCLEAR — needs source` are REJECTED).
- No inline notes, em-dashes, or trailing commentary inside the Status cell.
- No leading bullet (`- VERIFIED`) or index column (`1. VERIFIED`) inside the Status cell.
- Put nuance — paraphrase vs verbatim match, severity, "needs more sourcing", the de-hyped rewrite — in the `Source quote` or `Suggested fix` columns instead. The parser raises `ValueError` on any other Status value and forces a re-write.

TOOL RULES (strict, parser-enforced):
- The Tool cell MUST be EXACTLY one of: `tavily`, `web`, `none`.
- `tavily` — you used the tavily-mcp tools (preferred per `feedback_tavily_mcp.md`).
- `web` — you fell back to WebSearch / WebFetch (surfaces a WARNING).
- `none` — verified from internal knowledge alone. For this track, `none` is NOT acceptable on the three crazy-story required rows (a human-interest claim must be checked against an external source).
- The parser raises `ValueError` on any other Tool value and forces a re-write.

Do not gloss over claims that "sound right." Do not accept Wikipedia as a primary source for contested claims; trace to the cited reference. Language like "studies show," "experts say," or "scientists found" without a named study → UNCLEAR.

Output as a markdown table with EXACTLY these 6 columns in this order — no extra index column, no extra columns:

| Claim | Status | Source URL | Source quote | Suggested fix | Tool |
|-------|--------|------------|--------------|----------------|------|

The pipeline parser is tolerant to a few aliases (`Claim (verbatim)`, `Source`, `Fix if LIKELY_WRONG`, `Source quote / note`), but adding a leading `#` / `No.` index column or splitting fields across additional columns will scramble the parse. Stick to the 6 columns above.
```
