"""One-time OAuth consent flow for the TikTok Content Posting API.

The TikTok analogue of `tools/youtube_oauth_init.py`. Run once to authorize the
pipeline against the ShadowVerse TikTok account (@shadowversetec):

    python tools/tiktok_oauth_init.py                 # interim scopes (video.upload)
    python tools/tiktok_oauth_init.py --with-publish  # Phase 2 (adds video.publish)
    python tools/tiktok_oauth_init.py --force          # re-consent (e.g. after audit)
    python tools/tiktok_oauth_init.py --manual         # paste-the-code fallback

Flow:
1. Reads TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET from `.env` (created in the
   TikTok developer console — see credentials/README.md).
2. Opens a browser tab at TikTok's consent screen with PKCE (S256). The user
   signs in to @shadowversetec and grants the requested scopes.
3. TikTok redirects back to the registered redirect URI carrying `?code=...`.
   - Default: a tiny localhost HTTP server captures the code automatically.
   - `--manual`: if TikTok won't allow a localhost redirect, register an https
     URI you control, complete consent, then paste the redirected URL (or just
     the `code`) when prompted.
4. Exchanges the code for tokens and saves them to `credentials/tiktok_token.json`
   (access token ~24h, refresh token ~365d; see tiktok_token_helpers.py for the
   rotation footgun).
5. Smoke-test: calls /v2/user/info/ to confirm the token works; prints the
   connected display name + open_id.

Upload policy: TikTok posting is via the OFFICIAL Content Posting API only.
Cookie auth, browser-session hijacking, and third-party uploaders remain banned,
exactly as on the YouTube side (feedback_youtube_upload_policy.md / the TikTok
sibling policy memory).

NOTE on public posting: an UNAUDITED app can only post SELF_ONLY (private),
whether via the inbox or direct-post flow. Public posting requires this app to
pass TikTok's Content Posting API audit. This script just authorizes the account;
it does not change that ceiling.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.tiktok_token_helpers import (  # noqa: E402
    HTTP_TIMEOUT_S,
    SCOPES_DIRECT,
    SCOPES_INTERIM,
    TOKEN_PATH,
    exchange_code_for_token,
    load_client_credentials,
    load_token,
    save_token,
)

log = logging.getLogger("tiktok_oauth_init")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"

# Must match a redirect URI registered in the TikTok app's Login Kit settings.
# Override via TIKTOK_REDIRECT_URI in .env if you registered something else.
DEFAULT_REDIRECT_URI = "http://localhost:8742/callback"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(
    *, client_key: str, scopes: list[str], redirect_uri: str, state: str, challenge: str
) -> str:
    params = {
        "client_key": client_key,
        "scope": ",".join(scopes),  # TikTok expects comma-separated scopes
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code=&state= from the redirect."""

    def do_GET(self):  # noqa: N802 — http.server API
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        # Ignore stray requests (favicon, etc.) that carry no auth params.
        if "code" not in qs and "error" not in qs:
            self.send_response(404)
            self.end_headers()
            return
        self.server.auth_code = (qs.get("code") or [None])[0]      # type: ignore[attr-defined]
        self.server.auth_state = (qs.get("state") or [None])[0]    # type: ignore[attr-defined]
        self.server.auth_error = (qs.get("error") or [None])[0]    # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:sans-serif'>"
            "<h2>TikTok authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence default stderr logging
        return


def _capture_code_via_local_server(
    redirect_uri: str, expected_state: str
) -> str:
    """Run a localhost server until the OAuth redirect arrives; return the code."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    httpd = HTTPServer((host, port), _CallbackHandler)
    httpd.auth_code = None       # type: ignore[attr-defined]
    httpd.auth_state = None      # type: ignore[attr-defined]
    httpd.auth_error = None      # type: ignore[attr-defined]
    httpd.timeout = 300
    log.info("listening for the TikTok redirect on %s ...", redirect_uri)
    while httpd.auth_code is None and httpd.auth_error is None:  # type: ignore[attr-defined]
        httpd.handle_request()
    if httpd.auth_error:  # type: ignore[attr-defined]
        raise RuntimeError(f"TikTok returned an OAuth error on redirect: {httpd.auth_error}")  # type: ignore[attr-defined]
    if httpd.auth_state != expected_state:  # type: ignore[attr-defined]
        raise RuntimeError(
            "OAuth state mismatch (possible CSRF / stale tab). Expected "
            f"{expected_state!r}, got {httpd.auth_state!r}. Re-run the flow."  # type: ignore[attr-defined]
        )
    return httpd.auth_code  # type: ignore[attr-defined]


def _capture_code_via_paste(expected_state: str) -> str:
    """Manual fallback: user pastes the redirected URL (or bare code)."""
    print()
    print("After approving, your browser will redirect to your registered URI.")
    print("Paste the FULL redirected URL here (or just the `code` value):")
    raw = input("> ").strip()
    if "code=" in raw:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        code = (qs.get("code") or [None])[0]
        state = (qs.get("state") or [None])[0]
        if state is not None and state != expected_state:
            raise RuntimeError("OAuth state mismatch in the pasted URL. Re-run the flow.")
        if not code:
            raise RuntimeError("No `code` found in the pasted URL.")
        return code
    if not raw:
        raise RuntimeError("Empty input — expected a redirected URL or a code.")
    return raw  # treat the whole input as the bare code


def smoke_test(access_token: str) -> dict:
    """Confirm the token works by reading the authorized user's basic info."""
    resp = requests.get(
        USER_INFO_URL,
        params={"fields": "open_id,union_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT_S,
    )
    body = resp.json()
    if body.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"user/info smoke test failed: {body.get('error')}")
    return (body.get("data") or {}).get("user") or {}


def run_flow(*, scopes: list[str], redirect_uri: str, manual: bool) -> dict:
    client_key, client_secret = load_client_credentials()
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    url = _build_authorize_url(
        client_key=client_key, scopes=scopes, redirect_uri=redirect_uri,
        state=state, challenge=challenge,
    )

    print("=" * 70)
    print("Opening TikTok consent in your browser. Sign in as @shadowversetec")
    print(f"and grant: {', '.join(scopes)}")
    print("If the browser does not open, paste this URL manually:")
    print(url)
    print("=" * 70)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — headless box; manual paste still works
        pass

    if manual:
        code = _capture_code_via_paste(state)
    else:
        try:
            code = _capture_code_via_local_server(redirect_uri, state)
        except OSError as exc:
            raise RuntimeError(
                f"Could not bind a local server to {redirect_uri} ({exc}). "
                f"Re-run with --manual and register an https redirect URI you control."
            ) from exc

    record = exchange_code_for_token(
        code, client_key=client_key, client_secret=client_secret,
        redirect_uri=redirect_uri, code_verifier=verifier,
    )
    save_token(record)
    log.info("token saved to %s", TOKEN_PATH)
    return record


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="One-time TikTok OAuth consent for ShadowVerse.")
    parser.add_argument("--force", action="store_true",
                        help="Re-consent even if a valid token already exists.")
    parser.add_argument("--with-publish", action="store_true",
                        help="Request the video.publish scope too (Phase 2, post-audit Direct Post).")
    parser.add_argument("--manual", action="store_true",
                        help="Paste-the-code fallback if a localhost redirect isn't allowed.")
    args = parser.parse_args()

    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ENV_PATH}. Copy .env.template to .env and add TIKTOK_CLIENT_KEY / "
            f"TIKTOK_CLIENT_SECRET (see credentials/README.md)."
        )
    load_dotenv(ENV_PATH)

    import os
    redirect_uri = os.environ.get("TIKTOK_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    scopes = SCOPES_DIRECT if args.with_publish else SCOPES_INTERIM

    if not args.force and TOKEN_PATH.exists():
        try:
            existing = load_token(TOKEN_PATH)
            log.info("a token already exists at %s (scope=%s). Use --force to re-consent.",
                     TOKEN_PATH, existing.get("scope", "?"))
            record = existing
        except Exception:  # noqa: BLE001 — corrupt cache → re-run flow
            record = run_flow(scopes=scopes, redirect_uri=redirect_uri, manual=args.manual)
    else:
        record = run_flow(scopes=scopes, redirect_uri=redirect_uri, manual=args.manual)

    info = smoke_test(record["access_token"])

    print()
    print("=" * 60)
    print("TikTok OAuth consent successful")
    print(f"  display name:  {info.get('display_name', '(unknown)')}")
    print(f"  open_id:       {record.get('open_id') or info.get('open_id', '')}")
    print(f"  scope:         {record.get('scope', '')}")
    print(f"  token cached:  {TOKEN_PATH}")
    print("=" * 60)
    print()
    print("Next: set config.yaml -> analytics_pull.tiktok_username: shadowversetec, and run")
    print("      python tools/tiktok_upload.py --topic-id <id> --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
