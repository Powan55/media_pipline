"""Flip a published YouTube video's privacy status via the official Data API.

Built for the 2026-06-09 weekly review PU-14 integrity repair (privatize the
live video `Eaxrx6CVJ0s` / topic 2026-05-08_001, which carries an unresolved
[VERIFY:] citation — Manager C2 default: privatize FIRST, reversible, resolve
at leisure). Reusable for any owned video.

CLI:
    python tools/set_video_privacy.py --video-id Eaxrx6CVJ0s --privacy private
    python tools/set_video_privacy.py --video-id Eaxrx6CVJ0s --privacy public

Reads:
    credentials/token.json (OAuth — existing `youtube` scope, same token the
    upload path uses; videos.update is within the already-authorized scope)

Writes:
    YouTube — videos.update (status.privacyStatus only; other writable status
    fields are preserved verbatim from the fetched current status)
    Stdout — before/after privacy status, both fetched live from the API

Notes:
    - `status.publishAt` is intentionally DROPPED from the update body: it is
      only valid on scheduled-private videos with a future timestamp, and this
      tool's job is privatize/unprivatize, not re-scheduling.
    - Fail-loud: missing video, missing token, or API errors exit non-zero.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.oauth_token_helpers import (  # noqa: E402 — sys.path bootstrap above
    log_token_expiry_health,
    refresh_with_translation,
)

log = logging.getLogger("set_video_privacy")

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = REPO_ROOT / "credentials" / "token.json"

# Must match tools/youtube_oauth_init.py — re-run that with --force when these change.
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

ALLOWED_PRIVACY = ("public", "unlisted", "private")

# status fields that are writable via videos.update and safe to round-trip.
# publishAt is deliberately excluded — see module docstring.
_WRITABLE_STATUS_FIELDS = (
    "privacyStatus",
    "embeddable",
    "license",
    "publicStatsViewable",
    "selfDeclaredMadeForKids",
)


def load_credentials() -> Credentials:
    """Load OAuth credentials (same pattern as tools/youtube_upload.py)."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"credentials/token.json not found at {TOKEN_PATH}. "
            f"Run `python tools/youtube_oauth_init.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    log_token_expiry_health(creds, logger=log)
    if creds.expired and creds.refresh_token:
        refresh_with_translation(creds, token_path=TOKEN_PATH, logger=log)
    if not creds.valid:
        raise RuntimeError(
            "OAuth token invalid or missing scopes. Run "
            "`python tools/youtube_oauth_init.py --force` to re-consent."
        )
    return creds


def fetch_video(youtube, video_id: str) -> dict:
    """Fetch the video's snippet + status. Fail-loud if the id is unknown."""
    resp = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(
            f"video id {video_id!r} not found (not owned by this channel, "
            f"deleted, or a typo)"
        )
    return items[0]


def set_privacy(youtube, video_id: str, privacy: str, current_status: dict) -> None:
    """videos.update the privacyStatus, preserving other writable status fields."""
    body_status = {
        k: current_status[k] for k in _WRITABLE_STATUS_FIELDS if k in current_status
    }
    body_status["privacyStatus"] = privacy
    youtube.videos().update(
        part="status",
        body={"id": video_id, "status": body_status},
    ).execute()


def _print_state(label: str, video: dict) -> None:
    snippet = video.get("snippet", {})
    status = video.get("status", {})
    print(f"{label}:")
    print(f"  title:         {snippet.get('title', '(unknown)')}")
    print(f"  privacyStatus: {status.get('privacyStatus', '(unknown)')}")
    print(f"  uploadStatus:  {status.get('uploadStatus', '(unknown)')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set a YouTube video's privacy status via the official Data API. "
                    "Reversible — re-run with a different --privacy to flip back."
    )
    parser.add_argument("--video-id", required=True, help="YouTube video id, e.g. Eaxrx6CVJ0s")
    parser.add_argument(
        "--privacy", required=True, choices=ALLOWED_PRIVACY,
        help="Target privacyStatus. Required — no default.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    creds = load_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    try:
        before = fetch_video(youtube, args.video_id)
        _print_state("BEFORE", before)

        before_privacy = before.get("status", {}).get("privacyStatus")
        if before_privacy == args.privacy:
            print(f"\nvideo {args.video_id} is already privacy={args.privacy} — no-op.")
            return 0

        set_privacy(youtube, args.video_id, args.privacy, before.get("status", {}))

        # Verify by re-fetching — never trust the write blindly.
        after = fetch_video(youtube, args.video_id)
        _print_state("AFTER", after)

        after_privacy = after.get("status", {}).get("privacyStatus")
        if after_privacy != args.privacy:
            log.error(
                "verification failed: requested privacy=%s but API reports %s",
                args.privacy, after_privacy,
            )
            return 1
        print(f"\nvideo {args.video_id}: {before_privacy} -> {after_privacy} (verified)")
        return 0
    except HttpError as e:
        log.error("YouTube API error: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
