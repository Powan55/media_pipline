# T5 — `tools/archive_published.py` (06_published archive copy)

**Derives from:** WORKFLOW_AUDIT Phase 5 T1.8, 30_60_90_PLAN Day 6.1-6.2, defect: `06_published/` empty since channel start
**Severity:** LOW (no production risk; gap in cold-backup + postmortem prereq)
**Expected LoC:** ~60-90
**File:** `C:\ContentOps\_pipeline\tools\archive_published.py`

---

## Goal

After a successful YouTube upload (or via `--backfill`), copy the per-platform variants into `<channel_root>/06_published/<YYYY-MM>/<topic_id>/{youtube,tiktok,instagram}/`. Today the `06_published/` archive flow is configured but never invoked, so the channel has no canonical "this is what shipped" snapshot. Wave 3 will wire this into `tools/youtube_upload.py` post-upload hook.

## Inputs / Outputs

```python
def archive_topic(
    topic_id: str,
    *,
    channel_root: Path,
    when: datetime | None = None,    # defaults to now
    skip_missing: bool = False,      # if True, skip platforms whose variant doesn't exist
) -> dict[str, Path]:
    """Copy variants from 05_exports/{youtube,tiktok,instagram}/<topic_id>_{yt,tt,ig}.mp4
    into 06_published/<YYYY-MM>/<topic_id>/{youtube,tiktok,instagram}/<basename>.

    Returns dict of platform -> archived path.

    Raises FileNotFoundError if a required variant is missing and skip_missing=False.
    Raises FileExistsError if the destination already has a variant of the same name
    (idempotent guard — operator should explicitly --force to re-archive).
    """

def backfill_all(
    channel_root: Path,
    *,
    when: datetime | None = None,
    only_topic_ids: list[str] | None = None,
    skip_missing: bool = True,
) -> list[dict]:
    """Walk 04_renders/_final_master/ for *_QA_APPROVED.marker files and
    archive_topic() each one. Returns list of {topic_id, archived: dict[str, Path]}.
    """
```

CLI:

```
python tools/archive_published.py --topic-id <id>          # archive one
python tools/archive_published.py --backfill               # archive all G3-approved
python tools/archive_published.py --backfill --only 2026-05-05_001 2026-05-07_004
                                   [--channel-root PATH] [--force]
```

`--force` allows overwriting existing archive (use sparingly — operator-only).

## Acceptance criteria (testable)

1. `archive_topic()` correctly resolves the 3 expected source variants from `05_exports/{youtube,tiktok,instagram}/<topic_id>_{yt,tt,ig}.mp4`.
2. Destination layout = `06_published/<YYYY-MM>/<topic_id>/{youtube,tiktok,instagram}/<original_basename>`. Subdirs created on demand.
3. Returns dict `{"youtube": Path, "tiktok": Path, "instagram": Path}` listing the archived destinations.
4. `skip_missing=True` skips a platform whose source variant is missing; result dict omits that platform key.
5. `--backfill` walks `04_renders/_final_master/` for `*_QA_APPROVED.marker` files, extracts topic_id from `<topic_id>_master_QA_APPROVED.marker`, and archives each.
6. Refuses to overwrite without `--force` (FileExistsError → exit non-zero).
7. CLI exits 0 success; non-zero on per-topic failure (continue across topics in --backfill mode but report failures at end).
8. Uses `shutil.copy2` (preserve mtime). Does not move/delete source.

## Unit tests (`tests/test_archive_published.py`)

- `test_archive_topic_copies_three_variants` — synthesize 3 dummy MP4 files (touch + bytes), call archive_topic(), assert three new files exist at expected dest paths.
- `test_archive_topic_missing_variant_raises_when_strict` — drop just yt + tt variants; call with skip_missing=False; assert FileNotFoundError.
- `test_archive_topic_skip_missing` — same setup; call with skip_missing=True; assert returns dict without the missing platform.
- `test_archive_refuses_overwrite` — pre-create destination; call again; assert FileExistsError. Then call with `force=True` (or via CLI `--force`); assert overwrites cleanly.
- `test_backfill_picks_up_qa_markers` — scaffold three topics in `04_renders/_final_master/`, two with `*_QA_APPROVED.marker`, one without; call backfill; assert exactly 2 archived.
- `test_backfill_only_filter` — three approved topics; backfill with `only_topic_ids=["A"]`; assert only A archived.

All tests use tmp dir fixtures, never real `06_published/`.

## Dependencies

- stdlib `shutil`, `pathlib`, `datetime`, `argparse`, `logging`

## References — match this code style

- `tools/youtube_upload.py` — for argparse + audit log style (the post-upload hook in Wave 3 will call into this module)
- `pipeline.py` `await_final_qa` (lines 1690-1721) — for understanding the marker-file convention this module reads

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones (logger name `"archive_published"`)
- [ ] Idempotent (refuse to overwrite without --force)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths — channel_root via arg
- [ ] f-strings, not `%` or `.format()`. PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 success, non-zero failure
- [ ] Module docstring at top with usage example
- [ ] Importable as `from tools.archive_published import archive_topic` for Wave 3 post-upload hook
