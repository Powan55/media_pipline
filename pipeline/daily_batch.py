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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from daily_batch_hook_addendum import format_hook_addendum
from idea_generation import generate_ideas
from pipeline import (
    HumanReviewRequired,
    ManualLLMHalt,
    TopicJob,
    _build_run_id,
    load_config,
    run_for_topic,
    setup_logging,
)
from scoring import ScoredCandidate
from topics import is_topic_id_uploaded, next_topic_id_for_date

log = logging.getLogger("daily_batch")


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


def _load_picks_assignment(daily_dir: Path) -> dict[str, str]:
    """Load topic→topic_id mapping persisted at the daily IO dir.

    Returns an empty dict if the file is missing or malformed (logged as a warning).
    Keys are pick topics (verbatim strings from the parsed idea-gen response);
    values are topic IDs like "2026-05-07_003".
    """
    path = daily_dir / "picks_assignment.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {a["topic"]: a["topic_id"] for a in data.get("assignments", [])}
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log.warning("picks_assignment.json unreadable (%s); starting fresh", e)
        return {}


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
) -> dict:
    """Run today's full daily batch. Idempotent on re-run.

    Topic IDs persist in `_daily_<DATE>/picks_assignment.json` so re-runs after
    one or more topics have been dispatched do NOT allocate fresh IDs and orphan
    the previous dispatch's script work. The lookup key is the pick's topic
    string (stable across runs because picks come from a parsed JSON response).
    """
    log.info("daily batch starting (n_target=%d, n_picks=%d)", n_target, n_picks)

    # Step 1+2: idea generation halts at manual LLM stage if response missing.
    # Re-runs of daily_batch after the response file exists pick up here transparently.
    picks = generate_ideas(
        config,
        n_target=n_target,
        n_picks=n_picks,
        force_refresh_trends=force_refresh_trends,
    )
    log.info("got %d picks from idea_generation", len(picks))

    # Step 3+4: run the per-topic pipeline for each pick, sequentially.
    # Topic IDs are persisted at the daily IO dir (`_daily_<DATE>/picks_assignment.json`)
    # so re-runs of daily_batch don't allocate fresh IDs and orphan previous dispatch
    # work. On first allocation, persist immediately so a crash mid-batch leaves a
    # recoverable state. Lookup key is pick.topic — stable across runs because picks
    # come from idea_generation_RESPONSE.txt verbatim.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_dir = Path(config["llm"]["manual_io_dir"]) / f"_daily_{today}"
    daily_dir.mkdir(parents=True, exist_ok=True)
    assignments = _load_picks_assignment(daily_dir)

    channel_root = Path(config["paths"]["channel_root"])
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
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    today = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    setup_logging(config, _build_run_id(f"daily_{today}"))

    try:
        daily_batch(
            config,
            n_target=args.n_target,
            n_picks=args.n_picks,
            force_refresh_trends=args.refresh_trends,
        )
    except ManualLLMHalt as halt:
        # Idea-gen stage halted (operator/Claude needs to write the response file).
        log.info("daily batch halted at idea_generation: %s", halt)
        print(str(halt), file=__import__("sys").stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
