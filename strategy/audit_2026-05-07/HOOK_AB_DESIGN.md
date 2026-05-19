# Hook A/B framework — Phase 0 design

**Sprint 4 / Phase 0**. Author: Phase 0 design agent. Date: 2026-05-12.
Status: **Recommendation — green-light or push back.**

> Goal of this doc: pick ONE design path that systematically attributes Shorts performance to hook formula, decompose it into 5–8 file-disjoint implementation slices, and surface the few decisions that genuinely need operator input. Everything else is decided here.

---

## 1. Goal — what does "winning" mean

**Primary KPI: `hold_at_3s` (Shorts swipe-gate retention at 3 seconds).**

**Secondary KPI (tiebreaker, weight 0.3): `avg_view_pct` (overall AVD%).**

**Excluded from primary signal: raw `views`.** Used only as a *cohort eligibility filter* (≥40 views needed before a video's `hold_at_3s` is admitted to the leaderboard — below that, Analytics often returns null or a noisy value as confirmed by `_06_001` 14v / `_06_002` 9v / `_07_003` 7v all showing `n/a` in the hold@3s column of `all_videos_2026-05-12.md`).

**Justification — point by point against the four candidate KPIs:**

| KPI | Why considered | Why not primary |
|---|---|---|
| **`views`** | Volume of human reach — what "monetization-via-views" wants | Heavy-tailed and noisy at <100 sub scale: 7v..1163v on 14 videos. `_05_002` rode `_05_001`'s same-day halo to 232v with no real hook merit (`WINNING_PATTERNS_2026-05-12.md` § P5). Title-cleanliness, time-of-day flip, and topic class explain ≥80% of view variance — hook is downstream of all three. Optimizing on views = optimizing on confounds. |
| **`avg_view_pct` (AVD%)** | Pure retention quality | Survivorship-biased: `_07_006` Anthropic Dream got 65.1% AVD on 40 viewers who *already self-selected past the title*. AVD% measures how good the *script body* is once a viewer commits — that's mostly a §3 (body) signal, not a §1 (hook) signal. |
| **`hold_at_3s`** ✅ | Directly measures the swipe-gate the hook is meant to defeat | Populated since 2026-05-08 in the analytics CSV (`hold_at_3s` column, additive). Sample is small but every winner so far reads `100` (`_05_001`, `_05_002`, `_07_004`). The 3-second window IS the hook's job — this is a clean dependent variable. Confound: the `100`s are likely a YT-Analytics rounding artifact at low view counts; we'll need to plot the distribution before committing to it as a hard threshold (smoke-test 4). |
| **Composite** | "Best of both worlds" | Composite scores let weak signals dominate when one component is noisy. At 14 datapoints, a composite is over-fit before it ships. Single-KPI + tiebreaker is the right discipline. |

**Decision rule for the leaderboard (Slice 5):** rank formulas by *median `hold_at_3s` across videos that used them*, with `avg_view_pct` as tiebreaker. Videos under 40 views excluded from the rank input. Confidence interval column (Wilson score on the proportion of "above-median holds") so the operator can see how thin each formula's evidence is.

---

## 2. Design space exploration

### A — Tagging-only baseline (pure observation, no automation)

**(a) What it does:** Every shipped video gets its hook formula tag persisted in a structured file. A new `hook_leaderboard.py` joins `picks_assignment.json` + `script_RESPONSE.txt` (parsed `[formula: ...]` annotation on the operator-chosen hook) + `upload_log.csv` + `_weekly_analytics.csv` into one denormalized DataFrame. A Markdown report committed to `audit_2026-05-07/leaderboards/hook_leaderboard_<DATE>.md` shows formula × KPI medians and a cohort table.

**(b) Effort:** **S** (~400 LoC + tests).

**(c) Signal quality:** **High eventually, low now.** Honest about it. Each shipped video adds one datapoint per formula. With 1 video/day cadence and ~7 formulas in active use, expect ~30 days for the smallest-formula bucket to hit n=4.

**(d) Cadence impact:** **Zero.** No change to `daily_batch.py` flow, no change to operator's gate-2 ritual, no change to `/start -auto`.

**(e) Reversibility:** **Trivial.** All artifacts are read-only joins of existing files. Delete the leaderboard, the project is unchanged.

**(f) Failure modes:** Only learns from formulas the operator/Claude already picks. If a formula is never proposed, we'll never know it could win. **Mitigation:** the daily batch summary reports formula-coverage gaps and prods the next idea-gen LLM to broaden.

---

### B — Predictive selection (auto-pick best-predicted hook at gate 2)

**(a) What it does:** Build a `hook_scorer.py` that scores HOOK_A/B/C against a formula-quality model (regex + per-formula priors initially seeded from `WINNING_PATTERNS_2026-05-12.md`, later updated by leaderboard data). Auto-write the highest-scored hook as the first line of `script_FINAL.txt` at gate 2. Same tagging as A, but adds a feedback loop.

**(b) Effort:** **L** (~1100 LoC + tests, plus operator-trust burden — see (f)).

**(c) Signal quality:** **Mid.** The model itself is the experimental variable. We need ground truth from path A *first* before we know which features predict `hold_at_3s`. Predictive selection trained on n=14 will overfit and make worse picks than the operator's gut.

**(d) Cadence impact:** **Negative if rolled out cold.** Auto-pilot would replace the operator's gate-2 hook pick — but the operator just spent Sprint 3 building the gate-2 ritual. Unilateral takeover violates the "ask before big actions" principle.

**(e) Reversibility:** **Medium.** Requires reverting the gate-2 auto-write, restoring operator pick.

**(f) Failure modes:** Confounding (auto-picks become both selector AND outcome — leaderboard data corrupted by selection bias). Operator-trust break (auto-replacing a sacred-gate decision). Cold-start: model has no signal until path A's data accumulates anyway.

---

### C — Cross-platform variant ship (HOOK_A on YT, HOOK_B on TT, HOOK_C on IG)

**(a) What it does:** Ship the same body to all three platforms, each with a different hook variant. Compare retention curves of "same topic, different hook formula" across platforms.

**(b) Effort:** **L** — TikTok and Instagram pipeline branches are stubbed today (per `analytics_pull.py` "TikTok and Instagram branches still stubbed (Phase 4)"). This becomes a multi-sprint Phase 4 prerequisite, not a Sprint 4 deliverable.

**(c) Signal quality:** **Catastrophic confound.** Platform algorithms differ wildly (YT Shorts feed, TT FYP, IG Reels) — you can't separate hook-effect from platform-effect. The Cursor video with HOOK_A on YT might do 1124v while the same video with HOOK_B on TT does 50v *because TT punishes the same content harder*, not because HOOK_B is worse. The whole point of an A/B is controlling the noise; this design *adds* noise.

**(d) Cadence impact:** **Heavy negative** — TikTok + Instagram pipeline build is months of work before the experiment can even start.

**(e) Reversibility:** Once the pipeline is built it's a permanent maintenance burden.

**(f) Failure modes:** Platform-noise confound (above). Audience-overlap noise (the same viewer encountering the same topic with different hooks across apps invalidates the swipe-gate measurement on the second exposure).

---

### D — Sequential same-platform same-topic (HOOK_A on day N, HOOK_B on day N+7)

**(a) What it does:** Ship `_topic_v1` with HOOK_A on Tuesday. Same topic body shipped as `_topic_v2` with HOOK_B the following Tuesday. Compare hold@3s and AVD% directly.

**(b) Effort:** **M** — pipeline can already produce variants (3 hooks generated, only 1 shipped), but you need a "republish with alternate hook" mode and to ensure topic_id allocation doesn't trip the dedup guard (`is_topic_id_uploaded` in `daily_batch.py:140`).

**(c) Signal quality:** **High but slow.** Same audience, same algorithm, same topic — only the hook varies. Cleanest possible signal. Two datapoints per topic.

**(d) Cadence impact:** **Heavy.** Burns 2 upload slots per topic A/B'd. At 1 video/day cadence this halves new-topic throughput (~15 topics/month becomes ~7). View-count drag from re-publishing a topic the algorithm already showed to overlap audience.

**(e) Reversibility:** **High** — stop scheduling v2 ships and you're back to baseline.

**(f) Failure modes:** YouTube de-duplication / re-recommendation suppression on near-identical content (the algorithm down-weights re-uploads). Audience boredom signal from the second showing tanks the v2 retention regardless of hook quality. Topic staleness — by day N+7 the AI news cycle has moved on, so v2's lower performance is a recency confound.

---

### Hybrid A→B (recommended, see §3)

**(a) What it does:** Path A now (Sprint 4). Path B *deferred* with a clear gate: "once `hook_leaderboard.py` shows ≥3 formulas with n≥6 datapoints AND a top-formula `hold_at_3s` median ≥1.3× the bottom-formula median, build the predictive selector on top of the now-validated leaderboard data." Effort/signal/etc. inherit from A in Sprint 4 and B in a future sprint.

**Why it earns its keep:** It encodes the single most important operational decision — *don't build the auto-selector before you have ground truth* — into a binary trigger, instead of leaving it as vibes. It also sets the data-collection bar B will need (n≥6 per formula) so path A's instrumentation is built knowing it's the input layer to path B.

---

## 3. Recommended design — **Hybrid A→B (Sprint 4 = Path A only)**

**Choose Path A (Tagging-only baseline) for Sprint 4. Defer Path B (Predictive selection) behind a data-driven trigger.** Rejection of C and D is final.

### Justification against the brief's four criteria

1. **Will it actually attribute performance to formulas?** Yes. Path A produces a per-formula leaderboard joined to the same KPI (`hold_at_3s`) every video already reports. The attribution is direct: "videos using formula X had median hold of Y, 95% CI [Y_lo, Y_hi]." That's the only attribution method that doesn't bake in an unverified model.

2. **Will the operator trust it?** Yes. Path A changes nothing about the operator's gate-2 ritual. The leaderboard is read-only insight, not an automated takeover. Trust is earned by the leaderboard *agreeing with the operator's intuition* on the first ~10 datapoints, then expanded into auto-selection (Path B) only when the data warrants it.

3. **Will it survive the small-N noise problem?** It's *honest about it*. The leaderboard publishes confidence intervals next to medians and refuses to declare a winner until n≥6 per formula. Path B at n=14 would silently overfit; Path A at n=14 publishes a leaderboard with "insufficient data" annotations on every row.

4. **Will it move fast enough to matter?** First useful signal at ~day 30 (n≈4 per formula). First *trigger-able* signal for Path B at ~day 60 (n≈6 per formula × ≥3 formulas). Acceptable for a system that will run for years.

### What gets explicitly NOT built in Sprint 4

- No hook scoring model
- No auto-selection at gate 2
- No `script_FINAL.txt` rewriting
- No changes to `prompts/03_script_generation.md`
- No changes to `scoring.py` (production-pattern unchanged)
- No new platform branches (TT/IG remain stubbed)

### Path B revival trigger (record this so Sprint 5+ can act on it)

> Open `hook_scorer.py` (Path B) when the leaderboard report shows: (a) ≥3 formulas with n≥6 datapoints, (b) the top formula's median `hold_at_3s` is ≥1.3× the bottom formula's median, (c) Wilson 95% CI lower bound of the top formula > Wilson 95% CI upper bound of the bottom formula (i.e., separation that survives the small-N adjustment).

---

## 4. Implementation slices

Eight slices. **All file-disjoint** so eight Dev agents can work in parallel without touching each other's files. Sprint 3 lesson respected: each slice has its own production file + its own test file. No two slices write the same file.

> **Worktree convention:** each Dev gets their own `git worktree add` per Sprint 3's lesson on parallel branches.

---

### Slice 1 — `hook_selection_log.py`: persist the operator's chosen hook + formula

**Target file(s):**
- New: `C:\ContentOps\_pipeline\hook_selection_log.py`
- New: `C:\ContentOps\_pipeline\tests\test_hook_selection_log.py`
- Reads (no writes): `C:\ContentOps\channels\ShadowVerse\02_scripts\_drafts\<topic_id>\script_RESPONSE.txt`, `script_FINAL.txt`

**Expected LoC:** ~180 + ~150 tests = ~330

**What it does:** Module exposes `extract_chosen_hook(topic_id, channel_root) -> ChosenHook` with fields `(topic_id, hook_letter, hook_text, formula, all_three_hooks: list[(letter, text, formula)])`. Heuristic: read `script_RESPONSE.txt` to get the 3 candidate hooks + formulas; read `script_FINAL.txt` first non-empty non-`[B-ROLL]` line; match against the 3 candidates by exact-prefix match (operator may have edited; tolerate first-3-words match as fallback; flag mismatches as `formula="EDITED"` and persist verbatim text). Writes one JSON line per topic to a new append-only log: `C:\ContentOps\channels\ShadowVerse\01_research\hook_selection_log.jsonl`.

**Acceptance criteria:**
- `extract_chosen_hook("2026-05-10_001", channel_root)` returns `formula="Named-Actor"` and `hook_letter` matching the actually-shipped hook.
- A hook the operator hand-edited beyond first-3-words match is recorded with `formula="EDITED"` not crash.
- Re-running on the same topic_id is idempotent (no duplicate log lines — keyed on `topic_id`).
- 8+ unit tests covering: exact-match, prefix-match, EDITED fallback, missing FINAL, missing RESPONSE, multi-formula, no-formula-annotation legacy responses, idempotent re-run.

**Dependencies:** None. Fully greenfield.

---

### Slice 2 — `tools/backfill_hook_selections.py`: one-shot historical backfill

**Target file(s):**
- New: `C:\ContentOps\_pipeline\tools\backfill_hook_selections.py`
- New: `C:\ContentOps\_pipeline\tests\test_backfill_hook_selections.py`

**Expected LoC:** ~120 + ~80 tests = ~200

**What it does:** CLI script that walks every `02_scripts\_drafts\*\` and calls `hook_selection_log.extract_chosen_hook` for each. Idempotent — uses Slice 1's log keyed on topic_id. For the historical 14 published videos, the formulas observed in `all_videos_2026-05-12.md` Table 2 may not be tagged in the original `script_RESPONSE.txt` (the formula-annotation requirement is recent) — when missing, the script emits a CSV report `audit_2026-05-07\hook_selection_backfill_unresolved.csv` listing topic_id + reason for human resolution. This CSV is the operator's worklist.

**Acceptance criteria:**
- Running once produces `hook_selection_log.jsonl` with one row per existing topic dir.
- Topics whose RESPONSE.txt lacks `[formula:]` annotations are reported in the unresolved CSV — not silently dropped.
- 5+ tests on synthetic dirs covering: clean topic, missing-formula topic, edited-final-line topic, multi-topic walk, idempotent re-run.

**Dependencies:** Slice 1.

---

### Slice 3 — `analytics_join.py`: denormalized hook×performance DataFrame

**Target file(s):**
- New: `C:\ContentOps\_pipeline\analytics_join.py`
- New: `C:\ContentOps\_pipeline\tests\test_analytics_join.py`

**Expected LoC:** ~250 + ~180 tests = ~430

**What it does:** Module exposes `join_hooks_to_analytics(channel_root) -> list[HookPerformanceRow]`. Reads three existing files (no writes to them):
- `01_research\hook_selection_log.jsonl` (Slice 1's output)
- `01_research\upload_log.csv` (already has `topic_id ↔ video_id` join)
- `01_research\_weekly_analytics.csv` (additive, append-only — picks the LATEST row per `video_id`)

Returns one row per published topic: `(topic_id, video_id, hook_letter, hook_text, formula, views, hold_at_3s, avg_view_pct, days_live, eligible_for_leaderboard: bool)`. The `eligible_for_leaderboard` flag is `views >= 70 and hold_at_3s is not None`.

**Acceptance criteria:**
- Tested on synthetic CSVs + JSONL; returns expected rows.
- Skips uploaded-private videos (`_06_003`, no analytics row) without crashing.
- Picks the latest `pull_date` row when a video has multiple analytics rows (the CSV is append-only).
- Handles missing video_id (topic was never uploaded) cleanly — emits a row with `eligible_for_leaderboard=False, reason="not_uploaded"`.
- 8+ tests covering: happy path, multiple pull_dates per video, ineligible-low-views, ineligible-no-hold-data, never-uploaded, multi-topic.

**Dependencies:** Slice 1 (consumes its JSONL).

---

### Slice 4 — `hook_leaderboard_stats.py`: the math (median + Wilson CI)

**Target file(s):**
- New: `C:\ContentOps\_pipeline\hook_leaderboard_stats.py`
- New: `C:\ContentOps\_pipeline\tests\test_hook_leaderboard_stats.py`

**Expected LoC:** ~150 + ~150 tests = ~300

**What it does:** Pure-Python statistics module. Exposes:
- `formula_medians(rows) -> dict[formula, FormulaStat]` where `FormulaStat = (n, median_hold_at_3s, median_avg_view_pct, wilson_ci_above_median)` — `wilson_ci_above_median` is the Wilson 95% CI for the proportion of this formula's videos whose `hold_at_3s` exceeds the cohort median.
- `rank_formulas(rows) -> list[FormulaRank]` — sorted by median `hold_at_3s` desc, tiebreaker `avg_view_pct` desc, with an `evidence_strength` field set to one of `{"insufficient", "weak", "strong"}` based on the trigger thresholds in §3.

No I/O. Pure functions. Easy to unit-test.

**Acceptance criteria:**
- Wilson CI math matches a known reference (test against scipy's `proportion_confint(method='wilson')` outputs computed offline and embedded as expected values).
- `evidence_strength="insufficient"` when n<3 for a formula.
- `evidence_strength="strong"` only when n≥6 AND CI lower bound > 0.5.
- 6+ tests including edge cases (n=0, n=1, all-same-value, all-tied-at-median).

**Dependencies:** None for the math. Slice 3's `HookPerformanceRow` dataclass shape needs to be agreed *before* either Dev starts (this is a serialization point — see footnote). To preserve file-disjointness, Slice 4 takes the row-shape as a `Protocol`-typed input parameter; Slice 3 ensures its output satisfies that Protocol.

---

### Slice 5 — `hook_leaderboard.py`: CLI + Markdown report writer

**Target file(s):**
- New: `C:\ContentOps\_pipeline\hook_leaderboard.py`
- New: `C:\ContentOps\_pipeline\tests\test_hook_leaderboard.py`
- Writes (new): `C:\Users\laxmi\Documents\Project\audit_2026-05-07\leaderboards\hook_leaderboard_<UTC-DATE>.md`

**Expected LoC:** ~280 + ~150 tests = ~430

**What it does:** CLI: `python hook_leaderboard.py [--config path]`. Wires Slice 3 (data) to Slice 4 (stats) and renders a Markdown report. Sections: (a) Cohort summary (n total, n eligible, date range), (b) Formula leaderboard table (formula, n, median hold@3s, median AVD%, evidence_strength, top-3 example videos by hook+score), (c) Per-video appendix table (every eligible row), (d) Coverage gaps (formulas observed in `viral_hooks.md` but not yet tested).

**Acceptance criteria:**
- Running on synthetic data produces a deterministic Markdown file.
- The "insufficient" formulas show `n=X` not a misleading rank.
- Coverage gap section lists every formula in `viral_hooks.md` that has zero shipped videos.
- 5+ tests covering: empty cohort, all-insufficient cohort, mixed-evidence cohort, deterministic output, coverage-gap accuracy.

**Dependencies:** Slices 3 + 4.

---

### Slice 6 — `daily_batch` summary: surface formula choice in the daily summary

**Target file(s):**
- New: `C:\ContentOps\_pipeline\daily_batch_hook_addendum.py` (a separate module that `daily_batch.py` will eventually import — but the import wiring is deferred to slice 8 to keep this slice file-disjoint)
- New: `C:\ContentOps\_pipeline\tests\test_daily_batch_hook_addendum.py`

**Expected LoC:** ~120 + ~100 tests = ~220

**What it does:** Module exposes `format_hook_addendum(topic_id, channel_root) -> str` returning a Markdown block to append under each topic in `daily_batch_<DATE>.md`. The block lists: chosen hook letter + formula, the two un-chosen variants + their formulas (so the operator sees the alternatives), and a one-line "leaderboard says formula X has median hold@3s Y at n=N" footer (calls Slices 3+4 read-only). When `script_FINAL.txt` doesn't exist yet (gate 2 not passed), returns "(awaiting gate 2 selection)".

**Acceptance criteria:**
- Returns a non-empty markdown string for a topic with `script_FINAL.txt`.
- Returns the awaiting placeholder for a topic at gate 2.
- 5+ tests on synthetic topic dirs.

**Dependencies:** Slices 1 + 3 + 4 (read-only).

---

### Slice 7 — Documentation: hook A/B framework operator guide

**Target file(s):**
- New: `C:\Users\laxmi\Documents\Project\audit_2026-05-07\HOOK_AB_GUIDE.md`
- (no test file — docs)

**Expected LoC:** ~250 (markdown)

**What it does:** Operator-facing guide. Sections: (1) what the leaderboard tells you and what it doesn't, (2) how to read evidence_strength, (3) the Path B revival trigger (when to ask for the auto-selector to be built), (4) cron suggestion: `python hook_leaderboard.py` weekly via Windows Task Scheduler, (5) how to manually re-tag an EDITED hook in `hook_selection_log.jsonl` (operator escape hatch — directly edit the JSONL line, the system tolerates it).

**Acceptance criteria:** Operator reads it cold and can answer "is the current top formula trustworthy?" in <5 minutes. Doc PR-reviewed by a QA agent for clarity, not just typo-checked.

**Dependencies:** Slices 1–6 must have stable behavior to document.

---

### Slice 8 — Integration: wire everything into `daily_batch.py` and `/start -auto`

**Target file(s):**
- Edits (existing): `C:\ContentOps\_pipeline\daily_batch.py`
- Edits (existing): `C:\ContentOps\_pipeline\tests\test_daily_batch_allocator.py` (add 2 tests for the new addendum integration)
- (writes through existing artifacts — no new persistence files)

**Expected LoC:** ~80 + ~60 tests = ~140 (this slice is intentionally tiny — it's just the wiring)

**What it does:** Imports `daily_batch_hook_addendum.format_hook_addendum` (Slice 6) and `hook_selection_log.extract_chosen_hook` (Slice 1). Calls extract_chosen_hook *after* `script_FINAL.txt` exists (i.e., after `resolve_factcheck`) so the JSONL is appended to as part of the normal pipeline. Calls `format_hook_addendum` in `_write_batch_summary` so the operator sees hook context in the daily summary. **`/start -auto` does not need changes** — the addendum just appears in the same `daily_batch_<DATE>.md` it already reads.

**Acceptance criteria:**
- One full daily_batch run on a fresh topic_id produces a `hook_selection_log.jsonl` row + a daily summary with the hook addendum block.
- 2 additional tests in `test_daily_batch_allocator.py` covering the wiring (mock the slice 6 call, assert summary contains the marker).

**Dependencies:** Slices 1, 3, 4, 6. **This is the only slice that edits a pre-existing file.** It is the **single serialization point** in the plan — Slice 8 must land last, after all other slices are merged.

---

### Slice dependency graph (visual)

```
S1 (hook_selection_log) ──┬─→ S2 (backfill)
                          │
                          ├─→ S3 (analytics_join) ──┐
                          │                          ├─→ S5 (leaderboard CLI) ──┐
                          │                          │                           │
                          │   S4 (stats math) ───────┴───────────────────────────┤
                          │                                                      │
                          └─→ S6 (daily addendum) ───────────────────────────────┤
                                                                                  ├─→ S7 (docs)
                                                                                  │
                                                                                  └─→ S8 (integration, edits daily_batch.py)
```

**Parallelism map for the manager:**
- **Wave 1 (parallel, 3 Devs):** S1, S4, S7-stub. (S7 begins as outline; finalized after S5 lands.)
- **Wave 2 (parallel, 3 Devs):** S2, S3, S6 (all depend only on S1 / S4).
- **Wave 3 (parallel, 2 Devs):** S5 (depends on S3+S4), S7-finalize.
- **Wave 4 (single Dev, serialization point):** S8 (edits `daily_batch.py`).

Total: 8 slices, 4 waves, ~6 Dev-agent runs (some Devs handle 2 small slices). Mirrors Sprint 3's parallel architecture.

---

## 5. Open questions for operator

Five items max — these genuinely require operator input, not vibes:

1. **Where does the chosen-hook log live?** Recommendation: append-only JSONL at `C:\ContentOps\channels\ShadowVerse\01_research\hook_selection_log.jsonl` (sits next to `upload_log.csv` and `_weekly_analytics.csv`, same directory the analytics report already reads). Alternative: embed in `picks_assignment.json` (existing file, but bundled with allocator concerns — coupling smell). **OK to proceed with JSONL?**

2. **Where does the leaderboard report live?** Recommendation: Markdown at `C:\Users\laxmi\Documents\Project\audit_2026-05-07\leaderboards\hook_leaderboard_<UTC-DATE>.md` (OneDrive-synced, operator can open from any device, joins the existing audit dir convention). Alternative: under `C:\ContentOps\channels\ShadowVerse\01_research\`. **OK to put it in the audit dir?**

3. **Should the historical 14 videos be backfilled with formula tags by the operator manually, or by a sub-agent inferring from `all_videos_2026-05-12.md` Table 2?** Recommendation: sub-agent inferral, then operator approves the backfill CSV in one batch. (Saves operator time; reversible.) **OK to delegate to a sub-agent?**

4. **Eligibility threshold for the leaderboard: `views >= 70` (operator decision 2026-05-12 post-design).** Matches the loser-bar in `WINNING_PATTERNS_2026-05-12.md` cohort summary. Trade-off acknowledged: admits fewer winners in the early data (drops `_07_006` 40v and `_08_001` 73v from formula attribution) but keeps the loser-bar honest. Sub-70-view videos are excluded from formula attribution but still recorded in the chosen-hook log for completeness.

5. **Path B revival trigger sensitivity: top-formula median ≥1.3× bottom-formula median is my proposed bar.** Could be 1.2× (more eager to auto-select) or 1.5× (more conservative). **Stay at 1.3× for the documented trigger?**

---

## 6. Smoke-test plan (post-implementation Phase 2 input)

Synthetic checks the QA agents should run after Sprint 4 lands. Each is one specific command + one specific assertion.

1. **Slice 1 round-trip on `_10_001`:** `python -c "from hook_selection_log import extract_chosen_hook; print(extract_chosen_hook('2026-05-10_001', ...))"` → assert `formula == "Named-Actor"`, `hook_letter in {"A","B","C"}`, `len(all_three_hooks) == 3`.

2. **Slice 2 backfill produces unresolved CSV:** Run `python tools/backfill_hook_selections.py` on the live channel root → assert `hook_selection_log.jsonl` has ≥10 rows AND `audit_2026-05-07/hook_selection_backfill_unresolved.csv` exists with ≥1 row (we know historical RESPONSE.txt files predate the formula-annotation requirement, so unresolved should be non-empty — empty would mean the script silently swallowed missing tags).

3. **Slice 3 join accuracy:** Run `analytics_join.join_hooks_to_analytics(...)` → assert returned row count == row count in `upload_log.csv` (1:1 mapping for uploaded videos), `_06_003` row has `eligible_for_leaderboard=False reason="no_analytics_row"`.

4. **Slice 4 hold@3s distribution sanity:** With the live data, plot the `hold_at_3s` distribution → confirm not all winners read exactly `1.00` (if they do, the metric is YT-Analytics-rounded at low view counts and the leaderboard's primary KPI needs to fall back to AVD%; flag to operator).

5. **Slice 5 deterministic report:** Run `python hook_leaderboard.py` twice in a row → assert byte-identical output (modulo timestamp line). Assert "insufficient" appears next to every formula at current n.

6. **Slice 6 daily summary addendum:** Run a synthetic daily_batch with 1 pick → assert `daily_batch_<DATE>.md` contains "Chosen hook formula:" line.

7. **Slice 8 integration end-to-end:** Take a real next-day topic, run through `/start -auto` → assert `hook_selection_log.jsonl` gained one row AND `daily_batch_<DATE>.md` contains the hook addendum block AND the JSONL row's `formula` matches the formula tagged in `script_RESPONSE.txt`.

8. **Negative test — operator hand-edits `script_FINAL.txt`** to a hook that doesn't match any HOOK_A/B/C prefix → assert `formula == "EDITED"` (not crash, not silently mis-tagged).

---

**End of Phase 0 design.**
