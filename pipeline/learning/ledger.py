"""Canonical per-video learning ledger.

One row per video that ShadowVerse has hook data for, joining:
  identity (topic_id <-> video_id) <-> hook formula <-> idea-gen scoring <->
  Stage-1.5 quality dims <-> cluster <-> matured analytics outcomes.

This is the training substrate the rest of the loop reads. It is a DERIVED
snapshot — rebuilt deterministically from the source artifacts each cycle, never
appended to. Building it is pure read + derive; ``write_ledger_csv`` is the only
writer and it targets ``learning/learning_ledger.csv`` under the channel root.

Sources (all already produced by the pipeline):
  * ``01_research/hook_selection_log.jsonl``   — universe (topic_id, formula)
  * ``01_research/upload_log.csv``             — topic_id <-> video_id, slot
  * ``01_research/_weekly_analytics.csv``      — matured outcomes (latest pull)
  * ``02_scripts/_drafts/_daily_*/`` picks_assignment.json + idea_generation_RANKED.json
                                               — idea-gen scoring per topic_id
  * ``01_research/script_quality_log.jsonl``   — Stage-1.5 dims (forward-looking)
  * ``01_research/_video_clusters.csv``        — hand-curated cluster labels (optional)

Reuses ``analytics_join.join_hooks_to_analytics`` for the spine + eligibility
math, and ``learning.csv_health.load_latest_analytics_rows`` for the rich
columns (traffic share, engagement, duration) the join doesn't expose.

NOTE on scoring columns: ``idea_generation_RANKED.json`` persists
``weighted_total`` + the three bonus values but NOT the 8 normalized component
sub-scores (those are computed at rank time and not saved). The ledger captures
what is actually persisted; component columns are intentionally absent rather
than fabricated.

Stdlib only. Fail-soft callers wrap ``build_learning_ledger``; it tolerates every
missing source file by emitting blanks, never raising on absent data.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import analytics_join

from . import paths as learning_paths
from .csv_health import load_latest_analytics_rows
from .telemetry import load_quality_log

log = logging.getLogger("learning.ledger")

# Mirror of pipeline.SCRIPT_QUALITY_DIMENSIONS — kept local so the learning CLI
# doesn't import the heavy pipeline module (whisper/ffmpeg/google deps) just for
# a constant. Keep in sync with prompts/03_script_generation.md.
SCRIPT_QUALITY_DIMENSIONS: tuple[str, ...] = (
    "hook_strength",
    "second_hook_strength",
    "specificity",
    "opinion_density",
    "cited_observation_quality",
    "broll_cadence",
)

# A video is "matured" (eligible for learning) once it is this many days live —
# YouTube analytics need ~48h to stabilize for a fresh Short.
MATURITY_FLOOR_DAYS: int = 2

# Minimum views for a matured row to count as a real datapoint (below this the
# Analytics API often returns empty/partial rows). Matches the leaderboard's
# eligibility floor (operator decision 2026-05-12).
MIN_VIEWS_FLOOR: int = 70

LEDGER_CSV_NAME = "learning_ledger.csv"

# Heuristic anchor lexicon for title_anchor_present. A recognizable named anchor
# in the first words of the title is the breakout reach lever (~2x). This list
# is intentionally tunable — the analysis layer treats anchor-present as one
# feature among several, so precision matters less than consistency.
_ANCHOR_TERMS: frozenset[str] = frozenset(
    t.lower()
    for t in (
        "ChatGPT", "GPT", "OpenAI", "Claude", "Anthropic", "Gemini", "Google",
        "DeepMind", "Grok", "xAI", "Llama", "Meta", "Mistral", "DeepSeek",
        "Perplexity", "Midjourney", "Sora", "Runway", "ElevenLabs", "Suno",
        "Copilot", "Microsoft", "Apple", "iPhone", "Siri", "Tesla", "Neuralink",
        "Nvidia", "Amazon", "Alexa", "Cursor", "Aider", "Windows", "Android",
        "Sam", "Altman", "Elon", "Musk", "Zuckerberg", "Nadella", "Pichai",
        "Hassabis", "Murati", "Shazeer", "Karpathy", "Hinton", "Wired",
    )
)

_DIGIT_RE = re.compile(r"\d")
_PUBLISH_MINUTE_RE = re.compile(r"publishAt=\d{4}-\d{2}-\d{2}T\d{2}:(\d{2})")


@dataclass
class LedgerRow:
    """One denormalized per-video row. ``quality_dims`` is flattened to
    ``q_<dim>`` columns by :func:`ledger_row_to_dict`."""

    topic_id: str
    video_id: str | None
    title: str | None
    published_at: str | None
    pull_date: str | None
    days_live: int | None
    matured: bool
    track: str | None
    slot: int | None
    hook_letter: str | None
    hook_formula: str | None
    cluster: str | None
    # Scoring (idea-gen): only what RANKED.json actually persists.
    weighted_total: float | None
    counter_conventional_bonus: float | None
    ai_vendor_bonus: float | None
    named_human_bonus: float | None
    corporate_deal_damped: bool | None
    # Stage-1.5 craft quality.
    quality_dims: dict[str, float] = field(default_factory=dict)
    q_weighted_total: float | None = None
    # Derived features.
    title_anchor_present: bool | None = None
    word_count: int | None = None
    duration_s: float | None = None
    # Outcomes (matured analytics).
    views: int | None = None
    avg_view_pct: float | None = None
    avg_view_duration_sec: float | None = None
    hold_at_3s: float | None = None
    traffic_source_shorts_pct: float | None = None
    likes: int | None = None
    shares: int | None = None
    comments: int | None = None
    follower_delta: int | None = None
    # Learning eligibility.
    eligible: bool = False
    quarantine_reason: str = ""


# Stable CSV column order. q_<dim> columns are inserted positionally.
LEDGER_COLUMNS: tuple[str, ...] = (
    "topic_id", "video_id", "title", "published_at", "pull_date",
    "days_live", "matured", "track", "slot", "hook_letter", "hook_formula",
    "cluster", "weighted_total", "counter_conventional_bonus",
    "ai_vendor_bonus", "named_human_bonus", "corporate_deal_damped",
    *(f"q_{d}" for d in SCRIPT_QUALITY_DIMENSIONS), "q_weighted_total",
    "title_anchor_present", "word_count", "duration_s",
    "views", "avg_view_pct", "avg_view_duration_sec", "hold_at_3s",
    "traffic_source_shorts_pct", "likes", "shares", "comments",
    "follower_delta", "eligible", "quarantine_reason",
)


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------

def _opt_int(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _opt_float(raw) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def _load_upload_log_full(path: Path) -> dict[str, dict]:
    """topic_id -> latest upload row (video_id, privacy, title, ...)."""
    out: dict[str, dict] = {}
    if not path.exists() or path.stat().st_size == 0:
        return out
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            topic_id = (row.get("topic_id") or "").strip()
            if not topic_id:
                continue
            out[topic_id] = row  # last row wins (latest upload)
    return out


def _load_scoring_by_topic_id(channel_root: Path) -> dict[str, dict]:
    """Join every ``_daily_*`` dir's picks_assignment + RANKED into
    ``topic_id -> {scoring fields, track}``.

    picks_assignment maps topic-text -> topic_id; RANKED maps topic-text ->
    scoring. The exact topic string is the join key. Archived dirs (name ending
    ``_archived``) are skipped. Later dirs (sorted by name) win on collision.
    """
    out: dict[str, dict] = {}
    drafts = channel_root / "02_scripts" / "_drafts"
    if not drafts.exists():
        return out
    for daily_dir in sorted(drafts.glob("_daily_*")):
        if not daily_dir.is_dir() or daily_dir.name.endswith("_archived"):
            continue
        picks_path = daily_dir / "picks_assignment.json"
        ranked_path = daily_dir / "idea_generation_RANKED.json"
        if not picks_path.exists() or not ranked_path.exists():
            continue
        track = "general-tech" if daily_dir.name.endswith("_general-tech") else "ai-vendor"
        try:
            picks = json.loads(picks_path.read_text(encoding="utf-8"))
            ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping scoring scan for %s: %s", daily_dir.name, exc)
            continue
        topic_to_id = {
            a.get("topic"): a.get("topic_id")
            for a in picks.get("assignments", [])
            if a.get("topic") and a.get("topic_id")
        }
        for cand in ranked.get("ranked", []):
            topic = cand.get("topic")
            topic_id = topic_to_id.get(topic)
            if not topic_id:
                continue
            out[topic_id] = {
                "weighted_total": _opt_float(cand.get("weighted_total")),
                "counter_conventional_bonus": _opt_float(cand.get("counter_conventional_bonus")),
                "ai_vendor_bonus": _opt_float(cand.get("ai_vendor_bonus")),
                "named_human_bonus": _opt_float(cand.get("named_human_bonus")),
                "corporate_deal_damped": bool(cand.get("corporate_deal_damped", False)),
                "track": track,
            }
    return out


def _load_clusters(path: Path) -> dict[str, str]:
    """Build a lookup of ``key -> cluster`` keyed by BOTH video_id and topic_id
    (whichever a row carries). Hand-curated file; tolerated absent."""
    out: dict[str, str] = {}
    if not path.exists() or path.stat().st_size == 0:
        return out
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            cluster = (row.get("cluster") or "").strip()
            if not cluster:
                continue
            for key in ((row.get("video_id") or "").strip(), (row.get("topic_id") or "").strip()):
                if key:
                    out.setdefault(key, cluster)
    return out


# ---------------------------------------------------------------------------
# Derivers
# ---------------------------------------------------------------------------

def _slot_from_privacy(privacy: str | None) -> int | None:
    """Infer the publish slot from an upload_log privacy string.

    Slot 1 schedules at minute :25 (5:25 PM ET), slot 2 at minute :35
    (6:35 PM ET). Using the MINUTE is DST-robust (the UTC hour shifts, the
    minute doesn't). Non-scheduled rows yield None.
    """
    if not privacy:
        return None
    m = _PUBLISH_MINUTE_RE.search(privacy)
    if not m:
        return None
    minute = m.group(1)
    if minute == "25":
        return 1
    if minute == "35":
        return 2
    return None


def _title_anchor_present(title: str | None) -> bool | None:
    """Heuristic: does the title open with a recognizable named anchor?

    True if any anchor term appears in the first 6 words, or a digit appears in
    the first 4 words (a specific number is also an anchor). None if no title.
    """
    if not title:
        return None
    words = title.split()
    head = words[:6]
    if any(w.strip(".,:!?\"'").lower() in _ANCHOR_TERMS for w in head):
        return True
    if any(_DIGIT_RE.search(w) for w in words[:4]):
        return True
    return False


def _derive_duration_s(avg_view_duration_sec: float | None, avg_view_pct: float | None) -> float | None:
    """Total video length = averageViewDuration / (averageViewPercentage/100).

    YouTube defines averageViewPercentage = avd / videoDuration * 100, so the
    full length is recoverable when avp > 0. Returns None otherwise.
    """
    if avg_view_duration_sec is None or not avg_view_pct:
        return None
    if avg_view_pct <= 0:
        return None
    return round(avg_view_duration_sec * 100.0 / avg_view_pct, 2)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_learning_ledger(
    channel_root: Path | str,
    *,
    today: date | None = None,
    min_views_floor: int = MIN_VIEWS_FLOOR,
    maturity_floor_days: int = MATURITY_FLOOR_DAYS,
) -> list[LedgerRow]:
    """Build the per-video ledger. Pure read; tolerates every missing source."""
    channel_root = Path(channel_root)
    research = channel_root / "01_research"
    today = today or date.today()

    spine = analytics_join.join_hooks_to_analytics(
        channel_root, today=today, eligibility_min_views=min_views_floor
    )
    analytics_rows = load_latest_analytics_rows(research / "_weekly_analytics.csv")
    upload_rows = _load_upload_log_full(research / "upload_log.csv")
    scoring_by_tid = _load_scoring_by_topic_id(channel_root)
    quality_by_tid = load_quality_log(research / "script_quality_log.jsonl")
    clusters = _load_clusters(research / "_video_clusters.csv")

    rows: list[LedgerRow] = []
    for s in spine:
        tid = s.topic_id
        vid = s.video_id
        arow = analytics_rows.get(vid, {}) if vid else {}
        urow = upload_rows.get(tid, {})
        scoring = scoring_by_tid.get(tid, {})
        q = quality_by_tid.get(tid, {})

        title = (arow.get("title") or urow.get("title") or "").strip() or None
        avg_view_pct = s.avg_view_pct if s.avg_view_pct is not None else _opt_float(arow.get("avg_view_pct"))
        avg_view_duration_sec = _opt_float(arow.get("avg_view_duration_sec"))
        days_live = s.days_live
        matured = days_live is not None and days_live >= maturity_floor_days

        quarantine = _quarantine_reason(
            video_id=vid, has_analytics=bool(arow),
            days_live=days_live, views=s.views,
            min_views_floor=min_views_floor, maturity_floor_days=maturity_floor_days,
        )

        cluster = clusters.get(vid or "") or clusters.get(tid) or None
        q_dims = {
            d: float(v)
            for d, v in (q.get("dims") or {}).items()
            if isinstance(v, (int, float))
        }

        rows.append(
            LedgerRow(
                topic_id=tid,
                video_id=vid,
                title=title,
                published_at=(arow.get("published_at") or None),
                pull_date=(arow.get("pull_date") or None),
                days_live=days_live,
                matured=matured,
                track=scoring.get("track"),
                slot=_slot_from_privacy(urow.get("privacy")),
                hook_letter=s.hook_letter,
                hook_formula=s.formula,
                cluster=cluster,
                weighted_total=scoring.get("weighted_total"),
                counter_conventional_bonus=scoring.get("counter_conventional_bonus"),
                ai_vendor_bonus=scoring.get("ai_vendor_bonus"),
                named_human_bonus=scoring.get("named_human_bonus"),
                corporate_deal_damped=scoring.get("corporate_deal_damped"),
                quality_dims=q_dims,
                q_weighted_total=_opt_float(q.get("weighted_total")),
                title_anchor_present=_title_anchor_present(title),
                word_count=None,  # v1: duration_s is the length signal; precise body parsing deferred
                duration_s=_derive_duration_s(avg_view_duration_sec, avg_view_pct),
                views=s.views,
                avg_view_pct=avg_view_pct,
                avg_view_duration_sec=avg_view_duration_sec,
                hold_at_3s=s.hold_at_3s,
                traffic_source_shorts_pct=_opt_float(arow.get("traffic_source_shorts_pct")),
                likes=_opt_int(arow.get("likes")),
                shares=_opt_int(arow.get("shares")),
                comments=_opt_int(arow.get("comments")),
                follower_delta=_opt_int(arow.get("follower_delta")),
                eligible=(quarantine == ""),
                quarantine_reason=quarantine,
            )
        )
    log.info(
        "built learning ledger: %d rows (%d eligible for learning)",
        len(rows), sum(1 for r in rows if r.eligible),
    )
    return rows


def _quarantine_reason(
    *, video_id: str | None, has_analytics: bool, days_live: int | None,
    views: int | None, min_views_floor: int, maturity_floor_days: int,
) -> str:
    """Why a row is excluded from learning (empty string => eligible)."""
    if not video_id:
        return "not_uploaded"
    if not has_analytics:
        return "no_analytics_row"
    if days_live is None or days_live < maturity_floor_days:
        return "immature"
    if views is None or views < min_views_floor:
        return "low_views"
    return ""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def ledger_row_to_dict(row: LedgerRow) -> dict[str, str]:
    """Flatten a LedgerRow into a stable CSV dict (q_<dim> columns expanded)."""
    out: dict[str, str] = {}
    for col in LEDGER_COLUMNS:
        if col.startswith("q_") and col != "q_weighted_total":
            dim = col[2:]
            out[col] = _fmt(row.quality_dims.get(dim))
        else:
            out[col] = _fmt(getattr(row, col, None))
    return out


def write_ledger_csv(rows: list[LedgerRow], out_path: Path | str) -> Path:
    """Write the ledger as a fresh CSV snapshot (overwrites)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(LEDGER_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow(ledger_row_to_dict(r))
    return out_path


# ---------------------------------------------------------------------------
# CLI: python -m learning.ledger [--channel-root PATH] [--dry-run]
# ---------------------------------------------------------------------------

def _default_channel_root() -> Path:
    """Best-effort channel_root: read config.yaml paths, else the standard path."""
    try:
        import yaml  # local import; only needed for the CLI default

        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            cr = (cfg.get("paths") or {}).get("channel_root")
            if cr:
                return Path(cr)
    except Exception:  # noqa: BLE001 — CLI convenience only
        pass
    return Path(r"C:\ContentOps\channels\ShadowVerse")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build the ShadowVerse learning ledger.")
    parser.add_argument("--channel-root", default=None, help="Channel root dir.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary, do not write the CSV.")
    parser.add_argument("--limit", type=int, default=15, help="Rows to print in dry-run.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    channel_root = Path(args.channel_root) if args.channel_root else _default_channel_root()

    rows = build_learning_ledger(channel_root)
    eligible = [r for r in rows if r.eligible]
    by_reason: dict[str, int] = {}
    for r in rows:
        key = r.quarantine_reason or "eligible"
        by_reason[key] = by_reason.get(key, 0) + 1

    print(f"\nLearning ledger: {len(rows)} rows, {len(eligible)} eligible")
    print("Breakdown:", ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    n_hold = sum(1 for r in eligible if r.hold_at_3s is not None)
    n_traffic = sum(1 for r in eligible if r.traffic_source_shorts_pct is not None)
    print(f"Eligible rows carrying hold_at_3s: {n_hold}/{len(eligible)}; "
          f"traffic_source_shorts_pct: {n_traffic}/{len(eligible)}")

    print(f"\nTop {args.limit} eligible rows by views:")
    for r in sorted(eligible, key=lambda r: (r.views or 0), reverse=True)[: args.limit]:
        print(
            f"  {r.topic_id} [{r.hook_formula}] cluster={r.cluster} "
            f"views={r.views} hold={r.hold_at_3s} avp={r.avg_view_pct} "
            f"dur={r.duration_s}s anchor={r.title_anchor_present} slot={r.slot}"
        )

    if args.dry_run:
        print("\n(dry-run: ledger NOT written)")
        return 0

    out = write_ledger_csv(rows, learning_paths.ledger_csv(channel_root))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LedgerRow",
    "LEDGER_COLUMNS",
    "SCRIPT_QUALITY_DIMENSIONS",
    "MATURITY_FLOOR_DAYS",
    "MIN_VIEWS_FLOOR",
    "build_learning_ledger",
    "ledger_row_to_dict",
    "write_ledger_csv",
]
