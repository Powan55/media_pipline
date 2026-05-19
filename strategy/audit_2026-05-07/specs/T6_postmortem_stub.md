# T6 — `tools/postmortem_stub.py` (per-video postmortem md generator)

**Derives from:** WORKFLOW_AUDIT Phase 5 T1.9, 30_60_90_PLAN Day 6.3, defect: 0 postmortems despite `_TEMPLATE.md` existing
**Severity:** LOW (no production risk; compounding-learning surface missing)
**Expected LoC:** ~80
**File:** `C:\ContentOps\_pipeline\tools\postmortem_stub.py`

---

## Goal

Per-video postmortems are the operator's review surface for compounding learning. The `_TEMPLATE.md` already exists at `C:\Users\laxmi\Documents\Project\Channels\ShadowVerse\postmortems\_TEMPLATE.md` (verify in spawn). After each successful upload, emit `<topic_id>.md` to that postmortems dir, populated from the topic's metadata + render data.

If `_TEMPLATE.md` doesn't exist, the module emits a sensible default template (see "Default template" below) so the dir bootstraps itself.

## Inputs / Outputs

```python
@dataclass
class PostmortemData:
    topic_id: str
    slug: str                        # from metadata title or topic dir name
    render_date: datetime            # mtime of master.mp4
    upload_date: datetime | None     # from upload_log.csv if available
    video_url: str | None            # from upload_log.csv
    hook_formula: str | None         # parsed from script_FINAL.txt's "hook_formula:" line if present
    thumbnail_pattern: str | None    # parsed from metadata_RESPONSE.txt Pattern field
    duration_s: float | None         # ffprobe of master


def generate_postmortem(
    topic_id: str,
    *,
    channel_root: Path,
    project_root: Path,              # C:\Users\laxmi\Documents\Project\
    upload_log_csv: Path | None = None,
    template_path: Path | None = None,   # defaults to <project_root>/Channels/ShadowVerse/postmortems/_TEMPLATE.md
    overwrite: bool = False,
) -> Path:
    """Generate <project_root>/Channels/ShadowVerse/postmortems/<topic_id>.md.

    Pulls data from the topic dir + upload_log.csv + master mp4 metadata.
    Returns the path of the written postmortem file.
    Raises FileExistsError if target exists and overwrite=False.
    Raises FileNotFoundError if the topic's master.mp4 is missing (i.e., not yet ready for postmortem).
    """
```

CLI:

```
python tools/postmortem_stub.py --topic-id <id>
                                 [--channel-root PATH] [--project-root PATH]
                                 [--overwrite]
                                 [--backfill]    # all topics with QA markers
```

## Default template (used when `_TEMPLATE.md` missing)

```markdown
# Postmortem — {topic_id}

**Slug:** {slug}
**Hook formula:** {hook_formula}
**Thumbnail pattern:** {thumbnail_pattern}
**Render date:** {render_date}
**Upload date:** {upload_date}
**Video URL:** {video_url}
**Duration:** {duration_s} s

## Day 1 baseline (fill within 24-48h post-publish)
- 24h views:
- 24h avg-view %:
- 24h 3-sec retention %:

## Day 7 / Day 30 trend (fill at first weekly review)
- 7-day views:
- 7-day AVD trend:
- 30-day views:

## What I tried
-

## What I'd do differently
-

## Replication notes
-
```

## Acceptance criteria (testable)

1. `generate_postmortem()` reads from a real topic's artifacts when run against an existing topic dir; populates `topic_id`, `render_date`, and `slug` (slug derived from metadata title or topic dir name).
2. If `_TEMPLATE.md` exists, treats it as a Python `str.format()`-style template — substitutes `{topic_id}`, `{slug}`, `{hook_formula}`, `{thumbnail_pattern}`, `{render_date}`, `{upload_date}`, `{video_url}`, `{duration_s}`. Missing values render as `_(not yet captured)_`.
3. If `_TEMPLATE.md` doesn't exist, falls back to the inline default template above.
4. `upload_log.csv` parsing tolerates the schema documented in SESSION_HANDOFF.md: `uploaded_at, topic_id, video_id, url, privacy, title`. If the topic_id isn't found, leaves `upload_date`/`video_url` as None.
5. Refuses to overwrite without `overwrite=True`/`--overwrite` (`FileExistsError`).
6. `--backfill` walks `04_renders/_final_master/*_QA_APPROVED.marker`, calls `generate_postmortem()` for each topic; aggregates errors and reports at end.
7. Output path = `<project_root>/Channels/ShadowVerse/postmortems/<topic_id>.md`.
8. Output is valid markdown with at least the 4 sections (header, Day 1 baseline, What I tried, What I'd do differently).

## Unit tests (`tests/test_postmortem_stub.py`)

- `test_generate_with_template_substitutes` — set up tmp `_TEMPLATE.md` with `{topic_id}` placeholder; call generate; assert output contains the topic_id.
- `test_generate_falls_back_to_default_when_template_missing` — no `_TEMPLATE.md`; assert output contains the default-template signature lines (e.g. "Day 1 baseline").
- `test_generate_pulls_upload_data_from_csv` — synthesize an `upload_log.csv` with one row; assert generated md contains the `video_id` URL.
- `test_generate_handles_missing_csv_gracefully` — no CSV; assert output renders `_(not yet captured)_` for upload fields.
- `test_refuses_overwrite_without_flag` — pre-create target; assert FileExistsError; with overwrite=True, assert overwrites.
- `test_backfill_processes_all_marked_topics` — scaffold 3 topics, 2 with QA markers; assert 2 postmortems generated.

## Dependencies

- stdlib `csv`, `pathlib`, `datetime`, `argparse`, `logging`, `dataclasses`
- `ffmpeg-python` (already pinned) for duration probe — OR use subprocess+ffprobe directly (cleaner, fewer deps to import)

## References — match this code style

- `pipeline._parse_metadata_response` (lines 1827-1909) — for extracting fields from `metadata_RESPONSE.txt`
- `tools/youtube_upload.py` — upload_log.csv schema
- `Channels/ShadowVerse/postmortems/_TEMPLATE.md` — verify content during spawn; treat as source-of-truth template

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class
- [ ] Unit tests covering: happy path, ≥2 edge cases, ≥1 error path
- [ ] Explicit exceptions, not bare `except:`. No swallowed errors
- [ ] Logging at INFO for milestones (logger name `"postmortem_stub"`)
- [ ] Idempotent (refuses to overwrite without --overwrite)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths — channel_root and project_root via arg
- [ ] f-strings, not `%` or `.format()` for runtime strings; **but template substitution explicitly uses `str.format()` because that's how the template works**
- [ ] PEP 8 line length 100
- [ ] CLI: `argparse` with `--help`, exit 0 success, non-zero failure
- [ ] Module docstring at top with usage example
- [ ] Importable as `from tools.postmortem_stub import generate_postmortem` for Wave 3 post-upload hook
