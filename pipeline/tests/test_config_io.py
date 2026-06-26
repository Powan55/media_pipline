"""Unit tests for learning.config_io (comment-preserving single-key editor)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning.config_io import (  # noqa: E402
    ConfigEditError,
    apply_knob,
    read_yaml_key,
    revert_knob,
    set_yaml_text,
)

_SAMPLE = """\
script_quality:
  min_score: 0.50          # threshold below which scripts halt
  word_count_max: 98       # upper bound — tightened 2026-06-11
  duration_warn_s: 38.0    # WARN-ONLY <=38s reach lever

tts:
  provider: edge-tts       # default
  rate: "+10%"             # edge-tts speaking rate

fact_check:
  require_human_resolution: true   # SACRED GATE
"""


class TestSetYamlText(unittest.TestCase):
    def test_changes_only_the_value(self):
        old, new_text, changed = set_yaml_text(_SAMPLE, "script_quality.duration_warn_s", 37.0)
        self.assertEqual(old, "38.0")
        self.assertTrue(changed)
        self.assertIn("duration_warn_s: 37.0    # WARN-ONLY <=38s reach lever", new_text)
        # Other lines untouched.
        self.assertIn("min_score: 0.50          # threshold below which scripts halt", new_text)
        self.assertIn("word_count_max: 98       # upper bound — tightened 2026-06-11", new_text)

    def test_idempotent_noop(self):
        old, new_text, changed = set_yaml_text(_SAMPLE, "script_quality.word_count_max", 98)
        self.assertFalse(changed)
        self.assertEqual(new_text, _SAMPLE)

    def test_preserves_quoted_string(self):
        old, new_text, changed = set_yaml_text(_SAMPLE, "tts.rate", "+15%")
        self.assertEqual(old, "+10%")
        self.assertTrue(changed)
        self.assertIn('rate: "+15%"             # edge-tts speaking rate', new_text)

    def test_missing_key_raises(self):
        with self.assertRaises(ConfigEditError):
            set_yaml_text(_SAMPLE, "script_quality.nonexistent", 1)


class TestApplyRevert(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cfg = Path(self._td.name) / "config.yaml"
        self.cfg.write_text(_SAMPLE, encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_apply_writes_value_and_backup(self):
        res = apply_knob(self.cfg, "script_quality.duration_warn_s", 37.0)
        self.assertTrue(res.changed)
        self.assertEqual(res.old_value, "38.0")
        self.assertEqual(read_yaml_key(self.cfg, "script_quality.duration_warn_s"), "37.0")
        self.assertTrue(Path(res.snapshot).exists())

    def test_apply_is_idempotent(self):
        apply_knob(self.cfg, "script_quality.word_count_max", 95)
        res2 = apply_knob(self.cfg, "script_quality.word_count_max", 95)
        self.assertFalse(res2.changed)
        self.assertIsNone(res2.snapshot)

    def test_revert_round_trip_keeps_other_keys(self):
        before = self.cfg.read_text(encoding="utf-8")
        apply_knob(self.cfg, "script_quality.duration_warn_s", 36.0)
        revert_knob(self.cfg, "script_quality.duration_warn_s", "38.0")
        after = self.cfg.read_text(encoding="utf-8")
        self.assertEqual(before, after)  # byte-identical round-trip

    def test_refuses_sacred_key(self):
        with self.assertRaises(ConfigEditError):
            apply_knob(self.cfg, "fact_check.require_human_resolution", False)

    def test_refuses_scoring_weights_file(self):
        weights = Path(self._td.name) / "scoring_weights.json"
        weights.write_text("{}", encoding="utf-8")
        with self.assertRaises(ConfigEditError):
            apply_knob(weights, "niche_fit", 0.5)


if __name__ == "__main__":
    unittest.main()
