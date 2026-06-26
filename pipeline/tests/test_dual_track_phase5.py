"""Phase 5 dual-track tests (2026-06-21).

Covers: hook #15 (Personal-Breakthrough Lead) registration, the config-driven
fact-check prompt selection (general-tech points at the stricter variant;
ai-vendor defaults unchanged), and parser-safety of the general-tech 'add rows
only' fact-check format (the 3 crazy-story required rows stay within the strict
6-column / 4-status / 3-tool parser contract).
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
    ScriptDraft,
    _KNOWN_HOOK_FORMULAS,
    _parse_factcheck_response,
    fact_check_script,
)


# ---------------------------------------------------------------------------
# Hook #15 registration + GT fact-check prompt artifact
# ---------------------------------------------------------------------------


def test_personal_breakthrough_lead_registered():
    assert "personal-breakthrough lead" in _KNOWN_HOOK_FORMULAS


def test_general_tech_fact_check_prompt_exists_and_keeps_6col_contract():
    p = REPO_ROOT / "prompts" / "05_fact_check_general_tech.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "| Claim | Status | Source URL | Source quote | Suggested fix | Tool |" in text
    # Must not invent a new Status/Tool vocabulary (parser would reject it).
    assert "VERIFIED / UNCLEAR / LIKELY WRONG / UNVERIFIABLE" in text


# ---------------------------------------------------------------------------
# Config-driven fact-check prompt selection
# ---------------------------------------------------------------------------


def _script_draft() -> ScriptDraft:
    return ScriptDraft(
        topic_id="2026-06-21_001",
        hook_variants=["A nurse caught a misdiagnosis"],
        body="A nurse caught a misdiagnosis her hospital missed.",
        broll_cues=[],
        fact_check_queue=[],
        word_count=8,
    )


_VALID_FC_RESPONSE = (
    "| Claim | Status | Source URL | Source quote | Suggested fix | Tool |\n"
    "|-------|--------|------------|--------------|----------------|------|\n"
    "| A nurse caught a misdiagnosis | VERIFIED | https://news.example/x | \"she flagged it\" | (n/a) | tavily |\n"
)


def test_fact_check_script_honors_config_prompt(monkeypatch):
    captured: dict[str, str] = {}

    def fake_load_prompt(name, config):
        captured["name"] = name
        return "FACTCHECK {SCRIPT}"

    monkeypatch.setattr(pipeline, "load_prompt", fake_load_prompt)
    monkeypatch.setattr(pipeline, "_await_manual_response", lambda *a, **k: _VALID_FC_RESPONSE)

    config = {"fact_check": {"provider": "manual", "prompt": "05_fact_check_general_tech"}}
    report = fact_check_script(_script_draft(), config)
    assert captured["name"] == "05_fact_check_general_tech"
    assert len(report.claims) >= 1


def test_fact_check_script_defaults_to_standard_prompt(monkeypatch):
    captured: dict[str, str] = {}

    def fake_load_prompt(name, config):
        captured["name"] = name
        return "FACTCHECK {SCRIPT}"

    monkeypatch.setattr(pipeline, "load_prompt", fake_load_prompt)
    monkeypatch.setattr(pipeline, "_await_manual_response", lambda *a, **k: _VALID_FC_RESPONSE)

    config = {"fact_check": {"provider": "manual"}}  # no prompt key → default
    fact_check_script(_script_draft(), config)
    assert captured["name"] == "05_fact_check"


# ---------------------------------------------------------------------------
# Parser-safety: the GT 'add rows only' format stays within the strict parser
# ---------------------------------------------------------------------------


def test_general_tech_extra_rows_parse_cleanly():
    table = (
        "| Claim | Status | Source URL | Source quote | Suggested fix | Tool |\n"
        "|-------|--------|------------|--------------|----------------|------|\n"
        "| Protagonist Sarah Chen is real and named | VERIFIED | https://news/x | \"nurse Sarah Chen\" | (n/a) | tavily |\n"
        "| Framing 'caught misdiagnosis' matches source | VERIFIED | https://news/x | \"flagged the error\" | (n/a) | tavily |\n"
        "| Outcome not fabricated | UNCLEAR | https://news/x | \"outcome not stated\" | needs follow-up | web |\n"
        "| The hospital is in Ohio | LIKELY WRONG | https://news/x | \"Michigan\" | say Michigan | tavily |\n"
    )
    report = _parse_factcheck_response(table, "2026-06-21_001")
    assert len(report.claims) == 4
    assert {c.status for c in report.claims} <= {
        "VERIFIED", "UNCLEAR", "LIKELY_WRONG", "UNVERIFIABLE",
    }
