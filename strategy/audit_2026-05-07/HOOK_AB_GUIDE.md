# Hook A/B Framework — Operator Guide

> Sprint 4 deliverable. Tells the operator what shipped, where the data lives, what to run when, and how to read the leaderboard. Companion to `HOOK_AB_DESIGN.md` (the why) and `SPRINT4_RESULTS.md` (the what-shipped log).

---

## What this framework does

Every shipped video uses one of 11 canonical hook formulas (see `prompts/library/viral_hooks.md`). The script-gen prompt already produces 3 variants (HOOK_A/B/C, each with `[formula: NAME]`), the operator picks one at gate 2, the chosen hook ships in `script_FINAL.txt`. Until Sprint 4, the channel had no systematic way to ask "which formula is winning?"

The framework adds four things:

1. **A persistent log** (`hook_selection_log.jsonl`) of every chosen hook + its formula tag, joined to YouTube analytics by `topic_id ↔ video_id`.
2. **A leaderboard report** that ranks formulas by `hold_at_3s` (Shorts swipe-gate retention) with Wilson 95% confidence intervals — honest about small-N noise.
3. **A daily-batch addendum** showing the chosen hook + the two unchosen variants + a leaderboard footer per topic in `daily_batch_<DATE>.md`.
4. **A backfill CLI** for the historical 14 published videos that predate the formula-annotation requirement.

This is **Path A (Tagging-only baseline)** from `HOOK_AB_DESIGN.md` §3. Path B (predictive selection at gate 2) is deferred behind a data-driven trigger — see "Path B revival" below.

---

## Where the data lives

| File | Source-of-truth for | Schema |
|---|---|---|
| `C:/ContentOps/channels/ShadowVerse/01_research/hook_selection_log.jsonl` | Per-topic chosen hook + formula + 3 candidates | One JSON per line (Slice 1) |
| `C:/ContentOps/channels/ShadowVerse/01_research/upload_log.csv` | topic_id ↔ video_id join | Existing |
| `C:/ContentOps/channels/ShadowVerse/01_research/_weekly_analytics.csv` | Per-video performance KPIs | Existing append-only (Slice 3 uses latest `pull_date` per video) |
| `C:/Users/laxmi/Documents/Project/audit_2026-05-07/leaderboards/hook_leaderboard_<UTC-DATE>.md` | Operator-facing ranked leaderboard | Generated (Slice 5) |
| `C:/Users/laxmi/Documents/Project/audit_2026-05-07/hook_selection_backfill_unresolved.csv` | Worklist of historical topics with no `[formula:]` tags | Generated (Slice 2) |

---

## End-to-end flow for a new video

1. **Idea-gen** → operator/Claude picks topics (existing `daily_batch.py`)
2. **Script-gen** → LLM emits HOOK_A/B/C with `[formula: NAME]` annotations (existing `prompts/03_script_generation.md`)
3. **Operator at gate 2** picks one hook → it lands as the first line of `script_FINAL.txt`
4. **NEW: backfill ingest** → `python tools/backfill_hook_selections.py` walks every topic dir, parses RESPONSE+FINAL, appends to JSONL (idempotent — safe to re-run)
5. **Render → upload → analytics pull** (existing pipeline)
6. **NEW: leaderboard refresh** → `python hook_leaderboard.py` reads JSONL + analytics, writes `audit_2026-05-07/leaderboards/hook_leaderboard_<UTC-DATE>.md`
7. **NEW: daily-batch summary** automatically includes a chosen-hook + alternatives + leaderboard footer block per topic (Slice 8 wires this into `_write_batch_summary`)

---

## Daily commands the operator runs

After every batch ships (or on a cron — see "Cadence" below):

```powershell
cd C:\ContentOps\_pipeline
.\.venv\Scripts\Activate.ps1

# 1. Ingest the latest chosen hooks (idempotent — safe on every run)
python tools/backfill_hook_selections.py

# 2. Refresh the leaderboard
python hook_leaderboard.py
```

That's it. Both commands are idempotent and read-only against pipeline state — they only write to the JSONL log and the leaderboard markdown.

For dry-run previews:
```powershell
python tools/backfill_hook_selections.py --dry-run    # walks dirs, prints planned counts, writes nothing
python hook_leaderboard.py --dry-run                  # renders to stdout, doesn't write the file
```

---

## How to read the leaderboard

The leaderboard markdown has 4 sections:

### Cohort summary
Total topics in the hook log, count eligible for the leaderboard, breakdown of why some are ineligible. **Eligibility = `views >= 70` AND `hold_at_3s` populated.** Sub-70-view videos are still recorded in the JSONL but excluded from formula attribution per operator decision (2026-05-12).

### Formula leaderboard table
Ranked by median `hold_at_3s` (Shorts swipe-gate is what hooks are *for*), tiebreaker on median `avg_view_pct`. Each row shows:
- `n` — eligible-row count for this formula
- `Wilson CI (above cohort median)` — 95% confidence interval for "what fraction of this formula's videos beat the cohort median?"
- `Evidence` — one of:
  - `insufficient` (n < 3) — too few datapoints to say anything
  - `weak` (n = 3..5 OR n ≥ 6 but CI doesn't separate from chance) — directional but not significant
  - `strong` (n ≥ 6 AND CI lower bound > 0.5) — formula reliably above cohort median

### Per-video appendix
Every row in the JSONL with its analytics. Eligible flag + reason populated. Sorted by `topic_id` ascending so re-runs produce byte-identical output.

### Coverage gaps
Formulas in `prompts/library/viral_hooks.md` with **zero** eligible videos. Gives the operator a worklist: "schedule a video using each of these formulas to expand the dataset."

---

## When useful signal arrives

At ~1 video/day cadence, with 11 formulas:
- **First leaderboard with any cohort:** as soon as backfill runs (~14 historical videos, modulo unresolved)
- **First "weak" evidence per formula:** ~30 days post-Sprint-4 (n≥3 per formula assuming roughly even distribution)
- **First "strong" evidence per formula:** ~60 days post-Sprint-4 (n≥6 + Wilson CI separation)

The leaderboard publishes "insufficient" annotations on every under-evidenced formula. Don't over-read the early ranks — the framework is designed to be honest about small-N noise.

---

## The unresolved CSV — historical backfill

The 14 historical videos shipped before the `[formula: ...]` annotation was required in `script_RESPONSE.txt`. For those, `tools/backfill_hook_selections.py` writes a worklist to:

`C:/Users/laxmi/Documents/Project/audit_2026-05-07/hook_selection_backfill_unresolved.csv`

Schema:
```
topic_id, hook_text, formula_status, reason, all_three_hooks_json
```

Where `formula_status` is `UNTAGGED` (RESPONSE.txt has no `[formula:]` tags) or `EDITED` (operator's shipped FINAL doesn't match any of the 3 candidate hooks).

**Resolution path (operator + sub-agent):** spawn a sub-agent to infer the formula for each unresolved row from `audit_2026-05-07/analysis/all_videos_2026-05-12.md` Table 2 (which already has hook-formula classifications for all 14). The operator approves the proposed formulas in one batch, then either (a) hand-edits the JSONL to overwrite the UNTAGGED entries with the inferred formulas, or (b) re-runs `backfill_hook_selections.py` after appending `[formula: ...]` lines to the historical RESPONSE.txt files (cleaner — keeps the source-of-truth single).

---

## Daily-batch summary changes (Slice 8)

`daily_batch_<DATE>.md` now includes a "hook addendum" block under each topic:

```markdown
## 2026-05-15_001 — <topic>
- **Angle:** ...
- **Score:** ...
- **Status:** completed

```
(halt message)
```

**Chosen hook:** A - Cited-Observation Lead
> A Fields medalist tested ChatGPT 5.5.

**Alternatives:**
- B (Result-First / Mid-Action): Tim Gowers wrote about ChatGPT.
- C (Contradiction): Math's smartest just judged OpenAI.

**Leaderboard for "Cited-Observation Lead":** median hold@3s = 0.85 at n=4 (weak)
```

Topics still at gate 2 render `(awaiting gate 2 selection)`. The addendum never blocks the summary write — leaderboard failures are caught and the addendum still ships with `(leaderboard unavailable)`.

---

## Cadence

The framework is decoupled from `daily_batch.py` and `/start -auto` runs. The operator can refresh the leaderboard on any cadence:

- **After each ship:** simplest. Run the two commands manually after gate 3.
- **Daily cron:** Windows Task Scheduler can run both commands every morning (read-only against the channel state, idempotent — safe to re-run).
- **Weekly:** less work, fine for a low-velocity channel like this one.

The leaderboard markdown is timestamped in its filename (`hook_leaderboard_<UTC-DATE>.md`), so multiple snapshots accumulate in `audit_2026-05-07/leaderboards/`. No automatic cleanup — operator can prune old snapshots at any time.

---

## Path B revival trigger (when to graduate to predictive selection)

The Sprint 4 design doc explicitly defers Path B (auto-select the best-predicted hook at gate 2) behind a binary trigger. Open the Path B work order when **all three** conditions hold:

1. **≥3 formulas with n≥6 datapoints** in the eligible cohort
2. **Top-formula median `hold_at_3s` ≥1.3× bottom-formula median**
3. **Wilson 95% CI lower bound of the top formula > Wilson 95% CI upper bound of the bottom formula**

The leaderboard report's `evidence` column makes condition 3 visible at a glance. Until then, the leaderboard is observational only — the operator's gate-2 instinct is the selector.

---

## Troubleshooting

### "Leaderboard says n=0 for every formula"
Probably `hook_selection_log.jsonl` doesn't exist yet. Run `python tools/backfill_hook_selections.py` first.

### "Backfill reports more unresolved than expected"
Check `audit_2026-05-07/hook_selection_backfill_unresolved.csv` — each row has a `reason` column explaining why (missing RESPONSE, no formula tags, EDITED beyond match).

### "Leaderboard formula name doesn't match what I see in viral_hooks.md"
The canonicalizer in `hook_leaderboard.py:_canonicalize_formula_name` strips "The " prefix, " Hook" suffix, surrounding parentheses, and quote pairs. Check `viral_hooks.md` `### N. <Name>` headers — those are the source-of-truth. Both "Result-First Mid-Action" (entry 3) and "Result-First / Mid-Action" (entry 11) are intentionally distinct formulas.

### "Daily-batch summary doesn't have the addendum block"
Probably an older topic without `script_FINAL.txt` (gate 2 not yet passed) — the addendum renders `(awaiting gate 2 selection)` in that case. Confirm by checking the topic dir.

### "I want to override a chosen-formula classification"
Edit the JSONL line for that topic_id directly (one JSON per line; replace in place). Re-running backfill is idempotent and won't undo your edit unless `script_FINAL.txt` changes.

---

## File map (what landed in Sprint 4)

| File | Slice | Lines |
|---|---|---|
| `hook_selection_log.py` | 1 | 343 |
| `tools/backfill_hook_selections.py` | 2 | 494 |
| `analytics_join.py` | 3 | 419 |
| `hook_leaderboard_stats.py` | 4 | 303 |
| `hook_leaderboard.py` | 5 | 635 |
| `daily_batch_hook_addendum.py` | 6 | 225 |
| (this guide) | 7 | n/a |
| `daily_batch.py` (modified) | 8 | small wiring delta |

Plus 7 new test files totaling ~2400 LoC and ~95 unit tests. See `SPRINT4_RESULTS.md` for the full ledger.

---

**Last updated:** 2026-05-12 (Sprint 4 ship).
