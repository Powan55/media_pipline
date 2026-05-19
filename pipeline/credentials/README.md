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
