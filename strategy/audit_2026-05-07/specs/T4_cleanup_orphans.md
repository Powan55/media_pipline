# T4 — `tools/cleanup_orphans.py` (orphan _drafts dir mover)

**Derives from:** WORKFLOW_AUDIT Phase 5 T1.7, 30_60_90_PLAN Day 5.3-5.4, defect: 4 orphan dirs from old `picks_assignment` bug (`2026-05-06_004`, `_005`, `2026-05-07_001`, `_002`)
**Severity:** LOW (disk hygiene; no production risk)
**Expected LoC:** ~50
**File:** `C:\ContentOps\_pipeline\tools\cleanup_orphans.py`

---

## Goal

Walk `<channel_root>/02_scripts/_drafts/<topic_id>/` topic dirs. For each topic dir without a `script_FINAL.txt` AND with `mtime > 48h`, move it to `_drafts/_orphans/<YYYY-MM>/<topic_id>/`. Default mode is `--dry-run` (lists what *would* be moved, makes no changes). Live move requires `--apply`.

This is destructive in-spirit (operator `_drafts/<topic_id>` content moves dirs around). Per CLAUDE.md "always confirm before deleting" — `--dry-run` is the DEFAULT (no flag needed); live mode requires explicit `--execute`. Mover prints clear "would move N dirs" line in dry-run. Operator inspects, then re-runs with `--execute`.

## Inputs / Outputs

```python
def find_orphans(
    drafts_dir: Path,
    *,
    min_age_hours: float = 48.0,
    now: datetime | None = None,
) -> list[Path]:
    """Return list of topic-dir paths that are orphans by definition:
       (a) lack `script_FINAL.txt` AND
       (b) directory mtime older than `min_age_hours`.
    Excludes directories starting with `_` (orphans, _daily_<DATE>, etc).
    """

def move_to_orphans(
    orphan_dirs: list[Path],
    *,
    drafts_dir: Path,
    now: datetime | None = None,
) -> list[tuple[Path, Path]]:
    """Move each orphan dir to drafts_dir/_orphans/<YYYY-MM>/<orphan_basename>/.
    Returns list of (src, dst) tuples for the audit log. Idempotent — if dst already
    exists, raises FileExistsError (user must resolve manually rather than overwrite).
    """
```

CLI:

```
python tools/cleanup_orphans.py [--drafts-dir PATH] [--min-age-hours 48]
                                  [--execute]            # required for live move
                                  [--quiet]
```

Default: dry-run (the absence of `--execute` is dry-run). Stdout lists each candidate dir with size + mtime; exits 0 PASS, non-zero on partial failure.

## Acceptance criteria (testable)

1. `find_orphans()` correctly identifies dirs without `script_FINAL.txt` AND mtime > min_age_hours; ignores dirs starting with `_`; ignores files (only directory entries).
2. `find_orphans()` is read-only — never modifies the filesystem.
3. `move_to_orphans()` creates `_orphans/<YYYY-MM>/<basename>/` subdir if missing and moves entire dir tree (not copy + delete; use `shutil.move`).
4. CLI without `--execute` prints "DRY RUN — would move N dirs" header, lists dirs, exits 0 with no filesystem change.
5. CLI with `--execute` performs the move, writes audit log line per move, prints "moved N dirs" summary.
6. Refuses to move when destination already exists (`FileExistsError`) — operator must resolve manually.
7. Smoke test against the 4 known orphans (`2026-05-06_004`, `_05`, `2026-05-07_001`, `_002`) listed in MORNING_BRIEF must produce exactly those 4 in dry-run output **assuming the test runs the cleanup against the live channel root** — but unit tests must use a synthetic temp-dir fixture, not the real channel root.
8. Audit log line format: `<ISO timestamp>\t<src>\t<dst>` written to stdout (or stderr if --quiet).

## Unit tests (`tests/test_cleanup_orphans.py`)

- `test_find_orphans_picks_old_dirs_without_final` — set up tmp dir with: dir A (>48h, no FINAL), dir B (>48h, has FINAL), dir C (<48h, no FINAL), dir `_orphans` (any age). Assert returns only [A].
- `test_find_orphans_min_age_threshold` — feed `min_age_hours=0.001` so even fresh dirs qualify; assert returns all non-`_`-prefixed dirs without FINAL.
- `test_find_orphans_skips_underscore_prefixed` — `_orphans`, `_daily_2026-05-08`, `_archive` all skipped regardless.
- `test_find_orphans_ignores_files` — drop a top-level file in drafts_dir; assert it's not in results.
- `test_move_creates_target_subdir` — call `move_to_orphans([tmp_dir])`; assert `_orphans/YYYY-MM/<basename>` exists, original is gone, contents preserved.
- `test_move_refuses_existing_target` — pre-create the destination; assert `FileExistsError`, original untouched.
- `test_cli_dry_run_no_filesystem_change` — invoke without `--execute`; assert stdout contains "DRY RUN" and no source dir was moved.

## Dependencies

- stdlib `shutil`, `pathlib`, `datetime`, `argparse`, `logging`

## References — match this code style

- `tools/youtube_upload.py` — argparse + logger init pattern
- `pipeline.py` lines 208-251 (config loading / setup_logging) — for paths/logging idioms
- `daily_batch.py` — for orchestrator-style stdout output

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path (uses tmp dirs only — no real channel-root touches)
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones (logger name `"cleanup_orphans"`)
- [ ] Idempotent in dry-run; refuse-to-overwrite in apply mode
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths — drafts_dir resolved via CLI arg or default to `<config.paths.channel_root>/02_scripts/_drafts/`
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, `--execute` required for destructive action (dry-run default)
- [ ] Module docstring at top with usage example
- [ ] No agent frameworks; no LLM API calls
