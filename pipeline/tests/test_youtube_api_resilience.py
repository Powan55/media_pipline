"""Resilience tests for the YouTube Data/Analytics calls (WORKFLOW_AUDIT_2026-05-31 M2).

Before M2 neither `analytics_pull.py` nor `tools/youtube_upload.py` passed any
resilience knob: `.execute()` / `.next_chunk()` ran with no retries and the
services were built on the default httplib2 transport with NO socket timeout, so
a stalled connection could hang an unattended `/start -auto` run indefinitely.

M2 adds:
  - ``num_retries`` (googleapiclient built-in backoff) on every `.execute()` and
    the resumable `.next_chunk()`;
  - a wall-clock socket timeout via ``build(..., http=AuthorizedHttp(creds,
    http=httplib2.Http(timeout=...)))`` (the thing that actually prevents a hang —
    `.execute()` has no `timeout=` kwarg).

These tests assert num_retries is passed at representative analytics + upload
callsites, and that the http transport handed to ``build`` carries a non-None
timeout. No new pip dep (google-api-python-client / google-auth-httplib2 /
httplib2 are already installed).

Run:
    python -m pytest tests/test_youtube_api_resilience.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import analytics_pull  # noqa: E402
from tools import youtube_upload  # noqa: E402


# ---------------------------------------------------------------------------
# num_retries is passed at the API callsites
# ---------------------------------------------------------------------------


class _RecordingRequest:
    """Stands in for a googleapiclient HttpRequest; records .execute kwargs."""

    def __init__(self, sink: dict, result):
        self._sink = sink
        self._result = result

    def execute(self, **kwargs):
        self._sink.update(kwargs)
        return self._result


def test_upload_thumbnail_execute_uses_num_retries(tmp_path) -> None:
    """set_thumbnail()'s .execute() is called with num_retries >= 1."""
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"x" * 2048)

    sink: dict = {}
    fake_set = mock.MagicMock(return_value=_RecordingRequest(sink, {"ok": True}))
    fake_thumbnails = mock.MagicMock()
    fake_thumbnails.set = fake_set
    youtube = mock.MagicMock()
    youtube.thumbnails.return_value = fake_thumbnails

    youtube_upload.set_thumbnail(youtube, "vid123", thumb)
    assert sink.get("num_retries", 0) >= 1


def test_analytics_channels_execute_uses_num_retries() -> None:
    """An analytics-side Data call (_list_uploaded_videos channel lookup) passes
    num_retries on its .execute()."""
    from datetime import date

    sink: dict = {}
    fake_list = mock.MagicMock(
        return_value=_RecordingRequest(
            sink, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UP1"}}}]}
        )
    )
    yt_data = mock.MagicMock()
    yt_data.channels.return_value.list = fake_list
    # Make the playlist walk return immediately (empty page, no nextPageToken).
    yt_data.playlistItems.return_value.list.return_value = _RecordingRequest({}, {"items": []})

    analytics_pull._list_uploaded_videos(yt_data, "CHAN", date(2026, 1, 1))
    assert sink.get("num_retries", 0) >= 1


# ---------------------------------------------------------------------------
# build() is given a transport carrying a non-None wall-clock timeout
# ---------------------------------------------------------------------------


def test_timeout_http_helper_carries_nonnull_timeout() -> None:
    """_timeout_http wraps an httplib2.Http with a concrete socket timeout."""
    creds = mock.MagicMock()
    for mod in (analytics_pull, youtube_upload):
        authed = mod._timeout_http(creds)
        # AuthorizedHttp stores the underlying transport on .http.
        inner = getattr(authed, "http", None)
        assert inner is not None, f"{mod.__name__}: no inner http transport"
        assert getattr(inner, "timeout", None) is not None, (
            f"{mod.__name__}: transport timeout must be set, got None"
        )
        assert inner.timeout > 0


def test_analytics_build_uses_timeout_http() -> None:
    """pull_youtube_analytics builds services with http= (timeout'd) and not a
    bare credentials= default transport."""
    captured: list[dict] = []

    def _fake_build(serviceName, version, **kwargs):
        captured.append(kwargs)
        svc = mock.MagicMock()
        # Stop after the build by returning no videos from the uploads walk.
        svc.channels.return_value.list.return_value = _RecordingRequest(
            {}, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UP1"}}}]}
        )
        svc.playlistItems.return_value.list.return_value = _RecordingRequest({}, {"items": []})
        return svc

    creds = mock.MagicMock()
    from datetime import date

    with mock.patch.object(analytics_pull, "build", _fake_build):
        analytics_pull.pull_youtube_analytics(creds, "CHAN", date(2026, 1, 1))

    assert captured, "build was never called"
    for kwargs in captured:
        assert "credentials" not in kwargs, "must not pass credentials= alongside http="
        http = kwargs.get("http")
        assert http is not None, "build must receive an http= transport"
        inner = getattr(http, "http", None)
        assert getattr(inner, "timeout", None) is not None


def test_upload_build_call_passes_timeout_http_not_credentials() -> None:
    """The upload module's build() call must hand build() the timeout'd http=
    transport and NOT a bare credentials= (which would use the no-timeout
    default transport). Asserted on the source of main() to pin the wiring."""
    import inspect

    src = inspect.getsource(youtube_upload.main)
    # The Data service must be built via the timeout'd transport helper.
    assert 'http=_timeout_http(creds)' in src, (
        "youtube_upload.main must build the service with http=_timeout_http(creds)"
    )
    # And must not regress to credentials= on that build line.
    assert 'build("youtube", "v3", credentials=creds)' not in src
