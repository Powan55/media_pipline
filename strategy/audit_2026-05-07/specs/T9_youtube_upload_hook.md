# T9 — `tools/youtube_upload.py` post-upload archive + postmortem hook

**Derives from:** 30_60_90_PLAN Day 6.1 (archive_published Stage 13) + Day 6.3 (postmortem_stub Stage 14)
**Severity:** LOW (no production risk; bookkeeping after the irreversible upload)
**Expected LoC:** ~30 (additive edit to existing 381-line file)
**File:** `C:\ContentOps\_pipeline\tools\youtube_upload.py`
**Wave:** 3 (depends on T5, T6)

---

## Goal

After a successful YouTube upload, automatically (a) copy YT/TT/IG variants to `06_published/<YYYY-MM>/<topic_id>/` via `tools.archive_published.archive()` and (b) generate the per-video postmortem stub via `tools.postmortem_stub.generate()`. Closes Phase A defects #3 (06_published empty) and #5 (zero postmortems written).

**Critical constraint:** hook failures must NOT fail the upload. The upload is the side-effecting irreversible action; bookkeeping shouldn't make the operator re-upload. Hooks log + continue.

## Acceptance criteria (testable)

1. After `upload_video()` succeeds AND `set_thumbnail()` succeeds (or fails-tolerant), AND `append_upload_log()` succeeds, the script invokes:
   - `archive_published.archive(topic_id=args.topic_id, config=config)` — copies the 3 variants.
   - `postmortem_stub.generate(topic_id=args.topic_id, config=config, video_id=video_id, video_url=url)` — writes the markdown stub.
2. If `archive_published.archive()` raises, log the exception via `log.error("post-upload archive failed: %s", e)` and continue. Do NOT re-raise. Do NOT change the script's exit code.
3. If `postmortem_stub.generate()` raises, log the exception and continue. Same exit-code semantics.
4. Order matters: archive first, then postmortem (postmortem may want to reference archive paths in the stub body).
5. Both hooks are SKIPPED when `--dry-run` is passed.
6. Both hooks are SKIPPED when `args.privacy == "private"` AND `--publish-at` is None — the video isn't actually published yet, so archive + postmortem are premature. (Scheduled-private uploads with `--publish-at` SHOULD trigger the hooks since YouTube will auto-publish at the scheduled time.)
7. Imports of `archive_published` and `postmortem_stub` are at module top alongside the existing `pipeline` import (line 55 idiom).
8. Existing print-banner output unchanged. Hook output appears as additional log lines AFTER the banner.

## Implementation sketch

Insert after the `log_path = append_upload_log(...)` line (currently line 359):

```python
# Post-upload bookkeeping — failures here log + continue. The upload
# already succeeded; we don't want bookkeeping issues to make the
# operator re-upload. Skipped for non-published privacy (unscheduled-private)
# and for --dry-run.
should_run_hooks = not args.dry_run and (
    args.privacy != "private" or publish_at_utc is not None
)
if should_run_hooks:
    try:
        archive_paths = archive_published.archive(
            topic_id=args.topic_id, config=config,
        )
        log.info("archived to %s", archive_paths)
    except Exception as e:
        log.error("post-upload archive failed (upload itself succeeded): %s", e)

    try:
        postmortem_path = postmortem_stub.generate(
            topic_id=args.topic_id, config=config,
            video_id=video_id, video_url=url,
        )
        log.info("postmortem stub written to %s", postmortem_path)
    except Exception as e:
        log.error("post-upload postmortem stub failed (upload itself succeeded): %s", e)
```

Top-of-file import edit:
```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import _parse_metadata_response, MetadataBundle  # noqa: E402
from tools import archive_published, postmortem_stub  # noqa: E402
```

(Or, if the relative-import path doesn't work because `tools/` isn't a package, import as `from tools.archive_published import archive` and `from tools.postmortem_stub import generate`.)

## Acceptance test

- `test_dry_run_skips_hooks` — `subprocess.run` against a fixture; assert no archive dir created.
- `test_hook_failure_does_not_fail_upload` — mock `archive_published.archive` to raise; assert script exit code unchanged (use a mock-friendly entrypoint or patch via monkeypatch).
- `test_private_no_publishat_skips_hooks` — assert hook skip when `privacy=private` and no `--publish-at`.
- `test_scheduled_private_runs_hooks` — assert hook RUNS when `privacy=private` and `--publish-at` is provided.
- (Optional integration) — manual smoke after T5 + T6 land: dry-run a fictional topic, validate behavior.

## Dependencies

- T5 (`tools/archive_published.py`) — must expose `archive(topic_id, config) -> dict[Platform, Path]`.
- T6 (`tools/postmortem_stub.py`) — must expose `generate(topic_id, config, video_id, video_url) -> Path`.

## References

- Existing `youtube_upload.py` lines 277-377 — surgical edit site.
- `mcp__ccd_session__spawn_task`-style "fail-soft for bookkeeping" pattern.

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints (no new function signatures here, but if adding helpers — yes)
- [ ] Docstrings updated where comment block expands
- [ ] Unit tests cover: dry-run skip, hook-fail doesn't fail upload, private+no-publishat skip, scheduled-private runs hooks
- [ ] Explicit `except Exception as e` for the bookkeeping branch is OK here (justified by the fail-soft semantics) — but log full exception via `log.exception` if traceback would aid debugging
- [ ] Logging at INFO/ERROR appropriately
- [ ] Idempotent (T5 + T6 already idempotent per their specs)
- [ ] f-strings, PEP 8 line length 100
- [ ] No new CLI args added (hooks are unconditional except for the existing `--dry-run` and the privacy-state gate)
- [ ] No new pip deps
- [ ] Match existing youtube_upload.py code style
