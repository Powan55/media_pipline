"""Weekly (or ad-hoc) analytics ingestion.

Pulls per-video metrics from YouTube into a CSV that feeds the weekly analytics
review prompt (prompts/08_weekly_analytics_review.md). TikTok and Instagram
branches still stubbed (Phase 4).

YouTube path:
- OAuth-authenticated. Run `python tools/youtube_oauth_init.py` once to consent;
  subsequent runs read the cached token transparently.
- Reads the channel's "uploads" playlist to enumerate videos (cheap, avoids the
  expensive search.list quota).
- Per-video stats via videos.list (batched, 1 quota unit per 50 videos).
- Per-video analytics via youtubeAnalytics.reports.query() — averages over
  lifetime-to-date for each video. One Analytics quota unit per video; default
  daily Analytics quota is 200, so up to 200 videos/day before tuning is needed.

Schedule via Windows Task Scheduler:
    python C:\\ContentOps\\_pipeline\\analytics_pull.py --once

3-second retention coverage:
- `audienceWatchRatio` is queried per-video with the `elapsedVideoTimeRatio`
  dimension. The 3s-hold % is the bucket closest to (3 / video_duration_sec).
  Some Shorts return non-uniform sampling — we pick the closest available
  bucket and tolerate empty responses (returns None in that case).
- Traffic source breakdown uses `insightTrafficSourceType` dimension; we sum
  the views attributed to the Shorts feed (`SHORTS`) and divide by total
  views from the same query window. None if Analytics has no data yet.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from pipeline import load_config, setup_logging
from tools.oauth_token_helpers import (
    log_token_expiry_health,
    refresh_with_translation,
)

log = logging.getLogger("analytics_pull")

# Must mirror tools/youtube_oauth_init.py — this script reads tokens minted by
# that script. If scopes change there, change them here too AND re-run the init
# script with --force so the cached token gets fresh consent.
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN_PATH = Path(__file__).resolve().parent / "credentials" / "token.json"


@dataclass
class VideoMetrics:
    """Per-video metrics row. One per (video, pull_date) tuple in the CSV.

    `saves` is omitted because YouTube has no equivalent surface metric.

    New columns (additive, append-only — never reordered):
    - `hold_at_3s` is the audienceWatchRatio at elapsedVideoTimeRatio closest
      to (3 / video_duration_sec). Float in [0, 1] or None if Analytics has
      no data yet (sub-50-view videos often return empty rows).
    - `traffic_source_shorts_pct` is the share of views from the Shorts feed
      (insightTrafficSourceType == SHORTS) over total views in the same
      window. Float in [0, 1] or None if no data.
    """
    platform: str
    video_id: str
    title: str
    published_at: date
    views: int
    avg_view_pct: float          # Analytics API: averageViewPercentage (0-100)
    avg_view_duration_sec: float  # Analytics API: averageViewDuration (seconds)
    likes: int
    shares: int
    comments: int
    follower_delta: int          # Analytics API: subscribersGained (lifetime-to-date)
    hold_at_3s: float | None = None             # ratio in [0, 1] or None
    traffic_source_shorts_pct: float | None = None  # ratio in [0, 1] or None


def _load_credentials() -> Credentials:
    """Load the OAuth token saved by tools/youtube_oauth_init.py.

    Surfaces token expiry health on every load (INFO at >=24h, WARNING <24h,
    ERROR <6h). Refreshes if expired; translates `RefreshError` into an
    actionable `RuntimeError` pointing at the fix command. Errors loudly if
    missing or unrecoverable — this script does NOT run the consent flow
    itself (that's what the init script is for; keeps the recurring pull
    non-interactive).

    See `tools/oauth_token_helpers.py` for the shared helpers and audit
    finding H3 (WORKFLOW_AUDIT_2026-05-16) for context.
    """
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"OAuth token missing at {TOKEN_PATH}. Run "
            f"`python tools/youtube_oauth_init.py` to authorize, then re-run."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    log_token_expiry_health(creds, logger=log)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        refresh_with_translation(creds, token_path=TOKEN_PATH, logger=log)
        return creds
    raise RuntimeError(
        f"OAuth token at {TOKEN_PATH} is invalid and cannot refresh. "
        f"Run `python tools/youtube_oauth_init.py --force` to re-authorize."
    )


def _list_uploaded_videos(yt_data, channel_id: str, since: date) -> list[tuple[str, date]]:
    """Walk the channel's 'uploads' playlist; return (video_id, published_date)
    for items with published_date >= since.

    Note: the uploads playlist includes Private and Unlisted videos owned by
    the authenticated user — that's by design (we want analytics on everything
    we've published, including the gate-3-pending ruff video).
    """
    ch_resp = yt_data.channels().list(part="contentDetails", id=channel_id).execute()
    items = ch_resp.get("items") or []
    if not items:
        raise RuntimeError(f"channel id {channel_id} not found via Data API")
    uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    out: list[tuple[str, date]] = []
    page_token: str | None = None
    while True:
        resp = yt_data.playlistItems().list(
            playlistId=uploads_pl,
            part="contentDetails",
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            cd = item.get("contentDetails", {})
            vid = cd.get("videoId")
            ts = cd.get("videoPublishedAt") or ""
            if not vid or not ts:
                continue
            try:
                pub = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except ValueError:
                log.warning("skipping video %s: unparseable publishedAt %r", vid, ts)
                continue
            if pub >= since:
                out.append((vid, pub))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


_ISO8601_DURATION_RE = re.compile(
    r"^PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)


def _parse_iso8601_duration_seconds(iso: str) -> float | None:
    """Parse a YouTube Data API ISO-8601 duration (e.g. 'PT45S', 'PT1M3S')
    into seconds. Returns None if unparseable.
    """
    if not iso:
        return None
    m = _ISO8601_DURATION_RE.match(iso)
    if not m:
        return None
    h = int(m.group("h") or 0)
    mn = int(m.group("m") or 0)
    s = float(m.group("s") or 0.0)
    total = h * 3600 + mn * 60 + s
    return total if total > 0 else None


def _query_hold_at_3s(
    yt_an, video_id: str, start_iso: str, end_iso: str, duration_sec: float
) -> float | None:
    """Query audienceWatchRatio with elapsedVideoTimeRatio dimension and pick
    the bucket closest to (3 / duration_sec). Returns float in [0, 1] or None
    if Analytics has no data for this video yet.
    """
    if duration_sec <= 0:
        return None
    target_ratio = min(3.0 / duration_sec, 1.0)
    try:
        resp = yt_an.reports().query(
            ids="channel==MINE",
            startDate=start_iso,
            endDate=end_iso,
            metrics="audienceWatchRatio",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
        ).execute()
    except Exception as e:
        log.warning("audienceWatchRatio query failed for %s: %s", video_id, e)
        return None

    rows = resp.get("rows") or []
    if not rows:
        return None

    # rows look like [[elapsedRatio, watchRatio], ...] — pick the bucket whose
    # elapsedRatio is closest to target_ratio. Some Shorts return only a
    # handful of buckets so the closest may not be exactly at 3s.
    best: tuple[float, float] | None = None
    best_dist = float("inf")
    for row in rows:
        if len(row) < 2:
            continue
        try:
            elapsed = float(row[0])
            watch = float(row[1])
        except (TypeError, ValueError):
            continue
        dist = abs(elapsed - target_ratio)
        if dist < best_dist:
            best_dist = dist
            best = (elapsed, watch)
    if best is None:
        return None
    # clamp into [0, 1] defensively
    return max(0.0, min(1.0, best[1]))


def _query_traffic_source_shorts_pct(
    yt_an, video_id: str, start_iso: str, end_iso: str
) -> float | None:
    """Query views grouped by insightTrafficSourceType. Return the ratio of
    SHORTS-feed views over total views, in [0, 1]. None if no data.
    """
    try:
        resp = yt_an.reports().query(
            ids="channel==MINE",
            startDate=start_iso,
            endDate=end_iso,
            metrics="views",
            dimensions="insightTrafficSourceType",
            filters=f"video=={video_id}",
        ).execute()
    except Exception as e:
        log.warning("insightTrafficSourceType query failed for %s: %s", video_id, e)
        return None

    rows = resp.get("rows") or []
    if not rows:
        return None

    total = 0.0
    shorts = 0.0
    for row in rows:
        if len(row) < 2:
            continue
        try:
            source = str(row[0])
            views = float(row[1])
        except (TypeError, ValueError):
            continue
        total += views
        if source == "SHORTS":
            shorts += views
    if total <= 0:
        return None
    return max(0.0, min(1.0, shorts / total))


def pull_youtube_analytics(creds: Credentials, channel_id: str, since: date) -> list[VideoMetrics]:
    """Per-video stats for videos published since `since`.

    Uses Data API v3 (snippet+statistics) and Analytics API v2 (averages,
    shares, subscribers). One Analytics call per video; default daily quota
    is 200 → linear scale to ~200 videos/day before quota matters.
    """
    yt_data = build("youtube", "v3", credentials=creds, cache_discovery=False)
    yt_an = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)

    pairs = _list_uploaded_videos(yt_data, channel_id, since)
    if not pairs:
        log.info("no videos published since %s", since)
        return []
    log.info("found %d videos to analyze", len(pairs))

    video_ids = [vid for vid, _ in pairs]
    pub_by_id = dict(pairs)

    stats_resp = yt_data.videos().list(
        id=",".join(video_ids),
        part="snippet,statistics,contentDetails",
    ).execute()

    end_date = datetime.now(timezone.utc).date().isoformat()
    rows: list[VideoMetrics] = []

    for video in stats_resp.get("items", []):
        vid = video["id"]
        snippet = video["snippet"]
        stats = video.get("statistics", {})
        published = pub_by_id.get(vid) or datetime.fromisoformat(
            snippet["publishedAt"].replace("Z", "+00:00")
        ).date()

        try:
            an_resp = yt_an.reports().query(
                ids="channel==MINE",
                startDate=published.isoformat(),
                endDate=end_date,
                metrics=(
                    "views,likes,comments,shares,estimatedMinutesWatched,"
                    "averageViewDuration,averageViewPercentage,subscribersGained"
                ),
                filters=f"video=={vid}",
            ).execute()
        except Exception as e:
            log.warning("analytics query failed for video %s: %s", vid, e)
            an_resp = {}

        an_rows = an_resp.get("rows") or [[]]
        an_headers = [c.get("name") for c in an_resp.get("columnHeaders", [])]
        an_data = dict(zip(an_headers, an_rows[0])) if an_rows[0] else {}

        duration_iso = video.get("contentDetails", {}).get("duration", "")
        duration_sec = _parse_iso8601_duration_seconds(duration_iso) or 0.0
        start_iso = published.isoformat()

        hold_at_3s = (
            _query_hold_at_3s(yt_an, vid, start_iso, end_date, duration_sec)
            if duration_sec > 0 else None
        )
        traffic_source_shorts_pct = _query_traffic_source_shorts_pct(
            yt_an, vid, start_iso, end_date
        )

        rows.append(VideoMetrics(
            platform="youtube",
            video_id=vid,
            title=snippet["title"],
            published_at=published,
            views=int(stats.get("viewCount", 0)),
            avg_view_pct=float(an_data.get("averageViewPercentage") or 0.0),
            avg_view_duration_sec=float(an_data.get("averageViewDuration") or 0.0),
            likes=int(stats.get("likeCount", 0)),
            shares=int(an_data.get("shares") or 0),
            comments=int(stats.get("commentCount", 0)),
            follower_delta=int(an_data.get("subscribersGained") or 0),
            hold_at_3s=hold_at_3s,
            traffic_source_shorts_pct=traffic_source_shorts_pct,
        ))

    rows.sort(key=lambda r: r.published_at, reverse=True)
    return rows


def pull_tiktok_analytics(username: str, since: date) -> list[VideoMetrics]:
    """Per-video TikTok stats. Display API requires OAuth user-grant per account."""
    raise NotImplementedError(
        "Phase 4: TikTok Display API or Research API. "
        "Requires developer account + OAuth user grant. Verify regional availability."
    )


def pull_instagram_analytics(username: str, since: date) -> list[VideoMetrics]:
    """Per-Reels stats via the Instagram Graph API (Business/Creator account required)."""
    raise NotImplementedError(
        "Phase 4: Instagram Graph API via Facebook for Developers. "
        "Account must be Business/Creator and linked to a Facebook page."
    )


def merge_into_tracker(rows: list[VideoMetrics], output_path: Path) -> Path:
    """Append rows to the per-channel CSV. Writes header if the file is new/empty.

    Schema is APPEND-ONLY: existing columns must never be reordered or removed.
    Old rows written before a new column was added will read back with the
    new fields as empty strings — `read_existing_rows` handles that gracefully.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not output_path.exists() or output_path.stat().st_size == 0

    fields = [
        "pull_date", "platform", "video_id", "title", "published_at",
        "views", "avg_view_pct", "avg_view_duration_sec",
        "likes", "shares", "comments", "follower_delta",
        # Appended 2026-05-08 — additive, never reorder.
        "hold_at_3s", "traffic_source_shorts_pct",
    ]

    with output_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if is_new:
            w.writeheader()
        pull_date = date.today().isoformat()
        for r in rows:
            d = asdict(r)
            d["pull_date"] = pull_date
            d["published_at"] = r.published_at.isoformat()
            d["avg_view_pct"] = f"{r.avg_view_pct:.2f}"
            d["avg_view_duration_sec"] = f"{r.avg_view_duration_sec:.2f}"
            d["hold_at_3s"] = (
                f"{r.hold_at_3s:.4f}" if r.hold_at_3s is not None else ""
            )
            d["traffic_source_shorts_pct"] = (
                f"{r.traffic_source_shorts_pct:.4f}"
                if r.traffic_source_shorts_pct is not None else ""
            )
            w.writerow(d)
    return output_path


def read_existing_rows(output_path: Path) -> list[dict]:
    """Read the analytics CSV with backwards-compatible defaults for the
    additive `hold_at_3s` and `traffic_source_shorts_pct` columns. Old rows
    written before those columns existed will return them as empty strings.

    Used by tooling that diffs analytics over time. Never crashes on missing
    columns — that's the additive contract.
    """
    if not output_path.exists() or output_path.stat().st_size == 0:
        return []
    with output_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        out: list[dict] = []
        for row in reader:
            row.setdefault("hold_at_3s", "")
            row.setdefault("traffic_source_shorts_pct", "")
            out.append(row)
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull weekly analytics for ShadowVerse")
    parser.add_argument("--once", action="store_true", help="Run a single pull and exit")
    parser.add_argument("--days", type=int, default=7, help="Lookback window (default 7)")
    parser.add_argument("--all", action="store_true",
                        help="Pull lifetime stats (overrides --days)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    run_id = "analytics_" + datetime.now().strftime("%Y%m%dT%H%M%S")
    setup_logging(config, run_id)

    if args.all:
        since = date(2020, 1, 1)
    else:
        since = date.today() - timedelta(days=args.days)
    log.info("analytics pull since %s", since)

    yt_channel = config["analytics_pull"].get("youtube_channel_id", "")
    if not yt_channel or "<<TODO" in str(yt_channel):
        raise RuntimeError(
            "config.yaml analytics_pull.youtube_channel_id is unset. Run "
            "`python tools/youtube_oauth_init.py` and copy the channel id "
            "into config.yaml under analytics_pull.youtube_channel_id."
        )

    creds = _load_credentials()
    rows = pull_youtube_analytics(creds, yt_channel, since)

    output = Path(config["analytics_pull"]["output_path"])
    merge_into_tracker(rows, output)
    log.info("wrote %d rows to %s", len(rows), output)

    if rows:
        print()
        print(
            f"{'Title':<46}  {'Pub':<10}  {'Views':>6}  "
            f"{'Avg%':>5}  {'AvgSec':>6}  {'Hold3s':>6}  {'Sh%':>5}  "
            f"{'Likes':>5}  {'Cmts':>4}  {'Subs+':>5}"
        )
        print("-" * 122)
        for r in rows:
            t = (r.title[:44] + "..") if len(r.title) > 46 else r.title
            hold_str = f"{r.hold_at_3s * 100:>5.1f}%" if r.hold_at_3s is not None else "  --  "
            sh_str = (
                f"{r.traffic_source_shorts_pct * 100:>4.1f}%"
                if r.traffic_source_shorts_pct is not None else "  -- "
            )
            print(
                f"{t:<46}  {r.published_at}  {r.views:>6,}  "
                f"{r.avg_view_pct:>4.1f}%  {r.avg_view_duration_sec:>6.1f}  "
                f"{hold_str:>6}  {sh_str:>5}  "
                f"{r.likes:>5,}  {r.comments:>4,}  {r.follower_delta:>+5}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
