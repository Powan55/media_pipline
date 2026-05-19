"""Shared OAuth token health utilities for the YouTube Data + Analytics APIs.

Addresses audit finding H3 (WORKFLOW_AUDIT_2026-05-16): both `analytics_pull.py`
and `tools/youtube_upload.py` previously *reacted* to `creds.expired` by calling
`creds.refresh(Request())` with no proactive expiry surfacing and no translation
of `google.auth.exceptions.RefreshError`. When Google revoked the refresh token
under the 7-day OAuth-testing-status policy (observed cycle 7), the refresh call
raised a generic stacktrace instead of an actionable error pointing the operator
at the fix.

This module provides two small helpers:

- `log_token_expiry_health(creds, *, logger)` — INFO line with hours remaining
  on every load. Escalates to WARNING below 24h, ERROR below 6h. Surfaces the
  looming revocation before it breaks the pipeline.
- `refresh_with_translation(creds, *, token_path, logger)` — wraps
  `creds.refresh(Request())` and translates `RefreshError` into a `RuntimeError`
  whose message states the OAuth testing-status policy as likely cause and
  points at the fix commands. Persists the refreshed token to disk on success.

The 7-day testing-status policy is the structural root cause; moving the app to
in-production status in Google Cloud Console is the long-term fix. Both helpers
mention this so the operator sees the path forward without re-reading the audit.

Plain Python, fail-loud, pathlib, logging — per project engineering principles.
No new dependencies.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Thresholds for proactive expiry surfacing. Hard-coded (not config-driven) on
# purpose: these aren't operational knobs, they're alert levels tied to the
# 7-day testing-status policy. WARNING fires inside the last day; ERROR fires
# inside the last 6 hours (operator has time to re-consent before the next
# scheduled run hits the failure).
_WARN_THRESHOLD_HOURS = 24.0
_ERROR_THRESHOLD_HOURS = 6.0

# Reusable message fragments. Kept module-level so tests can assert against
# stable substrings instead of full strings.
_TESTING_STATUS_LIKELY_CAUSE = (
    "OAuth refresh token has been revoked or expired. Likely cause: Google's "
    "7-day testing-status policy on OAuth apps in 'Testing' mode."
)
_FIX_COMMAND_POINTER = (
    "Re-authorize by running: python tools\\youtube_oauth_init.py --force"
)
_LONG_TERM_FIX_POINTER = (
    "Long-term fix: move the OAuth app to in-production status in Google "
    "Cloud Console."
)


def log_token_expiry_health(creds: Credentials, *, logger: logging.Logger) -> None:
    """Log how many hours remain on the OAuth token's expiry.

    INFO at >=24h remaining, WARNING below 24h, ERROR below 6h. If the token
    has no `.expiry` attribute (some auth backends omit it), emits a DEBUG line
    and returns — never raises. This is purely observational.

    Designed to be called on every credential load so that an operator running
    *anything* (analytics pull, upload, manual scripts) sees the looming
    refresh before it breaks the pipeline.
    """
    expiry = getattr(creds, "expiry", None)
    if expiry is None:
        logger.debug("OAuth token has no .expiry attribute; cannot surface health")
        return

    # google-auth stores .expiry as a naive UTC datetime. Compare against a
    # naive UTC `now` to avoid the "can't subtract offset-naive and offset-aware
    # datetimes" trap.
    now_utc_naive = datetime.utcnow()
    delta = expiry - now_utc_naive
    hours_remaining = delta.total_seconds() / 3600.0

    msg = (
        f"OAuth token expiry health: {hours_remaining:.1f}h remaining "
        f"(expires {expiry.isoformat()} UTC)"
    )
    if hours_remaining < _ERROR_THRESHOLD_HOURS:
        logger.error(
            "%s — under %.1fh; refresh imminent. If refresh fails, see %s",
            msg,
            _ERROR_THRESHOLD_HOURS,
            _FIX_COMMAND_POINTER,
        )
    elif hours_remaining < _WARN_THRESHOLD_HOURS:
        logger.warning(
            "%s — under %.1fh; plan to re-authorize soon if needed",
            msg,
            _WARN_THRESHOLD_HOURS,
        )
    else:
        logger.info("%s", msg)


def refresh_with_translation(
    creds: Credentials,
    *,
    token_path: Path,
    logger: logging.Logger,
) -> None:
    """Refresh `creds` in place; translate RefreshError into actionable RuntimeError.

    On success: writes the refreshed token JSON back to `token_path`.

    On `google.auth.exceptions.RefreshError`: raises `RuntimeError` whose message
    names the 7-day OAuth testing-status policy as likely cause and points the
    operator at the fix command. The original exception is chained via `raise
    ... from exc` so the underlying Google error is still visible in tracebacks
    for diagnostic purposes.

    This is the proactive translation for audit H3: the old behavior surfaced
    `invalid_grant: Token has been expired or revoked` as a bare stacktrace,
    which is operator-hostile.
    """
    logger.info("OAuth token expired; refreshing via Request()")
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        msg = (
            f"{_TESTING_STATUS_LIKELY_CAUSE}\n"
            f"  Underlying error: {exc}\n"
            f"  {_FIX_COMMAND_POINTER}\n"
            f"  {_LONG_TERM_FIX_POINTER}"
        )
        # Logging at ERROR makes the actionable text visible even when callers
        # swallow the exception (which they shouldn't, but defense in depth).
        logger.error("%s", msg)
        raise RuntimeError(msg) from exc

    token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("OAuth token refreshed and persisted to %s", token_path)
