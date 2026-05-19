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

from pipeline import load_config, setup_logging

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

def pull_hacker_news(*, scan_top: int = 60, max_keep: int = 25) -> list[TrendCandidate]:
    """Scan HN top-story IDs, keep ones whose title matches a niche keyword."""
    log.info("hacker news top (scan_top=%d, keep<=%d)", scan_top, max_keep)
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
            (kw for kw in NICHE_KEYWORDS
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
# Orchestration
# -----------------------------------------------------------------------------

def pull_all(out_dir: Path, *, dry_run: bool = False) -> tuple[Path | None, list[TrendCandidate]]:
    """Run every wired source in turn. Failures are logged and skipped.

    Returns (artifact_path or None if dry_run, candidates list).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[TrendCandidate] = []

    def _safe(name: str, fn: Callable[[], list[TrendCandidate]]) -> None:
        try:
            results = fn()
            log.info("%s: %d candidates", name, len(results))
            candidates.extend(results)
        except NotImplementedError as e:
            log.info("%s: SKIPPED (stub) — %s", name, e.args[0] if e.args else "not implemented")
        except Exception as e:
            log.warning("%s: FAILED — %s", name, e)

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

    if not candidates:
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
    out_path = out_dir / f"trends_{today}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %s (%d candidates, %d dupes dropped)",
             out_path, len(deduped), len(candidates) - len(deduped))
    return out_path, deduped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily trend pull for ShadowVerse")
    parser.add_argument("--dry-run", action="store_true", help="Pull but don't write the artifact")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    run_id = "trend_" + datetime.now().strftime("%Y%m%dT%H%M%S")
    setup_logging(config, run_id)

    channel_root = Path(config["paths"]["channel_root"])
    out_dir = channel_root / "01_research"

    pull_all(out_dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
