# Pre-publish self-check (§10)

Run on EVERY script before rendering — final paranoia pass.
Substitutes: `{SCRIPT}`.

```
You are a strict content reviewer auditing this script for risks before production.

SCRIPT:
{SCRIPT}

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
