"""Unit tests for tools/backfill_hook_selections.py.

Stdlib ``unittest`` to match the cleanup_orphans pattern (codebase preference
per Slice 3 Wave 1 finding). All tests synthesize a fresh temp channel root
via ``tempfile.TemporaryDirectory`` — no test ever reads the live channel
root or the live audit_2026-05-07/ directory.

Coverage (per Sprint 4 slice 2 spec):
  - clean topic (formula auto-resolves) → JSONL row, NOT in CSV
  - missing-formula topic (UNTAGGED) → JSONL row, IN CSV with proper reason
  - EDITED topic → JSONL row, IN CSV with proper reason
  - missing-FINAL skipped to errors list, walked count not bumped
  - multi-topic walk: pseudo-dirs / orphans / files filtered out
  - idempotent re-run: second pass does not duplicate JSONL or CSV rows
  - CLI: --dry-run writes nothing to disk
  - CLI: missing drafts dir exits non-zero
  - reason mapping: missing RESPONSE vs untagged RESPONSE vs no-match
  - unicode safe: all_three_hooks_json round-trips through CSV
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import backfill_hook_selections as bhs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_RESPONSE_TAGGED = """\
HOOK_A: Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.   [formula: Specific-Number Promise]
HOOK_B: You think you're getting advice from Claude. You're getting a yes man.   [formula: Contradiction]
HOOK_C: Anthropic studied a million Claude chats. The relationship answer is the dishonest one.   [formula: Cited-Observation Lead]

[B-ROLL: phone] body irrelevant for hook extraction.
"""

_RESPONSE_UNTAGGED = """\
HOOK_A: Cursor users still paste context. A folder does it for you.
HOOK_B: I deleted half my Cursor prompts. The chat got better.
HOOK_C: Most Cursor users miss this. The /folder command beats prompt stuffing.

body irrelevant for hook extraction.
"""

# Final whose first sentence matches HOOK_A exactly.
_FINAL_MATCHES_A = (
    "Anthropic just told on Claude. Twenty five percent of relationship chats are pure flattery.\n"
    "[B-ROLL: phone] body content goes here.\n"
)

# Final whose hook is heavily rewritten — nothing matches via prefix or exact.
_FINAL_EDITED = (
    "Plot twist: the AI you trust the most lies to you the most.\n"
    "[B-ROLL: phone] body content.\n"
)


def _make_topic(channel_root: Path, topic_id: str, *,
                response: str | None, final: str | None) -> Path:
    """Create a topic dir with optional RESPONSE.txt and FINAL.txt."""
    d = channel_root / "02_scripts" / "_drafts" / topic_id
    d.mkdir(parents=True)
    if response is not None:
        (d / "script_RESPONSE.txt").write_text(response, encoding="utf-8")
    if final is not None:
        (d / "script_FINAL.txt").write_text(final, encoding="utf-8")
    return d


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return a list of dicts (skipping blank lines)."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read the unresolved CSV and return its data rows as dicts."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# backfill() — core engine
# ---------------------------------------------------------------------------


class TestBackfillEngine(unittest.TestCase):
    """End-to-end coverage of the backfill() function on synthetic channel roots."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.channel_root = Path(self._tmp.name)
        # Both outputs always go to tmp_path — never the real audit dir.
        self.log_path = self.channel_root / "01_research" / "hook_selection_log.jsonl"
        self.csv_path = self.channel_root / "audit" / "unresolved.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backfill(self, **overrides) -> bhs.BackfillResult:
        """Default-args wrapper that always pins log + CSV to tmp paths."""
        kwargs = {
            "channel_root": self.channel_root,
            "log_path": self.log_path,
            "unresolved_csv_path": self.csv_path,
        }
        kwargs.update(overrides)
        return bhs.backfill(**kwargs)

    def test_clean_topic_appends_jsonl_and_skips_csv(self) -> None:
        """A topic with proper [formula:] tags + matching FINAL → JSONL only."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)

        result = self._backfill()

        self.assertEqual(result.walked, 1)
        self.assertEqual(result.appended, 1)
        self.assertEqual(result.skipped_dup, 0)
        self.assertEqual(result.unresolved, 0)
        self.assertEqual(result.errors, [])

        rows = _read_jsonl(self.log_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic_id"], "2026-05-12_002")
        self.assertEqual(rows[0]["hook_letter"], "A")
        self.assertEqual(rows[0]["formula"], "Specific-Number Promise")

        # CSV is created (header row only) but contains no data rows.
        csv_rows = _read_csv(self.csv_path)
        self.assertEqual(csv_rows, [])
        self.assertTrue(self.csv_path.exists())

    def test_untagged_topic_lands_in_csv(self) -> None:
        """RESPONSE without [formula:] tags → UNTAGGED row in CSV + JSONL."""
        _make_topic(self.channel_root, "2026-05-05_001",
                    response=_RESPONSE_UNTAGGED, final=_FINAL_MATCHES_A)

        result = self._backfill()

        self.assertEqual(result.walked, 1)
        self.assertEqual(result.unresolved, 1)
        self.assertEqual(result.appended, 1)

        csv_rows = _read_csv(self.csv_path)
        self.assertEqual(len(csv_rows), 1)
        self.assertEqual(csv_rows[0]["topic_id"], "2026-05-05_001")
        self.assertEqual(csv_rows[0]["formula_status"], "UNTAGGED")
        self.assertEqual(csv_rows[0]["reason"], "no [formula:] tags in RESPONSE")
        # JSON-encoded hook list parses and has 3 items.
        hooks = json.loads(csv_rows[0]["all_three_hooks_json"])
        self.assertEqual(len(hooks), 3)
        self.assertEqual({h["letter"] for h in hooks}, {"A", "B", "C"})

    def test_edited_topic_lands_in_csv(self) -> None:
        """FINAL hook that matches none of HOOK_A/B/C → EDITED row in CSV."""
        _make_topic(self.channel_root, "2026-05-12_001",
                    response=_RESPONSE_TAGGED, final=_FINAL_EDITED)

        result = self._backfill()

        self.assertEqual(result.walked, 1)
        self.assertEqual(result.unresolved, 1)

        csv_rows = _read_csv(self.csv_path)
        self.assertEqual(len(csv_rows), 1)
        self.assertEqual(csv_rows[0]["formula_status"], "EDITED")
        self.assertEqual(
            csv_rows[0]["reason"],
            "FINAL hook didn't match any of the 3 candidates",
        )

    def test_missing_response_yields_untagged_with_proper_reason(self) -> None:
        """RESPONSE.txt absent → UNTAGGED + reason 'RESPONSE.txt missing'."""
        _make_topic(self.channel_root, "2026-05-04_001",
                    response=None, final=_FINAL_MATCHES_A)

        result = self._backfill()

        self.assertEqual(result.walked, 1)
        self.assertEqual(result.unresolved, 1)

        csv_rows = _read_csv(self.csv_path)
        self.assertEqual(len(csv_rows), 1)
        self.assertEqual(csv_rows[0]["formula_status"], "UNTAGGED")
        self.assertEqual(csv_rows[0]["reason"], "RESPONSE.txt missing")
        # No hooks parsed → empty JSON array.
        self.assertEqual(csv_rows[0]["all_three_hooks_json"], "[]")

    def test_missing_final_skipped_to_errors(self) -> None:
        """No script_FINAL.txt → topic recorded in errors, NOT in walked."""
        _make_topic(self.channel_root, "2026-05-09_001",
                    response=_RESPONSE_TAGGED, final=None)

        result = self._backfill()

        self.assertEqual(result.walked, 0)
        self.assertEqual(result.appended, 0)
        self.assertEqual(result.unresolved, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("2026-05-09_001", result.errors[0])

        # JSONL not created (no rows to write); CSV header-only.
        self.assertEqual(_read_jsonl(self.log_path), [])
        self.assertEqual(_read_csv(self.csv_path), [])

    def test_multi_topic_walk_filters_pseudo_dirs(self) -> None:
        """Pseudo-dirs (_daily_*, _orphans, files) must be skipped."""
        # Real topic, clean.
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)
        # Real topic, untagged.
        _make_topic(self.channel_root, "2026-05-05_001",
                    response=_RESPONSE_UNTAGGED, final=_FINAL_MATCHES_A)
        # Pseudo-dir: should be skipped silently.
        drafts = self.channel_root / "02_scripts" / "_drafts"
        (drafts / "_daily_2026-05-12").mkdir()
        (drafts / "_orphans").mkdir()
        # Stray file at top level.
        (drafts / "stray.txt").write_text("noise", encoding="utf-8")

        result = self._backfill()

        self.assertEqual(result.walked, 2)
        self.assertEqual(result.appended, 2)
        self.assertEqual(result.unresolved, 1)
        self.assertEqual(result.errors, [])

        rows = _read_jsonl(self.log_path)
        self.assertEqual(
            sorted(r["topic_id"] for r in rows),
            ["2026-05-05_001", "2026-05-12_002"],
        )

    def test_idempotent_rerun_no_duplicates(self) -> None:
        """Running twice yields the same JSONL + CSV (skipped_dup bumps)."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)
        _make_topic(self.channel_root, "2026-05-05_001",
                    response=_RESPONSE_UNTAGGED, final=_FINAL_MATCHES_A)

        first = self._backfill()
        self.assertEqual(first.appended, 2)
        self.assertEqual(first.skipped_dup, 0)

        rows_first = _read_jsonl(self.log_path)
        csv_first = _read_csv(self.csv_path)

        second = self._backfill()
        self.assertEqual(second.walked, 2)
        self.assertEqual(second.appended, 0)
        self.assertEqual(second.skipped_dup, 2)
        self.assertEqual(second.unresolved, 1)

        rows_second = _read_jsonl(self.log_path)
        csv_second = _read_csv(self.csv_path)
        self.assertEqual(len(rows_first), len(rows_second))
        self.assertEqual(
            [r["topic_id"] for r in rows_first],
            [r["topic_id"] for r in rows_second],
        )
        self.assertEqual(csv_first, csv_second)

    def test_dry_run_writes_nothing(self) -> None:
        """dry_run=True must leave the filesystem untouched."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)
        _make_topic(self.channel_root, "2026-05-05_001",
                    response=_RESPONSE_UNTAGGED, final=_FINAL_MATCHES_A)

        result = self._backfill(dry_run=True)

        # Counts reflect what WOULD be appended.
        self.assertEqual(result.walked, 2)
        self.assertEqual(result.appended, 2)
        self.assertEqual(result.unresolved, 1)

        # Nothing on disk.
        self.assertFalse(self.log_path.exists())
        self.assertFalse(self.csv_path.exists())

    def test_default_log_path_derived_from_channel_root(self) -> None:
        """When log_path is None, defaults under channel_root/01_research/."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)

        # Pin only the CSV; let log_path default.
        result = bhs.backfill(
            channel_root=self.channel_root,
            log_path=None,
            unresolved_csv_path=self.csv_path,
        )

        self.assertEqual(result.appended, 1)
        expected_log = (
            self.channel_root / "01_research" / "hook_selection_log.jsonl"
        )
        self.assertTrue(expected_log.exists())

    def test_drafts_dir_missing_raises(self) -> None:
        """An entirely missing drafts dir surfaces FileNotFoundError."""
        # Don't create 02_scripts/_drafts at all.
        with self.assertRaises(FileNotFoundError):
            bhs.backfill(
                channel_root=self.channel_root,
                log_path=self.log_path,
                unresolved_csv_path=self.csv_path,
            )

    def test_explicit_drafts_dir_override(self) -> None:
        """An explicit drafts_dir works even when channel_root has none."""
        alt_drafts = self.channel_root / "alt_drafts"
        alt_drafts.mkdir()
        td = alt_drafts / "2026-05-12_002"
        td.mkdir()
        (td / "script_RESPONSE.txt").write_text(_RESPONSE_TAGGED, encoding="utf-8")
        (td / "script_FINAL.txt").write_text(_FINAL_MATCHES_A, encoding="utf-8")

        # extract_chosen_hook still resolves via channel_root/02_scripts/_drafts/<topic>,
        # so this also requires the topic to exist there. Build that mirror too.
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)

        result = bhs.backfill(
            channel_root=self.channel_root,
            log_path=self.log_path,
            unresolved_csv_path=self.csv_path,
            drafts_dir=alt_drafts,
        )
        self.assertEqual(result.walked, 1)
        self.assertEqual(result.appended, 1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    """End-to-end CLI behavior (argparse + main())."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.channel_root = Path(self._tmp.name)
        self.log_path = self.channel_root / "01_research" / "hook_selection_log.jsonl"
        self.csv_path = self.channel_root / "audit" / "unresolved.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """Invoke ``main`` and capture (rc, stdout, stderr)."""
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = bhs.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_dry_run_cli_writes_nothing_and_prints_summary(self) -> None:
        """--dry-run should print the summary block and write zero files."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)

        rc, out, _ = self._run_cli([
            "--channel-root", str(self.channel_root),
            "--log-path", str(self.log_path),
            "--unresolved-csv", str(self.csv_path),
            "--dry-run",
        ])

        self.assertEqual(rc, 0)
        self.assertIn("BACKFILL SUMMARY", out)
        self.assertIn("walked:      1", out)
        self.assertFalse(self.log_path.exists())
        self.assertFalse(self.csv_path.exists())

    def test_live_cli_writes_jsonl_and_csv(self) -> None:
        """Default execute mode populates both JSONL and CSV."""
        _make_topic(self.channel_root, "2026-05-12_002",
                    response=_RESPONSE_TAGGED, final=_FINAL_MATCHES_A)
        _make_topic(self.channel_root, "2026-05-05_001",
                    response=_RESPONSE_UNTAGGED, final=_FINAL_MATCHES_A)

        rc, out, _ = self._run_cli([
            "--channel-root", str(self.channel_root),
            "--log-path", str(self.log_path),
            "--unresolved-csv", str(self.csv_path),
        ])

        self.assertEqual(rc, 0)
        self.assertIn("walked:      2", out)
        self.assertIn("appended:    2", out)
        self.assertIn("unresolved:  1", out)
        self.assertEqual(len(_read_jsonl(self.log_path)), 2)
        self.assertEqual(len(_read_csv(self.csv_path)), 1)

    def test_cli_returns_nonzero_on_missing_drafts_dir(self) -> None:
        """If drafts dir is missing entirely, CLI exits 1."""
        rc, _, _ = self._run_cli([
            "--channel-root", str(self.channel_root),
            "--log-path", str(self.log_path),
            "--unresolved-csv", str(self.csv_path),
        ])
        self.assertEqual(rc, 1)

    def test_cli_missing_config_returns_nonzero(self) -> None:
        """Without --channel-root, a missing config.yaml exits 1."""
        nowhere = self.channel_root / "no_such_config.yaml"
        rc, _, _ = self._run_cli([
            "--config", str(nowhere),
            "--log-path", str(self.log_path),
            "--unresolved-csv", str(self.csv_path),
        ])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
