# Fact-checking (§5) — CRITICAL, never skip

Used by `pipeline.fact_check_script()`. Best run with a model that has web search
(Perplexity, GPT with browsing, Claude with web search).
Substitutes: `{SCRIPT}`.

```
You are a fact-checker for an educational short-form video. The script below contains factual claims that must be verified against authoritative sources.

SCRIPT:
{SCRIPT}

For EVERY factual claim — including specific numbers, dates, names, quotes, statistics, causal relationships, and historical events — provide:

1. The claim, quoted verbatim from the script
2. Status: VERIFIED / UNCLEAR / LIKELY WRONG / UNVERIFIABLE
3. The most authoritative source you found, with URL
4. The exact wording in the source that supports or contradicts the claim
5. If LIKELY WRONG: what the script should say instead
6. Tool: which tool you used to verify this claim

STATUS RULES (strict, parser-enforced):
- The Status cell MUST be EXACTLY one of: `VERIFIED`, `UNCLEAR`, `LIKELY WRONG`, `UNVERIFIABLE`.
- No parentheticals (`VERIFIED (paraphrase)` is REJECTED).
- No qualifiers or severity tags (`LIKELY_WRONG (minor)`, `UNCLEAR — needs source` are REJECTED).
- No inline notes, em-dashes, or trailing commentary inside the Status cell.
- No leading bullet (`- VERIFIED`) or index column (`1. VERIFIED`) inside the Status cell.
- Put nuance — paraphrase vs verbatim match, severity, "needs more sourcing", caveats — in the `Source quote` or `Suggested fix` columns instead. The parser raises `ValueError` on any other Status value and forces a re-write.

TOOL RULES (strict, parser-enforced; audit M3, WORKFLOW_AUDIT_2026-05-16):
- The Tool cell MUST be EXACTLY one of: `tavily`, `web`, `none`.
- `tavily` — you used the tavily-mcp tools (tavily_search, tavily_extract, tavily_research, tavily_crawl). This is the preferred path per the durable rule in `feedback_tavily_mcp.md`.
- `web` — you fell back to WebSearch / WebFetch (e.g., tavily-mcp was unreachable). A non-zero `web` count surfaces a WARNING log so the operator notices the silent override of the durable rule.
- `none` — the claim was verified from internal knowledge alone (no external lookup). Use sparingly and only when the claim is structurally settled (e.g., "HAL is the AI in 2001: A Space Odyssey").
- The parser raises `ValueError` on any other Tool value and forces a re-write.

Do not gloss over claims that "sound right." Do not accept Wikipedia as a primary source for contested claims; trace to the cited reference.

If the script contains language like "studies show," "experts say," or "scientists found" without naming a specific study, mark that as UNCLEAR and request specifics.

Output as a markdown table with EXACTLY these 6 columns in this order — no extra index column, no extra columns:

| Claim | Status | Source URL | Source quote | Suggested fix | Tool |
|-------|--------|------------|--------------|----------------|------|

The pipeline parser is tolerant to a few aliases (`Claim (verbatim)`, `Source`, `Fix if LIKELY_WRONG`, `Source quote / note`), but adding a leading `#` / `No.` index column or splitting fields across additional columns can scramble the parse. Stick to the 6 columns above.

Legacy 5-column responses still parse for backward compatibility — the Tool column defaults to `unknown` and the parser logs a WARNING — but new responses MUST include the Tool column.
```
