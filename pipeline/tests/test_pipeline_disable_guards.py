"""Pin the WORKFLOW_AUDIT_2026-05-16 H2 ``allow_disable_in_production`` guards.

Stage 11 (`_run_prepublish_qa`) and Stage 1.5 (`evaluate_script_quality`) used
to be skippable via a single config edit:

  - ``prepublish_qa.enabled: false`` → ``log.warning`` + ``return``
  - ``script_quality.enforce_min_score: false`` → silent pass-through with
    only an ``info`` log

In ``/start -auto`` mode the gate-3 auto-approve is conditional on both gates
passing. "Passing" by being silently skipped meant a one-line config edit
during debugging (forgotten before the next auto-run) would let the unchecked
video proceed to auto-approve.

The fix: require an additional ``allow_disable_in_production: true`` flag
alongside the disable flag. Without the second flag, raise ``RuntimeError``.

These tests pin the contract:

  1. ``enabled=false`` / ``enforce_min_score=false`` WITHOUT the second flag
     raises ``RuntimeError``.
  2. ``enabled=false`` / ``enforce_min_score=false`` WITH the second flag
     skips / passes through silently (the legacy behavior).
  3. Default config (no keys set) does not raise — the guard only fires when
     someone explicitly disables the gate.

Run:
    python -m pytest tests/test_pipeline_disable_guards.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402
from pipeline import (  # noqa: E402
    SCRIPT_QUALITY_DIMENSIONS,
    ScriptDraft,
    _run_prepublish_qa,
    evaluate_script_quality,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_script(
    *,
    topic_id: str = "t1",
    scores: dict[str, float] | None = None,
) -> ScriptDraft:
    """Build a minimal ScriptDraft. Quality_scores default to all 1.0 (passes)."""
    if scores is None:
        scores = {dim: 1.0 for dim in SCRIPT_QUALITY_DIMENSIONS}
    return ScriptDraft(
        topic_id=topic_id,
        hook_variants=["A", "B", "C"],
        body="...",
        broll_cues=[],
        fact_check_queue=[],
        word_count=100,
        quality_scores=scores,
    )


# ---------------------------------------------------------------------------
# Stage 11 — _run_prepublish_qa
# ---------------------------------------------------------------------------


class TestPrepublishQAGuard:
    """``prepublish_qa.enabled=false`` requires ``allow_disable_in_production``."""

    def test_disabled_without_allow_flag_raises(self) -> None:
        config = {"prepublish_qa": {"enabled": False}}
        with pytest.raises(RuntimeError) as excinfo:
            _run_prepublish_qa(
                topic_id="t1",
                variants={},
                captions_path=Path("/tmp/captions.ass"),
                config=config,
            )
        msg = str(excinfo.value)
        assert "Stage 11" in msg
        assert "allow_disable_in_production" in msg

    def test_disabled_with_allow_flag_skips_silently(self) -> None:
        """Both flags set → skip path is allowed (unit-test / explicit bypass)."""
        config = {
            "prepublish_qa": {
                "enabled": False,
                "allow_disable_in_production": True,
            },
        }
        # Should return None without touching variants / captions
        result = _run_prepublish_qa(
            topic_id="t1",
            variants={},
            captions_path=Path("/tmp/captions.ass"),
            config=config,
        )
        assert result is None

    def test_default_config_does_not_raise_guard(self) -> None:
        """Default config (no prepublish_qa key) defaults to enabled=True; the
        guard does not fire — the function proceeds to its real work. We don't
        execute the real work here (no real videos), but we assert that the
        RuntimeError from the disable-guard is NOT raised."""
        config: dict = {"paths": {"channel_root": "/tmp"}}
        # The function will fail later when it tries to iterate empty variants
        # and import prepublish_qa, but it MUST NOT raise the
        # allow_disable_in_production RuntimeError.
        try:
            _run_prepublish_qa(
                topic_id="t1",
                variants={},
                captions_path=Path("/tmp/captions.ass"),
                config=config,
            )
        except RuntimeError as exc:
            assert "allow_disable_in_production" not in str(exc), (
                f"disable-guard fired on default config: {exc}"
            )
        except Exception:
            # Other exceptions (import errors, missing files) are fine — only
            # the disable-guard RuntimeError is what this test pins.
            pass


# ---------------------------------------------------------------------------
# Stage 1.5 — evaluate_script_quality
# ---------------------------------------------------------------------------


class TestScriptQualityGuard:
    """``script_quality.enforce_min_score=false`` requires ``allow_disable_in_production``."""

    def test_enforce_false_without_allow_flag_raises(self) -> None:
        script = _make_script()
        config = {"script_quality": {"enforce_min_score": False}}
        with pytest.raises(RuntimeError) as excinfo:
            evaluate_script_quality(script, config)
        msg = str(excinfo.value)
        assert "Stage 1.5" in msg
        assert "allow_disable_in_production" in msg

    def test_enforce_false_with_allow_flag_passes_through(self) -> None:
        """Both flags set → pass-through is allowed (unit-test / explicit bypass)."""
        # Use below-threshold scores so the pass-through path is exercised
        below = {dim: 0.10 for dim in SCRIPT_QUALITY_DIMENSIONS}
        script = _make_script(scores=below)
        config = {
            "script_quality": {
                "min_score": 0.50,
                "enforce_min_score": False,
                "allow_disable_in_production": True,
            },
        }
        result = evaluate_script_quality(script, config)
        assert result is script  # pure-gate contract: returns the same draft

    def test_enforce_true_does_not_require_allow_flag(self) -> None:
        """The guard only fires when someone explicitly disables enforcement."""
        script = _make_script()  # all 1.0 scores, passes
        config = {
            "script_quality": {
                "min_score": 0.50,
                "enforce_min_score": True,
            },
        }
        result = evaluate_script_quality(script, config)
        assert result is script

    def test_default_config_raises_guard(self) -> None:
        """The legacy default (enforce_min_score not set) is treated as
        false → the guard fires. This is intentional: production config must
        explicitly opt in to one path or the other."""
        script = _make_script()
        with pytest.raises(RuntimeError) as excinfo:
            evaluate_script_quality(script, {})
        assert "allow_disable_in_production" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Stage 1.5 — require_full_dimensions (H-3, 2026-06-19)
# ---------------------------------------------------------------------------


class TestRequireFullDimensions:
    """require_full_dimensions scores over ALL canonical dimensions (missing =
    0.0), closing the scorer-controlled-denominator exploit and making a missing
    QUALITY_SCORES section halt under enforce instead of silently passing."""

    def _cfg(self) -> dict:
        return {"script_quality": {
            "min_score": 0.50,
            "enforce_min_score": True,
            "require_full_dimensions": True,
        }}

    def test_all_dimensions_present_high_passes(self) -> None:
        script = _make_script()  # all 1.0
        assert evaluate_script_quality(script, self._cfg()) is script

    def test_omitting_weak_dimensions_cannot_inflate(self) -> None:
        """Only the 2 strong dims present at 1.0; the other 4 count as 0.0 ->
        mean 2/6 = 0.33 < 0.50 -> halts. Under the legacy mean-over-present this
        same script scored 1.0 and passed."""
        two = {SCRIPT_QUALITY_DIMENSIONS[0]: 1.0, SCRIPT_QUALITY_DIMENSIONS[1]: 1.0}
        script = _make_script(scores=two)
        with pytest.raises(pipeline.QualityCheckFailed):
            evaluate_script_quality(script, self._cfg())

    def test_no_quality_scores_section_halts_under_enforce(self) -> None:
        """An empty QUALITY_SCORES dict scores 0.0 across the board -> halt,
        instead of the legacy silent pass-through."""
        script = _make_script(scores={})
        with pytest.raises(pipeline.QualityCheckFailed):
            evaluate_script_quality(script, self._cfg())

    def test_legacy_default_still_averages_over_present(self) -> None:
        """With require_full_dimensions unset (default False), the same 2-strong-
        dims script averages over present only -> 1.0 -> passes. Pins that the
        flag (not some unrelated change) is what closes the exploit."""
        two = {SCRIPT_QUALITY_DIMENSIONS[0]: 1.0, SCRIPT_QUALITY_DIMENSIONS[1]: 1.0}
        script = _make_script(scores=two)
        config = {"script_quality": {"min_score": 0.50, "enforce_min_score": True}}
        assert evaluate_script_quality(script, config) is script

    def test_full_dimensions_ok_log_line_still_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The canonical 'Stage 1.5 OK' canary still fires in require_full mode."""
        script = _make_script()  # all 1.0
        with caplog.at_level("INFO", logger="pipeline"):
            evaluate_script_quality(script, self._cfg())
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Stage 1.5 OK" in joined


# ---------------------------------------------------------------------------
# Canonical OK log lines (canary for /start -auto's assertion grep)
# ---------------------------------------------------------------------------


class TestCanonicalOKLogLines:
    """`/start -auto` greps the per-run log for canonical OK prefixes before
    dropping `<topic_id>_master_QA_APPROVED.marker`. If we ever rename these
    log strings, the grep silently passes (or silently fails) — this test pins
    the exact prefix shape."""

    def test_stage_1_5_ok_log_line_emitted(self, caplog: pytest.LogCaptureFixture) -> None:
        script = _make_script()
        config = {
            "script_quality": {"min_score": 0.50, "enforce_min_score": True},
        }
        with caplog.at_level("INFO", logger="pipeline"):
            evaluate_script_quality(script, config)
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Stage 1.5 OK" in joined, f"missing 'Stage 1.5 OK' in: {joined}"
