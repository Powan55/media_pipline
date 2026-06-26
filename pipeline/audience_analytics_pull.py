"""Audience-aware analytics pull (demographics / geography / device / traffic).

`analytics_pull.py` pulls PER-VIDEO performance (views, retention, hold, shorts
share). It does NOT pull the channel-level AUDIENCE dimensions the deep-dive needs
— age/gender, geography, device, subscribed-vs-non-subscribed watch share, and
the traffic-source breakdown. This tool fills that gap via the YouTube Analytics
API v2 channel reports (ids=channel==MINE), and writes a dated Markdown report
(+ optional JSON) under ``01_research/_audience/``.

What it CANNOT pull: the Studio "When your viewers are on YouTube" hour-by-day
heatmap is a Studio-proprietary surface with no public Analytics API equivalent —
that one datum still requires a Studio read (see the sv-deep-dive skill). Every
other Audience-tab card is reproduced here.

Auth: reuses the OAuth token minted by ``tools/youtube_oauth_init.py`` (same
SCOPES / token path as analytics_pull). Self-contained — deliberately does NOT
import ``pipeline`` (heavy whisper/ffmpeg/google deps) so a broken pipeline never
blocks an audience pull, mirroring the learning module's precedent.

Footguns (see memory): the Norton "SSL scanning" MITM re-signs all HTTPS and
breaks these calls with CERTIFICATE_VERIFY_FAILED (fix = append the Norton root to
the venv certifi/cacert.pem); and a Testing-mode OAuth app revokes refresh tokens
~weekly (fix = re-run youtube_oauth_init.py --force, or flip the app to Production).

Usage:
    python audience_analytics_pull.py                 # last 28 days (Studio default)
    python audience_analytics_pull.py --days 90
    python audience_analytics_pull.py --start 2026-05-26 --end 2026-06-22
    python audience_analytics_pull.py --json          # also write the raw JSON
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from tools.oauth_token_helpers import (
    _FIX_COMMAND_POINTER,
    log_token_expiry_health,
    refresh_with_translation,
)

log = logging.getLogger("audience_analytics_pull")

# Mirror analytics_pull.py — same token, same scopes.
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN_PATH = Path(__file__).resolve().parent / "credentials" / "token.json"

YT_API_NUM_RETRIES = 3
YT_API_TIMEOUT_S = 30

DEFAULT_OUT_DIR = Path(
    r"C:\ContentOps\channels\ShadowVerse\01_research\_audience"
)
DEFAULT_DAYS = 28
TOP_GEO = 10


def _timeout_http(creds: Credentials) -> AuthorizedHttp:
    """Authorized httplib2 transport with a wall-clock socket timeout (mirrors
    analytics_pull) so a stalled connection can't hang an unattended run."""
    return AuthorizedHttp(creds, http=httplib2.Http(timeout=YT_API_TIMEOUT_S))


def _load_credentials() -> Credentials:
    """Load the OAuth token saved by tools/youtube_oauth_init.py (read-only here).

    Same contract as analytics_pull._load_credentials: surface expiry health,
    refresh if expired, translate RefreshError into an actionable message, and
    fail loud (this tool never runs the interactive consent flow itself).
    """
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"OAuth token missing at {TOKEN_PATH}. Run "
            f"`python tools/youtube_oauth_init.py` to authorize, then re-run."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    log_token_expiry_health(creds, logger=log)
    if not creds.has_scopes(SCOPES):
        raise RuntimeError(
            "OAuth token is missing one or more required scopes "
            f"({', '.join(SCOPES)}). {_FIX_COMMAND_POINTER}"
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        refresh_with_translation(creds, token_path=TOKEN_PATH, logger=log)
        return creds
    raise RuntimeError(
        f"OAuth token at {TOKEN_PATH} is invalid and cannot refresh. "
        f"Run `python tools/youtube_oauth_init.py --force` to re-authorize."
    )


def _query(yt_an, *, metrics: str, dimensions: str, start: str, end: str,
           sort: str | None = None, max_results: int | None = None) -> list[list]:
    """Run one channel-level Analytics report. Fail-soft: returns [] on error so
    one failing report never kills the rest of the pull."""
    kwargs = dict(
        ids="channel==MINE", startDate=start, endDate=end,
        metrics=metrics, dimensions=dimensions,
    )
    if sort:
        kwargs["sort"] = sort
    if max_results:
        kwargs["maxResults"] = max_results
    try:
        resp = yt_an.reports().query(**kwargs).execute(num_retries=YT_API_NUM_RETRIES)
    except Exception as e:  # noqa: BLE001 — one report failing must not kill the pull
        log.warning("audience report failed (metrics=%s dims=%s): %s", metrics, dimensions, e)
        return []
    return resp.get("rows") or []


def pull_audience(creds: Credentials, start: str, end: str) -> dict:
    """Pull every API-available Audience dimension for [start, end] (ISO dates)."""
    yt_an = build("youtubeAnalytics", "v2", http=_timeout_http(creds),
                  cache_discovery=False)

    # 1) Age + gender — viewerPercentage over (ageGroup, gender).
    age_gender = _query(yt_an, metrics="viewerPercentage",
                        dimensions="ageGroup,gender", start=start, end=end)
    # 2) Geography — ALL countries (so shares normalize against the true total,
    # matching Studio; render slices to the top N). Capping here would inflate
    # each share against the top-N sum instead of the channel total.
    geo = _query(yt_an, metrics="views", dimensions="country",
                 start=start, end=end, sort="-views")
    # 3) Device type — by WATCH TIME, to match Studio's "Watch time (hours)" card.
    device = _query(yt_an, metrics="estimatedMinutesWatched", dimensions="deviceType",
                    start=start, end=end, sort="-estimatedMinutesWatched")
    # 4) Subscribed vs not — watch-time share.
    subbed = _query(yt_an, metrics="estimatedMinutesWatched",
                    dimensions="subscribedStatus", start=start, end=end)
    # 5) Traffic source — views by insightTrafficSourceType.
    traffic = _query(yt_an, metrics="views", dimensions="insightTrafficSourceType",
                     start=start, end=end, sort="-views")

    return {
        "start": start, "end": end,
        "age_gender": age_gender, "geography": geo, "device": device,
        "subscribed": subbed, "traffic": traffic,
    }


def _pct_table(rows: list[list], label_idxs: list[int], value_idx: int,
               *, value_is_share: bool = False) -> list[tuple[str, float]]:
    """Collapse rows to (label, value), summing where labels repeat. When
    value_is_share is False, convert the raw values to a % of their total."""
    agg: dict[str, float] = {}
    for r in rows:
        if len(r) <= value_idx:
            continue
        label = " / ".join(str(r[i]) for i in label_idxs)
        try:
            val = float(r[value_idx])
        except (TypeError, ValueError):
            continue
        agg[label] = agg.get(label, 0.0) + val
    items = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    if value_is_share:
        return items
    total = sum(v for _, v in items) or 1.0
    return [(k, 100.0 * v / total) for k, v in items]


def render_report(data: dict) -> str:
    L: list[str] = []
    L.append(f"# ShadowVerse audience report - {data['start']} -> {data['end']}")
    L.append("")
    L.append("> API-pulled (YouTube Analytics v2). NOTE: the Studio "
             "\"When your viewers are on YouTube\" heatmap has no API equivalent — "
             "read it from Studio (see the sv-deep-dive skill).")
    L.append("")

    # Gender (sum viewerPercentage across age within each gender).
    L.append("## Gender (share of views)")
    L.append("")
    gender = _pct_table(data["age_gender"], [1], 2, value_is_share=True)
    # viewerPercentage already sums to 100 across all (age,gender) cells; collapse to gender.
    gtotal = sum(v for _, v in gender) or 1.0
    for k, v in gender:
        L.append(f"- {k}: {100.0 * v / gtotal:.1f}%")
    L.append("")

    # Age (collapse across gender).
    L.append("## Age (share of views)")
    L.append("")
    age = _pct_table(data["age_gender"], [0], 2, value_is_share=True)
    atotal = sum(v for _, v in age) or 1.0
    for k, v in sorted(age, key=lambda kv: kv[0]):
        L.append(f"- {k}: {100.0 * v / atotal:.1f}%")
    L.append("")

    L.append(f"## Top geographies (share of GEO-ATTRIBUTED views, top {TOP_GEO})")
    L.append("")
    L.append("> Note: the API `country` dimension excludes views with no attributed "
             "country (~⅓ for Shorts), so these shares are of geo-attributed views. "
             "Studio normalizes against ALL views, so its US% reads lower (~0.6×). "
             "Ranking is identical; cross-check ranking, not the absolute %.")
    L.append("")
    for k, v in _pct_table(data["geography"], [0], 1)[:TOP_GEO]:
        L.append(f"- {k}: {v:.1f}%")
    L.append("")

    L.append("## Device type (share of watch time)")
    L.append("")
    for k, v in _pct_table(data["device"], [0], 1):
        L.append(f"- {k}: {v:.1f}%")
    L.append("")

    L.append("## Watch time: subscribed vs not (share)")
    L.append("")
    for k, v in _pct_table(data["subscribed"], [0], 1):
        L.append(f"- {k}: {v:.1f}%")
    L.append("")

    L.append("## Traffic sources (share of views)")
    L.append("")
    for k, v in _pct_table(data["traffic"], [0], 1):
        L.append(f"- {k}: {v:.1f}%")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pull channel-level audience analytics.")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Lookback window in days (default {DEFAULT_DAYS}, matches Studio).")
    p.add_argument("--start", default=None, help="Explicit ISO start date (YYYY-MM-DD).")
    p.add_argument("--end", default=None, help="Explicit ISO end date (YYYY-MM-DD).")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory.")
    p.add_argument("--json", action="store_true", help="Also write the raw JSON rows.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.start and args.end:
        start, end = args.start, args.end
    else:
        today = datetime.now(timezone.utc).date()
        end = today.isoformat()
        start = (today - timedelta(days=args.days)).isoformat()
    log.info("audience pull %s -> %s", start, end)

    creds = _load_credentials()
    data = pull_audience(creds, start, end)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.fromisoformat(end).isoformat()
    md_path = out_dir / f"audience_{stamp}.md"
    md_path.write_text(render_report(data), encoding="utf-8")
    log.info("wrote %s", md_path)
    print(render_report(data))

    if args.json:
        json_path = out_dir / f"audience_{stamp}.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("wrote %s", json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
