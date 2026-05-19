"""One-shot historical backfill of the hook selection log.

Walks every ``<channel_root>/02_scripts/_drafts/<topic_id>/`` directory, calls
Slice 1's ``hook_selection_log.extract_chosen_hook`` on each, and:

  1. Appends each result to ``hook_selection_log.jsonl`` via Slice 1's
     ``append_to_log`` (idempotent — duplicate detection is handled there).
  2. For topics whose extraction yielded ``formula="UNTAGGED"`` or ``"EDITED"``,
     writes a worklist CSV to
     ``<project_root>/audit_2026-05-07/hook_selection_backfill_unresolved.csv``.
     The operator + a follow-up inferral sub-agent will use this CSV to fill in
     the missing formulas.

Topics whose extraction auto-resolved (formula == one of the named formulas in
``viral_hooks.md``) appear in the JSONL log only — never in the CSV.

CLI:
    # Production: write to default JSONL + default CSV path.
    python tools/backfill_hook_selections.py

    # Inspect counts without writing anything.
    python tools/backfill_hook_selections.py --dry-run

    # Override the config.yaml location.
    python tools/backfill_hook_selections.py --config /path/to/config.yaml

    # Override every output / input path explicitly (handy for testing).
    python tools/backfill_hook_selections.py \\
        --drafts-dir D:\\some\\_drafts \\
        --log-path D:\\out\\hook_selection_log.jsonl \\
        --unresolved-csv D:\\out\\worklist.csv

Exit codes:
    0 — success (dry-run always; or live with at least one walked topic)
    1 — catastrophic error (drafts_dir missing entirely, config.yaml missing
        with no --drafts-dir override, etc.)

This module never invokes an LLM, never imports a framework, depends only on
the standard library (csv, json, re, logging, argparse, dataclasses, pathlib,
typing) plus PyYAML for config loading (already in requirements.txt).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Ensure the pipeline repo root is on sys.path so we can import the sibling
# top-level module ``hook_selection_log`` regardless of how the CLI is invoked
# (``python tools/backfill_hook_selections.py`` from the repo root, or via
# ``-m tools.backfill_hook_selections``). Tests handle this independently by
# inserting REPO_ROOT in conftest-style preamble; this guard makes the script
# itself runnable without that.
_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))

from hook_selection_log import (  # noqa: E402  — sys.path bootstrap above
    FORMULA_EDITED,
    FORMULA_UNTAGGED,
    ChosenHook,
    HookCandidate,
    append_to_log,
    extract_chosen_hook,
)

log = logging.getLogger("backfill_hook_selections")

# Topic-id directory format: YYYY-MM-DD_NNN. Pseudo-dirs (_daily_<DATE>,
# _orphans, _archive, etc.) and stray top-level files are filtered out.
_TOPIC_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{3}$")

# Default CSV path. Tests must always pass an explicit override (see
# tests/test_backfill_hook_selections.py — every test threads ``tmp_path``).
_DEFAULT_UNRESOLVED_CSV = Path(
    r"C:/Users/laxmi/Documents/Project/audit_2026-05-07/"
    r"hook_selection_backfill_unresolved.csv"
)

# CSV header — locked schema; downstream inferral sub-agent depends on it.
CSV_FIELDNAMES: list[str] = [
    "topic_id",
    "hook_text",
    "formula_status",
    "reason",
    "all_three_hooks_json",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass(frozen=True)
class BackfillResult:
    """Summary of one backfill run.

    Attributes:
        walked: how many topic dirs were inspected (passed the topic-id regex
            and survived the missing-FINAL skip).
        appended: how many JSONL rows were written or rewritten by
            ``append_to_log`` (returned True). Updates to an existing row count
            here too — that's intentional; the CLI prints both.
        skipped_dup: how many topics were already in the JSONL with byte-equal
            content (``append_to_log`` returned False).
        unresolved: how many topics ended up in the CSV worklist (formula was
            ``UNTAGGED`` or ``EDITED``).
        errors: per-topic error messages (e.g. missing FINAL). One string per
            problem topic, formatted ``"<topic_id>: <reason>"``.
    """

    walked: int
    appended: int
    skipped_dup: int
    unresolved: int
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config / path resolution
# ---------------------------------------------------------------------------


def _load_config(config_path: Path) -> dict:
    """Load ``config.yaml``. Fails loud with a useful message if missing."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. Pass --drafts-dir to "
            f"bypass config loading entirely."
        )
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


def _channel_root_from_config(config: dict) -> Path:
    """Pull ``paths.channel_root`` out of a loaded config dict."""
    return Path(config["paths"]["channel_root"])


# ---------------------------------------------------------------------------
# Walk / classify
# ---------------------------------------------------------------------------


def _iter_topic_dirs(drafts_dir: Path) -> list[Path]:
    """Return the sorted list of directories under ``drafts_dir`` whose names
    match the ``YYYY-MM-DD_NNN`` topic-id format.

    Pseudo-dirs (``_daily_<DATE>``, ``_orphans``, ``_archive``, ...) and stray
    files are filtered out by the regex check.
    """
    if not drafts_dir.exists():
        raise FileNotFoundError(f"drafts_dir does not exist: {drafts_dir}")
    if not drafts_dir.is_dir():
        raise NotADirectoryError(f"drafts_dir is not a directory: {drafts_dir}")

    out: list[Path] = []
    for entry in sorted(drafts_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not _TOPIC_ID_RE.match(entry.name):
            continue
        out.append(entry)
    return out


def _reason_for_unresolved(chosen: ChosenHook, response_path: Path) -> str:
    """Generate a short human-readable reason string for the CSV's ``reason``
    column based on which sentinel formula came back and the underlying
    RESPONSE state.

    Cases (in priority order):
      - ``UNTAGGED`` + missing RESPONSE.txt -> "RESPONSE.txt missing"
      - ``UNTAGGED`` + RESPONSE.txt present  -> "no [formula:] tags in RESPONSE"
      - ``EDITED``                            -> "FINAL hook didn't match any of the 3 candidates"

    Anything else returns ``"unknown"`` defensively.
    """
    if chosen.formula == FORMULA_UNTAGGED:
        if not response_path.exists():
            return "RESPONSE.txt missing"
        return "no [formula:] tags in RESPONSE"
    if chosen.formula == FORMULA_EDITED:
        return "FINAL hook didn't match any of the 3 candidates"
    return "unknown"


def _hooks_to_json(candidates: list[HookCandidate]) -> str:
    """Serialize the 3 hook candidates to a compact JSON array of objects.

    Schema: ``[{"letter": "A", "text": "...", "formula": "..."}, ...]``.
    Empty list → ``"[]"``. ``ensure_ascii=False`` keeps unicode legible in the
    CSV.
    """
    payload = [
        {"letter": c.letter, "text": c.text, "formula": c.formula}
        for c in candidates
    ]
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CSV worklist
# ---------------------------------------------------------------------------


def _write_unresolved_csv(rows: list[dict[str, str]], csv_path: Path) -> None:
    """Write the unresolved-formula worklist CSV.

    Behavior:
      - Always creates the parent dir if missing.
      - Always overwrites — the backfill is idempotent, so re-running with the
        same drafts dir produces the same CSV (modulo new topics added since).
      - Header is fixed to ``CSV_FIELDNAMES``.

    Empty ``rows`` still writes a header-only CSV so downstream tooling can
    rely on the file existing after a run.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def backfill(
    channel_root: Path,
    *,
    log_path: Path | None = None,
    unresolved_csv_path: Path | None = None,
    drafts_dir: Path | None = None,
    dry_run: bool = False,
) -> BackfillResult:
    """Walk topic dirs, extract chosen hooks, append to JSONL, emit unresolved CSV.

    Args:
        channel_root: ``<channel_root>`` (e.g.
            ``C:/ContentOps/channels/ShadowVerse``). Used to derive default
            ``log_path`` and ``drafts_dir`` if those are not passed explicitly.
        log_path: where to write the JSONL log. Defaults to
            ``<channel_root>/01_research/hook_selection_log.jsonl``.
        unresolved_csv_path: where to write the worklist CSV. Defaults to
            the fixed Windows path under ``Documents\\Project\\audit_2026-05-07``.
            Tests should always override this with ``tmp_path``.
        drafts_dir: where to walk for topic dirs. Defaults to
            ``<channel_root>/02_scripts/_drafts``.
        dry_run: if True, walk + classify but write nothing. Counts in the
            returned ``BackfillResult`` reflect what *would* have been written.

    Returns:
        ``BackfillResult`` with per-bucket counts and an ``errors`` list of
        per-topic problem messages.

    Skip rules:
        - Dirs that don't match the topic_id regex (``_daily_<DATE>``,
          ``_orphans``, etc.) are silently skipped at the walk step.
        - Topic dirs without ``script_FINAL.txt`` are recorded in
          ``BackfillResult.errors`` and otherwise skipped — they don't count
          toward ``walked``.

    Raises:
        FileNotFoundError / NotADirectoryError: only when ``drafts_dir`` itself
        is missing or not a directory. Per-topic errors never propagate.
    """
    channel_root = Path(channel_root)
    drafts_dir = Path(drafts_dir) if drafts_dir is not None else (
        channel_root / "02_scripts" / "_drafts"
    )
    log_path = Path(log_path) if log_path is not None else (
        channel_root / "01_research" / "hook_selection_log.jsonl"
    )
    unresolved_csv_path = (
        Path(unresolved_csv_path)
        if unresolved_csv_path is not None
        else _DEFAULT_UNRESOLVED_CSV
    )

    topic_dirs = _iter_topic_dirs(drafts_dir)
    log.info("found %d topic dirs under %s", len(topic_dirs), drafts_dir)

    errors: list[str] = []
    unresolved_rows: list[dict[str, str]] = []
    walked = 0
    appended = 0
    skipped_dup = 0

    for topic_dir in topic_dirs:
        topic_id = topic_dir.name
        try:
            chosen = extract_chosen_hook(topic_id, channel_root)
        except FileNotFoundError as e:
            # script_FINAL.txt missing — record and move on.
            log.warning("skipping %s: %s", topic_id, e)
            errors.append(f"{topic_id}: {e}")
            continue
        except OSError as e:
            # Filesystem hiccup on a single topic — don't take down the whole run.
            log.warning("skipping %s due to OSError: %s", topic_id, e)
            errors.append(f"{topic_id}: {e}")
            continue

        walked += 1

        if chosen.formula in (FORMULA_UNTAGGED, FORMULA_EDITED):
            response_path = topic_dir / "script_RESPONSE.txt"
            unresolved_rows.append(
                {
                    "topic_id": chosen.topic_id,
                    "hook_text": chosen.hook_text,
                    "formula_status": chosen.formula,
                    "reason": _reason_for_unresolved(chosen, response_path),
                    "all_three_hooks_json": _hooks_to_json(chosen.all_three_hooks),
                }
            )

        if dry_run:
            # In dry-run we still want to know which JSONL writes WOULD happen.
            # We cannot consult append_to_log without writing, so we treat every
            # walked row as a potential append. The CLI surfaces this caveat.
            appended += 1
            continue

        wrote = append_to_log(chosen, log_path)
        if wrote:
            appended += 1
        else:
            skipped_dup += 1

    if not dry_run:
        _write_unresolved_csv(unresolved_rows, unresolved_csv_path)
        log.info(
            "wrote %d unresolved rows to %s",
            len(unresolved_rows),
            unresolved_csv_path,
        )
    else:
        log.info(
            "[dry-run] would write %d unresolved rows to %s",
            len(unresolved_rows),
            unresolved_csv_path,
        )

    return BackfillResult(
        walked=walked,
        appended=appended,
        skipped_dup=skipped_dup,
        unresolved=len(unresolved_rows),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Walk every <channel_root>/02_scripts/_drafts/<topic_id>/ and "
            "backfill hook_selection_log.jsonl from the existing FINAL+RESPONSE "
            "pairs. Topics whose formula can't be auto-inferred go into a "
            "worklist CSV under audit_2026-05-07/."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=(
            f"Path to config.yaml (default: {DEFAULT_CONFIG_PATH}). Ignored "
            f"when --drafts-dir is given AND the JSONL/CSV paths are also "
            f"explicit."
        ),
    )
    parser.add_argument(
        "--channel-root",
        type=Path,
        default=None,
        help=(
            "Override the channel root. Defaults to config.paths.channel_root."
        ),
    )
    parser.add_argument(
        "--drafts-dir",
        type=Path,
        default=None,
        help=(
            "Override the drafts dir to walk. Defaults to "
            "<channel_root>/02_scripts/_drafts."
        ),
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help=(
            "Override the JSONL log path. Defaults to "
            "<channel_root>/01_research/hook_selection_log.jsonl."
        ),
    )
    parser.add_argument(
        "--unresolved-csv",
        type=Path,
        default=None,
        help=(
            "Override the unresolved-worklist CSV path. Defaults to "
            f"{_DEFAULT_UNRESOLVED_CSV}."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + classify, print planned counts, write nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. See module docstring for usage."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Resolve channel_root: explicit flag > config.yaml.
    if args.channel_root is not None:
        channel_root = args.channel_root
    else:
        try:
            config = _load_config(args.config)
        except FileNotFoundError as e:
            log.error("%s", e)
            return 1
        try:
            channel_root = _channel_root_from_config(config)
        except KeyError as e:
            log.error("config.yaml missing required key: %s", e)
            return 1

    log.info("channel_root:        %s", channel_root)
    log.info("drafts_dir:          %s", args.drafts_dir or
             channel_root / "02_scripts" / "_drafts")
    log.info("log_path:            %s", args.log_path or
             channel_root / "01_research" / "hook_selection_log.jsonl")
    log.info("unresolved_csv:      %s", args.unresolved_csv or
             _DEFAULT_UNRESOLVED_CSV)
    log.info("mode:                %s", "DRY RUN" if args.dry_run else "EXECUTE")

    try:
        result = backfill(
            channel_root=channel_root,
            log_path=args.log_path,
            unresolved_csv_path=args.unresolved_csv,
            drafts_dir=args.drafts_dir,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        log.error("backfill aborted: %s", e)
        return 1

    # Summary block — printed regardless of dry-run.
    print("=" * 60)
    print("BACKFILL SUMMARY")
    print("=" * 60)
    print(f"  walked:      {result.walked}")
    print(f"  appended:    {result.appended}{' (planned)' if args.dry_run else ''}")
    print(f"  skipped_dup: {result.skipped_dup}{' (n/a in dry-run)' if args.dry_run else ''}")
    print(f"  unresolved:  {result.unresolved}{' (planned)' if args.dry_run else ''}")
    print(f"  errors:      {len(result.errors)}")
    if result.errors:
        print("  --- errors ---")
        for msg in result.errors:
            print(f"    {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
