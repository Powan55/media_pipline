"""Integration test for the auto-resolve gate-2 promotion path.

Exercises ``pipeline.await_fact_check_resolution`` in auto-resolve mode with a
synthetic ``script_RESPONSE.txt`` that mirrors the 2026-05-13 ``_12_002``
failure pattern (``SCRIPT_BODY (uses HOOK_A as the verbal opener):`` header
between hooks and body). The test pins the contract:

    1. ``script_FINAL.txt`` is written to disk.
    2. Its body contains the original prose and every ``[B-ROLL: ...]`` cue.
    3. Its body does NOT contain the substring ``SCRIPT_BODY``.
    4. The Sprint 5 Layer-2 scan ``_scan_script_for_artifacts_or_halt`` does
       NOT halt on the new file — meaning the parser eliminates the artifact
       at the source and Layer 2's fire rate on auto-resolve drops to zero by
       construction.

The operator-signed and manual paths in ``await_fact_check_resolution`` are
NOT touched here (see the parent task scope) — this test only exercises the
``auto_resolve=True`` branch where the parser fix lives.
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
    FactCheckReport,
    ScriptDraft,
    _scan_script_for_artifacts_or_halt,
    await_fact_check_resolution,
)


# ---------------------------------------------------------------------------
# Fixture text — mirrors the _12_002 failure pattern
# ---------------------------------------------------------------------------


_12_002_RESPONSE_TEXT = (
    "HOOK_A: Claude just admitted twenty-five percent of its advice is flattery.   [formula: Contradiction]\n"
    "HOOK_B: Anthropic published the number itself.   [formula: Specific-Number Promise]\n"
    "HOOK_C: One Reddit thread noticed Claude turning into a therapist.   [formula: Cited-Observation Lead]\n"
    "\n"
    "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
    "Claude just admitted twenty-five percent of its advice is flattery. "
    "[B-ROLL: Anthropic Claude logo zoom-in] "
    "Anthropic's own research post says a quarter of personal-use conversations are emotional-support requests. "
    "[B-ROLL: Reddit thread screenshot scrolling] "
    "And the spirituality category? Nearly triple the average for everything else. "
    "[B-ROLL: bar chart bars rising]\n"
    "\n"
    "FACT_CHECK_QUEUE\n"
    "- Anthropic 25% emotional-support figure\n"
    "- spirituality category 3x average\n"
    "\n"
    "QUALITY_SCORES\n"
    "- hook_strength: 0.85\n"
    "- specificity: 0.70\n"
    "- opinion_density: 0.60\n"
    "- cited_observation_quality: 0.80\n"
    "- broll_cadence: 0.75\n"
    "- rationale: Specific-number opening, named source, three B-ROLL cues.\n"
)


# Body field on ScriptDraft. In production this is what
# `_parse_script_response` returned BEFORE the fix — it includes the
# SCRIPT_BODY artifact. Embedding the dirty body here proves the auto-resolve
# branch derives its `proposed_body` from the response file via the new
# parser, NOT from `script.body`.
_DIRTY_SCRIPT_BODY = (
    "SCRIPT_BODY (uses HOOK_A as the verbal opener):\n"
    "Claude just admitted twenty-five percent of its advice is flattery. "
    "[B-ROLL: Anthropic Claude logo zoom-in] "
    "Anthropic's own research post says a quarter of personal-use conversations are emotional-support requests. "
    "[B-ROLL: Reddit thread screenshot scrolling] "
    "And the spirituality category? Nearly triple the average for everything else. "
    "[B-ROLL: bar chart bars rising]"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def topic_id() -> str:
    return "2026-05-13_test_001"


@pytest.fixture
def topic_dir(tmp_path: Path, topic_id: str) -> Path:
    """Build a per-topic dir with the synthetic ``script_RESPONSE.txt``."""
    d = tmp_path / topic_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "script_RESPONSE.txt").write_text(_12_002_RESPONSE_TEXT, encoding="utf-8")
    return d


@pytest.fixture
def auto_resolve_config(tmp_path: Path) -> dict:
    return {
        "fact_check": {
            "require_human_resolution": True,
            "auto_resolve_gate_2": True,
        },
        "llm": {
            "manual_io_dir": str(tmp_path),
        },
    }


@pytest.fixture
def script_draft(topic_id: str) -> ScriptDraft:
    """A ScriptDraft whose `body` carries the dirty SCRIPT_BODY artifact.

    The auto-resolve branch should NOT read from this field — it should
    re-parse `script_RESPONSE.txt`. If the new code path is wrong and falls
    back to `script.body`, the integration test fails on the SCRIPT_BODY
    substring assertion.
    """
    return ScriptDraft(
        topic_id=topic_id,
        hook_variants=[
            "Claude just admitted twenty-five percent of its advice is flattery.",
            "Anthropic published the number itself.",
            "One Reddit thread noticed Claude turning into a therapist.",
        ],
        hook_formulas=["Contradiction", "Specific-Number Promise", "Cited-Observation Lead"],
        body=_DIRTY_SCRIPT_BODY,
        broll_cues=[
            "Anthropic Claude logo zoom-in",
            "Reddit thread screenshot scrolling",
            "bar chart bars rising",
        ],
        fact_check_queue=[
            "Anthropic 25% emotional-support figure",
            "spirituality category 3x average",
        ],
        word_count=42,
    )


@pytest.fixture
def empty_report(topic_id: str) -> FactCheckReport:
    """No claims — no fixes to apply. Exercises the parse → extract → write
    path with zero `_try_apply_fix` calls in between."""
    return FactCheckReport(topic_id=topic_id, claims=[])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutoResolvePromotionFixesArtifact:
    """The 2026-05-13 `_12_002` regression must not recur in auto-resolve mode."""

    def test_script_final_is_written(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        assert (topic_dir / "script_FINAL.txt").exists()

    def test_script_final_excludes_script_body_artifact(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        final_text = (topic_dir / "script_FINAL.txt").read_text(encoding="utf-8")
        assert "SCRIPT_BODY" not in final_text
        assert "(uses HOOK_A" not in final_text
        assert "verbal opener" not in final_text

    def test_script_final_preserves_broll_cues(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        final_text = (topic_dir / "script_FINAL.txt").read_text(encoding="utf-8")
        assert "[B-ROLL: Anthropic Claude logo zoom-in]" in final_text
        assert "[B-ROLL: Reddit thread screenshot scrolling]" in final_text
        assert "[B-ROLL: bar chart bars rising]" in final_text

    def test_script_final_preserves_original_prose(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        final_text = (topic_dir / "script_FINAL.txt").read_text(encoding="utf-8")
        assert "twenty-five percent" in final_text
        assert "spirituality category" in final_text

    def test_sprint_5_layer_2_scan_passes_on_auto_resolve_output(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        """The defense-in-depth Layer-2 scan should be a no-op on the new
        file. This is the property that makes the parser fix complete:
        Layer 2 stays as defense-in-depth, but its fire rate on auto-resolve
        output is zero by construction."""
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        final_path = topic_dir / "script_FINAL.txt"
        # Should NOT raise PipelineQAFailed.
        _scan_script_for_artifacts_or_halt(final_path, script_draft.topic_id)

    def test_returned_script_carries_clean_body(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        """The returned ScriptDraft's `body` field should be the clean body —
        downstream TTS / caption stages read from `script.body`."""
        out = await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        assert "SCRIPT_BODY" not in out.body
        assert "twenty-five percent" in out.body

    def test_auto_resolution_audit_written(
        self,
        script_draft: ScriptDraft,
        empty_report: FactCheckReport,
        auto_resolve_config: dict,
        topic_dir: Path,
    ) -> None:
        await_fact_check_resolution(script_draft, empty_report, auto_resolve_config)
        # The audit file is part of the auto-resolve contract.
        assert (topic_dir / "factcheck_AUTO_RESOLUTION.md").exists()
