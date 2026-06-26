"""Shared OAuth token utilities for the TikTok Content Posting API.

The TikTok analogue of `tools/oauth_token_helpers.py` (which serves the YouTube
Data + Analytics APIs). TikTok does NOT ship a Python client library the way
Google does, so we manage the OAuth 2.0 token lifecycle by hand against the raw
endpoints:

- Authorize:  https://www.tiktok.com/v2/auth/authorize/   (interactive, see
              `tools/tiktok_oauth_init.py`)
- Token:      https://open.tiktokapis.com/v2/oauth/token/  (exchange + refresh)

Token model (per TikTok docs, 2026):
- access_token  — lifetime ~24h (`expires_in: 86400`)
- refresh_token — lifetime ~365d (`refresh_expires_in: 31536000`)

CRITICAL FOOTGUN — refresh-token ROTATION. TikTok MAY return a NEW refresh_token
on every refresh, and the old one stops working: "The returned refresh_token may
be different than the one passed in the payload. You must use the newly-returned
token if the value is different than the previous one." So every refresh MUST
persist the (possibly rotated) refresh_token back to disk, or the NEXT run's
refresh fails with invalid_grant. This is the TikTok cousin of the YouTube
weekly-revoke incident (`project_oauth_production_flip.md`) — the helper below
re-persists unconditionally.

Token file: `credentials/tiktok_token.json` — our own flat schema (there is no
google `Credentials` object to serialize):

    {
      "access_token": "...",
      "refresh_token": "...",
      "open_id": "...",
      "scope": "user.info.basic,video.upload",
      "token_type": "Bearer",
      "obtained_at": "2026-06-24T12:00:00+00:00",
      "expires_at": "2026-06-25T12:00:00+00:00",
      "refresh_expires_at": "2027-06-24T12:00:00+00:00"
    }

Secrets (TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET) live in `.env`, never in
config.yaml or this file — same convention as PEXELS_API_KEY etc.

Plain Python, fail-loud, pathlib, logging, no agent frameworks — per project
engineering principles. The only new runtime dep is `requests`, already pinned.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("tiktok_token_helpers")

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = REPO_ROOT / "credentials" / "tiktok_token.json"

TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Scopes. video.upload covers the pre-audit inbox flow; video.publish is added at
# Phase 2 (Direct Post) — re-run tiktok_oauth_init.py --force after the audit to
# mint a token that carries it.
SCOPES_INTERIM = ["user.info.basic", "video.upload"]
SCOPES_DIRECT = ["user.info.basic", "video.upload", "video.publish"]

# Network knobs — mirror youtube_upload.py's resilience posture so an unattended
# /start -auto refresh can't hang forever on a stalled socket.
HTTP_TIMEOUT_S = 30

# Refresh the access token this many seconds BEFORE its stated expiry, so a token
# that is technically valid but about to lapse mid-upload gets rotated first.
EXPIRY_LEEWAY_S = 300

_WARN_THRESHOLD_HOURS = 24.0
_ERROR_THRESHOLD_HOURS = 6.0
# Refresh-token (365d) low-water mark — surface well ahead of the hard wall so the
# operator can re-consent on their own schedule, not at a 3am failed auto-run.
_REFRESH_WARN_DAYS = 30.0

_FIX_COMMAND_POINTER = (
    "Re-authorize by running: python tools\\tiktok_oauth_init.py --force"
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_client_credentials() -> tuple[str, str]:
    """Return (client_key, client_secret) from the environment.

    Assumes the caller has already loaded `.env` via python-dotenv (the entry
    points do this). Raises an actionable RuntimeError if either is missing so the
    operator knows exactly which `.env` key to fill.
    """
    key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    missing = [name for name, val in
               (("TIKTOK_CLIENT_KEY", key), ("TIKTOK_CLIENT_SECRET", secret)) if not val]
    if missing:
        raise RuntimeError(
            f"Missing TikTok OAuth secret(s) in .env: {', '.join(missing)}. "
            f"Create a TikTok developer app (developers.tiktok.com/apps), add the "
            f"Content Posting API product, and copy the client key/secret into "
            f"{REPO_ROOT / '.env'} (see credentials/README.md)."
        )
    return key, secret


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (PID-tagged temp sibling + os.replace).

    os.replace is atomic on the same filesystem (Windows included), so a
    concurrent reader — e.g. the OTHER /start -auto sub-agent loading the token at
    the same moment — never sees a half-written file, and two concurrent refreshes
    resolve to last-writer-wins on a COMPLETE token. Mirrors the YouTube helper's
    `_atomic_write_text`.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_token(token_path: Path = TOKEN_PATH) -> dict:
    """Load the cached TikTok token record, or raise an actionable error."""
    if not token_path.exists():
        raise FileNotFoundError(
            f"credentials/tiktok_token.json not found at {token_path}. "
            f"Run `python tools/tiktok_oauth_init.py` first to authorize "
            f"@shadowversetec."
        )
    return json.loads(token_path.read_text(encoding="utf-8"))


def save_token(record: dict, token_path: Path = TOKEN_PATH) -> None:
    """Persist a token record atomically."""
    _atomic_write_text(token_path, json.dumps(record, indent=2, sort_keys=True))


def build_token_record(resp: dict, *, previous: dict | None = None) -> dict:
    """Turn a raw TikTok token response into our stored record.

    Computes absolute `expires_at` / `refresh_expires_at` from the relative
    `expires_in` / `refresh_expires_in` so callers can check expiry without the
    original issue time. Carries forward the prior refresh_token when the response
    omits one (TikTok usually returns it, but be defensive), and preserves
    open_id / scope across a refresh that doesn't echo them.
    """
    now = _now_utc()
    previous = previous or {}

    access_token = resp.get("access_token")
    if not access_token:
        raise RuntimeError(f"TikTok token response missing access_token: {resp}")

    # TikTok ROTATION: prefer the freshly-returned refresh_token; fall back to the
    # prior one only if the response omitted it.
    refresh_token = resp.get("refresh_token") or previous.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"TikTok token response missing refresh_token and none cached: {resp}"
        )

    expires_in = int(resp.get("expires_in", 86400))
    refresh_expires_in = int(resp.get("refresh_expires_in", 31536000))

    record = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "open_id": resp.get("open_id") or previous.get("open_id", ""),
        "scope": resp.get("scope") or previous.get("scope", ""),
        "token_type": resp.get("token_type") or previous.get("token_type", "Bearer"),
        "obtained_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat(timespec="seconds"),
        "refresh_expires_at": (
            now + timedelta(seconds=refresh_expires_in)
        ).isoformat(timespec="seconds"),
    }
    return record


def _post_token_request(payload: dict) -> dict:
    """POST to the TikTok token endpoint (form-encoded) and return parsed JSON.

    Translates HTTP/transport errors and TikTok's `error`/`error_description`
    envelope into an actionable RuntimeError pointing at the re-consent command.
    """
    try:
        resp = requests.post(
            TIKTOK_TOKEN_URL,
            data=payload,  # application/x-www-form-urlencoded
            headers={"Cache-Control": "no-cache"},
            timeout=HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"TikTok token request failed (network): {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(
            f"TikTok token endpoint returned non-JSON (HTTP {resp.status_code}): "
            f"{resp.text[:300]!r}"
        )

    # v2 oauth errors come back flat: {"error": "...", "error_description": "...", "log_id": "..."}
    if body.get("error"):
        raise RuntimeError(
            f"TikTok OAuth error: {body.get('error')} — "
            f"{body.get('error_description', '(no description)')} "
            f"(log_id={body.get('log_id', 'n/a')}). {_FIX_COMMAND_POINTER}"
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"TikTok token endpoint HTTP {resp.status_code}: {body}. {_FIX_COMMAND_POINTER}"
        )
    return body


def exchange_code_for_token(
    code: str, *, client_key: str, client_secret: str, redirect_uri: str,
    code_verifier: str | None = None, previous: dict | None = None,
) -> dict:
    """Exchange an authorization `code` for a token record (used by oauth_init).

    Pass `code_verifier` when the authorize request used PKCE (S256) — TikTok
    requires the verifier on the token exchange in that case.
    """
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    resp = _post_token_request(payload)
    return build_token_record(resp, previous=previous)


def _refresh(record: dict, *, client_key: str, client_secret: str) -> dict:
    resp = _post_token_request({
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": record["refresh_token"],
    })
    return build_token_record(resp, previous=record)


def log_token_expiry_health(record: dict, *, logger: logging.Logger) -> None:
    """Surface access- and refresh-token expiry on every load (observational)."""
    now = _now_utc()
    try:
        expires_at = datetime.fromisoformat(record["expires_at"])
        refresh_expires_at = datetime.fromisoformat(record["refresh_expires_at"])
    except (KeyError, ValueError):
        logger.debug("token record has no parseable expiry fields; skipping health log")
        return

    hours = (expires_at - now).total_seconds() / 3600.0
    msg = f"TikTok access-token health: {hours:.1f}h remaining (expires {expires_at.isoformat()})"
    if hours < _ERROR_THRESHOLD_HOURS:
        logger.error("%s — refresh imminent. If refresh fails, %s", msg, _FIX_COMMAND_POINTER)
    elif hours < _WARN_THRESHOLD_HOURS:
        logger.warning("%s — will auto-refresh", msg)
    else:
        logger.info("%s", msg)

    refresh_days = (refresh_expires_at - now).total_seconds() / 86400.0
    if refresh_days < _REFRESH_WARN_DAYS:
        logger.warning(
            "TikTok refresh-token expires in %.1f days (%s). Re-consent before then: %s",
            refresh_days, refresh_expires_at.isoformat(), _FIX_COMMAND_POINTER,
        )


def get_valid_access_token(
    *,
    token_path: Path = TOKEN_PATH,
    client_key: str | None = None,
    client_secret: str | None = None,
    logger: logging.Logger = log,
) -> dict:
    """Return a token record whose access_token is valid, refreshing if needed.

    Loads the cached record, surfaces expiry health, and refreshes (persisting the
    possibly-rotated refresh_token) when the access token is within EXPIRY_LEEWAY_S
    of expiry. Returns the full record (caller reads `record["access_token"]`).

    Raises an actionable RuntimeError on a hard refresh failure (e.g. the
    refresh_token itself expired/revoked) pointing at the re-consent command.
    """
    if client_key is None or client_secret is None:
        client_key, client_secret = load_client_credentials()

    record = load_token(token_path)
    log_token_expiry_health(record, logger=logger)

    now = _now_utc()
    try:
        expires_at = datetime.fromisoformat(record["expires_at"])
    except (KeyError, ValueError):
        # Corrupt/legacy record — force a refresh to normalize it.
        expires_at = now

    if (expires_at - now).total_seconds() <= EXPIRY_LEEWAY_S:
        logger.info("TikTok access token expired/near-expiry; refreshing")
        try:
            record = _refresh(record, client_key=client_key, client_secret=client_secret)
        except RuntimeError:
            logger.error(
                "TikTok token refresh failed — the refresh_token may be revoked or "
                "expired (365d cap), or rotation was not persisted on a prior run. %s",
                _FIX_COMMAND_POINTER,
            )
            raise
        save_token(record, token_path)
        logger.info("TikTok token refreshed and persisted to %s", token_path)

    return record
