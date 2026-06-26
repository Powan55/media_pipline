"""Upload a finalized ShadowVerse video to TikTok via the official Content Posting API.

The TikTok analogue of `tools/youtube_upload.py`. TikTok is NOT like YouTube:

  * There is no free, hands-off PUBLIC auto-post for an UNAUDITED app. Both the
    "inbox" flow (video.upload) and the "direct post" flow (video.publish) are
    capped at SELF_ONLY (private) until the app passes TikTok's Content Posting
    API audit. Public posting is a one-flag flip (mode: direct, privacy_level:
    PUBLIC_TO_EVERYONE) ONLY after that audit lands. See the TikTok upload-policy
    memory + the project plan for the full story.
  * Posting is OFFICIAL API only. Cookie auth, browser-session hijacking, and
    third-party uploaders are banned, exactly as on the YouTube side.

Two modes (config.tiktok.mode, overridable with --mode):
  * inbox  (default, pre-audit): pushes the video to the creator's TikTok app
            drafts/inbox. The creator opens TikTok and finishes posting. NOTE:
            the inbox init takes NO caption — the operator adds the caption +
            privacy in-app. This script prints the generated caption so it can be
            copied.
  * direct (post-audit): publishes via Direct Post with post_info (caption +
            privacy_level). Auto-publishes; fully hands-off once audited.

CLI:
    python tools/tiktok_upload.py --topic-id 2026-06-24_002 --dry-run
    python tools/tiktok_upload.py --topic-id 2026-06-24_002             # uses config defaults
    python tools/tiktok_upload.py --topic-id 2026-06-24_002 --mode inbox
    python tools/tiktok_upload.py --topic-id 2026-06-24_002 --mode direct --privacy PUBLIC_TO_EVERYONE

Reads:
    config.yaml (paths.channel_root, tiktok.*)
    .env (TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET)
    <channel_root>/05_exports/tiktok/<topic_id>_tt.mp4         (the upload payload)
    <channel_root>/02_scripts/_drafts/<topic_id>/metadata_RESPONSE.txt
    credentials/tiktok_token.json (OAuth — run tools/tiktok_oauth_init.py first)

Writes:
    TikTok — Content Posting API (inbox or direct)
    <channel_root>/01_research/tiktok_upload_log.csv (append-only audit row)
    Stdout — publish_id, status, mode, privacy
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

# Reuse the metadata parser from pipeline.py for consistency with the rest of the
# pipeline (same call youtube_upload.py makes).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import _parse_metadata_response, MetadataBundle  # noqa: E402
from tools.tiktok_token_helpers import (  # noqa: E402
    HTTP_TIMEOUT_S,
    get_valid_access_token,
    load_client_credentials,
)

log = logging.getLogger("tiktok_upload")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
ENV_PATH = REPO_ROOT / ".env"

API_BASE = "https://open.tiktokapis.com"
INBOX_INIT_URL = f"{API_BASE}/v2/post/publish/inbox/video/init/"
DIRECT_INIT_URL = f"{API_BASE}/v2/post/publish/video/init/"
STATUS_FETCH_URL = f"{API_BASE}/v2/post/publish/status/fetch/"
CREATOR_INFO_URL = f"{API_BASE}/v2/post/publish/creator_info/query/"

ALLOWED_MODES = ("inbox", "direct")
ALLOWED_PRIVACY = (
    "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY",
)

# TikTok caption ("title") hard cap.
MAX_CAPTION_CHARS = 2200
# Single-request upload ceiling: a whole video <= 64 MB may be one chunk. Our
# Shorts are well under this; reject anything larger with a clear pointer rather
# than silently mis-chunking (chunking is intentionally not implemented — YAGNI).
SINGLE_CHUNK_MAX_BYTES = 64 * 1024 * 1024

# Status poll: terminal states per TikTok docs.
_SUCCESS_STATUSES = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
_FAILED_STATUSES = {"FAILED"}
_POLL_INTERVAL_S = 3
_POLL_MAX_S = 180


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def find_paths(config: dict, topic_id: str) -> tuple[Path, Path]:
    """Resolve + verify the TikTok variant and the metadata file."""
    channel_root = Path(config["paths"]["channel_root"])
    video = channel_root / "05_exports" / "tiktok" / f"{topic_id}_tt.mp4"
    metadata = channel_root / "02_scripts" / "_drafts" / topic_id / "metadata_RESPONSE.txt"
    missing = [(label, p) for label, p in (("video", video), ("metadata", metadata))
               if not p.exists()]
    if missing:
        details = "\n".join(f"  - {label}: {p}" for label, p in missing)
        raise FileNotFoundError(f"required input(s) missing for {topic_id}:\n{details}")
    return video, metadata


def build_caption(bundle: MetadataBundle) -> str:
    """Compose the TikTok caption from the metadata bundle (caption + hashtags)."""
    caption = (bundle.tiktok_caption or "").strip()
    hashtags = " ".join(h for h in (bundle.tiktok_hashtags or []) if h.strip())
    if hashtags and hashtags not in caption:
        caption = (caption.rstrip() + "\n\n" + hashtags).strip()
    if len(caption) > MAX_CAPTION_CHARS:
        log.warning("caption is %d chars; truncating to %d", len(caption), MAX_CAPTION_CHARS)
        caption = caption[:MAX_CAPTION_CHARS]
    return caption


# --- Audit log (idempotency guard) ----------------------------------------

def _log_path(config: dict) -> Path:
    return Path(config["paths"]["channel_root"]) / "01_research" / "tiktok_upload_log.csv"


def _find_existing_upload(config: dict, topic_id: str) -> dict | None:
    """Return the most recent tiktok_upload_log.csv row for topic_id, or None.

    Fail-soft toward ALLOWING the upload (a missing/unreadable log returns None),
    exactly like youtube_upload.py's guard. Separate from the YouTube
    upload_log.csv so neither reader can break the other.
    """
    p = _log_path(config)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", newline="") as f:
            matches = [r for r in csv.DictReader(f) if r.get("topic_id") == topic_id]
    except OSError as e:
        log.warning("idempotency check: could not read %s (%s) — allowing upload", p, e)
        return None
    return matches[-1] if matches else None


def append_log(config: dict, topic_id: str, publish_id: str, mode: str,
               privacy: str, status: str, title: str) -> Path:
    p = _log_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["uploaded_at", "topic_id", "publish_id", "mode", "privacy", "status", "title"])
        w.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            topic_id, publish_id, mode, privacy, status, title,
        ])
    return p


# --- TikTok API plumbing ---------------------------------------------------

def _check_tiktok_error(body: dict, *, where: str) -> None:
    """Raise on a non-ok TikTok error envelope ({"error": {"code","message","log_id"}})."""
    err = body.get("error") or {}
    code = err.get("code")
    if code not in (None, "ok"):
        raise RuntimeError(
            f"TikTok API error during {where}: {code} — {err.get('message', '(no message)')} "
            f"(log_id={err.get('log_id', 'n/a')})"
        )


def _post_json(url: str, *, token: str, body: dict, where: str) -> dict:
    try:
        resp = requests.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            timeout=HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"TikTok {where} request failed (network): {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            f"TikTok {where} returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]!r}"
        )
    _check_tiktok_error(data, where=where)
    if resp.status_code >= 400:
        raise RuntimeError(f"TikTok {where} HTTP {resp.status_code}: {data}")
    return data


def query_creator_info(token: str) -> dict:
    """Direct-post precheck: returns allowed privacy levels + posting limits."""
    data = _post_json(CREATOR_INFO_URL, token=token, body={}, where="creator_info/query")
    return data.get("data") or {}


def init_upload(token: str, *, mode: str, video_size: int, caption: str,
                privacy: str, video_made_with_ai: bool) -> tuple[str, str]:
    """Initialize the publish. Returns (publish_id, upload_url)."""
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": video_size,
        "chunk_size": video_size,     # single chunk (file < 64 MB)
        "total_chunk_count": 1,
    }
    if mode == "inbox":
        # Inbox init takes NO post_info — the creator sets caption/privacy in-app.
        body = {"source_info": source_info}
        data = _post_json(INBOX_INIT_URL, token=token, body=body, where="inbox/video/init")
    else:  # direct
        post_info = {
            "title": caption,
            "privacy_level": privacy,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        }
        # NOTE: AI-generated-content disclosure (config.tiktok.video_made_with_ai)
        # is intentionally NOT injected here — the exact post_info field name has
        # shifted across TikTok API versions and an unrecognized key 400s the
        # request. Verify the current field against TikTok docs when the Direct
        # Post audit lands, then wire it in. Until then the AIGC label is set
        # in-app. (video_made_with_ai is read so linters/future-me see the intent.)
        _ = video_made_with_ai
        body = {"post_info": post_info, "source_info": source_info}
        data = _post_json(DIRECT_INIT_URL, token=token, body=body, where="video/init")
    d = data.get("data") or {}
    publish_id = d.get("publish_id")
    upload_url = d.get("upload_url")
    if not publish_id or not upload_url:
        raise RuntimeError(f"init response missing publish_id/upload_url: {data}")
    return publish_id, upload_url


def upload_file(upload_url: str, video_path: Path, video_size: int) -> None:
    """Single-request PUT of the whole file to the TikTok-provided upload URL."""
    data = video_path.read_bytes()
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(video_size),
        "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
    }
    try:
        resp = requests.put(upload_url, data=data, headers=headers, timeout=300)
    except requests.RequestException as exc:
        raise RuntimeError(f"TikTok media upload PUT failed (network): {exc}") from exc
    if resp.status_code not in (200, 201, 202, 206):
        raise RuntimeError(
            f"TikTok media upload PUT returned HTTP {resp.status_code}: {resp.text[:300]!r}"
        )


def poll_status(token: str, publish_id: str) -> str:
    """Poll status/fetch until a terminal state; return the final status string."""
    deadline = time.monotonic() + _POLL_MAX_S
    last = "UNKNOWN"
    while time.monotonic() < deadline:
        data = _post_json(STATUS_FETCH_URL, token=token,
                          body={"publish_id": publish_id}, where="status/fetch")
        last = (data.get("data") or {}).get("status", "UNKNOWN")
        log.info("publish %s status: %s", publish_id, last)
        if last in _SUCCESS_STATUSES:
            return last
        if last in _FAILED_STATUSES:
            fail_reason = (data.get("data") or {}).get("fail_reason", "(none)")
            raise RuntimeError(f"TikTok publish FAILED (publish_id={publish_id}): {fail_reason}")
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"TikTok publish status did not reach a terminal state within {_POLL_MAX_S}s "
        f"(last={last}, publish_id={publish_id})"
    )


# --- main ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload a ShadowVerse video to TikTok via the official Content Posting API."
    )
    parser.add_argument("--topic-id", required=True,
                        help="Topic id (e.g. 2026-06-24_002). Resolves the _tt.mp4 + metadata paths.")
    parser.add_argument("--mode", choices=ALLOWED_MODES, default=None,
                        help="inbox (pre-audit, default from config) | direct (post-audit Direct Post).")
    parser.add_argument("--privacy", choices=ALLOWED_PRIVACY, default=None,
                        help="Direct-post privacy_level (default from config). Unaudited apps are "
                             "forced to SELF_ONLY regardless.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse + verify inputs/caption without calling TikTok or loading creds.")
    parser.add_argument("--force", action="store_true",
                        help="Override the idempotency guard (upload even if already logged).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    tiktok_cfg = config.get("tiktok") or {}
    mode = args.mode or tiktok_cfg.get("mode", "inbox")
    privacy = args.privacy or tiktok_cfg.get("privacy_level", "SELF_ONLY")
    video_made_with_ai = bool(tiktok_cfg.get("video_made_with_ai", True))
    if mode not in ALLOWED_MODES:
        raise ValueError(f"invalid tiktok.mode {mode!r}; expected one of {ALLOWED_MODES}")

    video_path, metadata_path = find_paths(config, args.topic_id)
    bundle = _parse_metadata_response(metadata_path.read_text(encoding="utf-8"), args.topic_id)
    caption = build_caption(bundle)
    video_size = video_path.stat().st_size

    log.info("topic_id:   %s", args.topic_id)
    log.info("mode:       %s", mode)
    log.info("privacy:    %s%s", privacy, "" if mode == "direct" else " (inbox: set in-app)")
    log.info("video file: %s (%.1f MB)", video_path.name, video_size / 1e6)
    log.info("caption:    %d chars", len(caption))
    if mode == "inbox":
        log.info("inbox mode - the generated caption is informational; add it in-app:")
        log.info("  %s", caption.replace("\n", " / "))

    if video_size > SINGLE_CHUNK_MAX_BYTES:
        raise RuntimeError(
            f"{video_path.name} is {video_size/1e6:.1f} MB (> 64 MB single-chunk ceiling). "
            f"Chunked upload is not implemented (Shorts are always well under). "
            f"Re-encode smaller or add chunking."
        )

    if args.dry_run:
        log.info("--dry-run: inputs + caption OK; skipping creds + TikTok calls")
        return 0

    # Idempotency guard (mirror youtube_upload.py): never double-post.
    if not args.force:
        existing = _find_existing_upload(config, args.topic_id)
        if existing is not None:
            log.warning(
                "topic %s already posted to TikTok (publish_id=%s, mode=%s, at %s) per %s — "
                "skipping. Re-run with --force to post again.",
                args.topic_id, existing.get("publish_id"), existing.get("mode"),
                existing.get("uploaded_at"), _log_path(config),
            )
            print(f"Skipped — {args.topic_id} already in tiktok_upload_log.csv "
                  f"(publish_id={existing.get('publish_id')}). Use --force to override.")
            return 0

    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ENV_PATH}. Add TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET "
            f"(see credentials/README.md)."
        )
    load_dotenv(ENV_PATH)
    client_key, client_secret = load_client_credentials()
    record = get_valid_access_token(client_key=client_key, client_secret=client_secret, logger=log)
    token = record["access_token"]

    try:
        if mode == "direct":
            # Direct Post precheck: validate the requested privacy is actually
            # allowed for this creator (and surface the audit ceiling clearly).
            info = query_creator_info(token)
            allowed = info.get("privacy_level_options") or []
            if allowed and privacy not in allowed:
                raise RuntimeError(
                    f"privacy_level {privacy!r} not in creator's allowed options {allowed}. "
                    f"Unaudited apps only get ['SELF_ONLY']; pass --privacy SELF_ONLY or "
                    f"complete the TikTok Direct Post audit for public posting."
                )

        publish_id, upload_url = init_upload(
            token, mode=mode, video_size=video_size, caption=caption,
            privacy=privacy, video_made_with_ai=video_made_with_ai,
        )
        log.info("init OK — publish_id=%s", publish_id)
        upload_file(upload_url, video_path, video_size)
        log.info("media uploaded; polling status ...")
        status = poll_status(token, publish_id)

        log_path = append_log(config, args.topic_id, publish_id, mode, privacy, status, caption[:80])
        log.info("logged TikTok post to %s", log_path)

        print()
        print("=" * 60)
        print("TikTok post complete")
        print(f"  topic_id:   {args.topic_id}")
        print(f"  mode:       {mode}")
        print(f"  publish_id: {publish_id}")
        print(f"  status:     {status}")
        if mode == "inbox":
            print("  next:       open TikTok -> drafts/inbox -> add caption -> post "
                  "(privacy is SELF_ONLY until the app is audited)")
        else:
            print(f"  privacy:    {privacy}")
        print("=" * 60)
        return 0
    except RuntimeError as e:
        log.error("TikTok upload failed: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
