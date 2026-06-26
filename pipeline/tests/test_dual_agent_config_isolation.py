"""Unit tests for daily_batch._isolate_config_for_topic + the contract
that load_config(path) honors an explicit path.

Context: 2026-05-22 cross-contamination postmortem — two sub-agents in the
`/start -auto` dual-video shape shared a single canonical config.yaml. When
sub-agent A's recovery logic flipped `render.hardware_accel`, sub-agent B's
in-flight render observed the mutation. Isolation copies the config to a
per-topic_id temp dir, and apex passes `--config <isolated-path>` to each
sub-agent. The canonical config.yaml is NEVER mutated by sub-agent runs.

Runnable under pytest and stdlib unittest. No real channel root touched.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daily_batch import _isolate_config_for_topic  # noqa: E402
from pipeline import load_config  # noqa: E402


class _TmpConfig:
    """Per-test scratch holding a synthesized config.yaml."""

    def __enter__(self) -> Path:
        self._tmp = Path(tempfile.mkdtemp(prefix="shadowverse-cfg-test-"))
        self._cfg = self._tmp / "config.yaml"
        # Minimal but realistic config — enough that load_config() round-trips.
        self._cfg.write_text(
            "render:\n"
            "  hardware_accel: nvenc\n"
            "  resolution: [1080, 1920]\n"
            "paths:\n"
            "  channel_root: C:/ContentOps/channels/ShadowVerse\n",
            encoding="utf-8",
        )
        return self._cfg

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


class IsolateConfigForTopicTests(unittest.TestCase):
    """Direct unit tests on daily_batch._isolate_config_for_topic()."""

    def test_isolate_config_creates_separate_temp_dirs(self) -> None:
        """Two calls with different topic_ids return distinct paths in
        distinct parent dirs — the foundational invariant for sub-agent
        isolation."""
        with _TmpConfig() as src:
            path_a = _isolate_config_for_topic(
                "2026-05-22_001", source_config=src,
            )
            path_b = _isolate_config_for_topic(
                "2026-05-22_002", source_config=src,
            )
            try:
                self.assertNotEqual(path_a, path_b)
                self.assertNotEqual(path_a.parent, path_b.parent)
                # Both copies exist with the canonical content.
                self.assertTrue(path_a.exists())
                self.assertTrue(path_b.exists())
                # Temp dir name encodes the topic_id so apex can correlate
                # halts to sub-agents.
                self.assertIn("2026-05-22_001", path_a.parent.name)
                self.assertIn("2026-05-22_002", path_b.parent.name)
            finally:
                shutil.rmtree(path_a.parent, ignore_errors=True)
                shutil.rmtree(path_b.parent, ignore_errors=True)

    def test_isolate_config_preserves_canonical(self) -> None:
        """Writing to the isolated copy must NOT mutate the canonical
        source. This is the load-bearing invariant — the whole point of
        isolation."""
        with _TmpConfig() as src:
            original_content = src.read_text(encoding="utf-8")
            isolated = _isolate_config_for_topic(
                "2026-05-22_003", source_config=src,
            )
            try:
                # Simulate sub-agent mutating its isolated config (e.g.,
                # flipping hardware_accel after an NVENC failure).
                isolated.write_text(
                    "render:\n  hardware_accel: none\n",
                    encoding="utf-8",
                )
                # The canonical config is untouched.
                self.assertEqual(
                    src.read_text(encoding="utf-8"),
                    original_content,
                    "canonical config was mutated by sub-agent write — "
                    "isolation is broken",
                )
            finally:
                shutil.rmtree(isolated.parent, ignore_errors=True)

    def test_isolate_config_raises_when_source_missing(self) -> None:
        """Fail loud if the canonical config is missing — never silently
        return an empty path or copy nothing."""
        with self.assertRaises(FileNotFoundError):
            _isolate_config_for_topic(
                "2026-05-22_004",
                source_config=Path("/nope/does_not_exist.yaml"),
            )

    def test_isolate_config_is_concurrency_safe(self) -> None:
        """WORKFLOW_AUDIT_2026-05-31 T1 — lock the isolation invariant under REAL
        concurrency, not just sequential calls.

        Fan out K threads each calling _isolate_config_for_topic for a distinct
        topic_id, then writing a unique marker into its own returned copy. The
        mechanism (tempfile.mkdtemp gives each call a unique dir) is inherently
        concurrency-safe — this test pins that so a future "optimization" that
        introduces a shared path can't regress it silently. Asserts:
          (a) all K returned paths are unique;
          (b) all K parent temp dirs are unique;
          (c) the canonical source is byte-identical to its pre-run content;
          (d) each isolated copy holds ONLY its own marker (no cross-talk).
        """
        K = 8
        with _TmpConfig() as src:
            canonical_before = src.read_text(encoding="utf-8")

            def _worker(i: int) -> Path:
                topic_id = f"2026-05-22_{i:03d}"
                dest = _isolate_config_for_topic(topic_id, source_config=src)
                # Each sub-agent stamps a unique marker into ITS copy only.
                dest.write_text(f"marker-{topic_id}\n", encoding="utf-8")
                return dest

            paths: list[Path] = []
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=K) as ex:
                    futures = [ex.submit(_worker, i) for i in range(K)]
                    paths = [f.result() for f in concurrent.futures.as_completed(futures)]

                # (a) unique copy paths.
                self.assertEqual(len(paths), K)
                self.assertEqual(len({str(p) for p in paths}), K,
                                 "isolated copy paths collided under concurrency")
                # (b) unique parent temp dirs.
                self.assertEqual(len({str(p.parent) for p in paths}), K,
                                 "isolated temp dirs collided under concurrency")
                # (c) canonical source untouched.
                self.assertEqual(src.read_text(encoding="utf-8"), canonical_before,
                                 "canonical config mutated by a concurrent sub-agent")
                # (d) each copy holds only its own marker.
                for p in paths:
                    topic_id = p.parent.name  # prefix encodes the topic_id
                    body = p.read_text(encoding="utf-8")
                    self.assertIn("marker-2026-05-22_", body)
                    # The marker's topic_id must match the one in this dir's name.
                    marker_id = body.strip().removeprefix("marker-")
                    self.assertIn(marker_id, p.parent.name,
                                  "a copy holds another sub-agent's marker — cross-talk")
            finally:
                for p in paths:
                    shutil.rmtree(p.parent, ignore_errors=True)


class LoadConfigRespectsExplicitPathTests(unittest.TestCase):
    """Contract test: pipeline.load_config(path) reads from the explicit
    path argument, not the canonical PIPELINE_ROOT/config.yaml.

    This is the load-bearing contract for the isolation work — if
    `load_config` ignored the path arg, isolation would be ineffective."""

    def test_config_load_respects_explicit_path(self) -> None:
        # Skip if the canonical .env is missing (load_config requires it).
        env_path = REPO_ROOT / ".env"
        if not env_path.exists():
            self.skipTest(
                f"{env_path} missing — load_config requires .env to coexist"
            )
        with _TmpConfig() as src:
            # Use a distinctive marker so we can prove load_config read THIS
            # file and not the canonical one.
            src.write_text(
                "render:\n"
                "  hardware_accel: SENTINEL_MARKER_FOR_TEST\n"
                "paths:\n"
                "  channel_root: /tmp/test_channel_root\n",
                encoding="utf-8",
            )
            config = load_config(src)
            self.assertEqual(
                config["render"]["hardware_accel"],
                "SENTINEL_MARKER_FOR_TEST",
            )


if __name__ == "__main__":
    unittest.main()
