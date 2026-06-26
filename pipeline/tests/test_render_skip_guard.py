"""Unit tests for the re-render skip-guard (`pipeline._approved_master_intact`).

CODE_AUDIT item (e): the render re-encodes unconditionally on every re-run, even
when an approved, structurally-intact master already exists (~1-2 min wasted). The
skip-guard is a pure, read-only pre-check ABOVE the RenderLock that returns True
only when ALL of:
    1. config.render.skip_if_approved_master is true,
    2. the final master file exists,
    3. its gate-3 `<stem>_QA_APPROVED.marker` exists, and
    4. tools.media_integrity.check_integrity passes on the master.

Contract pinned here (the audit's four cases):
    pass            -> skip   (returns True)
    integrity-fail  -> re-render (returns False, never raises)
    marker-missing  -> re-render (returns False)
    config-off      -> re-render (returns False)
plus: master-missing -> False, config-key-absent -> False, and a generic probe
error -> False (the guard must NEVER break the render path).

check_integrity is monkeypatched (the guard imports it lazily as
`from tools.media_integrity import check_integrity`, so patching the attribute on
the `tools.media_integrity` module makes the lazy import resolve to the patch).
No ffmpeg is invoked; masters are plain tmp files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402
import tools.media_integrity as media_integrity  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_master_and_marker(tmp_path: Path, *, with_marker: bool = True) -> Path:
    """Create a fake master file (+ its gate-3 marker, matching await_final_qa)."""
    master = tmp_path / "2026-06-11_001_master.mp4"
    master.write_bytes(b"fake master bytes")
    if with_marker:
        marker = master.parent / f"{master.stem}_QA_APPROVED.marker"
        marker.write_text("", encoding="utf-8")
    return master


def _config(*, enabled: bool | None = True) -> dict:
    if enabled is None:
        return {"render": {}}  # key absent
    return {"render": {"skip_if_approved_master": enabled}}


def _patch_integrity_pass(monkeypatch) -> None:
    monkeypatch.setattr(media_integrity, "check_integrity", lambda p, **kw: {"size_bytes": 10})


def _patch_integrity_fail(monkeypatch) -> None:
    def _boom(p, **kw):
        raise media_integrity.MediaIntegrityError(p, "fake: moov atom not found")

    monkeypatch.setattr(media_integrity, "check_integrity", _boom)


# ---------------------------------------------------------------------------
# The four audit cases
# ---------------------------------------------------------------------------


def test_pass_returns_true_skip(monkeypatch, tmp_path):
    """master + marker + integrity-OK + enabled -> True (skip the encode)."""
    master = _make_master_and_marker(tmp_path)
    _patch_integrity_pass(monkeypatch)
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is True


def test_integrity_fail_returns_false_rerender(monkeypatch, tmp_path, caplog):
    """master + marker but integrity FAILS -> False (re-render), and it does NOT raise."""
    master = _make_master_and_marker(tmp_path)
    _patch_integrity_fail(monkeypatch)
    # Must not raise — a corrupt approved master falls through to a fresh render.
    result = pipeline._approved_master_intact(master, _config(enabled=True))
    assert result is False
    assert any("FAILED integrity" in r.getMessage() for r in caplog.records)


def test_marker_missing_returns_false_rerender(monkeypatch, tmp_path):
    """master + integrity-OK but NO gate-3 marker -> False (re-render)."""
    master = _make_master_and_marker(tmp_path, with_marker=False)
    _patch_integrity_pass(monkeypatch)
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is False


def test_config_off_returns_false_rerender(monkeypatch, tmp_path):
    """Everything present but skip_if_approved_master=false -> False (legacy re-render)."""
    master = _make_master_and_marker(tmp_path)
    _patch_integrity_pass(monkeypatch)
    assert pipeline._approved_master_intact(master, _config(enabled=False)) is False


# ---------------------------------------------------------------------------
# Edge cases — the guard must only ever AVOID work, never break the render path
# ---------------------------------------------------------------------------


def test_master_missing_returns_false(monkeypatch, tmp_path):
    """No master file at all -> False (and integrity is never probed)."""
    master = tmp_path / "2026-06-11_001_master.mp4"  # never created
    probed = {"n": 0}

    def _should_not_run(p, **kw):
        probed["n"] += 1
        return {}

    monkeypatch.setattr(media_integrity, "check_integrity", _should_not_run)
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is False
    assert probed["n"] == 0  # short-circuits before the integrity probe


def test_config_key_absent_returns_false(monkeypatch, tmp_path):
    """render section present but skip_if_approved_master key absent -> False (default off)."""
    master = _make_master_and_marker(tmp_path)
    _patch_integrity_pass(monkeypatch)
    assert pipeline._approved_master_intact(master, _config(enabled=None)) is False


def test_integrity_filenotfound_returns_false(monkeypatch, tmp_path):
    """check_integrity raising FileNotFoundError -> False (re-render), no raise."""
    master = _make_master_and_marker(tmp_path)

    def _gone(p, **kw):
        raise FileNotFoundError(2, "vanished mid-check")

    monkeypatch.setattr(media_integrity, "check_integrity", _gone)
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is False


def test_generic_probe_error_returns_false(monkeypatch, tmp_path, caplog):
    """An unexpected probe exception must NOT propagate -> False (re-render)."""
    master = _make_master_and_marker(tmp_path)

    def _explode(p, **kw):
        raise RuntimeError("ffprobe segfaulted")

    monkeypatch.setattr(media_integrity, "check_integrity", _explode)
    result = pipeline._approved_master_intact(master, _config(enabled=True))
    assert result is False
    assert any("integrity probe errored" in r.getMessage() for r in caplog.records)


def test_marker_construction_matches_await_final_qa(monkeypatch, tmp_path):
    """The guard's marker name must match await_final_qa's exactly (stem + suffix).

    A drift here would let the guard look for the wrong marker and never skip.
    """
    master = _make_master_and_marker(tmp_path, with_marker=False)
    _patch_integrity_pass(monkeypatch)
    # With no marker -> no skip.
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is False
    # Create the marker with the SAME construction await_final_qa uses.
    (master.parent / f"{master.stem}_QA_APPROVED.marker").write_text("", encoding="utf-8")
    assert pipeline._approved_master_intact(master, _config(enabled=True)) is True
