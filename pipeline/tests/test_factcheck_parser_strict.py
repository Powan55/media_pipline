"""Strict-or-raise tests for `pipeline._parse_factcheck_response` status cell.

Pins the fix for WORKFLOW_AUDIT_2026-05-16 H1 (recurring c7+c8). The parser
previously fell back to `UNVERIFIABLE` with `log.warning` for any non-canonical
status (e.g. ``VERIFIED (paraphrase)``, ``LIKELY_WRONG (minor)``,
``UNCLEAR — needs source``). That silent downgrade meant auto-resolve mode
(`auto_resolve_gate_2: true`) shipped the claim untouched because
``UNVERIFIABLE`` is not in the eligible-for-fix set.

These tests assert the parser now raises a `ValueError` carrying the offending
raw status string, the canonical 4-tuple, and re-run guidance.

Also covers the audit M3 6th `Tool` column (added 2026-05-16): canonical
tavily/web/none values parse, 5-column legacy falls back to `unknown` with a
header WARNING, invalid Tool raises ValueError, and the mix-summary log line
fires on parse.

Run:
    python -m pytest tests/test_factcheck_parser_strict.py -v
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import _parse_factcheck_response  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_HEADER = (
    "| Claim | Status | Source URL | Source quote | Suggested fix |\n"
    "|-------|--------|------------|--------------|----------------|\n"
)

# 6-column header (audit M3) — new responses since 2026-05-16.
_HEADER_6 = (
    "| Claim | Status | Source URL | Source quote | Suggested fix | Tool |\n"
    "|-------|--------|------------|--------------|----------------|------|\n"
)


def _table(status_cell: str, claim_text: str = "Anthropic 25% emotional-support figure") -> str:
    """Build a single-row 5-column legacy fact-check table with the given Status cell."""
    return (
        _HEADER
        + f"| {claim_text} | {status_cell} | https://example.com | \"...\" | (n/a) |\n"
    )


def _table_6(
    status_cell: str,
    tool_cell: str = "tavily",
    claim_text: str = "Anthropic 25% emotional-support figure",
) -> str:
    """Build a single-row 6-column fact-check table with the given Status + Tool cells."""
    return (
        _HEADER_6
        + f"| {claim_text} | {status_cell} | https://example.com | \"...\" | (n/a) | {tool_cell} |\n"
    )


# ---------------------------------------------------------------------------
# Canonical statuses still parse cleanly (guard against over-correction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_cell, expected",
    [
        ("VERIFIED", "VERIFIED"),
        ("UNCLEAR", "UNCLEAR"),
        ("LIKELY WRONG", "LIKELY_WRONG"),
        ("LIKELY_WRONG", "LIKELY_WRONG"),
        ("UNVERIFIABLE", "UNVERIFIABLE"),
    ],
)
def test_canonical_status_still_parses(status_cell: str, expected: str) -> None:
    """The 4 canonical statuses (+ LIKELY_WRONG alias) round-trip without raising."""
    report = _parse_factcheck_response(_table(status_cell), topic_id="t1")
    assert len(report.claims) == 1
    assert report.claims[0].status == expected


# ---------------------------------------------------------------------------
# Non-canonical statuses raise — recurring c7+c8 patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_cell",
    [
        "VERIFIED (paraphrase)",
        "LIKELY_WRONG (minor)",
        "UNCLEAR — needs source",
        "- VERIFIED",
        "1. VERIFIED",
        "VERIFIED with caveat",
        "LIKELY WRONG (severe)",
        "UNCLEAR, needs more sourcing",
    ],
)
def test_non_canonical_status_raises_value_error(status_cell: str) -> None:
    """Any non-canonical Status cell raises ValueError with the offending string."""
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(_table(status_cell), topic_id="t1")
    msg = str(excinfo.value)
    assert status_cell in msg, f"offending status {status_cell!r} missing from {msg!r}"
    # Canonical 4-tuple must be cited in the error so the operator/LLM knows
    # what to put in the cell when they re-write factcheck_RESPONSE.txt.
    assert "VERIFIED" in msg
    assert "UNCLEAR" in msg
    assert "LIKELY WRONG" in msg
    assert "UNVERIFIABLE" in msg
    # Re-run guidance points the operator at the producer artifact.
    assert "factcheck_RESPONSE.txt" in msg


def test_error_message_includes_claim_text() -> None:
    """The raised ValueError references the offending claim so the operator
    can locate it in factcheck_RESPONSE.txt — not just the status string."""
    claim = "ChatGPT cost forty-two billion dollars to train"
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(
            _table("VERIFIED (paraphrase)", claim_text=claim),
            topic_id="t1",
        )
    assert claim[:40] in str(excinfo.value)


# ---------------------------------------------------------------------------
# WORKFLOW_AUDIT_2026-05-31 H2 — empty Claim cell raises (phantom-row guard)
# ---------------------------------------------------------------------------


def test_empty_claim_cell_raises() -> None:
    """A row with a non-empty Status but an EMPTY Claim cell is a phantom row.

    It survives the full-empty-row skip (the Status/URL cells are populated) and
    the literal-'claim'-header skip, so without the H2 guard it would append a
    FactClaim with claim_text='' and inflate the gate-2 unresolved count. The
    parser must raise a ValueError naming the Claim cell + the producer artifact.
    """
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(_table("VERIFIED", claim_text=""), topic_id="t1")
    msg = str(excinfo.value)
    assert "Claim" in msg
    assert "factcheck_RESPONSE.txt" in msg


@pytest.mark.parametrize("blank_claim", [" ", "   ", '""', '" "', '   "   "  '])
def test_whitespace_or_quote_only_claim_raises(blank_claim: str) -> None:
    """A claim cell that is only whitespace and/or quote chars normalizes to
    empty (same way FactClaim normalizes claim_text) and must also raise — not
    sneak through as a blank-named claim."""
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(_table("VERIFIED", claim_text=blank_claim), topic_id="t1")
    assert "Claim" in str(excinfo.value)


def test_nonempty_claim_still_parses_after_h2() -> None:
    """Regression guard: H2's raise must not break the happy path. A normal
    non-empty claim still parses to exactly one FactClaim (mirrors
    test_canonical_status_still_parses)."""
    report = _parse_factcheck_response(
        _table("VERIFIED", claim_text="Anthropic 25% emotional-support figure"),
        topic_id="t1",
    )
    assert len(report.claims) == 1
    assert report.claims[0].status == "VERIFIED"
    assert report.claims[0].claim_text == "Anthropic 25% emotional-support figure"


# ---------------------------------------------------------------------------
# Audit M3 — Tool column (tavily / web / none / unknown legacy fallback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_cell", ["tavily", "web", "none"])
def test_canonical_tool_value_parses(tool_cell: str) -> None:
    """All 3 canonical Tool values round-trip without raising."""
    report = _parse_factcheck_response(_table_6("VERIFIED", tool_cell), topic_id="t1")
    assert len(report.claims) == 1
    assert report.claims[0].tool_used == tool_cell


def test_tool_value_uppercase_normalized() -> None:
    """Tool cell is case-insensitive — TAVILY / Tavily / tavily all parse."""
    report = _parse_factcheck_response(_table_6("VERIFIED", "TAVILY"), topic_id="t1")
    assert report.claims[0].tool_used == "tavily"


@pytest.mark.parametrize(
    "tool_cell",
    ["perplexity", "google-search", "tavily-mcp", "websearch", "internal"],
)
def test_non_canonical_tool_raises(tool_cell: str) -> None:
    """Any non-canonical Tool cell raises ValueError citing the canonical 3-tuple."""
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(_table_6("VERIFIED", tool_cell), topic_id="t1")
    msg = str(excinfo.value)
    assert tool_cell in msg, f"offending tool {tool_cell!r} missing from {msg!r}"
    # Canonical 3-tuple must be cited so the operator knows the allowed values.
    assert "tavily" in msg
    assert "web" in msg
    assert "none" in msg
    assert "factcheck_RESPONSE.txt" in msg


def test_legacy_5col_falls_back_to_unknown_with_warning(caplog) -> None:
    """5-column legacy responses parse with tool_used='unknown' + WARNING."""
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        report = _parse_factcheck_response(_table("VERIFIED"), topic_id="t1")
    assert report.claims[0].tool_used == "unknown"
    # WARNING must mention the missing Tool column so the operator can act.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("Tool" in r.getMessage() for r in warnings), (
        f"expected WARNING about missing Tool column, got: "
        f"{[r.getMessage() for r in warnings]}"
    )


def test_tool_mix_summary_log_fires(caplog) -> None:
    """The summary log line `fact-check tool mix:` fires after parsing."""
    response = (
        _HEADER_6
        + "| Claim A | VERIFIED | https://a | \"q\" | (n/a) | tavily |\n"
        + "| Claim B | VERIFIED | https://b | \"q\" | (n/a) | web |\n"
        + "| Claim C | VERIFIED | https://c | \"q\" | (n/a) | none |\n"
    )
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _parse_factcheck_response(response, topic_id="t1")
    info_lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("tool mix" in line for line in info_lines), info_lines
    assert any("tavily=1" in line and "web=1" in line and "none=1" in line for line in info_lines)


def test_web_count_above_zero_emits_warning(caplog) -> None:
    """Non-zero `web` count triggers a WARNING citing the durable rule."""
    response = (
        _HEADER_6
        + "| Claim A | VERIFIED | https://a | \"q\" | (n/a) | web |\n"
        + "| Claim B | VERIFIED | https://b | \"q\" | (n/a) | tavily |\n"
    )
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        _parse_factcheck_response(response, topic_id="t1")
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "WebSearch" in line or "WebFetch" in line for line in warnings
    ), f"expected WebSearch/WebFetch warning, got: {warnings}"
    assert any("feedback_tavily_mcp.md" in line for line in warnings)


def test_web_count_zero_no_warning(caplog) -> None:
    """When every claim used tavily, no WARNING about WebSearch fires."""
    response = (
        _HEADER_6
        + "| Claim A | VERIFIED | https://a | \"q\" | (n/a) | tavily |\n"
        + "| Claim B | VERIFIED | https://b | \"q\" | (n/a) | tavily |\n"
    )
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        _parse_factcheck_response(response, topic_id="t1")
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any("WebSearch" in line or "WebFetch" in line for line in warnings), (
        f"unexpected WebSearch warning: {warnings}"
    )


def test_empty_tool_cell_in_6col_raises() -> None:
    """An empty Tool cell when the column IS present is malformed — raise."""
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(_table_6("VERIFIED", ""), topic_id="t1")
    msg = str(excinfo.value)
    assert "Tool" in msg
    assert "tavily" in msg


def test_tool_header_alias_tool_used() -> None:
    """`Tool used` header alias is recognized in addition to bare `Tool`."""
    header = (
        "| Claim | Status | Source URL | Source quote | Suggested fix | Tool used |\n"
        "|-------|--------|------------|--------------|----------------|-----------|\n"
    )
    response = (
        header
        + "| Claim A | VERIFIED | https://a | \"q\" | (n/a) | tavily |\n"
    )
    report = _parse_factcheck_response(response, topic_id="t1")
    assert report.claims[0].tool_used == "tavily"


def test_cycle9_factcheck_5col_legacy_still_parses(tmp_path) -> None:
    """Cycle-9 (2026-05-16_001) factcheck_RESPONSE.txt is 5-column legacy
    and MUST continue to parse after the M3 change (backward compat)."""
    # Inline a representative slice of the cycle-9 response so the test is
    # self-contained and doesn't depend on the live ContentOps tree.
    cycle9_excerpt = (
        "| Claim | Status | Source URL | Source quote | Suggested fix |\n"
        "|-------|--------|------------|--------------|----------------|\n"
        "| Anthropic stress-tested Claude | VERIFIED | https://example.com | \"...\" | No fix needed |\n"
        "| Claude saw it would be shut off | VERIFIED | https://example.com | \"...\" | No fix needed |\n"
        "| Dario Amodei announced it | UNCLEAR | https://example.com | \"...\" | Replace 'Dario Amodei' with 'Anthropic' |\n"
    )
    report = _parse_factcheck_response(cycle9_excerpt, topic_id="2026-05-16_001")
    assert len(report.claims) == 3
    # All rows fall back to unknown since the Tool column was absent.
    assert all(c.tool_used == "unknown" for c in report.claims)


# ---------------------------------------------------------------------------
# WORKFLOW_AUDIT_2026-05-31 L2 — a detected header that leaves a REQUIRED
# column unmapped must fail loud instead of silently using a positional default.
# ---------------------------------------------------------------------------


def test_header_present_but_required_column_unmapped_raises() -> None:
    """A header IS detected (separator row present), but Status is labelled with
    a non-alias (`Verdict-Code`) so it fails to classify. Without L2 the Status
    column would silently retain its positional default and parse the WRONG cell;
    the parser must instead raise a ValueError naming the unmapped column and the
    producer artifact.
    """
    response = (
        "| Claim | Verdict-Code | URL | Quote | Fix |\n"
        "|-------|--------------|-----|-------|-----|\n"
        "| Anthropic 25% figure | VERIFIED | https://example.com | \"q\" | (n/a) |\n"
    )
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(response, topic_id="t1")
    msg = str(excinfo.value)
    # Names the unmapped required column...
    assert "status" in msg
    # ...echoes the header cells the parser saw...
    assert "Verdict-Code" in msg
    # ...and points the operator at the producer artifact to re-run.
    assert "factcheck_RESPONSE.txt" in msg


@pytest.mark.parametrize(
    "bad_header, missing",
    [
        # Claim mislabelled (no "claim" substring) -> claim unmapped.
        ("| Assertion | Status | Source URL | Quote | Fix |", "claim"),
        # URL mislabelled (not url/link/source) -> url unmapped.
        ("| Claim | Status | Reference | Quote | Fix |", "url"),
    ],
)
def test_other_unmappable_required_columns_raise(bad_header: str, missing: str) -> None:
    """Symmetric coverage: a mislabelled Claim or URL header column also raises."""
    sep = "|" + "|".join(["---"] * (bad_header.count("|") - 1)) + "|"
    response = (
        f"{bad_header}\n{sep}\n"
        "| Anthropic 25% figure | VERIFIED | https://example.com | \"q\" | (n/a) |\n"
    )
    with pytest.raises(ValueError) as excinfo:
        _parse_factcheck_response(response, topic_id="t1")
    assert missing in str(excinfo.value)
    assert "factcheck_RESPONSE.txt" in str(excinfo.value)


def test_canonical_header_still_parses_after_l2() -> None:
    """Regression: a standard header maps claim/status/url cleanly, so the L2
    raise never fires and the row parses normally."""
    report = _parse_factcheck_response(_table("VERIFIED"), topic_id="t1")
    assert len(report.claims) == 1
    assert report.claims[0].status == "VERIFIED"


def test_six_col_canonical_header_still_parses_after_l2() -> None:
    """Regression: the 6-column M3 header (with the optional Tool column) maps
    all required columns and parses without tripping the L2 raise."""
    report = _parse_factcheck_response(_table_6("VERIFIED", "tavily"), topic_id="t1")
    assert len(report.claims) == 1
    assert report.claims[0].tool_used == "tavily"


def test_legacy_no_header_positional_table_unaffected_by_l2() -> None:
    """Regression: a table with NO header row (no separator) hits the strict
    positional 5-column default path (header_idx is None). The L2 raise fires
    ONLY when a header is detected, so this legacy shape must still parse."""
    no_header = (
        "| Anthropic 25% figure | VERIFIED | https://example.com | \"q\" | (n/a) |\n"
        "| ChatGPT trained on data | UNCLEAR | https://example.com | \"q\" | fix it |\n"
    )
    report = _parse_factcheck_response(no_header, topic_id="t1")
    assert len(report.claims) == 2
    assert report.claims[0].status == "VERIFIED"
    assert report.claims[1].status == "UNCLEAR"
