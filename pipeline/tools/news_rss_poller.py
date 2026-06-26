"""AI-vendor news RSS poller for ShadowVerse.

Polls public RSS/Atom feeds from the major consumer-AI vendors and appends new
entries to a JSON queue file. The manager session reads the queue when the
operator types ``start`` and surfaces new drops as candidate topics for the
12h-ship news cadence (vendor news → script → render → upload within ~12 hours).

Notification model: queue-file only. The poller never sends toasts, emails, or
Discord pings — manager-on-start is the agreed surface (per A5 task brief).

CLI:
  python tools/news_rss_poller.py --once
  python tools/news_rss_poller.py --once --feeds anthropic,openai
  python tools/news_rss_poller.py --once --queue C:/some/queue.json --seen C:/some/seen.json
  python tools/news_rss_poller.py --once --max-age-hours 168

The Windows scheduled task should invoke the ``--once`` form every 30 minutes.
See the team-A5 return for the recommended ``schtasks /create`` command.

Feed selection (verified live 2026-05-08 via curl + tavily search):
  - Anthropic: no native RSS exists on anthropic.com (operator-checked
    https://www.anthropic.com/news/rss.xml → 404). Falls back to the
    community-maintained Olshansk feed which scrapes claude.com/blog —
    this covers Anthropic's product announcements (the news that would
    drive a 12h-ship video).
  - OpenAI: native RSS at openai.com/news/rss.xml (revived September 2025
    after the 2023 site rebuild dropped the original openai.com/blog/rss.xml).
  - Google AI / DeepMind: native RSS at deepmind.google/blog/rss.xml.

If a feed URL ever 404s in production, the per-feed try/except logs WARNING and
the other feeds keep working — one bad feed never kills the run.

Engineering notes (per pipeline CLAUDE.md):
  - Plain Python + feedparser + stdlib. No agent frameworks.
  - Fail loud: per-feed exceptions log + continue; structural bugs propagate.
  - pathlib for all file paths.
  - logging via the standard library, with rotating file handler at 10MB.
  - Atomic writes: ``write to <path>.tmp + os.replace()`` so a partial write
    never corrupts the queue or seen-set on crash / power loss.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

import feedparser

log = logging.getLogger("news_rss_poller")

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------

DEFAULT_CHANNEL_RESEARCH = Path(r"C:\ContentOps\channels\ShadowVerse\01_research")
DEFAULT_QUEUE_PATH = DEFAULT_CHANNEL_RESEARCH / "news_drops_queue.json"
DEFAULT_SEEN_PATH = DEFAULT_CHANNEL_RESEARCH / "news_drops_seen.json"
DEFAULT_LOG_PATH = DEFAULT_CHANNEL_RESEARCH / "news_rss_poller.log"

# Fetch timeout for feed requests. feedparser exposes no timeout= kwarg, so we
# apply this via the process-wide default socket timeout around parse() (see
# fetch_feed: save/restore in finally). Keep this generous — feed servers
# occasionally take a few seconds to render.
HTTP_TIMEOUT_S = 30

# Truncate entry summaries before persisting. Keeps the queue file small and
# makes downstream LLM prompts cheaper. 500 chars is enough to convey the gist
# of a vendor announcement without dragging in marketing fluff.
SUMMARY_MAX_CHARS = 500

# Default seen-set retention. 168h = 7 days. Vendor news older than a week is
# stale for a 12h-ship channel and we don't need to remember it forever.
DEFAULT_MAX_AGE_HOURS = 168

USER_AGENT = (
    "ShadowVerse-news-rss-poller/0.1 "
    "(faceless AI Shorts; contact official.shadowverse@gmail.com)"
)

# Canonical feed URLs. Verified live via curl 2026-05-08.
# Each entry: feed_key -> (display_name, feed_url).
FEEDS: dict[str, tuple[str, str]] = {
    "anthropic": (
        "Anthropic / Claude Blog",
        # Olshansk-maintained mirror of claude.com/blog (no native RSS on
        # anthropic.com). Updated multiple times daily; covers product
        # announcements which is the news-drop surface we care about.
        "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_claude.xml",
    ),
    "openai": (
        "OpenAI News",
        "https://openai.com/news/rss.xml",
    ),
    "google": (
        "Google DeepMind Blog",
        "https://deepmind.google/blog/rss.xml",
    ),
}

# General-tech track feeds (broad consumer-tech + viral-old fills, added
# 2026-06-21 for the dual-track slot). These surface fresh device / product / OS
# news (iPhone / Meta / Windows / Tesla / gadgets) that the AI-vendor feeds above
# never carry. The crazy-tech-story LEAD genre is sourced SEPARATELY via tavily-in-
# the-apex-loop (human-interest stories won't cluster in RSS) — see start.md.
# Whether these are polled at all is gated by config
# (news_rss.general_tech_feeds_enabled) at the ORCHESTRATION layer (start.md /
# scheduled task); this module stays config-free and is driven by the --track
# flag. Reddit's .rss endpoint needs no API key. URLs to re-verify live on first
# run (RSS endpoints drift / occasionally go JS-only — the poller degrades
# gracefully via bozo + per-feed isolation).
GENERAL_TECH_FEEDS: dict[str, tuple[str, str]] = {
    "theverge": ("The Verge", "https://www.theverge.com/rss/index.xml"),
    "9to5mac": ("9to5Mac", "https://9to5mac.com/feed/"),
    "engadget": ("Engadget", "https://www.engadget.com/rss.xml"),
    "arstechnica": ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    "reddit_technology": (
        "r/technology (top/day)",
        "https://www.reddit.com/r/technology/top/.rss?t=day",
    ),
    "reddit_gadgets": (
        "r/gadgets (top/day)",
        "https://www.reddit.com/r/gadgets/top/.rss?t=day",
    ),
}

# Track → feed-registry map. The CLI --track flag and callers select via this.
# An unknown track key would KeyError at the call site — intentional fail-loud.
FEEDS_BY_TRACK: dict[str, dict[str, tuple[str, str]]] = {
    "ai-vendor": FEEDS,
    "general-tech": GENERAL_TECH_FEEDS,
}


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------


@dataclass
class NewsDrop:
    """One vendor news entry promoted to the queue.

    Field names are stable — manager session reads this shape when surfacing
    queued drops on ``start``. Don't rename without updating the manager
    contract.
    """

    id: str                       # entry.id with entry.link fallback (dedup key)
    feed: str                     # feed_key: "anthropic" | "openai" | "theverge" | ...
    title: str
    url: str
    published_at: str             # ISO 8601, parsed from entry.published if present
    summary: str                  # truncated to SUMMARY_MAX_CHARS
    added_to_queue_at: str        # ISO 8601 of when this script appended it
    track: str = "ai-vendor"      # "ai-vendor" | "general-tech" — consumers filter by this
    extra: dict = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


def setup_logging(log_path: Path) -> None:
    """Attach a RotatingFileHandler + StreamHandler to the module logger.

    Idempotent: re-running setup_logging swaps handlers cleanly so unit tests
    that import this module multiple times don't accrete log handlers.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # Drop any handlers we previously attached (handle module re-imports).
    for h in list(log.handlers):
        log.removeHandler(h)
    fh = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.INFO)


# -----------------------------------------------------------------------------
# Atomic JSON I/O
# -----------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON atomically: serialize to <path>.tmp, then os.replace().

    On Windows ``os.replace`` is atomic for files on the same volume — a
    crash mid-write leaves either the old file or the new file, never a
    half-written one. Critical for the seen-set: a corrupt seen-set would
    cause us to re-emit every news drop on the next poll.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: object) -> object:
    """Read JSON from path, return default if missing or unparseable."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not parse %s: %s — falling back to default", path, e)
        return default


# -----------------------------------------------------------------------------
# Seen-set utilities
# -----------------------------------------------------------------------------


def load_seen(seen_path: Path) -> dict[str, str]:
    """Load the seen-set: {entry_id: ISO 8601 added_to_queue_at}.

    Returns a fresh dict if the file is missing or empty. The added_at value
    is what enables max-age pruning.
    """
    raw = _read_json(seen_path, default={})
    if not isinstance(raw, dict):
        log.warning("seen-set at %s wasn't a dict — resetting", seen_path)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def prune_seen(seen: dict[str, str], max_age_hours: int) -> dict[str, str]:
    """Drop entries older than max_age_hours from the seen-set.

    Returns a new dict — does not mutate the input. Entries with malformed
    timestamps are kept (we'd rather over-remember than re-emit).
    """
    if max_age_hours <= 0:
        return dict(seen)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    pruned: dict[str, str] = {}
    dropped = 0
    for entry_id, added_at_iso in seen.items():
        try:
            added_at = datetime.fromisoformat(added_at_iso)
        except (TypeError, ValueError):
            # Keep malformed entries — safer than re-emitting on next poll.
            pruned[entry_id] = added_at_iso
            continue
        if added_at >= cutoff:
            pruned[entry_id] = added_at_iso
        else:
            dropped += 1
    if dropped:
        log.info("seen-set: pruned %d entries older than %dh", dropped, max_age_hours)
    return pruned


# -----------------------------------------------------------------------------
# Feed parsing
# -----------------------------------------------------------------------------


def _entry_id(entry: dict) -> str | None:
    """Best-effort dedup key for a feed entry.

    feedparser normalizes entry.id (Atom) and entry.guid (RSS) into entry.id,
    but some feeds omit it — fall back to entry.link, then None.
    """
    # feedparser dict-access via .get works on both Atom and RSS.
    eid = entry.get("id") or entry.get("guid") or entry.get("link")
    if not eid:
        return None
    return str(eid).strip()


def _entry_published_iso(entry: dict) -> str:
    """Extract a published timestamp as ISO 8601, fallback to now()."""
    # feedparser populates entry.published_parsed (a time.struct_time tuple)
    # when it can parse the source's pubDate / updated field.
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except (TypeError, ValueError):
            pass
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        return str(raw)
    return datetime.now(timezone.utc).isoformat()


def _entry_summary(entry: dict) -> str:
    """Truncated plaintext summary of an entry, capped at SUMMARY_MAX_CHARS."""
    raw = (
        entry.get("summary")
        or entry.get("description")
        or entry.get("subtitle")
        or ""
    )
    s = str(raw).strip()
    if len(s) > SUMMARY_MAX_CHARS:
        s = s[: SUMMARY_MAX_CHARS - 3].rstrip() + "..."
    return s


def fetch_feed(feed_key: str, feed_url: str) -> list[dict]:
    """Fetch + parse one feed. Returns the raw feedparser entries list.

    Raises on hard failures (network unreachable, malformed XML); the caller
    is expected to wrap this in try/except so one bad feed doesn't kill the
    rest of the poll.
    """
    log.info("fetch %s: %s", feed_key, feed_url)
    # feedparser has no timeout= kwarg; it uses urllib under the hood, which
    # honors the process-wide default socket timeout. Set it around the parse
    # call and restore the prior default in finally so a stalled feed server
    # can't hang the fetch indefinitely and we don't leak a global timeout into
    # the rest of the process.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(HTTP_TIMEOUT_S)
    try:
        parsed = feedparser.parse(
            feed_url,
            agent=USER_AGENT,
            request_headers={"User-Agent": USER_AGENT},
        )
    finally:
        socket.setdefaulttimeout(prev_timeout)
    # feedparser sets .bozo=1 when the feed is malformed; it still returns
    # whatever entries it managed to parse, so we treat bozo as a warning,
    # not a hard failure.
    if getattr(parsed, "bozo", 0):
        bozo_exc = getattr(parsed, "bozo_exception", None)
        log.warning("%s: feedparser bozo flag set: %s", feed_key, bozo_exc)
    entries = list(getattr(parsed, "entries", []) or [])
    log.info("fetch %s: %d entries returned", feed_key, len(entries))
    return entries


def entries_to_drops(
    feed_key: str,
    entries: Iterable[dict],
    seen: dict[str, str],
    *,
    track: str = "ai-vendor",
) -> list[NewsDrop]:
    """Filter feed entries through the seen-set, materialize NewsDrops.

    Mutates `seen` — adds each accepted entry's id with the now() timestamp.
    `track` tags each drop so the shared queue is consumable per-track downstream
    (defaults "ai-vendor" so existing positional callers are unchanged).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    drops: list[NewsDrop] = []
    for entry in entries:
        eid = _entry_id(entry)
        if not eid:
            log.warning("%s: skipping entry with no id/guid/link", feed_key)
            continue
        if eid in seen:
            continue
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("link", "")).strip()
        if not title or not url:
            log.warning("%s: skipping entry missing title/link (id=%s)", feed_key, eid)
            continue
        drop = NewsDrop(
            id=eid,
            feed=feed_key,
            title=title,
            url=url,
            published_at=_entry_published_iso(entry),
            summary=_entry_summary(entry),
            added_to_queue_at=now_iso,
            track=track,
        )
        drops.append(drop)
        seen[eid] = now_iso
    log.info("%s: %d new drop(s) after dedup", feed_key, len(drops))
    return drops


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def poll_once(
    *,
    feeds: dict[str, tuple[str, str]],
    queue_path: Path,
    seen_path: Path,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    track: str = "ai-vendor",
) -> list[NewsDrop]:
    """Poll all `feeds` once, append new entries to the queue, update seen-set.

    Returns the list of NewsDrops appended this run. Per-feed failures log a
    WARNING and skip — they do not raise out. `track` tags every drop produced
    this run (defaults "ai-vendor"); two sequential per-track polls share one
    queue file and append their own track-tagged drops.
    """
    seen = load_seen(seen_path)
    seen = prune_seen(seen, max_age_hours)

    queue_existing = _read_json(queue_path, default=[])
    if not isinstance(queue_existing, list):
        log.warning("queue at %s wasn't a list — starting fresh", queue_path)
        queue_existing = []

    new_drops: list[NewsDrop] = []
    for feed_key, (display_name, feed_url) in feeds.items():
        try:
            entries = fetch_feed(feed_key, feed_url)
        except Exception as e:  # noqa: BLE001 — explicit per-feed isolation
            log.warning("%s (%s): FETCH FAILED — %s", feed_key, display_name, e)
            continue
        try:
            drops = entries_to_drops(feed_key, entries, seen, track=track)
        except Exception as e:  # noqa: BLE001
            log.warning("%s (%s): PARSE FAILED — %s", feed_key, display_name, e)
            continue
        for d in drops:
            log.info(
                "queue+ %s: %s (%s)",
                feed_key,
                d.title[:80] + ("..." if len(d.title) > 80 else ""),
                d.url,
            )
        new_drops.extend(drops)

    if new_drops:
        queue_existing.extend(asdict(d) for d in new_drops)
        _atomic_write_json(queue_path, queue_existing)
        log.info("queue: appended %d, total %d at %s",
                 len(new_drops), len(queue_existing), queue_path)
    else:
        log.info("queue: no new drops this run")

    # Always write the seen-set, even when no new drops landed — pruning may
    # have removed stale entries and we want that persisted.
    _atomic_write_json(seen_path, seen)

    return new_drops


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _parse_feeds_arg(
    arg: str | None,
    registry: dict[str, tuple[str, str]] = FEEDS,
) -> dict[str, tuple[str, str]]:
    """Resolve --feeds CSV to a dict of selected feeds. None = all in `registry`."""
    if not arg:
        return dict(registry)
    keys = [k.strip().lower() for k in arg.split(",") if k.strip()]
    selected: dict[str, tuple[str, str]] = {}
    for k in keys:
        if k not in registry:
            raise SystemExit(
                f"Unknown feed key: {k!r}. Available: {sorted(registry)}"
            )
        selected[k] = registry[k]
    if not selected:
        return dict(registry)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll AI-vendor news RSS feeds; append new entries to a queue file."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll all selected feeds once and exit. Required for scheduled-task use.",
    )
    parser.add_argument(
        "--track",
        default="ai-vendor",
        choices=sorted(FEEDS_BY_TRACK),
        help=(
            "Which feed registry to poll and tag drops with. "
            "'ai-vendor' (default): anthropic,openai,google. "
            "'general-tech': theverge,9to5mac,engadget,arstechnica,reddit_technology,reddit_gadgets."
        ),
    )
    parser.add_argument(
        "--feeds",
        default=None,
        help="CSV of feed keys to poll within the selected --track. Default: all in that track.",
    )
    parser.add_argument(
        "--queue",
        default=str(DEFAULT_QUEUE_PATH),
        help=f"Queue JSON path. Default: {DEFAULT_QUEUE_PATH}",
    )
    parser.add_argument(
        "--seen",
        default=str(DEFAULT_SEEN_PATH),
        help=f"Seen-set JSON path. Default: {DEFAULT_SEEN_PATH}",
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        help=(
            "Drop seen-set entries older than this many hours each run "
            f"(default: {DEFAULT_MAX_AGE_HOURS}). Set <=0 to disable pruning."
        ),
    )
    parser.add_argument(
        "--log-path",
        default=str(DEFAULT_LOG_PATH),
        help=f"Log file path. Default: {DEFAULT_LOG_PATH}",
    )
    args = parser.parse_args(argv)

    if not args.once:
        # We don't ship a continuous-polling daemon — operator owns scheduling
        # via Windows Task Scheduler. Fail loud rather than silently no-op.
        parser.error("--once is required (this tool runs as a scheduled-task one-shot).")

    setup_logging(Path(args.log_path))
    log.info("news_rss_poller start (once, track=%s)", args.track)
    registry = FEEDS_BY_TRACK[args.track]
    feeds = _parse_feeds_arg(args.feeds, registry)
    log.info("polling feeds: %s", sorted(feeds))

    poll_once(
        feeds=feeds,
        queue_path=Path(args.queue),
        seen_path=Path(args.seen),
        max_age_hours=args.max_age_hours,
        track=args.track,
    )
    log.info("news_rss_poller done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
