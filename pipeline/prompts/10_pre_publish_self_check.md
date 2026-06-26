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
7. OPENING FRAME / swipe-survival (set 2026-06-24 analytics deep-dive — frame 0 IS the Shorts cover since no thumbnail is uploaded; 93% of reach is the Shorts feed and 63.8% of viewers swipe away at second 0-1, BEFORE the 3s hold). Inspect the FIRST [B-ROLL] cue and the opening 3-5 words as the cover the feed shows: flag it if frame 0 is a logo / channel bumper / homepage / slow fade-from-black / generic establishing stock shot, or if it lacks a single high-contrast mute-legible subject (a result, a reaction, or a split-comparison) with implied motion. The opening overlay phrase must carry STAKES or CURIOSITY (not the payoff — preserve setup→twist→payoff). Recommend a stronger frame-0 cue + overlay if the cover would lose the swipe. See `Documents\Project\.research\analytics_deepdive_2026-06-24\OPENING_FRAME_SPEC.md`.

For each flag, suggest a specific revision.

If the script has FEWER than 3 flags, also point out the WEAKEST sentence and recommend a stronger alternative.
```
