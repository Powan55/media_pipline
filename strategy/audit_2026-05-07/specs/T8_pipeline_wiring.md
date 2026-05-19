# T8 — `pipeline.py` wiring + `config.yaml.template` updates

**Derives from:** RESOLUTIONS R7, 30_60_90_PLAN Days 2.1, 3.3, 3.4, 4.4, 5.2, 5.5, WORKFLOW_AUDIT Phase 5 T1.1/T1.2/T1.3/T1.6
**Severity:** HIGH
**Expected LoC:** ~150 (mostly diff against existing `pipeline.py`; ~25 lines net new in `config.yaml.template`)
**Files:** `C:\ContentOps\_pipeline\pipeline.py`, `C:\ContentOps\_pipeline\config.yaml.template`
**Wave:** 3 (depends on T1, T2, T3, T7, T10)

---

## Goal

Wire the new tools into the production pipeline. Six discrete edits:

1. **Stage 7.5 (loudnorm)** — call `tools.audio_loudnorm.normalize_vo()` between `generate_voiceover` and `fetch_assets`. Removes the inline `loudnorm=I=-14:LRA=11:TP=-1.5` filter from `_vo_edge_tts` (which was single-pass and TP=-1.5 instead of R1's TP=-1.0).
2. **Stage 9 caption swap** — `generate_captions` dispatches on `config.captions.style`: `"word_pop"` (default, calls `tools.caption_word_pop.render_ass()`) or `"legacy"` (existing static block, kept for one-flip rollback).
3. **Stereo `-ac 2` mux** — add `-ac 2` to `render_master`'s ffmpeg output kwargs AND to `_fade_variant`'s output kwargs in `generate_variants`. Closes R7.
4. **Stage 10.1 (post-master integrity)** — call `tools.media_integrity.check_integrity()` on the master immediately after `render_master` returns. Raise `PipelineHalted` (new subclass `IntegrityCheckFailed`) on failure.
5. **Stage 11 (prepublish QA)** — call `tools.prepublish_qa.run_qa()` on each variant. Raise `PipelineHalted` (new subclass `PrepublishQAFailed`) on failure. Also call `media_integrity.check_integrity()` on each variant (Stage 11.2 per Phase A 5.5).
6. **`tts.provider` config branch** — extend `generate_voiceover` to dispatch to `tools.tts_elevenlabs.synthesize()` when `config.tts.provider == "elevenlabs"`. Default stays `edge-tts`. ElevenLabs path imports the module lazily (deferred import inside the branch) so the rest of the pipeline runs without `elevenlabs` installed.

`config.yaml.template` updates:
- Add `captions.style: word_pop` (default) — values: `word_pop` | `legacy`
- Add `captions.font_name: "Montserrat Black"`
- Add `loudnorm.target_lufs: -14.0`, `loudnorm.target_tp: -1.0`, `loudnorm.target_lra: 11.0`
- Add `prepublish_qa.enabled: true`, `prepublish_qa.check_cited_observation: false` (R6 narrow scope, default off)
- Document `tts.provider: elevenlabs` is a valid value but **not** the default until the operator approves activation.

## Acceptance criteria (testable)

1. `python -m pipeline --help` still works (no parser regressions).
2. Importing `pipeline` does NOT fail when `tools.tts_elevenlabs`'s deps (`elevenlabs` package) are absent — module is imported lazily.
3. Reading `config.yaml.template` and parsing as YAML yields all new keys with expected defaults: `captions.style="word_pop"`, `loudnorm.target_lufs=-14.0`, `prepublish_qa.enabled=True`, `prepublish_qa.check_cited_observation=False`.
4. Existing inline `loudnorm=I=-14:LRA=11:TP=-1.5` filter in `_vo_edge_tts` (lines ~1383-1389) is REMOVED. WAV output post-edge-tts is the raw conversion; loudnorm now runs as Stage 7.5.
5. `render_master` ffmpeg output kwargs include `"ac": 2` (stereo upmix). `_fade_variant` output kwargs include `"ac": 2`.
6. Two new exception classes: `IntegrityCheckFailed(PipelineHalted)`, `PrepublishQAFailed(PipelineHalted)`. Both inherit `PipelineHalted` so the existing `except PipelineHalted as halt` in `main()` (line 2053) catches them.
7. `run_for_topic` flow now reads (in order): `generate_script` → `evaluate_script_quality` → `fact_check_script` → `await_fact_check_resolution` → `fetch_assets` → `generate_voiceover` → `audio_loudnorm.normalize_vo` (Stage 7.5) → `generate_captions` → `render_master` → `media_integrity.check_integrity(master)` (Stage 10.1) → `await_final_qa` → `generate_variants` → for each variant `media_integrity.check_integrity(v)` + `prepublish_qa.run_qa(v)` (Stage 11) → `generate_metadata` → `generate_thumbnail` → `schedule_publishing`.
8. `generate_captions` reads `config.captions.style`; with `style="legacy"` it produces the existing static-block ASS; with `style="word_pop"` it delegates to `tools.caption_word_pop.render_ass()`.
9. Unit tests: `test_pipeline_imports_without_elevenlabs` (force `tools.tts_elevenlabs` to raise on import; assert `pipeline` imports OK), `test_config_template_parses` (load template, assert new keys present).
10. Smoke: invoke `pipeline.run_for_topic` with mocked stage functions (or just import and confirm graph is wired) — no NotImplementedError on the new branches.
11. Existing tests / behaviors that must NOT regress: gate-3 marker mechanism, `ManualLLMHalt` semantics, `QualityCheckFailed`, `daily_batch.py` orchestration import paths.

## Implementation notes

- **Imports stay at module top for T1, T2, T3, T7** (those are pure Python, no heavy deps). **Deferred import for T10** (`tools.tts_elevenlabs`) — wrap in `try/except ImportError` inside the elevenlabs branch with an actionable error message.
- **Stage 7.5 helper**: write `_normalize_vo_loudness(vo_path: Path, config: dict) -> Path` that calls `audio_loudnorm.normalize_vo(vo_path, vo_path)` (in-place), reading `config.loudnorm` for targets. Caller passes the result back to `render_master`.
- **Caption style dispatch**: rename existing inline body to `_generate_captions_legacy()`; new top-level `generate_captions()` dispatches via `if config["captions"].get("style", "word_pop") == "legacy": return _generate_captions_legacy(...)` else `caption_word_pop.render_ass(...)`.
- **Variant integrity** loops over `variants.values()` and calls both checks per file. Aggregate failures into one `PrepublishQAFailed` exception so the operator sees all problems at once.

## Dependencies

- T1, T2, T3, T7, T10 all merged into `tools/` before this task starts.
- No new pip deps. ffmpeg already required.

## References

- Existing `pipeline.run_for_topic` (line 1991) — the canonical orchestration.
- `_vo_edge_tts` lines 1352-1392 — what to surgically prune.
- `render_master` lines 1632-1648 — where `-ac 2` lands in the kwargs dict.
- `_fade_variant` lines 1764-1784 — the second `-ac 2` site.
- `generate_captions` lines 1490-1554 — what becomes `_generate_captions_legacy`.
- `PipelineHalted` lines 57-65 — new exception base.

---

## Production-standards checklist (REQUIRED)

- [ ] Type hints on every function signature (params + return)
- [ ] Docstrings on every public function/class
- [ ] Unit tests cover: import-without-elevenlabs, config template parses, exception subclassing
- [ ] Explicit exceptions; no bare `except:`
- [ ] Logging at INFO for stage transitions (use existing `log = logging.getLogger("pipeline")`)
- [ ] Idempotent (re-running after each gate is safe)
- [ ] No mutable default arguments
- [ ] No hardcoded absolute paths (config-driven)
- [ ] f-strings, PEP 8 line length 100
- [ ] Match existing pipeline.py conventions: pathlib, structured logging, fail-loud, no print
- [ ] Preserve sacred-gate semantics for `await_final_qa` and `await_fact_check_resolution`
- [ ] Comment minimally — one short line where the WHY isn't obvious from code (config-template inline comments OK)
