"""Daily batch orchestrator for ShadowVerse.

Workflow per call:
  1. Run trend_pull (handled by idea_generation if today's artifact missing)
  2. Run idea_generation — builds prompt, halts on first call so the operator
     /Claude can respond in chat with ranked candidates
  3. On resume, parse + score + rank, take top N picks
  4. For each pick, allocate a fresh topic-id and run the per-topic pipeline
  5. Catch halts independently per topic — one topic's manual halt does NOT
     block the others' pipeline runs
  6. Write a `daily_batch_<UTC-DATE>.md` summary listing each topic's status
     and the next operator action for any halt

Re-running this script is safe: it picks up where it left off via the
manual-LLM file-IO halts, and per-topic pipeline state is durable on disk.

CLI:
  python daily_batch.py                       # n_target=10, n_picks=2 (default)
  python daily_batch.py --n-picks 3           # ask for 10 ideas, advance top 3
  python daily_batch.py --n-target 15         # broader candidate pool to score
  python daily_batch.py --refresh-trends      # force trend_pull re-run
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from daily_batch_hook_addendum import format_hook_addendum
from idea_generation import daily_io_dirname, generate_ideas
from pipeline import (
    HumanReviewRequired,
    ManualLLMHalt,
    PIPELINE_ROOT,
    PipelineHalted,
    TopicJob,
    _build_run_id,
    load_config,
    run_for_topic,
    setup_logging,
    validate_config,
)
from scoring import ScoredCandidate
from topics import is_topic_id_uploaded, next_topic_id_for_date

log = logging.getLogger("daily_batch")


# Regex matching a daily-batch topic_id dir under 02_scripts/_drafts/, e.g.
# "2026-05-22_001". The date prefix is the UTC date the daily batch ran;
# the suffix is a 3-digit sequence id.
import re as _re
_TOPIC_ID_DIR_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{3})$")


class PicksAssignmentCorrupted(PipelineHalted):
    """Raised when picks_assignment.json is unreadable / malformed.

    Surfaces a halt-on-corruption (vs the silent return-{} behavior pre-2026-05-22)
    so the operator can rescue orphaned topic_id drafts before the next batch
    silently reallocates fresh ids.

    Use the ``--rescue-orphans`` flag to scan and surface candidates for manual
    recovery before re-allocating.
    """

    def __init__(self, path: Path, exception: Exception, postmortem_path: Path | None = None):
        self.picks_path = path
        self.exception = exception
        self.postmortem_path = postmortem_path
        pm_hint = (
            f"  Postmortem stub: {postmortem_path}\n"
            if postmortem_path is not None
            else "  (no postmortem stub written)\n"
        )
        super().__init__(
            f"\n\n  [PICKS-CORRUPT] picks_assignment.json at {path} is unreadable.\n"
            f"  Exception: {type(exception).__name__}: {exception}\n"
            f"{pm_hint}"
            f"  1. Inspect the corrupted file ({path}) and the postmortem stub for context.\n"
            f"  2. Re-run with `--rescue-orphans` to list orphaned topic_id drafts.\n"
            f"  3. Restore picks_assignment.json from the orphan list (or delete it to start fresh).\n"
            f"  4. Re-run daily_batch.py.\n"
        )


def _isolate_config_for_topic(
    topic_id: str,
    *,
    source_config: Path | None = None,
) -> Path:
    """Copy `config.yaml` to a per-topic_id temp dir and return the new path.

    Defense against the 2026-05-22 cross-contamination: in the dual-video
    `/start -auto` shape, two sub-agents run in parallel and would otherwise
    share a single canonical `config.yaml`. If sub-agent A's halt logic edits
    `render.hardware_accel` mid-run, sub-agent B's in-flight render sees the
    flip. Per-sub-agent config isolation eliminates the shared mutable.

    Contract: the apex Claude orchestrator calls this helper once per dispatched
    sub-agent BEFORE invoking `daily_batch.py --config <returned-path> ...`.
    The orchestrator owns the canonical config and is the sole writer to it
    during the apex window.

    Args:
      topic_id: stable id for the sub-agent (e.g. "2026-05-22_001"). Used in
        the temp-dir prefix so apex can correlate temp dirs to sub-agents.
      source_config: which config to copy; defaults to the canonical
        `<PIPELINE_ROOT>/config.yaml`. Test hook.

    Returns:
      Path to the isolated copy of config.yaml. The temp dir is intentionally
      NOT cleaned up by this helper — apex orchestrator (or the OS's %TEMP%
      sweep) handles teardown so post-mortem inspection is always possible.
    """
    if source_config is None:
        source_config = PIPELINE_ROOT / "config.yaml"
    if not source_config.exists():
        raise FileNotFoundError(
            f"Cannot isolate config for {topic_id}: source {source_config} "
            f"does not exist"
        )
    temp_dir = Path(tempfile.mkdtemp(prefix=f"shadowverse-batch-{topic_id}-"))
    dest = temp_dir / "config.yaml"
    shutil.copy2(source_config, dest)
    log.info(
        "isolated config for topic_id=%s: %s -> %s",
        topic_id, source_config, dest,
    )
    return dest


@dataclass
class TopicResult:
    """Outcome of one pick passing through run_for_topic."""
    topic_id: str
    topic: str
    angle: str
    weighted_total: float
    status: str             # completed | halted_manual_llm | halted_human_review | completed_through_metadata | error | dropped_already_uploaded
    halt_stage: str | None  # which stage halted, when applicable
    halt_message: str       # operator-facing summary of next action
    next_action_path: Path | None  # the file the operator/Claude needs to write or approve


@dataclass
class AllocationResult:
    """Outcome of attempting to assign a topic_id to a pick.

    `topic_id is None` means the pick has been dropped (e.g., persisted id
    points at an already-uploaded video) and the caller must NOT invoke
    `_run_one_topic` for this iteration. `reason` is a stable string for
    branching/telemetry; `log_message` is the human-readable explanation the
    caller should emit at the appropriate level.
    """
    topic_id: str | None
    reason: str               # "reused" | "fresh" | "dropped_already_uploaded"
    log_message: str


def _load_picks_assignment(
    daily_dir: Path,
    *,
    channel_root: Path | None = None,
    postmortems_dir: Path | None = None,
) -> dict[str, str]:
    """Load topic→topic_id mapping persisted at the daily IO dir.

    Returns an empty dict if the file is missing (fresh batch). If the file
    exists but is malformed, FAILS LOUD: raises :class:`PicksAssignmentCorrupted`
    (a :class:`PipelineHalted` subclass) so the operator can rescue orphaned
    topic_id drafts before the next batch silently reallocates fresh ids.

    Pre-2026-05-22 this branch returned ``{}`` with a WARNING log, which orphaned
    yesterday's drafted script work on the next run (see the 2026-05-22 dual-video
    cycle postmortem and `feedback_engineering_principles.md`'s fail-loud rule).

    Args:
      daily_dir: directory containing ``picks_assignment.json``.
      channel_root: channel root path (used to compute the orphan-scan listing
        for the postmortem stub). Optional; when omitted, the postmortem stub
        skips the orphan listing.
      postmortems_dir: explicit destination for the postmortem stub. When
        omitted, falls back to ``daily_dir`` itself so a stub is always written
        even in test environments without a project-root layout.
    """
    path = daily_dir / "picks_assignment.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {a["topic"]: a["topic_id"] for a in data.get("assignments", [])}
    except (json.JSONDecodeError, KeyError, OSError) as e:
        # Fail loud. Write a postmortem stub with the corrupted file contents
        # (truncated), the exception, today's drafts listing, and a recovery hint.
        log.error(
            "picks_assignment.json at %s is unreadable (%s: %s); HALTING. "
            "Use --rescue-orphans to scan for orphaned topic_id drafts.",
            path, type(e).__name__, e,
        )
        pm_path: Path | None = None
        try:
            pm_path = _write_picks_corrupt_postmortem(
                path, e,
                channel_root=channel_root,
                postmortems_dir=postmortems_dir,
            )
        except Exception as pm_exc:  # noqa: BLE001 — postmortem must not mask the halt
            log.error(
                "failed to write picks_assignment corruption postmortem: %s: %s",
                type(pm_exc).__name__, pm_exc,
            )
        raise PicksAssignmentCorrupted(path, e, postmortem_path=pm_path) from e


def _scan_for_orphaned_topic_ids(
    channel_root: Path, daily_date: str,
) -> list[str]:
    """Walk ``02_scripts/_drafts/`` for orphaned topic_id directories.

    An orphan is a directory whose name matches ``^{daily_date}_\\d{3}$`` AND
    which contains a non-empty ``script_RESPONSE.txt`` file. These are drafts
    the operator (or a previous run) produced that would be invisible to the
    new batch after a ``picks_assignment.json`` corruption.

    Args:
      channel_root: e.g. ``C:/ContentOps/channels/ShadowVerse``.
      daily_date: ``YYYY-MM-DD`` (typically today UTC; the date prefix of the
        topic_ids the corruption would orphan).

    Returns:
      Sorted list of orphan topic_ids, e.g. ``["2026-05-22_001", "2026-05-22_002"]``.
      Empty list if no orphans.
    """
    drafts = Path(channel_root) / "02_scripts" / "_drafts"
    if not drafts.exists():
        return []
    expected = _re.compile(rf"^{_re.escape(daily_date)}_\d{{3}}$")
    orphans: list[str] = []
    for child in drafts.iterdir():
        if not child.is_dir():
            continue
        if not expected.match(child.name):
            continue
        response = child / "script_RESPONSE.txt"
        if not response.exists():
            continue
        try:
            if response.stat().st_size == 0:
                continue
        except OSError:
            continue
        orphans.append(child.name)
    return sorted(orphans)


def _resolve_postmortems_dir(
    channel_root: Path | None,
    postmortems_dir: Path | None,
    daily_dir: Path,
) -> Path:
    """Pick the destination for a picks-assignment corruption postmortem stub.

    Order of preference:
      1. Explicit ``postmortems_dir`` arg (caller / test override).
      2. ``<project_root>/Channels/ShadowVerse/postmortems`` if ``channel_root``
         resolves the project root via the canonical path layout.
      3. Fallback to ``daily_dir`` itself so a stub is always emitted.
    """
    if postmortems_dir is not None:
        postmortems_dir.mkdir(parents=True, exist_ok=True)
        return postmortems_dir
    # Channel root looks like .../ContentOps/channels/ShadowVerse — we don't have
    # a deterministic mapping from there to the project root, so fall through to
    # daily_dir unless a caller explicitly wires it. The orphan-scan listing
    # is the important payload either way.
    return daily_dir


def _write_picks_corrupt_postmortem(
    picks_path: Path,
    exception: Exception,
    *,
    channel_root: Path | None = None,
    postmortems_dir: Path | None = None,
) -> Path:
    """Write a markdown postmortem stub describing a picks_assignment.json
    corruption event. Returns the path written.

    Stub contents:
      - timestamp (UTC), exception type+message
      - corrupted file contents truncated to 500 chars
      - today's ``02_scripts/_drafts/`` orphan listing (if channel_root provided)
      - manual recovery hint
    """
    daily_dir = picks_path.parent
    pm_dir = _resolve_postmortems_dir(channel_root, postmortems_dir, daily_dir)
    pm_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pm_path = pm_dir / f"picks_assignment_corrupt_{ts}.md"

    # Best-effort read of the corrupted file; on read failure use a placeholder.
    try:
        raw = picks_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raw = f"(unreadable: {e})"
    truncated = raw if len(raw) <= 500 else raw[:500] + "\n...(truncated)"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    orphans: list[str] = []
    if channel_root is not None:
        try:
            orphans = _scan_for_orphaned_topic_ids(channel_root, today)
        except Exception as scan_exc:  # noqa: BLE001
            log.warning("orphan scan failed during postmortem write: %s", scan_exc)

    orphan_block = (
        "\n".join(f"- `{tid}`" for tid in orphans)
        if orphans
        else "_(no orphans detected; pass --rescue-orphans to re-scan from the CLI)_"
    )

    body = (
        f"# Postmortem — picks_assignment.json corruption ({ts})\n\n"
        f"**Corrupted file:** `{picks_path}`\n"
        f"**Exception:** `{type(exception).__name__}: {exception}`\n"
        f"**Detected at (UTC):** `{datetime.now(timezone.utc).isoformat()}`\n"
        f"**Daily date scanned:** `{today}`\n\n"
        f"## Corrupted file contents (first 500 chars)\n\n"
        f"```\n{truncated}\n```\n\n"
        f"## Orphaned topic_id drafts under 02_scripts/_drafts/{today}_*\n\n"
        f"{orphan_block}\n\n"
        f"## Manual recovery hint\n\n"
        f"1. Inspect the corrupted file and the orphan list above.\n"
        f"2. If any orphan represents real script work to preserve, reconstruct\n"
        f"   `picks_assignment.json` by hand: copy the JSON shell from a recent\n"
        f"   `_daily_*` dir and re-insert the `topic` -> `topic_id` mappings.\n"
        f"3. If no orphans matter, delete `picks_assignment.json` to start a fresh\n"
        f"   batch — `daily_batch.py` will allocate new topic_ids cleanly.\n"
        f"4. Re-run `python daily_batch.py [--rescue-orphans]`.\n"
    )
    pm_path.write_text(body, encoding="utf-8")
    log.error("wrote picks-corruption postmortem stub: %s", pm_path)
    return pm_path


def _persist_picks_assignment(daily_dir: Path, mapping: dict[str, str]) -> None:
    """Persist topic→topic_id mapping. Called immediately after each new allocation
    so a crash mid-batch leaves a recoverable state."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "date": today,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assignments": [
            {"topic": t, "topic_id": tid} for t, tid in mapping.items()
        ],
    }
    path = daily_dir / "picks_assignment.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _allocate_topic_id_for_pick(
    pick: ScoredCandidate,
    assignments: dict[str, str],
    daily_dir: Path,
    channel_root: Path,
) -> AllocationResult:
    """Decide what topic_id (if any) to use for `pick` in this batch iteration.

    Three outcomes, encoded in `AllocationResult.reason`:

      * ``"reused"`` — `picks_assignment.json` already has a fresh (not-uploaded)
        entry for this pick; re-use the persisted id so re-runs resume past the
        previous halt instead of orphaning prior dispatch work.
      * ``"fresh"`` — no persisted entry; allocate the next free id for today
        and persist it immediately so a crash mid-batch leaves a recoverable
        state.
      * ``"dropped_already_uploaded"`` — `picks_assignment.json` had an entry
        but that topic_id is already in `upload_log.csv` / the 06_published
        archive. **The pick is dropped** for this batch: returning the same
        topic re-allocated would silently ship a duplicate video (see the
        2026-05-10 Adam Dunkels near-miss). The stale entry is removed from
        `picks_assignment.json` so future runs don't re-encounter it, and the
        caller is expected to surface a ``status="dropped_already_uploaded"``
        TopicResult to the batch summary. The next `daily_batch` invocation
        will produce a genuinely fresh pick via idea-gen.

    Mutates `assignments` and persists to disk on the "fresh" and
    "dropped_already_uploaded" branches.
    """
    persisted_id = assignments.get(pick.topic)
    if persisted_id is not None and is_topic_id_uploaded(persisted_id, channel_root):
        # Stale entry — the persisted topic_id has already been uploaded under
        # this pick. Reallocating would ship a duplicate. Drop the pick and
        # remove the entry so the next batch starts cleanly.
        assignments.pop(pick.topic, None)
        _persist_picks_assignment(daily_dir, assignments)
        return AllocationResult(
            topic_id=None,
            reason="dropped_already_uploaded",
            log_message=(
                f"persisted topic_id {persisted_id} for pick {pick.topic[:60]!r} "
                f"has already been uploaded (06_published archive or upload_log "
                f"row); dropping this pick to prevent duplicate shipping. Stale "
                f"picks_assignment.json entry removed; the next daily_batch run "
                f"will allocate a fresh pick from idea-gen."
            ),
        )

    if persisted_id is not None:
        return AllocationResult(
            topic_id=persisted_id,
            reason="reused",
            log_message=f"reusing persisted topic_id {persisted_id} for pick {pick.topic[:60]!r}",
        )

    topic_id = next_topic_id_for_date(channel_root)
    assignments[pick.topic] = topic_id
    _persist_picks_assignment(daily_dir, assignments)
    return AllocationResult(
        topic_id=topic_id,
        reason="fresh",
        log_message=f"allocated new topic_id {topic_id} for pick {pick.topic[:60]!r}",
    )


def _run_one_topic(pick: ScoredCandidate, topic_id: str, config: dict) -> TopicResult:
    """Run pipeline for one pick. Translate exceptions into a TopicResult, never re-raise."""
    topic = TopicJob(
        id=topic_id,
        topic=pick.topic,
        angle=pick.angle,
        hook_concept=pick.hook_concept,
    )

    common = dict(
        topic_id=topic_id,
        topic=pick.topic,
        angle=pick.angle,
        weighted_total=pick.weighted_total,
    )

    try:
        run_for_topic(topic, config)
    except ManualLLMHalt as halt:
        return TopicResult(
            **common,
            status="halted_manual_llm",
            halt_stage=halt.stage_name,
            halt_message=str(halt),
            next_action_path=halt.response_path,
        )
    except HumanReviewRequired as halt:
        return TopicResult(
            **common,
            status="halted_human_review",
            halt_stage=halt.gate_name,
            halt_message=str(halt),
            next_action_path=halt.action_path,
        )
    except NotImplementedError as e:
        # schedule_publishing is an expected stub — anything past it has been produced
        return TopicResult(
            **common,
            status="completed_through_metadata",
            halt_stage="schedule_publishing",
            halt_message=str(e),
            next_action_path=None,
        )
    except Exception as e:
        log.exception("topic %s errored at unexpected stage", topic_id)
        return TopicResult(
            **common,
            status="error",
            halt_stage=None,
            halt_message=f"{type(e).__name__}: {e}",
            next_action_path=None,
        )

    return TopicResult(
        **common,
        status="completed",
        halt_stage=None,
        halt_message="(fully shipped through schedule_publishing)",
        next_action_path=None,
    )


def _write_batch_summary(
    channel_root: Path,
    picks: list[ScoredCandidate],
    results: list[TopicResult],
) -> Path:
    """Write a markdown summary of the day's batch the operator reads to know what to do next."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = channel_root / "01_research"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"daily_batch_{today}.md"

    lines: list[str] = []
    lines.append(f"# Daily batch summary: {today}")
    lines.append("")
    lines.append(
        f"- Picks: {len(picks)}"
    )
    by_status: dict[str, list[TopicResult]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)
    for status, items in sorted(by_status.items()):
        lines.append(f"- {status}: {len(items)}")
    lines.append("")

    for r in results:
        lines.append(f"## {r.topic_id} — {r.topic}")
        lines.append("")
        lines.append(f"- **Angle:** {r.angle}")
        lines.append(f"- **Score:** {r.weighted_total:.3f}")
        lines.append(f"- **Status:** {r.status}")
        if r.halt_stage:
            lines.append(f"- **Halted at:** {r.halt_stage}")
        if r.next_action_path:
            lines.append(f"- **Next action file:** `{r.next_action_path}`")
        lines.append("")
        lines.append("```")
        lines.append(r.halt_message.strip() or "(no message)")
        lines.append("```")
        lines.append("")

        # Hook-A/B addendum (Slice 8): chosen hook + alternatives + leaderboard
        # footer. Skipped for the synthetic "<dropped>" sentinel because there
        # is no per-topic drafts dir to read. Best-effort — any failure is
        # logged and swallowed so a malformed addendum never blocks the
        # summary write the operator depends on.
        if r.topic_id != "<dropped>":
            try:
                addendum = format_hook_addendum(r.topic_id, channel_root)
            except Exception as exc:  # noqa: BLE001 — addendum must never block summary
                log.warning(
                    "hook addendum unavailable for topic_id=%r: %s",
                    r.topic_id, exc,
                )
            else:
                if addendum:
                    lines.append(addendum)
                    lines.append("")

    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def daily_batch(
    config: dict,
    *,
    n_target: int = 10,
    n_picks: int = 2,
    force_refresh_trends: bool = False,
    track: str = "ai-vendor",
) -> dict:
    """Run today's full daily batch for `track`. Idempotent on re-run.

    Topic IDs persist in `_daily_<DATE>[_<track>]/picks_assignment.json` so re-runs
    after one or more topics have been dispatched do NOT allocate fresh IDs and
    orphan the previous dispatch's script work. The lookup key is the pick's topic
    string (stable across runs because picks come from a parsed JSON response).

    track='general-tech' draws from the general-tech idea-gen prompt + weights and
    isolates its IO under the track-suffixed daily dir. Topic-IDs stay globally
    unique because next_topic_id_for_date scans the shared 02_scripts/_drafts tree;
    run the two tracks SEQUENTIALLY (or allocate both before parallel dispatch — see
    start.md) so a parallel allocate-before-draft-dir-exists race can't hand both
    tracks the same id.
    """
    log.info("daily batch starting (n_target=%d, n_picks=%d, track=%s)", n_target, n_picks, track)

    # Step 1+2: idea generation halts at manual LLM stage if response missing.
    # Re-runs of daily_batch after the response file exists pick up here transparently.
    picks = generate_ideas(
        config,
        n_target=n_target,
        n_picks=n_picks,
        force_refresh_trends=force_refresh_trends,
        track=track,
    )
    log.info("got %d picks from idea_generation", len(picks))

    # Step 3+4: run the per-topic pipeline for each pick, sequentially.
    # Topic IDs are persisted at the daily IO dir (`_daily_<DATE>/picks_assignment.json`)
    # so re-runs of daily_batch don't allocate fresh IDs and orphan previous dispatch
    # work. On first allocation, persist immediately so a crash mid-batch leaves a
    # recoverable state. Lookup key is pick.topic — stable across runs because picks
    # come from idea_generation_RESPONSE.txt verbatim.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Must match generate_ideas' base dir for this track (shared helper prevents drift).
    daily_dir = Path(config["llm"]["manual_io_dir"]) / daily_io_dirname(track, date=today)
    daily_dir.mkdir(parents=True, exist_ok=True)
    channel_root = Path(config["paths"]["channel_root"])
    # Pass channel_root so the postmortem stub can scan for orphaned drafts
    # if the persisted assignment file turns out to be corrupted.
    assignments = _load_picks_assignment(daily_dir, channel_root=channel_root)

    results: list[TopicResult] = []
    for i, pick in enumerate(picks, start=1):
        alloc = _allocate_topic_id_for_pick(pick, assignments, daily_dir, channel_root)
        if alloc.topic_id is None:
            # Pick dropped — do NOT invoke run_for_topic. Surface to the caller
            # via a TopicResult so the batch summary flags it for the operator.
            # Re-invoking idea-gen for the dropped slot here would either re-halt
            # on manual LLM or pick the same topic again; the right primitive is
            # to let the next daily_batch invocation produce a fresh pick.
            log.warning("[%d/%d] %s", i, len(picks), alloc.log_message)
            results.append(TopicResult(
                topic_id="<dropped>",
                topic=pick.topic,
                angle=pick.angle,
                weighted_total=pick.weighted_total,
                status="dropped_already_uploaded",
                halt_stage="picks_allocation",
                halt_message=alloc.log_message,
                next_action_path=None,
            ))
            continue

        topic_id = alloc.topic_id
        log.info("[%d/%d] %s", i, len(picks), alloc.log_message)
        log.info("[%d/%d] running pipeline for topic %s — %s",
                 i, len(picks), topic_id, pick.topic)
        result = _run_one_topic(pick, topic_id, config)
        log.info("[%d/%d] topic %s status=%s halt_stage=%s",
                 i, len(picks), topic_id, result.status, result.halt_stage)
        results.append(result)

    summary_path = _write_batch_summary(channel_root, picks, results)
    log.info("daily batch complete; summary at %s", summary_path)

    return {
        "picks": picks,
        "results": results,
        "summary_path": summary_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShadowVerse daily batch orchestrator")
    parser.add_argument("--n-target", type=int, default=10,
                        help="Number of candidate ideas to ask the LLM for (default 10)")
    parser.add_argument("--n-picks", type=int, default=2,
                        help="Number of top-ranked picks to advance through pipeline (default 2)")
    parser.add_argument("--refresh-trends", action="store_true",
                        help="Force trend_pull re-run even if today's artifact exists")
    parser.add_argument(
        "--track", default="ai-vendor", choices=["ai-vendor", "general-tech"],
        help=(
            "Topic track. 'general-tech' (dual-track slot) draws from the "
            "general-tech idea-gen prompt + weights and isolates IO under "
            "_daily_<date>_general-tech. Run the two tracks sequentially."
        ),
    )
    parser.add_argument(
        "--config", default=None,
        help=(
            "Path to config.yaml. In the dual-video /start -auto shape, apex "
            "should call _isolate_config_for_topic(topic_id) and pass the "
            "returned path here so sub-agents don't share a mutable config "
            "(see the 2026-05-22 cross-contamination postmortem)."
        ),
    )
    parser.add_argument(
        "--rescue-orphans", action="store_true",
        help=(
            "After a PicksAssignmentCorrupted halt, scan 02_scripts/_drafts/ "
            "for orphaned topic_id directories under today's UTC date and "
            "print the list to stdout for manual recovery. Per the 2026-05-22 "
            "fail-loud rule, this flag does NOT mask the halt — it only "
            "augments the diagnostic output."
        ),
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    validate_config(config)  # M4: fail fast on missing keys before any stage runs
    today = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    setup_logging(config, _build_run_id(f"daily_{today}"))

    try:
        daily_batch(
            config,
            n_target=args.n_target,
            n_picks=args.n_picks,
            force_refresh_trends=args.refresh_trends,
            track=args.track,
        )
    except ManualLLMHalt as halt:
        # Idea-gen stage halted (operator/Claude needs to write the response file).
        log.info("daily batch halted at idea_generation: %s", halt)
        print(str(halt), file=__import__("sys").stderr)
        return 3
    except PicksAssignmentCorrupted as halt:
        # picks_assignment.json was unreadable. Halt loud — and if the operator
        # passed --rescue-orphans, print the orphan scan so they can inspect.
        log.error("daily batch halted at picks_assignment load: %s", halt)
        print(str(halt), file=__import__("sys").stderr)
        if args.rescue_orphans:
            channel_root = Path(config["paths"]["channel_root"])
            scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            orphans = _scan_for_orphaned_topic_ids(channel_root, scan_date)
            print(
                f"\n[--rescue-orphans] orphaned topic_id drafts under "
                f"{channel_root}/02_scripts/_drafts/ matching ^{scan_date}_\\d{{3}}$:",
                file=__import__("sys").stderr,
            )
            if not orphans:
                print("  (none)", file=__import__("sys").stderr)
            else:
                for tid in orphans:
                    print(f"  - {tid}", file=__import__("sys").stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
