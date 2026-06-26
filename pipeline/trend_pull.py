"""Daily trend ingestion for ShadowVerse.

Pulls candidate topics from sources that don't require account credentials, so it
runs unattended:

  - Vendor changelogs (HTML): Cursor
  - GitHub releases (public API): anthropic/claude-code, astral-sh/uv, astral-sh/ruff,
    modelcontextprotocol/servers
  - Hacker News top stories (Firebase API), filtered by niche-keyword match

Sources that need credentials are kept as stubs in this file so they can be flipped
on without restructuring (Phase 2/3):
  - Reddit (praw): set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET in .env
  - YouTube Data API: set YOUTUBE_API_KEY in .env
  - Google Trends (pytrends): no key, but unofficial; activate when needed

Output: one JSON artifact per day at
  channels/<channel>/01_research/trends_<YYYY-MM-DD>.json

Each candidate is a TrendCandidate dataclass serialized as a dict. The downstream
idea-generation stage (prompts/02_idea_generation.md) loads this artifact and asks
the LLM to score N ranked angles against the niche style guide.

CLI:
  python trend_pull.py                 # pull all wired sources, write today's artifact
  python trend_pull.py --dry-run       # pull but don't write the artifact
  python trend_pull.py --config <path> # use a non-default config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from pipeline import load_config, setup_logging, validate_config

log = logging.getLogger("trend_pull")

USER_AGENT = "ShadowVerse-trend-pull/0.2 (faceless dev-AI Shorts research; contact official.shadowverse@gmail.com)"
HTTP_TIMEOUT_S = 12

# Niche keywords — used to filter broad sources (HN top stories) down to dev-AI relevance.
# Keep lowercased; matching is substring-on-title.
NICHE_KEYWORDS: tuple[str, ...] = (
    "cursor", "claude code", "claude", "anthropic", "openai", "chatgpt", "copilot", "gemini",
    "uv", "ruff", "astral",
    "mcp", "model context protocol",
    "ollama", "llama.cpp", "lm studio", "vllm",
    "warp", "raycast", "zed", "helix",
    "cline", "aider", "windsurf", "codeium", "continue.dev",
    "ai coding", "ai assistant", "ai ide", "ai agent",
    "prompt engineering", "tool use", "agent framework",
    "rag", "embedding", "vector db", "fine-tune", "fine tuning",
)

# General-tech track keywords (dual-track slot, 2026-06-21) — used to filter HN
# top stories down to broad consumer-tech relevance (Apple/Meta/Microsoft/Tesla/
# Neuralink/gadgets) for the general-tech track. Same matcher as NICHE_KEYWORDS
# (word-bounded substring-on-title, lowercased). Deliberately broad: the operator
# chose full general-tech scope, and AI-adjacent consumer tech (Apple Intelligence,
# Copilot) is welcome here too. The crazy-tech-story LEAD genre is sourced via the
# news-RSS queue + tavily-in-loop (start.md), NOT HN keyword matching.
GENERAL_TECH_KEYWORDS: tuple[str, ...] = (
    # Apple
    "apple", "iphone", "ipad", "macbook", "mac", "ios", "ipados", "macos",
    "siri", "vision pro", "airpods", "apple watch", "apple intelligence",
    # Google / Android
    "google", "pixel", "android", "chromebook",
    # Microsoft / Windows
    "microsoft", "windows", "surface", "xbox", "copilot",
    # Meta
    "meta", "quest", "oculus", "ray-ban", "instagram", "whatsapp", "threads",
    # Tesla / Musk / Neuralink / SpaceX
    "tesla", "elon", "musk", "neuralink", "spacex", "starlink", "cybertruck", "robotaxi",
    # Other hardware makers / silicon
    "samsung", "galaxy", "sony", "playstation", "nintendo", "nvidia", "amd",
    "intel", "qualcomm", "snapdragon",
    # Consumer-tech platforms / giants
    "amazon", "alexa", "echo", "ring", "netflix", "spotify", "tiktok", "uber",
    # Generic consumer-tech signals
    "smartphone", "smartwatch", "smart glasses", "ar glasses", "vr headset",
    "wearable", "gadget", "robot", "drone", "self-driving", "electric car",
    "chip", "processor", "battery", "foldable",
)

# GitHub repos to track for new releases. (repo, niche-tag) — niche-tag flows into
# TrendCandidate.tag and is what the idea-gen prompt anchors topics on.
TRACKED_GITHUB_REPOS: tuple[tuple[str, str], ...] = (
    ("anthropics/claude-code",          "claude"),
    ("astral-sh/uv",                    "uv"),
    ("astral-sh/ruff",                  "ruff"),
    ("modelcontextprotocol/servers",    "mcp"),
    ("ollama/ollama",                   "ollama"),
    ("ggerganov/llama.cpp",             "llama.cpp"),
    ("Aider-AI/aider",                  "aider"),
    ("cline/cline",                     "cline"),
)


@dataclass
class TrendCandidate:
    """One topic seed surfaced by a public source.

    The downstream idea-generation prompt reads a list of these and produces ranked
    angles. Keep field names stable — the prompt template references them.
    """
    source: str                       # e.g., "github:anthropics/claude-code", "hn", "cursor:changelog"
    url: str
    title: str
    summary: str                      # 1–3 sentence excerpt or release-notes snippet
    surfaced_at: str                  # ISO-8601 timestamp when this script saw it
    published_at: str | None          # ISO-8601 timestamp from the source, when known
    score: float | None               # source-specific signal (HN points, etc.); None if N/A
    tag: str                          # tool/topic anchor (lowercase): "cursor" | "claude" | "uv" | ...
    extra: dict = field(default_factory=dict)


def _http_get(url: str, *, accept: str = "text/html,application/json") -> requests.Response:
    """Single-retry HTTP GET with our identifying user agent. Raises on non-2xx."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return resp


# -----------------------------------------------------------------------------
# GitHub releases (public API, no auth required for read)
# -----------------------------------------------------------------------------

def pull_github_releases(repo: str, tag: str, *, limit: int = 5) -> list[TrendCandidate]:
    """Recent published (non-draft, non-prerelease) releases for a public GitHub repo."""
    api = f"https://api.github.com/repos/{repo}/releases?per_page={limit}"
    log.info("github releases: %s", repo)
    resp = _http_get(api, accept="application/vnd.github+json")
    out: list[TrendCandidate] = []
    for r in resp.json():
        if r.get("draft") or r.get("prerelease"):
            continue
        body = (r.get("body") or "").strip()
        # GitHub release bodies can be massive; keep first ~600 chars for the LLM
        summary = body[:600] + ("..." if len(body) > 600 else "")
        title = f"{repo} {r.get('tag_name', '')} — {(r.get('name') or '').strip() or 'release'}"
        out.append(TrendCandidate(
            source=f"github:{repo}",
            url=r["html_url"],
            title=title.strip(" —"),
            summary=summary,
            surfaced_at=datetime.now(timezone.utc).isoformat(),
            published_at=r.get("published_at"),
            score=None,
            tag=tag,
            extra={"github_id": r.get("id"), "tag_name": r.get("tag_name")},
        ))
    return out


# -----------------------------------------------------------------------------
# Hacker News top stories (Firebase API, no auth)
# -----------------------------------------------------------------------------

def pull_hacker_news(
    *,
    scan_top: int = 60,
    max_keep: int = 25,
    keywords: tuple[str, ...] = NICHE_KEYWORDS,
) -> list[TrendCandidate]:
    """Scan HN top-story IDs, keep ones whose title matches a `keywords` entry.

    `keywords` defaults to the AI-vendor NICHE_KEYWORDS; the general-tech track
    passes GENERAL_TECH_KEYWORDS instead.
    """
    log.info("hacker news top (scan_top=%d, keep<=%d, keywords=%d)", scan_top, max_keep, len(keywords))
    top_ids = _http_get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        accept="application/json",
    ).json()
    out: list[TrendCandidate] = []
    for sid in top_ids[:scan_top]:
        try:
            story = _http_get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                accept="application/json",
            ).json()
        except requests.RequestException as e:
            log.debug("hn item %d fetch failed: %s", sid, e)
            continue
        if not story or story.get("deleted") or story.get("dead"):
            continue
        title = (story.get("title") or "").strip()
        if not title:
            continue
        title_lower = title.lower()
        # Word-boundary match so "zed" doesn't match inside "authoriZED",
        # "uv" doesn't match inside "discover", "cline" doesn't match inside "decline", etc.
        matched = next(
            (kw for kw in keywords
             if re.search(rf"(?:^|\W){re.escape(kw)}(?:\W|$)", title_lower)),
            None,
        )
        if not matched:
            continue
        published_at = (
            datetime.fromtimestamp(story["time"], tz=timezone.utc).isoformat()
            if story.get("time")
            else None
        )
        out.append(TrendCandidate(
            source="hn",
            url=story.get("url") or f"https://news.ycombinator.com/item?id={sid}",
            title=title,
            summary=(story.get("text") or "")[:500],
            surfaced_at=datetime.now(timezone.utc).isoformat(),
            published_at=published_at,
            score=float(story.get("score") or 0),
            # Map matched keyword onto a coarse tag bucket
            tag=_keyword_to_tag(matched),
            extra={"hn_id": sid, "by": story.get("by"), "descendants": story.get("descendants")},
        ))
        if len(out) >= max_keep:
            break
    return out


def _keyword_to_tag(keyword: str) -> str:
    """Coarsen niche-keyword matches into a small tag vocabulary the idea-gen prompt
    knows how to anchor topics on. Falls back to the keyword itself.
    """
    kw = keyword.lower()
    mapping = {
        "claude code": "claude",
        "model context protocol": "mcp",
        "lm studio": "lm-studio",
        "ai coding": "ai-coding",
        "ai assistant": "ai-coding",
        "ai ide": "ai-coding",
        "ai agent": "agents",
        "agent framework": "agents",
        "tool use": "agents",
        "prompt engineering": "prompts",
        "fine-tune": "training",
        "fine tuning": "training",
        "vector db": "rag",
    }
    return mapping.get(kw, kw.replace(" ", "-").replace(".", ""))


# -----------------------------------------------------------------------------
# Cursor changelog (HTML scrape, regex-based — no bs4 dep)
# -----------------------------------------------------------------------------

# Pattern: heading-like lines followed by a date marker. Cursor's changelog page
# is server-rendered well enough that release titles appear in the raw HTML.
# This is intentionally tolerant: if Cursor restructures the page, we drop a
# warning rather than crash the daily pull.
_CURSOR_HEADING_RE = re.compile(
    r"<h[12][^>]*>(?P<title>[^<]{4,200})</h[12]>",
    re.IGNORECASE,
)


def pull_cursor_changelog(*, limit: int = 6) -> list[TrendCandidate]:
    """Scrape recent entries from cursor.com/changelog with a simple heading regex.

    Best-effort. If the page becomes JS-only, this returns an empty list and logs
    a warning — operator should switch to a different source for Cursor news.
    """
    url = "https://cursor.com/changelog"
    log.info("cursor changelog scrape")
    resp = _http_get(url)
    headings = _CURSOR_HEADING_RE.findall(resp.text)
    if not headings:
        log.warning("cursor changelog: no headings matched (page may be JS-only now)")
        return []
    out: list[TrendCandidate] = []
    seen: set[str] = set()
    for raw_title in headings:
        title = raw_title.strip()
        if not title or title.lower() in seen:
            continue
        # Skip nav/footer-like headings
        if title.lower() in ("changelog", "cursor", "menu", "navigation"):
            continue
        seen.add(title.lower())
        out.append(TrendCandidate(
            source="cursor:changelog",
            url=url,
            title=title,
            summary="",  # we don't try to extract body text without bs4; LLM can webfetch the page
            surfaced_at=datetime.now(timezone.utc).isoformat(),
            published_at=None,
            score=None,
            tag="cursor",
        ))
        if len(out) >= limit:
            break
    return out


# -----------------------------------------------------------------------------
# Stubs preserved from Phase-1 design — wire when credentials/decisions arrive
# -----------------------------------------------------------------------------

def pull_reddit_top(
    client_id: str,
    client_secret: str,
    user_agent: str,
    subreddits: list[str],
    time_window: str = "day",
) -> list[TrendCandidate]:
    """Top posts in dev-AI subreddits for the given window (hour/day/week/...).

    STUB — operator hasn't created a Reddit script-app yet. To activate:
      1. https://www.reddit.com/prefs/apps → create app, type=script
      2. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT in `.env`
      3. Implement here:  praw.Reddit(...).subreddit(sub).top(time_filter=time_window, limit=25)
      4. Map each post → TrendCandidate(source=f"reddit:{sub}", tag=<keyword-match>, ...)
    """
    raise NotImplementedError(
        "Reddit pull requires REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET in .env. "
        "Operator: create a script-type app at https://www.reddit.com/prefs/apps"
    )


def pull_youtube_trending(api_key: str, search_terms: list[str], days_back: int = 7) -> list[TrendCandidate]:
    """Top-performing videos for each search term in the last N days. STUB (Phase 3)."""
    raise NotImplementedError(
        "YouTube Data API: build('youtube', 'v3', developerKey=api_key); "
        "search.list(q=term, order='viewCount', publishedAfter=ISO8601, maxResults=10). "
        "Daily quota is 10,000 units; search.list costs 100 per call."
    )


def pull_google_trends(terms: list[str], geo: str = "") -> list[TrendCandidate]:
    """Search-interest score over the last 7 days for each term. STUB (unofficial)."""
    raise NotImplementedError(
        "pytrends.request.TrendReq(); pytrends.build_payload(kw_list=terms, "
        "timeframe='now 7-d', geo=geo); pytrends.interest_over_time(). "
        "pytrends is unofficial — wrap activation in try/except and treat as soft signal."
    )


# -----------------------------------------------------------------------------
# News-RSS queue ingestion (reads tools/news_rss_poller.py's queue file)
# -----------------------------------------------------------------------------

DEFAULT_NEWS_QUEUE_NAME = "news_drops_queue.json"


def pull_news_rss_queue(
    queue_path: Path,
    *,
    track: str = "ai-vendor",
    max_keep: int = 25,
) -> list[TrendCandidate]:
    """Surface queued news-RSS drops for `track` as TrendCandidates.

    Reads the queue file written by tools/news_rss_poller.py (NEVER fetches the
    network here — the poller owns fetching, on its own scheduled cadence). Filters
    to the requested track; drops written before the track field existed default to
    'ai-vendor'. A missing/unreadable/malformed queue yields [] (fail-soft, mirrors
    the rest of trend_pull's per-source isolation).
    """
    if not queue_path.exists():
        log.info("news-rss queue: %s absent — 0 candidates", queue_path)
        return []
    try:
        raw = json.loads(queue_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("news-rss queue unreadable (%s) — 0 candidates", e)
        return []
    if not isinstance(raw, list):
        log.warning("news-rss queue not a list — 0 candidates")
        return []
    out: list[TrendCandidate] = []
    for drop in raw:
        if not isinstance(drop, dict):
            continue
        if str(drop.get("track", "ai-vendor")) != track:
            continue
        url = str(drop.get("url", "")).strip()
        title = str(drop.get("title", "")).strip()
        if not url or not title:
            continue
        out.append(TrendCandidate(
            source=f"news_rss:{drop.get('feed', 'unknown')}",
            url=url,
            title=title,
            summary=str(drop.get("summary", ""))[:500],
            surfaced_at=datetime.now(timezone.utc).isoformat(),
            published_at=drop.get("published_at"),
            score=None,
            tag=str(drop.get("feed", "news")),
            extra={"news_drop_id": drop.get("id"), "track": track},
        ))
        if len(out) >= max_keep:
            break
    log.info("news-rss queue: %d candidates for track=%s", len(out), track)
    return out


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def pull_all(
    out_dir: Path,
    *,
    dry_run: bool = False,
    track: str = "ai-vendor",
) -> tuple[Path | None, list[TrendCandidate]]:
    """Run the wired sources for `track` in turn. Failures are logged and skipped.

    track='ai-vendor' (default): GitHub dev-AI releases + Cursor changelog + HN
    (AI niche keywords) + Reddit stub — byte-identical to the pre-dual-track flow.
    track='general-tech': HN (general-tech keywords) + the news-RSS queue
    (general-tech drops). The general-tech artifact is written to a track-suffixed
    filename so a same-day ai-vendor pull is never clobbered.

    Returns (artifact_path or None if dry_run, candidates list).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[TrendCandidate] = []
    # Track real (non-stub) sources we actually attempted vs the subset that
    # raised a hard failure, so we can distinguish "every source failed"
    # (degraded — operator must act) from "sources ran but matched nothing"
    # (benign empty day). NotImplementedError stubs are SKIPs, not attempts.
    attempted: list[str] = []
    failed: list[str] = []

    def _safe(name: str, fn: Callable[[], list[TrendCandidate]]) -> None:
        try:
            results = fn()
        except NotImplementedError as e:
            log.info("%s: SKIPPED (stub) — %s", name, e.args[0] if e.args else "not implemented")
            return
        except Exception as e:
            attempted.append(name)
            failed.append(name)
            log.warning("%s: FAILED — %s", name, e)
            return
        attempted.append(name)
        log.info("%s: %d candidates", name, len(results))
        candidates.extend(results)

    if track == "ai-vendor":
        # GitHub releases
        for repo, tag in TRACKED_GITHUB_REPOS:
            _safe(f"github:{repo}", lambda r=repo, t=tag: pull_github_releases(r, t))

        # Vendor changelogs
        _safe("cursor:changelog", pull_cursor_changelog)

        # Hacker News
        _safe("hn", pull_hacker_news)

        # Stubs — these will log SKIPPED until wired
        reddit_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        reddit_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
        if reddit_id and reddit_secret:
            ua = os.environ.get("REDDIT_USER_AGENT", USER_AGENT)
            # When wired, the body of pull_reddit_top will replace the stub
            _safe("reddit", lambda: pull_reddit_top(
                reddit_id, reddit_secret, ua,
                ["LocalLLaMA", "MachineLearning", "ChatGPTCoding", "cursor", "ClaudeAI", "programming"],
            ))
        else:
            log.info("reddit: SKIPPED (no REDDIT_CLIENT_ID/SECRET in env)")
    elif track == "general-tech":
        # General-tech track: broad consumer-tech HN scan + the news-RSS queue
        # (the dev-AI GitHub repos and Cursor changelog are off-topic here).
        _safe("hn", lambda: pull_hacker_news(keywords=GENERAL_TECH_KEYWORDS))
        _safe("news_rss", lambda: pull_news_rss_queue(
            out_dir / DEFAULT_NEWS_QUEUE_NAME, track="general-tech",
        ))
    else:
        raise ValueError(
            f"unknown track {track!r}; expected 'ai-vendor' or 'general-tech'"
        )

    if attempted and len(failed) == len(attempted):
        # Every real source raised — the pull is fully degraded and the artifact
        # will be empty for an infrastructure reason, not a quiet news day.
        # Escalate to ERROR so an unattended run surfaces it loudly; still write
        # the artifact below (downstream tolerates empty).
        log.error(
            "trend_pull: ALL %d sources failed (%s) — artifact will be empty/degraded; check network",
            len(attempted),
            ", ".join(failed),
        )
    elif not candidates:
        log.warning("trend_pull produced 0 candidates — check network / source coverage")

    # De-duplicate on URL
    seen: set[str] = set()
    deduped: list[TrendCandidate] = []
    for c in candidates:
        if c.url in seen:
            continue
        seen.add(c.url)
        deduped.append(c)

    payload = {
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(deduped),
        "duplicates_dropped": len(candidates) - len(deduped),
        "candidates": [asdict(c) for c in deduped],
    }

    if dry_run:
        log.info("dry-run: %d candidates (not writing)", len(deduped))
        return None, deduped

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # ai-vendor keeps the historical trends_<date>.json name (back-compat); other
    # tracks are suffixed so a same-day pull of one track never clobbers the other.
    name = f"trends_{today}.json" if track == "ai-vendor" else f"trends_{track}_{today}.json"
    out_path = out_dir / name
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %s (%d candidates, %d dupes dropped)",
             out_path, len(deduped), len(candidates) - len(deduped))
    return out_path, deduped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily trend pull for ShadowVerse")
    parser.add_argument("--dry-run", action="store_true", help="Pull but don't write the artifact")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--track",
        default="ai-vendor",
        choices=["ai-vendor", "general-tech"],
        help="Topic track to pull. 'general-tech' uses consumer-tech HN keywords + the news-RSS queue.",
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    validate_config(config)  # M4: fail fast on missing keys before any stage runs
    run_id = "trend_" + datetime.now().strftime("%Y%m%dT%H%M%S")
    setup_logging(config, run_id)

    channel_root = Path(config["paths"]["channel_root"])
    out_dir = channel_root / "01_research"

    pull_all(out_dir, dry_run=args.dry_run, track=args.track)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
