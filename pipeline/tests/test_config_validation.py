"""Tests for pipeline.validate_config (WORKFLOW_AUDIT_2026-05-31 M4).

`load_config` previously returned the raw YAML dict with NO key validation, so a
missing key surfaced as a bare KeyError deep inside a stage (e.g.
config["assets"]["preferred_stock_provider"] at fetch_assets). M4 adds a separate
`validate_config()` — called from the CLI entrypoints, NOT from bare load_config —
that fails fast at startup, naming ALL missing keys at once.

These tests assert:
  - the real config.yaml passes validation;
  - a config missing keys raises ONE ValueError that names every missing path;
  - load_config itself stays permissive (the dual-agent isolation contract test
    loads a deliberately minimal config and must not start raising).

Run:
    python -m pytest tests/test_config_validation.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import load_config, validate_config  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config.yaml"


def test_validate_config_passes_on_full_config() -> None:
    """The real config.yaml (post-H1, with paths.project_root) validates clean."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    # Must not raise.
    validate_config(config)


def test_validate_config_reports_all_missing_keys() -> None:
    """A config missing TWO keys raises a single ValueError naming BOTH paths
    (aggregated, not one-at-a-time)."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    # Drop one key from two different sections.
    del config["assets"]["preferred_stock_provider"]
    del config["tts"]["provider"]
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    msg = str(excinfo.value)
    assert "assets.preferred_stock_provider" in msg
    assert "tts.provider" in msg


def test_validate_config_reports_whole_missing_section() -> None:
    """If an entire required section is absent, every key under it is named."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    del config["paths"]  # whole section gone
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    msg = str(excinfo.value)
    for key in ("paths.channel_root", "paths.logs", "paths.prompts", "paths.project_root"):
        assert key in msg, f"{key} missing from aggregated error: {msg!r}"


def test_validate_config_requires_project_root() -> None:
    """H1's paths.project_root is part of the required set (M4 guards it)."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    del config["paths"]["project_root"]
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    assert "paths.project_root" in str(excinfo.value)


def test_validate_config_presence_only_does_not_mutate_sacred_keys() -> None:
    """validate_config reads sacred keys to assert presence but must NOT inject
    defaults or change their values."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    before_fc = dict(config["fact_check"])
    before_pub = dict(config["publishing"])
    validate_config(config)
    assert config["fact_check"] == before_fc
    assert config["publishing"] == before_pub


def _full_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_optional_tracks_block_absent_is_fine() -> None:
    """A config with NO tracks block validates clean (single-track mode)."""
    config = _full_config()
    config.pop("tracks", None)
    validate_config(config)  # must not raise


def test_well_formed_tracks_block_passes() -> None:
    """The dual-track block shipped in config.yaml is well-formed."""
    config = _full_config()
    assert "tracks" in config, "config.yaml should carry the dual-track block"
    validate_config(config)  # must not raise


def test_malformed_dual_track_enabled_raises() -> None:
    config = _full_config()
    config.setdefault("tracks", {})["dual_track_enabled"] = "yes"  # not a bool
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    assert "dual_track_enabled" in str(excinfo.value)


def test_malformed_general_tech_slot_raises() -> None:
    config = _full_config()
    config.setdefault("tracks", {}).setdefault("general_tech", {})["slot"] = "two"  # not an int
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    assert "slot" in str(excinfo.value)


def test_malformed_suppress_flag_raises() -> None:
    config = _full_config()
    config.setdefault("tracks", {}).setdefault("general_tech", {})["suppress_ai_vendor_bonus"] = 1  # not a bool
    with pytest.raises(ValueError) as excinfo:
        validate_config(config)
    assert "suppress_ai_vendor_bonus" in str(excinfo.value)


def test_load_config_stays_permissive_on_minimal_config(tmp_path) -> None:
    """load_config must NOT validate — a minimal config (only render +
    paths.channel_root, like the dual-agent isolation fixture) still loads
    without raising. (validate_config is the strict gate, called separately.)"""
    minimal = tmp_path / "config.yaml"
    minimal.write_text(
        "render:\n  resolution: [1080, 1920]\npaths:\n  channel_root: C:\\fake\\channel\n",
        encoding="utf-8",
    )
    # load_config also requires a .env; the repo's .env exists (baseline suite
    # depends on it), so this exercises the permissive load path end-to-end.
    config = load_config(minimal)
    assert config["paths"]["channel_root"] == r"C:\fake\channel"
    # And the strict validator WOULD reject this minimal config — proving the
    # two are decoupled (load permissive, validate strict).
    with pytest.raises(ValueError):
        validate_config(config)
