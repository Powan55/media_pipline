"""Serialization tests for `idea_generation._build_audit_payload`.

Regression net for the `named_human_bonus` field on the audit JSON. Commit
7efb22e added the bonus to `ScoredCandidate` and the weighted-total math, but
forgot the surface-only field on `idea_generation_RANKED.json`. These tests
lock the field set so the next bonus addition can't quietly slip past again.

Added 2026-05-12 (Sprint 3 / Item 1).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from idea_generation import _build_audit_payload
from scoring import ScoredCandidate


def _make_candidate(**overrides: object) -> ScoredCandidate:
    """Build a `ScoredCandidate` with sensible defaults; override per-field as needed."""
    defaults: dict = {
        "topic": "Generic AI news",
        "angle": "Vendor shipped a thing",
        "hook_concept": "AI did stuff",
        "why_now": "today",
        "audience": "general AI consumers",
        "source_indexes": [0, 1],
        "cited_observation_candidate": {"source_handle": "u/test"},
        "counter_conventional_bonus": 0.0,
        "ai_vendor_bonus": 0.05,
        "named_human_bonus": 0.0,
        "weighted_total": 0.5,
        "rationale": "baseline",
    }
    defaults.update(overrides)
    return ScoredCandidate(**defaults)


# -----------------------------------------------------------------------------
# Happy path — named_human_bonus = 0.05 must round-trip through JSON
# -----------------------------------------------------------------------------

class TestNamedHumanBonusSerialization:
    def test_named_human_bonus_present_when_nonzero(self, tmp_path: Path) -> None:
        candidate = _make_candidate(named_human_bonus=0.05, weighted_total=1.067)
        payload = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "trends.json",
        )

        # Round-trip through JSON the same way generate_ideas writes it on disk.
        round_tripped = json.loads(json.dumps(payload))
        entry = round_tripped["ranked"][0]

        assert "named_human_bonus" in entry
        assert entry["named_human_bonus"] == 0.05

    def test_named_human_bonus_serialized_when_zero(self, tmp_path: Path) -> None:
        """0.0 is still emitted - it is NOT a falsy-omit field."""
        candidate = _make_candidate(named_human_bonus=0.0)
        payload = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "trends.json",
        )

        round_tripped = json.loads(json.dumps(payload))
        entry = round_tripped["ranked"][0]

        assert "named_human_bonus" in entry
        assert entry["named_human_bonus"] == 0.0


# -----------------------------------------------------------------------------
# Regression - the two pre-existing bonuses must still emit
# -----------------------------------------------------------------------------

class TestExistingBonusFieldsStillEmit:
    def test_counter_conventional_and_ai_vendor_fields_present(self, tmp_path: Path) -> None:
        candidate = _make_candidate(
            counter_conventional_bonus=0.05,
            ai_vendor_bonus=0.05,
            named_human_bonus=0.05,
        )
        payload = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "trends.json",
        )
        entry = json.loads(json.dumps(payload))["ranked"][0]

        assert entry["counter_conventional_bonus"] == 0.05
        assert entry["ai_vendor_bonus"] == 0.05
        assert entry["named_human_bonus"] == 0.05


# -----------------------------------------------------------------------------
# Field-set lock - guard against future fields being added to ScoredCandidate
# but forgotten in the serializer
# -----------------------------------------------------------------------------

class TestAuditPayloadShape:
    def test_per_candidate_field_set_is_complete(self, tmp_path: Path) -> None:
        candidate = _make_candidate()
        payload = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "trends.json",
        )
        entry = payload["ranked"][0]

        expected_fields = {
            "topic",
            "angle",
            "hook_concept",
            "why_now",
            "audience",
            "source_indexes",
            "cited_observation_candidate",
            "counter_conventional_bonus",
            "ai_vendor_bonus",
            "named_human_bonus",
            "corporate_deal_damped",
            "corporate_deal_penalty",
            "weighted_total",
            "rationale",
        }
        assert set(entry.keys()) == expected_fields

    def test_top_level_payload_metadata(self, tmp_path: Path) -> None:
        trends_path = tmp_path / "trends_2026-05-12.json"
        payload = _build_audit_payload(
            [_make_candidate()], n_target=10, n_picks=2, trends_path=trends_path,
        )
        assert payload["n_target"] == 10
        assert payload["n_picks"] == 2
        assert payload["trends_artifact"] == str(trends_path)
        assert "ranked_at" in payload
        # ranked_at must be ISO-8601 parseable
        datetime.fromisoformat(payload["ranked_at"])

    def test_empty_ranked_list_is_valid(self, tmp_path: Path) -> None:
        """No candidates => empty ranked array, not a crash."""
        payload = _build_audit_payload(
            [], n_target=10, n_picks=2, trends_path=tmp_path / "trends.json",
        )
        assert payload["ranked"] == []

    def test_order_preserved(self, tmp_path: Path) -> None:
        """The serializer must NOT re-sort - `ranked` came in pre-sorted from rank_candidates."""
        a = _make_candidate(topic="first", weighted_total=0.9)
        b = _make_candidate(topic="second", weighted_total=0.5)
        c = _make_candidate(topic="third", weighted_total=0.1)
        payload = _build_audit_payload(
            [a, b, c], n_target=10, n_picks=2, trends_path=tmp_path / "t.json",
        )
        topics = [e["topic"] for e in payload["ranked"]]
        assert topics == ["first", "second", "third"]


# -----------------------------------------------------------------------------
# Idempotence - same input => identical candidate block (modulo ranked_at timestamp)
# -----------------------------------------------------------------------------

class TestIdempotence:
    def test_per_candidate_block_is_deterministic(self, tmp_path: Path) -> None:
        candidate = _make_candidate(named_human_bonus=0.05)
        payload_1 = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "t.json",
        )
        payload_2 = _build_audit_payload(
            [candidate], n_target=10, n_picks=2, trends_path=tmp_path / "t.json",
        )
        # ranked_at differs (timestamp); the candidate block must not.
        assert payload_1["ranked"] == payload_2["ranked"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
