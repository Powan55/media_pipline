"""Compute the two publishAt slots for a /start -auto dual-video run.

Reads `upload_log.csv`, finds the next free day where both Eastern-time slots
(default 11:25 and 12:35 — moved to the midday audience-online peak 2026-06-24;
were 17:25/18:35) are unoccupied (or validates an explicit `--date`
override), and prints an RFC3339 JSON envelope to stdout. The offset string is
generated via `zoneinfo.ZoneInfo("America/New_York")` so DST is automatic
(`-04:00` during EDT, `-05:00` during EST).

Exit codes:
    0  -> success; stdout is the success JSON envelope.
    1  -> slot collision on an explicit --date; stdout is the collision JSON.
    2  -> argparse / date-parse / no-free-day failure; stderr is human-readable.

Examples:
    python tools/compute_publish_slots.py
    python tools/compute_publish_slots.py --date 5/25/26
    python tools/compute_publish_slots.py --date 2026-05-25
    python tools/compute_publish_slots.py --slots 11:25,12:35 --date 2026-05-25
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_UPLOAD_LOG = Path(
    r"C:\ContentOps\channels\ShadowVerse\01_research\upload_log.csv"
)
DEFAULT_SLOTS = "11:25,12:35"
MAX_ROLL_DAYS = 30
COLLISION_WINDOW = timedelta(minutes=1)

# Dual-track de-confound (2026-06-25): the two tracks that can fill the daily
# slots. assign_track_slots() randomizes which one ships in the earlier vs later
# slot per run so track (content) is orthogonal to slot (time-of-day).
TRACKS: tuple[str, str] = ("ai-vendor", "general-tech")

# upload_log.csv schema: the 'privacy' column embeds the publishAt as
#   scheduled (publishAt=2026-05-21T22:30:00Z)
# for scheduled-private uploads. This regex extracts that timestamp.
_PUBLISH_AT_RE = re.compile(
    r"publishAt=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"
)


_MIN_YEAR = 2020
_MAX_YEAR = 2100


def _parse_date_arg(raw: str) -> date:
    """Parse M/D/YY, M/D/YYYY, or ISO YYYY-MM-DD into a date.

    Two-digit years are pinned to 2000-2099 (`5/25/26` -> 2026-05-25).
    Reject typos like `1/1/100` (year 100 AD) or `5/25/2999` by rejecting
    any year outside [2020, 2100]. The channel started in 2026; no realistic
    publishAt is outside this window.
    """
    raw = raw.strip()
    # ISO first — unambiguous.
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        pass
    else:
        if not (_MIN_YEAR <= parsed.year <= _MAX_YEAR):
            raise ValueError(
                f"invalid date {raw!r}: year {parsed.year} outside allowed range "
                f"[{_MIN_YEAR}, {_MAX_YEAR}]"
            )
        return parsed
    # M/D/YY or M/D/YYYY.
    parts = raw.split("/")
    if len(parts) == 3:
        try:
            month = int(parts[0])
            day = int(parts[1])
            year_raw = int(parts[2])
        except ValueError as exc:
            raise ValueError(f"invalid date {raw!r}: non-integer component") from exc
        # Accept 2-digit (00-99 -> 2000-2099) or 4-digit years in [2020, 2100].
        # Reject 1-digit and 3-digit as ambiguous/typos (e.g. `1/1/100`).
        year_str = parts[2]
        if len(year_str) == 2:
            year = 2000 + year_raw
        elif len(year_str) == 4:
            year = year_raw
        else:
            raise ValueError(
                f"invalid date {raw!r}: year component must be 2 or 4 digits, got {year_str!r}"
            )
        if not (_MIN_YEAR <= year <= _MAX_YEAR):
            raise ValueError(
                f"invalid date {raw!r}: year {year} outside allowed range "
                f"[{_MIN_YEAR}, {_MAX_YEAR}]"
            )
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"invalid date {raw!r}: {exc}") from exc
    raise ValueError(
        f"invalid date {raw!r}: expected M/D/YY, M/D/YYYY, or YYYY-MM-DD"
    )


def _parse_slots_arg(raw: str) -> list[time]:
    """Parse 'HH:MM,HH:MM' into a list of two `time` objects."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"--slots must be two comma-separated HH:MM values, got {raw!r}")
    out: list[time] = []
    for p in parts:
        try:
            hh, mm = p.split(":")
            out.append(time(int(hh), int(mm)))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"invalid slot {p!r}: expected HH:MM") from exc
    return out


def _load_existing_publishes(log_path: Path) -> list[dict]:
    """Read upload_log.csv and extract rows with a parsable publishAt.

    Returns list of {topic_id, video_id, publish_at_utc} dicts. Missing file
    -> empty list (a fresh run with no prior uploads).
    """
    if not log_path.exists():
        return []
    rows: list[dict] = []
    with log_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            privacy = row.get("privacy") or ""
            m = _PUBLISH_AT_RE.search(privacy)
            if not m:
                continue
            raw = m.group(1)
            # Python <3.11 ISO parser rejects trailing 'Z'; normalize.
            iso = raw.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            rows.append(
                {
                    "topic_id": row.get("topic_id") or "",
                    "video_id": row.get("video_id") or "",
                    "publish_at_utc": dt.astimezone(timezone.utc),
                }
            )
    return rows


def _slot_status(
    candidate_local: datetime, existing: list[dict]
) -> tuple[str, dict | None]:
    """Return ('free', None) or ('occupied', {topic_id,video_id,publish_at})."""
    candidate_utc = candidate_local.astimezone(timezone.utc)
    for entry in existing:
        if abs(entry["publish_at_utc"] - candidate_utc) <= COLLISION_WINDOW:
            return "occupied", {
                "topic_id": entry["topic_id"],
                "video_id": entry["video_id"],
                "publish_at": entry["publish_at_utc"]
                .astimezone(ET)
                .isoformat(timespec="seconds"),
            }
    return "free", None


def _build_slot(target: date, slot_time: time) -> datetime:
    """Construct an Eastern-local datetime for a date+time pair."""
    return datetime.combine(target, slot_time, tzinfo=ET)


def compute_slots(
    *,
    explicit_date: date | None,
    slots: list[time],
    log_path: Path,
    today: date | None = None,
) -> tuple[int, dict]:
    """Core algorithm. Returns (exit_code, payload_dict).

    Pure function — no stdout/sys.exit side effects, for testability.
    `today` defaults to today's local date and exists for deterministic tests.
    """
    existing = _load_existing_publishes(log_path)
    today = today or datetime.now(ET).date()

    if explicit_date is not None:
        slot1 = _build_slot(explicit_date, slots[0])
        slot2 = _build_slot(explicit_date, slots[1])
        s1_status, s1_conflict = _slot_status(slot1, existing)
        s2_status, s2_conflict = _slot_status(slot2, existing)
        if s1_status == "occupied" or s2_status == "occupied":
            payload = {
                "error": "collision",
                "date": explicit_date.isoformat(),
                "slot1_status": s1_status,
                "slot2_status": s2_status,
            }
            if s1_conflict is not None:
                payload["slot1_conflict"] = s1_conflict
            if s2_conflict is not None:
                payload["slot2_conflict"] = s2_conflict
            return 1, payload
        return 0, {
            "date": explicit_date.isoformat(),
            "slot1": slot1.isoformat(timespec="seconds"),
            "slot2": slot2.isoformat(timespec="seconds"),
            "rolled_days": 0,
        }

    # No explicit date: roll forward from tomorrow.
    start = today + timedelta(days=1)
    for offset in range(MAX_ROLL_DAYS + 1):
        candidate = start + timedelta(days=offset)
        slot1 = _build_slot(candidate, slots[0])
        slot2 = _build_slot(candidate, slots[1])
        s1_status, _ = _slot_status(slot1, existing)
        s2_status, _ = _slot_status(slot2, existing)
        if s1_status == "free" and s2_status == "free":
            return 0, {
                "date": candidate.isoformat(),
                "slot1": slot1.isoformat(timespec="seconds"),
                "slot2": slot2.isoformat(timespec="seconds"),
                "rolled_days": offset,
            }
    return 2, {
        "error": "no_free_day_within_cap",
        "cap_days": MAX_ROLL_DAYS,
        "searched_from": start.isoformat(),
    }


def assign_track_slots(target_date: date, *, tracks: tuple[str, str] = TRACKS) -> dict:
    """Decide which track ships in slot 1 (earlier) vs slot 2 (later) for a run.

    DE-CONFOUND (2026-06-25): the dual-track measurement compares the general-tech
    track against the ai-vendor control. If general-tech ALWAYS ships in the later
    slot (the prior hardcoded behavior), track is perfectly confounded with
    time-of-day — a track effect can't be told apart from a clock effect. This
    randomizes the track->slot mapping per run so, over the measurement window,
    each track lands in each slot ~50% of the time and slot (time) becomes
    orthogonal to track (content).

    Deterministic per date: seeded by the target date's ISO string, so a same-day
    re-run yields the SAME assignment (idempotent — matching the slot-collision
    and stale-idea-gen re-run safety elsewhere) while different dates vary.
    Returns ``{"slot1": <track>, "slot2": <track>, "swapped": bool, "seed": int}``.
    """
    seed = int(hashlib.sha256(target_date.isoformat().encode("utf-8")).hexdigest()[:8], 16)
    swapped = bool(seed & 1)
    slot1, slot2 = (tracks[1], tracks[0]) if swapped else (tracks[0], tracks[1])
    return {"slot1": slot1, "slot2": slot2, "swapped": swapped, "seed": seed}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Compute the two publishAt slots for a /start -auto run. "
            "Examples: "
            "`python tools/compute_publish_slots.py` (next free day); "
            "`python tools/compute_publish_slots.py --date 5/25/26`; "
            "`python tools/compute_publish_slots.py --date 2026-05-25`."
        ),
    )
    p.add_argument(
        "--date",
        dest="date_raw",
        default=None,
        help="Explicit publish date (M/D/YY, M/D/YYYY, or YYYY-MM-DD). Halts on collision.",
    )
    p.add_argument(
        "--upload-log",
        default=str(DEFAULT_UPLOAD_LOG),
        help="Path to upload_log.csv (default: ShadowVerse channel log).",
    )
    p.add_argument(
        "--slots",
        default=DEFAULT_SLOTS,
        help="Comma-separated HH:MM,HH:MM Eastern-local slot times (default: 11:25,12:35).",
    )
    p.add_argument(
        "--dual-track",
        action="store_true",
        help="Add a date-seeded track->slot assignment (de-confound) to the output "
        "envelope under 'track_assignment'. Omit => envelope unchanged (ai-vendor-only).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        slots = _parse_slots_arg(args.slots)
        explicit_date = _parse_date_arg(args.date_raw) if args.date_raw else None
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log_path = Path(args.upload_log)
    exit_code, payload = compute_slots(
        explicit_date=explicit_date,
        slots=slots,
        log_path=log_path,
    )
    # De-confound: attach the date-seeded track->slot mapping only when the caller
    # opts in, so the ai-vendor-only envelope stays byte-identical.
    if exit_code == 0 and args.dual_track:
        payload["track_assignment"] = assign_track_slots(date.fromisoformat(payload["date"]))
    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
