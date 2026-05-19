# Weekly analytics review (§8)

Run on Sundays after `analytics_pull.py` populates the weekly CSV.
Substitutes: `{ANALYTICS_DATA}`, `{NICHE}`, `{MEDIAN_RETENTION_4W}`, `{MEDIAN_VIEWS_4W}`.

```
You are a content strategist reviewing the past week's performance.

DATA (last 7 days):
{ANALYTICS_DATA}

CHANNEL CONTEXT:
- Niche: {NICHE}
- Median retention over the last 4 weeks: {MEDIAN_RETENTION_4W}%
- Median views over the last 4 weeks: {MEDIAN_VIEWS_4W}

Produce:

1. TOP 3 winners — what made them work? Be specific (hook, format, topic, timing). Avoid generic answers.
2. BOTTOM 3 losers — what likely caused the underperformance?
3. TOPIC CLUSTERS to expand vs. retire (based on retention, not raw views)
4. FORMAT experiments to try next week
5. PATTERNS that suggest "inauthentic content" or templating risk — be brutal here, this is a self-audit
6. ONE specific, testable hypothesis for next week (single variable change)

Be specific. Don't tell me to "post more consistently" or "improve hooks." Tell me what to literally change.
```
