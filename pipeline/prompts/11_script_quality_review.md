# Script quality review (§11)

Standalone critique prompt for ad-hoc use — runs independently of the pipeline. Use when:
- You want a second-pass critique of a script_FINAL.txt before render
- You're tuning the viral_hooks library and want to see how an old script scores
- You're triaging why a published video underperformed

This prompt does NOT modify the script. It produces a critique JSON the operator reads and acts on manually. The pipeline stage `evaluate_script_quality` uses the same rubric in self-scored form during normal runs.

Substitutes: `{NICHE_STYLE_GUIDE}`, `{SCRIPT}` (paste the full script_FINAL.txt verbatim).

References:
- `prompts/library/viral_hooks.md`
- `prompts/library/thumbnail_patterns.md`

```
You are a script-quality reviewer for a faceless dev/AI Shorts channel.

NICHE & STYLE GUIDE:
{NICHE_STYLE_GUIDE}

SCRIPT TO REVIEW (verbatim, including [B-ROLL: ...] cues and the chosen-hook line if present):
{SCRIPT}

Score the script on these 6 dimensions, each 0.0..1.0. Be tough — a "perfect" 1.0 means the script is publish-ready and likely to outperform peers; 0.5 means mediocre; below 0.4 means rewrite.

1. **hook_strength**: Does the first sentence punch hard in the first 4 words? Does it use a recognizable formula from prompts/library/viral_hooks.md (Contradiction / Specific-Number Promise / Result-First Mid-Action / Comparison / Anti-Pattern / Specific-Question / Measured-Claim / Cited-Observation Lead / Format-Branded)? Generic openers ("In this video", "Today we're looking at") get 0.0–0.2.

2. **second_hook_strength**: Does the script include a clear second-hook beat at 0:08–0:10 (before the 13–15s attention cliff observed in the 2026-05-05 batch)? Score requires THREE elements: (a) hard visual change — a genuinely new B-ROLL context, not a zoom or pan; (b) verbal twist — curiosity gap, contradiction to the opening claim, or "but here's the thing" pivot; (c) the verbal line is short enough for the captions stage to render as a 3–7-word on-screen overlay. All three present + lands in the 0:08–0:10 window: 0.9+. Two of three or wrong window: 0.5. Missing or no second hook: 0.0–0.2.

3. **specificity**: Does the body name specific keystrokes, flags, file paths, version numbers, or measured outcomes? "Cmd-K" beats "the command palette." ".cursor/rules/" beats "a config file." "Cursor 3.x" beats "the latest version." Vague language gets 0.2; named specifics get 0.8+.

4. **opinion_density**: Is there a strong stance, contrarian claim, or anti-pattern setup? Does the script name losers, not just winners? "Stop using X, do Y" gets 0.9. "Most devs do X, here's why it's wrong" gets 0.8. Neutral how-to ("here are some uses for X") gets 0.3.

5. **cited_observation_quality**: Is there ONE cited observation in the script with a retrievable URL and a named source? Anonymous "a developer says" gets 0.0. A real named handle (u/<x>, hn:<id>, forum.cursor.com:<thread>, github:<user>/<repo>#<issue>) with a quote that's retrievable gets 0.8+. The source must support the claim, not just be tangentially related.

6. **broll_cadence**: Count the [B-ROLL: ...] cues in the body. Divide by spoken-word count. Aim for one cue per 8–10 spoken words (visual change every 2–3 seconds when rendered). 12–18 cues for a 130-word script is the target. Score 0.9+ if you hit the target; 0.5 if half-density; 0.2 or lower if there are <5 cues for a full-length script.

OUTPUT FORMAT (strict JSON, no prose around it):

{
  "scores": {
    "hook_strength": 0.0,
    "second_hook_strength": 0.0,
    "specificity": 0.0,
    "opinion_density": 0.0,
    "cited_observation_quality": 0.0,
    "broll_cadence": 0.0
  },
  "weighted_total": 0.0,
  "verdict": "publish | revise | rewrite",
  "strongest_dimension": "<name>",
  "weakest_dimension": "<name>",
  "concrete_fixes": [
    "<one-line specific actionable fix>",
    "<one-line specific actionable fix>"
  ],
  "rationale": "<2-3 sentences explaining the verdict>"
}

Verdict thresholds:
- weighted_total >= 0.65: "publish"
- weighted_total 0.50–0.64: "revise" (apply concrete_fixes, then re-score)
- weighted_total < 0.50: "rewrite" (the draft has structural issues, start over)

The weighted_total is the equal-weighted mean of the 6 scores rounded to 2 decimals. Do not invent additional weights.
```
