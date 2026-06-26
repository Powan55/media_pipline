# OAuth credentials directory

This directory holds Google Cloud OAuth artifacts for the YouTube Data API +
YouTube Analytics API integration. Gitignored except for this README and the
`.gitkeep` placeholder — secrets must never enter the repo.

## Files (after setup)

- `client_secrets.json` — downloaded from Google Cloud Console → Credentials →
  OAuth 2.0 Client IDs → "ShadowVerse Desktop" → Download JSON. Contains the
  app's client ID/secret. Per Google's docs, the "secret" for desktop apps is
  not truly secret (it ships with the app), but treat it as one anyway.
- `token.json` — cached OAuth refresh token after the first consent flow. Tied
  to a single Google account; do not share. Auto-written by
  `tools/youtube_oauth_init.py` and refreshed on each `analytics_pull.py` run.

## One-time setup

See `tools/youtube_oauth_init.py` docstring for the full flow. Summary:

1. Create a Google Cloud project + enable YouTube Data API v3 + YouTube
   Analytics API
2. Configure the OAuth consent screen (External, Testing, add your gmail as
   a test user)
3. Create an OAuth 2.0 Client ID (type: Desktop app), download the JSON
4. Save it here as `client_secrets.json`
5. Run `python tools/youtube_oauth_init.py` once. A browser opens; sign in
   to the ShadowVerse Google account; grant `youtube.readonly` and
   `yt-analytics.readonly`. Token gets cached as `token.json`
6. Subsequent runs of `analytics_pull.py` use the cached token transparently

---

## TikTok Content Posting API (added 2026-06-24)

The TikTok cross-post (`tools/tiktok_upload.py`) uses the **official** Content
Posting API via OAuth — cookie auth + third-party uploaders are banned, same as
YouTube. Unlike Google, TikTok's client key/secret are plain strings that live in
`.env`, not a JSON file.

### Files (after setup)

- `tiktok_token.json` — cached OAuth token (access ~24h, refresh ~365d). Auto-written
  by `tools/tiktok_oauth_init.py` and refreshed by `tools/tiktok_upload.py`.
  **Footgun:** TikTok rotates the refresh token on refresh — `tiktok_token_helpers.py`
  re-persists it every time; never hand-edit this file.

### Secrets (in `.env`, NOT here)

- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — from the TikTok developer app.
- `TIKTOK_REDIRECT_URI` — optional; only if you registered a non-default URI.

### One-time setup

1. Go to <https://developers.tiktok.com/apps> and register an app (sign in as the
   owner of @shadowversetec).
2. Add the **Login Kit** and **Content Posting API** products.
3. Request scopes: `user.info.basic`, `video.upload` (+ `video.publish` for Phase 2
   Direct Post). Add a **privacy-policy URL** and **terms URL** (required).
4. Register a redirect URI — default `http://localhost:8742/callback` (if TikTok
   rejects localhost, register an https URI you control and use `--manual`).
5. Move the app to **production / Live** (sandbox cannot post real content).
6. Copy the client key/secret into `.env` (`TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET`).
7. Run `python tools/tiktok_oauth_init.py` once — a browser opens; authorize
   @shadowversetec. Token cached as `tiktok_token.json`.

### The audit wall (important)

An **unaudited** app can only post **SELF_ONLY (private)** — on BOTH the inbox and
direct-post flows. PUBLIC posting requires the app to pass TikTok's Content Posting
API audit (~2–4 weeks; needs a Business account + the public website + privacy/ToS
above). Pre-audit, use `config.yaml tiktok.mode: inbox` (the video lands in
@shadowversetec's drafts; finish posting in-app). Post-audit, flip to `mode: direct`
+ `privacy_level: PUBLIC_TO_EVERYONE` for fully-automated public posting — no code
change. Run `python tools/tiktok_oauth_init.py --force --with-publish` after the
audit to mint a token carrying `video.publish`.
