"""Unit tests for tools/compute_publish_slots.py.

Synthetic upload_log.csv fixtures are written under tmp_path; the helper is
exercised both via its main() CLI entrypoint (subprocess-free, captures stdout
via capsys) and via compute_slots() directly for the deterministic-today cases.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import compute_publish_slots as cps  # noqa: E402


# Explicit-slot fixture used by the compute_slots() direct tests below — passed
# in via slots=..., so these tests are decoupled from the module DEFAULT_SLOTS
# (which moved to the midday peak 11:25/12:35 on 2026-06-24). Kept at the former
# evening times to exercise the EDT/EST offset + collision math unchanged.
SLOTS_DEFAULT = [time(17, 25), time(18, 35)]
# These fixture slots translate to UTC as 21:25Z (EDT) / 22:35Z (EDT) for May
# dates and 22:25Z (EST) / 23:35Z (EST) for November dates.


def _write_log(
    path: Path, rows: list[dict], header: tuple = (
        "uploaded_at", "topic_id", "video_id", "url", "privacy", "title"
    )
) -> Path:
    """Write a synthetic upload_log.csv at `path`."""
    lines = [",".join(header)]
    for row in rows:
        cells = [str(row.get(col, "")) for col in header]
        # Quote the privacy column when it contains commas/parens just like csv would,
        # but our test rows use the same shape as the real log (no commas in privacy).
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _scheduled_cell(publish_at_utc: str) -> str:
    """Format the privacy-column value the way youtube_upload.py writes it."""
    return f"scheduled (publishAt={publish_at_utc})"


# --- compute_slots() direct tests (with deterministic `today=...`) ----------------


def test_empty_log_no_date_returns_tomorrow(tmp_path):
    """Case 1: empty log + no --date -> tomorrow's slots, rolled_days=0."""
    log = tmp_path / "upload_log.csv"  # does not exist
    exit_code, payload = cps.compute_slots(
        explicit_date=None,
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 0
    assert payload["date"] == "2026-05-21"
    assert payload["slot1"] == "2026-05-21T17:25:00-04:00"
    assert payload["slot2"] == "2026-05-21T18:35:00-04:00"
    assert payload["rolled_days"] == 0


def test_tomorrow_slot1_occupied_rolls_one_day(tmp_path):
    """Case 2: tomorrow's 5:25 slot occupied -> rolls to tomorrow+1."""
    log = tmp_path / "upload_log.csv"
    # 17:25 EDT on 2026-05-21 = 21:25 UTC. Conflict with slot1 only.
    _write_log(log, [{
        "uploaded_at": "2026-05-20T00:00:00+00:00",
        "topic_id": "2026-05-19_001",
        "video_id": "abc123",
        "url": "https://www.youtube.com/watch?v=abc123",
        "privacy": _scheduled_cell("2026-05-21T21:25:00Z"),
        "title": "preempts slot1",
    }])
    exit_code, payload = cps.compute_slots(
        explicit_date=None,
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 0
    assert payload["date"] == "2026-05-22"
    assert payload["rolled_days"] == 1


def test_tomorrow_both_slots_occupied_rolls(tmp_path):
    """Case 3: tomorrow both slots occupied -> rolls to tomorrow+1."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [
        {
            "uploaded_at": "2026-05-20T00:00:00+00:00",
            "topic_id": "2026-05-19_001",
            "video_id": "abc",
            "url": "https://www.youtube.com/watch?v=abc",
            "privacy": _scheduled_cell("2026-05-21T21:25:00Z"),
            "title": "slot1",
        },
        {
            "uploaded_at": "2026-05-20T00:00:01+00:00",
            "topic_id": "2026-05-19_002",
            "video_id": "def",
            "url": "https://www.youtube.com/watch?v=def",
            "privacy": _scheduled_cell("2026-05-21T22:35:00Z"),
            "title": "slot2",
        },
    ])
    exit_code, payload = cps.compute_slots(
        explicit_date=None,
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 0
    assert payload["date"] == "2026-05-22"
    assert payload["rolled_days"] == 1


def test_explicit_date_both_free(tmp_path):
    """Case 4: --date with both slots free -> returns those slots, no rolling."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 25),
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 0
    assert payload["date"] == "2026-05-25"
    assert payload["slot1"] == "2026-05-25T17:25:00-04:00"
    assert payload["slot2"] == "2026-05-25T18:35:00-04:00"
    assert payload["rolled_days"] == 0


def test_explicit_date_slot1_occupied_returns_collision(tmp_path):
    """Case 5: --date with slot1 occupied -> exit 1 with collision JSON."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [{
        "uploaded_at": "2026-05-20T00:00:00+00:00",
        "topic_id": "2026-05-19_001",
        "video_id": "occupier",
        "url": "https://www.youtube.com/watch?v=occupier",
        "privacy": _scheduled_cell("2026-05-25T21:25:00Z"),  # 17:25 EDT
        "title": "occupying slot1",
    }])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 25),
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 1
    assert payload["error"] == "collision"
    assert payload["slot1_status"] == "occupied"
    assert payload["slot2_status"] == "free"
    assert payload["slot1_conflict"]["topic_id"] == "2026-05-19_001"
    assert payload["slot1_conflict"]["video_id"] == "occupier"
    assert "slot2_conflict" not in payload


# --- Date / arg parsing ------------------------------------------------------


def test_mdyy_parsing():
    """Case 6: M/D/YY parses as 20YY."""
    assert cps._parse_date_arg("5/25/26") == date(2026, 5, 25)
    assert cps._parse_date_arg("12/1/26") == date(2026, 12, 1)
    # 4-digit year accepted too.
    assert cps._parse_date_arg("5/25/2026") == date(2026, 5, 25)


def test_iso_parsing():
    """Case 7: ISO YYYY-MM-DD parses."""
    assert cps._parse_date_arg("2026-05-25") == date(2026, 5, 25)


def test_invalid_date_format_raises():
    """Case 10: invalid date format -> ValueError (CLI maps to exit 2)."""
    with pytest.raises(ValueError):
        cps._parse_date_arg("not-a-date")
    with pytest.raises(ValueError):
        cps._parse_date_arg("13/40/26")  # bad month/day
    with pytest.raises(ValueError):
        cps._parse_date_arg("5/25")  # too few components


def test_three_digit_year_rejected():
    """F-007: a typo of `1/1/100` must NOT parse as year 100 AD.

    Before this fix `1/1/100` fell into the 4-digit branch and silently
    constructed `date(100, 1, 1)`. We now reject any year outside
    [2020, 2100] and explicitly reject 1- or 3-digit year components in
    the slash form.
    """
    # 3-digit year component should be rejected outright (slash form).
    with pytest.raises(ValueError, match=r"2 or 4 digits"):
        cps._parse_date_arg("1/1/100")
    # 1-digit year component also rejected.
    with pytest.raises(ValueError, match=r"2 or 4 digits"):
        cps._parse_date_arg("1/1/5")
    # A 4-digit but out-of-range year is rejected with the range message.
    with pytest.raises(ValueError, match=r"outside allowed range"):
        cps._parse_date_arg("1/1/2999")
    with pytest.raises(ValueError, match=r"outside allowed range"):
        cps._parse_date_arg("1/1/1999")
    # ISO YYYY-MM-DD with out-of-range year is also rejected.
    with pytest.raises(ValueError, match=r"outside allowed range"):
        cps._parse_date_arg("1999-01-01")
    with pytest.raises(ValueError, match=r"outside allowed range"):
        cps._parse_date_arg("0100-01-01")


def test_year_pivot_boundaries_accepted():
    """Boundaries of the [2020, 2100] window are accepted on both ISO and slash forms."""
    assert cps._parse_date_arg("2020-01-01") == date(2020, 1, 1)
    assert cps._parse_date_arg("2100-12-31") == date(2100, 12, 31)
    assert cps._parse_date_arg("1/1/20") == date(2020, 1, 1)
    assert cps._parse_date_arg("12/31/99") == date(2099, 12, 31)


# --- DST boundary tests ------------------------------------------------------


def test_november_date_is_est_offset(tmp_path):
    """Case 8: November (after DST end) returns -05:00 offset string."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 11, 15),
        slots=SLOTS_DEFAULT,
        log_path=log,
    )
    assert exit_code == 0
    assert payload["slot1"].endswith("-05:00")
    assert payload["slot2"].endswith("-05:00")


def test_may_date_is_edt_offset(tmp_path):
    """Case 9: May (during DST) returns -04:00 offset string."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 15),
        slots=SLOTS_DEFAULT,
        log_path=log,
    )
    assert exit_code == 0
    assert payload["slot1"].endswith("-04:00")
    assert payload["slot2"].endswith("-04:00")


# --- CLI main() integration --------------------------------------------------


def test_main_cli_invalid_date_exits_2(tmp_path, capsys):
    """Case 10 (CLI variant): main() returns 2 on invalid --date."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    rc = cps.main(["--date", "not-a-date", "--upload-log", str(log)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err


def test_main_cli_explicit_date_stdout_is_json(tmp_path, capsys):
    """main() prints JSON to stdout; downstream PowerShell can ConvertFrom-Json."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    rc = cps.main([
        "--date", "5/25/26", "--upload-log", str(log),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["date"] == "2026-05-25"
    # No --slots passed -> exercises the module DEFAULT_SLOTS, moved to the
    # midday audience-online peak (11:25/12:35 ET) on 2026-06-24.
    assert payload["slot1"] == "2026-05-25T11:25:00-04:00"


def test_custom_slots_override(tmp_path):
    """--slots override changes the slot times in output."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [])
    parsed = cps._parse_slots_arg("09:00,10:30")
    assert parsed == [time(9, 0), time(10, 30)]
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 25),
        slots=parsed,
        log_path=log,
    )
    assert exit_code == 0
    assert payload["slot1"] == "2026-05-25T09:00:00-04:00"
    assert payload["slot2"] == "2026-05-25T10:30:00-04:00"


def test_missing_upload_log_is_treated_as_empty(tmp_path):
    """Missing upload_log.csv = no prior uploads, not an error."""
    log = tmp_path / "does_not_exist.csv"
    exit_code, payload = cps.compute_slots(
        explicit_date=None,
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 0
    assert payload["rolled_days"] == 0


def test_collision_window_is_one_minute(tmp_path):
    """publishAt within +/-1 min of a slot counts as a collision."""
    log = tmp_path / "upload_log.csv"
    # 17:24:30 EDT = 21:24:30 UTC -- 30 seconds before slot1 17:25 -> collision.
    _write_log(log, [{
        "uploaded_at": "2026-05-20T00:00:00+00:00",
        "topic_id": "2026-05-19_001",
        "video_id": "near",
        "url": "https://www.youtube.com/watch?v=near",
        "privacy": _scheduled_cell("2026-05-25T21:24:30Z"),
        "title": "near miss",
    }])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 25),
        slots=SLOTS_DEFAULT,
        log_path=log,
    )
    assert exit_code == 1
    assert payload["slot1_status"] == "occupied"


def test_rows_without_publishat_are_ignored(tmp_path):
    """Public/private rows with no publishAt embed are ignored (not collisions)."""
    log = tmp_path / "upload_log.csv"
    _write_log(log, [
        # Old-format private row, no publishAt
        {
            "uploaded_at": "2026-05-20T00:00:00+00:00",
            "topic_id": "2026-05-19_001",
            "video_id": "noschedule",
            "url": "https://www.youtube.com/watch?v=noschedule",
            "privacy": "private",
            "title": "no schedule",
        },
    ])
    exit_code, payload = cps.compute_slots(
        explicit_date=date(2026, 5, 25),
        slots=SLOTS_DEFAULT,
        log_path=log,
    )
    assert exit_code == 0


def test_no_free_day_within_cap_returns_exit_2(tmp_path, monkeypatch):
    """If 30 days are all booked -> exit 2 with no_free_day_within_cap error."""
    log = tmp_path / "upload_log.csv"
    # Shrink the cap to 2 days for a fast test, then book all 3 candidate days.
    monkeypatch.setattr(cps, "MAX_ROLL_DAYS", 2)
    rows = []
    base_date = date(2026, 5, 21)
    for offset in range(3):  # tomorrow, +1, +2 all booked
        d = base_date.fromordinal(base_date.toordinal() + offset)
        for utc_time in ("21:25:00Z", "22:35:00Z"):
            rows.append({
                "uploaded_at": "2026-05-20T00:00:00+00:00",
                "topic_id": f"2026-05-19_{offset:03d}",
                "video_id": f"vid{offset}{utc_time[:2]}",
                "url": "https://www.youtube.com/",
                "privacy": _scheduled_cell(f"{d.isoformat()}T{utc_time}"),
                "title": f"booked {offset}",
            })
    _write_log(log, rows)
    exit_code, payload = cps.compute_slots(
        explicit_date=None,
        slots=SLOTS_DEFAULT,
        log_path=log,
        today=date(2026, 5, 20),
    )
    assert exit_code == 2
    assert payload["error"] == "no_free_day_within_cap"


# --- Dual-track de-confound (assign_track_slots + --dual-track) ----------------


def test_assign_track_slots_is_deterministic():
    """Same date -> identical assignment (idempotent re-runs)."""
    d = date(2026, 7, 1)
    assert cps.assign_track_slots(d) == cps.assign_track_slots(d)


def test_assign_track_slots_covers_both_tracks():
    """The two slots always carry the two distinct tracks."""
    a = cps.assign_track_slots(date(2026, 7, 1))
    assert a["slot1"] != a["slot2"]
    assert {a["slot1"], a["slot2"]} == set(cps.TRACKS)


def test_assign_track_slots_varies_by_date():
    """Both orderings occur across a short window (track <-> slot is not fixed)."""
    swaps = {
        cps.assign_track_slots(date(2026, 1, 1) + timedelta(days=i))["swapped"]
        for i in range(20)
    }
    assert swaps == {True, False}


def test_assign_track_slots_roughly_balanced():
    """Over a year, each track lands in each slot ~50% -> slot orthogonal to track."""
    n = 366
    swapped = sum(
        cps.assign_track_slots(date(2026, 1, 1) + timedelta(days=i))["swapped"]
        for i in range(n)
    )
    frac = swapped / n
    assert 0.4 <= frac <= 0.6, f"swap fraction {frac:.2f} not ~balanced"


def test_main_dual_track_includes_assignment(tmp_path, capsys):
    """--dual-track adds a well-formed track_assignment to the success envelope."""
    log = tmp_path / "upload_log.csv"  # absent -> no collisions
    rc = cps.main(["--dual-track", "--date", "2026-07-01", "--upload-log", str(log)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ta = payload["track_assignment"]
    assert set(ta) == {"slot1", "slot2", "swapped", "seed"}
    assert {ta["slot1"], ta["slot2"]} == set(cps.TRACKS)


def test_main_without_dual_track_omits_assignment(tmp_path, capsys):
    """Default (no --dual-track) envelope is unchanged: no track_assignment key."""
    log = tmp_path / "upload_log.csv"
    rc = cps.main(["--date", "2026-07-01", "--upload-log", str(log)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "track_assignment" not in payload
