"""Upload a finalized ShadowVerse video to YouTube via the official Data API.

Per the operator's upload policy (set 2026-05-07 evening, memory:
`feedback_youtube_upload_policy.md`): NEVER upload without explicit per-video
operator approval. The pipeline does NOT auto-invoke this script. The operator
runs it (or asks Claude to run it on their behalf in chat) AFTER:
  1. The gate-3 master is approved (`<topic_id>_master_QA_APPROVED.marker`),
  2. Variants + metadata + thumbnail are produced,
  3. The operator has explicitly said "yes, upload <topic_id>" for this
     specific topic_id (no batch approval, no implicit approval).

CLI:
    python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy public
    python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy unlisted
    python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy private
    python tools/youtube_upload.py --topic-id 2026-05-06_003 --privacy unlisted --dry-run

Reads:
    config.yaml (paths.channel_root, etc.)
    <channel_root>/05_exports/youtube/<topic_id>_yt.mp4    (the upload payload)
    <channel_root>/04_renders/_thumbnails/<topic_id>_thumbnail.png
    <channel_root>/02_scripts/_drafts/<topic_id>/metadata_RESPONSE.txt
    credentials/token.json (OAuth — must include the `youtube` scope; rerun
                            tools/youtube_oauth_init.py --force if it doesn't)

Writes:
    YouTube — videos.insert + thumbnails.set
    <channel_root>/01_research/upload_log.csv (append-only audit row)
    Stdout — video URL, video_id, status

Banned (per the upload policy memory): cookie auth, browser-session hijacking,
yt-dlp, yt-upload-cli, any non-Google-OAuth automation. This script only uses
the official Data API via OAuth.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
import re

import httplib2
import yaml
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Reuse the metadata parser from pipeline.py for consistency with the rest of the pipeline.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import _parse_metadata_response, MetadataBundle  # noqa: E402
from hook_selection_log import (  # noqa: E402  — sys.path bootstrap above
    append_to_log as hook_append_to_log,
    extract_chosen_hook,
)
from tools.archive_published import archive_topic  # noqa: E402
from tools.oauth_token_helpers import (  # noqa: E402
    _FIX_COMMAND_POINTER,
    log_token_expiry_health,
    refresh_with_translation,
)
from tools.postmortem_stub import generate_postmortem  # noqa: E402

log = logging.getLogger("youtube_upload")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TOKEN_PATH = REPO_ROOT / "credentials" / "token.json"

# YouTube category id for AI/tech videos. 28 = Science & Technology.
DEFAULT_CATEGORY_ID = "28"

# Must match tools/youtube_oauth_init.py — re-run that with --force when these change.
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

ALLOWED_PRIVACY = ("public", "unlisted", "private")

# YouTube hard caps — enforced server-side, but trim here for clean errors.
MAX_TITLE_CHARS = 100
MAX_DESC_CHARS = 5000

# M2 (WORKFLOW_AUDIT_2026-05-31): resilience knobs for the Data API calls.
# num_retries → googleapiclient's built-in exponential backoff on transient
# 5xx/socket errors; the transport socket timeout is what actually prevents an
# unattended /start -auto upload hanging forever on a stalled connection
# (.execute()/next_chunk() have NO timeout= kwarg). Both rely on already-installed
# deps (google-api-python-client / google-auth-httplib2 / httplib2) — no new dep.
YT_API_NUM_RETRIES = 3
YT_API_TIMEOUT_S = 60  # uploads stream large bodies; allow a longer wall-clock than analytics


def _timeout_http(creds: Credentials) -> AuthorizedHttp:
    """Authorized httplib2 transport with a wall-clock socket timeout.

    Pass as ``build(..., http=_timeout_http(creds))`` INSTEAD of
    ``credentials=creds`` (mutually exclusive — AuthorizedHttp carries creds).
    """
    return AuthorizedHttp(creds, http=httplib2.Http(timeout=YT_API_TIMEOUT_S))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def load_credentials() -> Credentials:
    """Load OAuth credentials with the required scopes; refresh if expired.

    Surfaces token expiry health on every load (INFO at >=24h, WARNING <24h,
    ERROR <6h). Translates `google.auth.exceptions.RefreshError` into an
    actionable `RuntimeError` pointing at `python tools/youtube_oauth_init.py
    --force` — addresses audit finding H3 (WORKFLOW_AUDIT_2026-05-16) so the
    cycle-7 testing-status-revocation incident surfaces a clean fix path
    instead of a generic stacktrace.
    """
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"credentials/token.json not found at {TOKEN_PATH}. "
            f"Run `python tools/youtube_oauth_init.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    log_token_expiry_health(creds, logger=log)
    # M3 (WORKFLOW_AUDIT_2026-05-31): a token can be structurally valid yet carry
    # the WRONG scopes (e.g. minted before yt-analytics.readonly was added). Without
    # this pre-check that surfaces ONLY later as an untranslated HTTP 403
    # insufficientPermissions deep inside an API call. Convert it to the same
    # actionable load-time message the refresh path already uses. has_scopes() is a
    # pure read on the scopes google-auth already parsed — no re-implementation.
    if not creds.has_scopes(SCOPES):
        raise RuntimeError(
            "OAuth token is missing one or more required scopes "
            f"({', '.join(SCOPES)}). {_FIX_COMMAND_POINTER}"
        )
    if creds.expired and creds.refresh_token:
        refresh_with_translation(creds, token_path=TOKEN_PATH, logger=log)
    if not creds.valid:
        raise RuntimeError(
            "OAuth token invalid or missing scopes. Run "
            "`python tools/youtube_oauth_init.py --force` to re-consent with the "
            "required scopes (youtube + yt-analytics.readonly)."
        )
    return creds


def find_paths(
    config: dict, topic_id: str, *, require_thumbnail: bool = True
) -> tuple[Path, Path | None, Path]:
    """Resolve and verify the three input file paths.

    When `require_thumbnail=False`, the returned thumbnail path is None and the
    thumbnail file is not required to exist on disk. Used by `--no-thumbnail`
    uploads while the thumbnail-generation refactor is pending (per
    `feedback_no_thumbnails.md`).
    """
    channel_root = Path(config["paths"]["channel_root"])
    video = channel_root / "05_exports" / "youtube" / f"{topic_id}_yt.mp4"
    thumb = channel_root / "04_renders" / "_thumbnails" / f"{topic_id}_thumbnail.png"
    metadata = channel_root / "02_scripts" / "_drafts" / topic_id / "metadata_RESPONSE.txt"

    required: list[tuple[str, Path]] = [("video", video), ("metadata", metadata)]
    if require_thumbnail:
        required.append(("thumbnail", thumb))
    missing = [(label, p) for label, p in required if not p.exists()]
    if missing:
        details = "\n".join(f"  - {label}: {p}" for label, p in missing)
        raise FileNotFoundError(f"required input(s) missing for {topic_id}:\n{details}")
    return video, (thumb if require_thumbnail else None), metadata


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _parse_publish_at(raw: str) -> datetime:
    """Parse + validate an RFC 3339 datetime string. Returns timezone-aware datetime in UTC.

    Accepts both 'Z' suffix and explicit timezone offset (e.g., '-04:00'). Rejects naive
    datetimes (no timezone) — YouTube API requires explicit timezone information.
    """
    if not _RFC3339_RE.match(raw):
        raise ValueError(
            f"--publish-at must be RFC 3339 with timezone (e.g., "
            f"'2026-05-08T18:45:00-04:00' or '2026-05-08T22:45:00Z'). Got: {raw!r}"
        )
    # Python's fromisoformat accepts 'Z' suffix as of 3.11; we're on 3.12.
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"--publish-at requires explicit timezone. Got naive datetime: {raw!r}")
    parsed_utc = parsed.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    if parsed_utc <= now_utc:
        raise ValueError(
            f"--publish-at must be in the future. Got {parsed_utc.isoformat()} "
            f"(now: {now_utc.isoformat()})"
        )
    return parsed_utc


def build_snippet_and_status(
    bundle: MetadataBundle,
    privacy: str,
    category_id: str,
    publish_at_utc: datetime | None = None,
) -> tuple[dict, dict]:
    """Build YouTube `snippet` and `status` request bodies from a parsed metadata bundle.

    When `publish_at_utc` is provided, the video is uploaded as scheduled-private:
    YouTube auto-flips privacy to public at that timestamp. Per the YouTube API,
    scheduled publishing requires `privacyStatus=private`, so we enforce that.
    """
    title = (bundle.youtube_title or "").strip()
    if not title:
        raise ValueError("metadata_RESPONSE.txt yielded an empty youtube_title; cannot upload")
    if len(title) > MAX_TITLE_CHARS:
        log.warning("title is %d chars; truncating to %d", len(title), MAX_TITLE_CHARS)
        title = title[:MAX_TITLE_CHARS]

    description = (bundle.youtube_description or "").strip()
    hashtags = " ".join(bundle.youtube_hashtags or [])
    # Append hashtags to the description if not already present, since YouTube
    # surfaces the first 3 hashtags from the description as clickable chips above
    # the title.
    if hashtags and hashtags not in description:
        description = (description.rstrip() + "\n\n" + hashtags).strip()
    if len(description) > MAX_DESC_CHARS:
        log.warning("description is %d chars; truncating to %d", len(description), MAX_DESC_CHARS)
        description = description[:MAX_DESC_CHARS]

    tags = [t for t in (bundle.youtube_tags or []) if t.strip()]

    snippet = {
        "title": title,
        "description": description,
        "tags": tags,
        "categoryId": category_id,
        "defaultLanguage": "en",
        "defaultAudioLanguage": "en",
    }
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
    }
    if publish_at_utc is not None:
        if privacy != "private":
            raise ValueError(
                f"--publish-at requires --privacy private (got: {privacy!r}). YouTube "
                "auto-flips the video from private to public at the publishAt time."
            )
        # YouTube Data API expects RFC 3339 with 'Z' suffix for UTC.
        status["publishAt"] = publish_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return snippet, status


def upload_video(youtube, video_path: Path, snippet: dict, status: dict) -> str:
    """Resumable upload with progress logging. Returns the new YouTube video_id."""
    size_mb = video_path.stat().st_size / 1e6
    log.info(
        "uploading %s (%.1f MB) — privacy=%s",
        video_path.name, size_mb, status["privacyStatus"],
    )
    media = MediaFileUpload(
        str(video_path),
        # chunksize=-1 uploads the whole file in a SINGLE request instead of
        # multiple 10 MiB chunks. The chunked resumable path triggers httplib2
        # `RedirectMissingLocation` on an inter-chunk 308 redirect, which errors
        # client-side AFTER the server has created a 0-duration broken video
        # (root cause of the 2026-06-19/20 duplicate/0-duration incident, which
        # was misattributed to concurrency). Single-shot avoids the inter-chunk
        # redirect entirely; safe for the channel's small (<50 MB) Shorts.
        # (fix 2026-06-20)
        chunksize=-1,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body={"snippet": snippet, "status": status},
        media_body=media,
        notifySubscribers=True,
    )
    response = None
    last_logged_progress = 0.0
    while response is None:
        # M2: num_retries gives each resumable chunk built-in backoff on transient
        # failures; combined with the transport timeout this stops a stalled upload
        # hanging an unattended run.
        chunk_status, response = request.next_chunk(num_retries=YT_API_NUM_RETRIES)
        if chunk_status is not None:
            progress = chunk_status.progress()
            # Log every 10% to keep stdout sane on tiny + huge files.
            if progress - last_logged_progress >= 0.10 or progress >= 1.0:
                log.info("upload progress: %.0f%%", progress * 100)
                last_logged_progress = progress
    if not response or "id" not in response:
        raise RuntimeError(f"upload failed: response missing 'id': {response}")
    return response["id"]


def set_thumbnail(youtube, video_id: str, thumbnail_path: Path) -> None:
    """Upload a custom thumbnail for a freshly-uploaded video."""
    size_kb = thumbnail_path.stat().st_size / 1024
    log.info("setting thumbnail for video %s — %s (%.1f KB)",
             video_id, thumbnail_path.name, size_kb)
    media = MediaFileUpload(str(thumbnail_path), mimetype="image/png", resumable=False)
    youtube.thumbnails().set(
        videoId=video_id, media_body=media
    ).execute(num_retries=YT_API_NUM_RETRIES)


def _upload_log_path(config: dict) -> Path:
    """Canonical path to the append-only upload audit log."""
    return Path(config["paths"]["channel_root"]) / "01_research" / "upload_log.csv"


def _find_existing_upload(config: dict, topic_id: str) -> dict | None:
    """Return the most recent upload_log.csv row for topic_id, or None.

    The pre-insert idempotency guard (H-1): a re-invoked uploader (resumable
    chunk-timeout retry, exit-3 confusion, manual re-run) would otherwise call
    videos().insert a second time and create a DUPLICATE scheduled video. This
    reads the same append-only log append_upload_log writes.

    Fail-soft toward ALLOWING the upload: a missing log means no prior upload; an
    unreadable log logs a WARNING and returns None. The guard must never BLOCK a
    legitimate first upload because the local log couldn't be read — the real
    risk it defends against (re-invoke after a successful write) always has a
    readable, freshly-written log.
    """
    log_path = _upload_log_path(config)
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8", newline="") as f:
            matches = [r for r in csv.DictReader(f) if r.get("topic_id") == topic_id]
    except OSError as e:
        log.warning("idempotency check: could not read %s (%s) — allowing upload", log_path, e)
        return None
    return matches[-1] if matches else None


def append_upload_log(
    config: dict, topic_id: str, video_id: str, privacy: str, title: str
) -> Path:
    """Record the upload in 01_research/upload_log.csv (append-only audit)."""
    log_path = _upload_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    new_file = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow([
                "uploaded_at", "topic_id", "video_id", "url", "privacy", "title",
            ])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            topic_id,
            video_id,
            f"https://www.youtube.com/watch?v={video_id}",
            privacy,
            title,
        ])
    return log_path


def _append_hook_selection_log(
    args: argparse.Namespace,
    config: dict,
) -> None:
    """Append the operator's hook selection to hook_selection_log.jsonl (audit L1).

    Reads `script_FINAL.txt` + `script_RESPONSE.txt` for the topic, reconciles
    which HOOK_A/B/C variant was shipped, and appends the result to
    `<channel_root>/01_research/hook_selection_log.jsonl`. The log writer is
    idempotent on `topic_id` (see hook_selection_log.append_to_log) so re-runs
    on the same topic are a no-op or overwrite-in-place.

    Skipped when:
      - ``args.no_hook_log`` is True (operator debugging escape hatch).
    The dry-run / unpublished-private skip is handled by `_run_post_upload_hooks`
    — this helper is invoked only on a path where the upload event is real.

    Fail-soft: any exception is logged + swallowed, never raised. The upload has
    already succeeded; the hook log is bookkeeping. This mirrors the
    archive/postmortem fail-soft pattern above.

    Addresses audit L1 (WORKFLOW_AUDIT_2026-05-16): before this, the writer was
    invoked only by `tools/backfill_hook_selections.py` and required a manual
    operator CLI run after each ship. The cycle-7 operator flag confirmed the
    gap was still costing hook A/B coverage.
    """
    if getattr(args, "no_hook_log", False):
        log.info("--no-hook-log: skipping hook_selection_log append")
        return

    channel_root = Path(config["paths"]["channel_root"])
    log_path = channel_root / "01_research" / "hook_selection_log.jsonl"

    try:
        chosen = extract_chosen_hook(args.topic_id, channel_root=channel_root)
        wrote = hook_append_to_log(chosen, log_path=log_path)
        if wrote:
            log.info(
                "appended hook selection for %s to %s (letter=%s formula=%s)",
                args.topic_id, log_path, chosen.hook_letter, chosen.formula,
            )
        else:
            log.info(
                "hook selection for %s already up-to-date in %s (no-op)",
                args.topic_id, log_path,
            )
    except Exception as exc:  # noqa: BLE001 — fail-soft for bookkeeping
        # Common causes: script_FINAL.txt missing (FileNotFoundError), file lock,
        # JSONL schema mismatch. Never block the upload result on any of these.
        log.warning(
            "post-upload hook_selection_log append failed for %s "
            "(upload itself succeeded): %s",
            args.topic_id, exc,
        )


def _run_post_upload_hooks(
    args: argparse.Namespace,
    config: dict,
    publish_at_utc: datetime | None,
) -> None:
    """Run bookkeeping hooks (archive + postmortem + hook-log) after a successful upload.

    Failures here log + continue — the upload itself already succeeded and is
    irreversible, so bookkeeping issues must NOT change the script's exit code or
    force the operator to re-upload.

    Hooks are skipped when:
      - ``args.dry_run`` is True (no real upload happened)
      - ``args.privacy == "private"`` AND ``publish_at_utc`` is None (the video
        isn't actually published yet, so archive + postmortem are premature).
    Hooks DO run for ``privacy=private`` when ``--publish-at`` is set, since
    YouTube auto-flips to public at that time.

    Order: archive first, then postmortem (postmortem may reference archive paths),
    then hook_selection_log (independent — last so its WARNING does not interleave
    with the more critical archive/postmortem output).
    """
    if args.dry_run:
        log.info("--dry-run: skipping post-upload hooks")
        return
    if args.privacy == "private" and publish_at_utc is None:
        log.info(
            "privacy=private without --publish-at: skipping post-upload hooks "
            "(video isn't actually published yet)"
        )
        return

    channel_root = Path(config["paths"]["channel_root"])
    project_root = Path(config["paths"]["project_root"])

    try:
        archived_paths = archive_topic(
            args.topic_id,
            channel_root=channel_root,
            skip_missing=True,
        )
        log.info("archived %d variant(s) to 06_published: %s",
                 len(archived_paths), archived_paths)
    except Exception as exc:  # noqa: BLE001 — fail-soft for bookkeeping
        log.error("post-upload archive failed (upload itself succeeded): %s", exc)

    try:
        postmortem_path = generate_postmortem(
            args.topic_id,
            channel_root=channel_root,
            project_root=project_root,
        )
        log.info("postmortem stub written to %s", postmortem_path)
    except Exception as exc:  # noqa: BLE001 — fail-soft for bookkeeping
        log.error(
            "post-upload postmortem stub failed (upload itself succeeded): %s", exc
        )

    # Audit L1: append the operator's hook selection to the leaderboard log.
    # Own helper so it can be tested + skipped independently of archive/postmortem.
    _append_hook_selection_log(args, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload a ShadowVerse video to YouTube via the official Data API. "
                    "Requires explicit per-video operator approval — see "
                    "feedback_youtube_upload_policy.md."
    )
    parser.add_argument(
        "--topic-id", required=True,
        help="Topic id (e.g. 2026-05-06_003). Resolves video / thumbnail / metadata paths.",
    )
    parser.add_argument(
        "--privacy", required=True, choices=ALLOWED_PRIVACY,
        help="YouTube privacyStatus for the uploaded video. Required — no default.",
    )
    parser.add_argument(
        "--category-id", default=DEFAULT_CATEGORY_ID,
        help=f"YouTube category id (default {DEFAULT_CATEGORY_ID} = Science & Technology).",
    )
    parser.add_argument(
        "--publish-at", default=None,
        help="Schedule the publish via YouTube's official scheduling. Accepts RFC 3339 "
             "datetime with timezone offset (e.g., '2026-05-08T18:45:00-04:00') or UTC "
             "'Z' suffix. Forces --privacy private; YouTube auto-flips to public at the "
             "specified time.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + verify all inputs without uploading. Useful for sanity-checking "
             "metadata parsing and OAuth scope before the real run.",
    )
    parser.add_argument(
        "--no-thumbnail", action="store_true",
        help="Upload the video without setting a custom thumbnail. YouTube will auto-select "
             "a cover frame. Required while the thumbnail-generation refactor is pending "
             "(see feedback_no_thumbnails.md).",
    )
    parser.add_argument(
        "--no-hook-log", action="store_true",
        help="Skip the post-upload hook_selection_log.jsonl append (audit L1, P5). "
             "Default behavior is to auto-append the operator's hook choice after a "
             "successful upload; this flag is a debugging escape hatch.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Override the idempotency guard: upload even if upload_log.csv already "
             "has a row for this topic_id (e.g. the prior upload was deleted). Default "
             "is to skip as a no-op so a re-invoked upload can't create a duplicate "
             "scheduled video.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    publish_at_utc = _parse_publish_at(args.publish_at) if args.publish_at else None

    config = load_config()
    video_path, thumbnail_path, metadata_path = find_paths(
        config, args.topic_id, require_thumbnail=not args.no_thumbnail,
    )

    metadata_text = metadata_path.read_text(encoding="utf-8")
    bundle = _parse_metadata_response(metadata_text, args.topic_id)
    snippet, status = build_snippet_and_status(
        bundle, args.privacy, args.category_id, publish_at_utc=publish_at_utc,
    )

    log.info("topic_id:    %s", args.topic_id)
    log.info("title:       %s", snippet["title"])
    log.info("description: %d chars", len(snippet["description"]))
    log.info("tags:        %d (%s)", len(snippet["tags"]),
             ", ".join(snippet["tags"][:5]) + ("…" if len(snippet["tags"]) > 5 else ""))
    log.info("category:    %s", snippet["categoryId"])
    log.info("privacy:     %s", status["privacyStatus"])
    if "publishAt" in status:
        log.info("publishAt:   %s (UTC) — YouTube will auto-flip to public at this time", status["publishAt"])
    log.info("video file:  %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)
    if thumbnail_path is not None:
        log.info("thumbnail:   %s (%.1f KB)", thumbnail_path.name, thumbnail_path.stat().st_size / 1024)
    else:
        log.info("thumbnail:   (skipped — YouTube will auto-select cover frame)")

    if args.dry_run:
        log.info("--dry-run: skipping upload")
        return 0

    # Idempotency guard (H-1): never double-publish. A prior upload of this
    # topic_id is recorded in upload_log.csv; re-invoking the uploader would
    # otherwise create a duplicate scheduled video. Skip as a no-op unless --force.
    if not args.force:
        existing = _find_existing_upload(config, args.topic_id)
        if existing is not None:
            existing_id = existing.get("video_id", "")
            existing_url = existing.get("url") or f"https://www.youtube.com/watch?v={existing_id}"
            log.warning(
                "topic %s already uploaded (video_id=%s, privacy=%s, at %s) per %s — "
                "skipping to avoid a duplicate. Re-run with --force to upload anyway.",
                args.topic_id, existing_id, existing.get("privacy"),
                existing.get("uploaded_at"), _upload_log_path(config),
            )
            print()
            print("=" * 60)
            print("Upload skipped — topic already uploaded (idempotency guard)")
            print(f"  topic_id:  {args.topic_id}")
            print(f"  video_id:  {existing_id}")
            print(f"  url:       {existing_url}")
            print(f"  uploaded:  {existing.get('uploaded_at')}")
            print("  (re-run with --force to upload again)")
            print("=" * 60)
            return 0

    creds = load_credentials()
    # M2: timeout'd authorized transport (http=) instead of credentials= so a
    # stalled socket can't hang the unattended upload. AuthorizedHttp carries the
    # creds, so credentials= must NOT also be passed (mutually exclusive).
    youtube = build("youtube", "v3", http=_timeout_http(creds))

    try:
        video_id = upload_video(youtube, video_path, snippet, status)
        url = f"https://www.youtube.com/watch?v={video_id}"
        log.info("upload complete; video_id=%s url=%s", video_id, url)

        if thumbnail_path is not None:
            try:
                set_thumbnail(youtube, video_id, thumbnail_path)
                log.info("thumbnail set successfully")
            except HttpError as e:
                log.error("thumbnail upload failed (video uploaded successfully): %s", e)
                log.error("you'll need to set the thumbnail manually in YouTube Studio")
        else:
            log.info("thumbnail upload skipped (--no-thumbnail); YouTube auto-selects cover")

        # Log the effective privacy (scheduled-private renders as 'scheduled (publishAt=...)' for clarity)
        log_privacy = (
            f"scheduled (publishAt={status['publishAt']})"
            if "publishAt" in status
            else args.privacy
        )
        log_path = append_upload_log(config, args.topic_id, video_id, log_privacy, snippet["title"])
        log.info("logged upload to %s", log_path)

        # Post-upload bookkeeping (archive variants + write postmortem stub).
        # Fail-soft: hook errors are logged but do not change the exit code.
        _run_post_upload_hooks(args, config, publish_at_utc)

        print()
        print("=" * 60)
        print("Upload complete")
        print(f"  topic_id:  {args.topic_id}")
        print(f"  video_id:  {video_id}")
        print(f"  url:       {url}")
        print(f"  title:     {snippet['title']}")
        print(f"  privacy:   {args.privacy}")
        if "publishAt" in status:
            print(f"  publishAt: {status['publishAt']} (UTC)")
            print(f"  scheduled: YouTube will auto-flip to public at the publishAt time")
        print("=" * 60)
        return 0
    except HttpError as e:
        log.error("YouTube API error: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
