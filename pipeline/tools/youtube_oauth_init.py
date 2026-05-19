"""One-time OAuth consent flow for the YouTube Data API + Analytics API.

Run once to authorize the pipeline against the ShadowVerse YouTube channel:

    python tools/youtube_oauth_init.py

Flow:
1. Reads `credentials/client_secrets.json` (OAuth desktop client config
   downloaded from Google Cloud Console — see credentials/README.md for setup).
2. Opens a browser tab at Google's consent screen. The user signs in to the
   ShadowVerse Google account and grants the requested scopes.
3. Google redirects back to a localhost port that the script briefly listens on.
4. Script saves access + refresh tokens to `credentials/token.json`.
5. Smoke-test: calls `channels.list(mine=True)` to confirm the token works,
   prints channel id / title / counts.

Subsequent pipeline runs (analytics_pull.py + tools/youtube_upload.py) read
token.json and refresh transparently — no further consent prompts unless the
token is revoked or scopes change. Run `python tools/youtube_oauth_init.py
--force` to re-consent (e.g., when scopes change, like the 2026-05-07 evening
upgrade from `youtube.readonly` to `youtube` for upload support).

Scopes requested:
- youtube:               read + write — channel/video metadata, video upload,
                         thumbnail set, privacy edits. Replaces `youtube.readonly`
                         as of 2026-05-07 evening when upload automation was
                         enabled per the per-video-approval policy (see memory
                         `feedback_youtube_upload_policy.md`).
- yt-analytics.readonly: per-video analytics (retention, traffic sources).

Upload policy: videos are uploadable via the official Data API but ALWAYS
require explicit per-video operator approval before `tools/youtube_upload.py`
is invoked. The token alone does NOT authorize uploads — the operator does, per
video, in chat. Cookie auth and third-party uploaders remain banned.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger("youtube_oauth_init")

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRETS_PATH = REPO_ROOT / "credentials" / "client_secrets.json"
TOKEN_PATH = REPO_ROOT / "credentials" / "token.json"


def load_or_run_flow(force: bool = False) -> Credentials:
    """Load cached token if valid, else run the consent flow.

    `force=True` runs the consent flow even if a valid token exists. Use after
    scope changes or to re-authorize a different account.
    """
    creds: Credentials | None = None
    if TOKEN_PATH.exists() and not force:
        log.info("loading cached token: %s", TOKEN_PATH)
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        log.info("cached token is valid; no consent flow needed")
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("token expired; refreshing")
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not CLIENT_SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"client_secrets.json missing at {CLIENT_SECRETS_PATH}. "
            f"See credentials/README.md for setup."
        )

    log.info("running consent flow (a browser tab will open)")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    log.info("token saved: %s", TOKEN_PATH)
    return creds


def smoke_test(creds: Credentials) -> dict:
    """Confirm the token works by reading the authenticated channel."""
    yt = build("youtube", "v3", credentials=creds)
    resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(
            "channels.list(mine=True) returned no items. The OAuth'd Google "
            "account may not own a YouTube channel, or the brand-account "
            "wasn't selected during consent."
        )
    ch = items[0]
    return {
        "id": ch["id"],
        "title": ch["snippet"]["title"],
        "subscriber_count": int(ch["statistics"].get("subscriberCount", 0)),
        "video_count": int(ch["statistics"].get("videoCount", 0)),
        "view_count": int(ch["statistics"].get("viewCount", 0)),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    force = "--force" in sys.argv

    creds = load_or_run_flow(force=force)
    info = smoke_test(creds)

    print()
    print("=" * 60)
    print("OAuth consent successful")
    print(f"  channel id:        {info['id']}")
    print(f"  channel title:     {info['title']}")
    print(f"  subscriber count:  {info['subscriber_count']:,}")
    print(f"  video count:       {info['video_count']:,}")
    print(f"  total view count:  {info['view_count']:,}")
    print(f"  token cached at:   {TOKEN_PATH}")
    print("=" * 60)
    print()
    print("Next: copy the channel id above into config.yaml under "
          "analytics_pull.youtube_channel_id (replaces the <<TODO>> marker), "
          "then run `python analytics_pull.py --once` for the first read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
