"""Unit tests for the pre-insert idempotency guard in tools/youtube_upload.py (H-1).

The guard reads the append-only upload_log.csv before videos().insert and skips
as a no-op when a row for the topic_id already exists — so a re-invoked uploader
(resumable chunk-timeout retry, exit-3 confusion, manual re-run) can't create a
DUPLICATE scheduled video. `--force` overrides it.

Coverage:
    _find_existing_upload (pure):
      - missing log              -> None
      - no matching topic        -> None
      - single match             -> the row
      - most-recent of duplicates-> the LAST row
      - unreadable log           -> None (fail-soft toward allowing the upload)
    main() guard:
      - existing row, no --force -> skip (upload_video NOT called), rc 0
      - existing row, --force    -> proceed (upload_video called)
      - no existing row          -> proceed (first upload allowed)
    CLI:
      - --force default false / present when set
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import youtube_upload  # noqa: E402

_HEADER = ["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"]


def _write_log(channel_root: Path, rows: list[list[str]]) -> Path:
    """Create <channel_root>/01_research/upload_log.csv with header + rows."""
    research = channel_root / "01_research"
    research.mkdir(parents=True, exist_ok=True)
    log_path = research / "upload_log.csv"
    with log_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for r in rows:
            w.writerow(r)
    return log_path


def _row(topic_id: str, video_id: str, when: str = "2026-06-18T22:00:00+00:00") -> list[str]:
    return [when, topic_id, video_id,
            f"https://www.youtube.com/watch?v={video_id}", "public", f"title for {video_id}"]


# ---------------------------------------------------------------------------
# _find_existing_upload — pure log inspection
# ---------------------------------------------------------------------------


class TestFindExistingUpload(unittest.TestCase):
    def test_missing_log_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = {"paths": {"channel_root": td}}
            self.assertIsNone(youtube_upload._find_existing_upload(config, "2026-06-18_001"))

    def test_no_matching_topic_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _write_log(Path(td), [_row("2026-06-18_001", "AAA"), _row("2026-06-18_002", "BBB")])
            config = {"paths": {"channel_root": td}}
            self.assertIsNone(youtube_upload._find_existing_upload(config, "2026-06-18_999"))

    def test_single_match_returns_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _write_log(Path(td), [_row("2026-06-18_001", "AAA"), _row("2026-06-18_002", "BBB")])
            config = {"paths": {"channel_root": td}}
            found = youtube_upload._find_existing_upload(config, "2026-06-18_002")
            self.assertIsNotNone(found)
            self.assertEqual(found["video_id"], "BBB")
            self.assertEqual(found["topic_id"], "2026-06-18_002")

    def test_most_recent_of_duplicates_returns_last(self) -> None:
        """If a topic somehow has two rows, the guard reports the LAST (newest) one."""
        with tempfile.TemporaryDirectory() as td:
            _write_log(Path(td), [
                _row("2026-06-18_001", "OLD", when="2026-06-18T10:00:00+00:00"),
                _row("2026-06-18_001", "NEW", when="2026-06-18T20:00:00+00:00"),
            ])
            config = {"paths": {"channel_root": td}}
            found = youtube_upload._find_existing_upload(config, "2026-06-18_001")
            self.assertEqual(found["video_id"], "NEW")

    def test_unreadable_log_returns_none_failsoft(self) -> None:
        """An unreadable log must NOT block a legitimate upload — returns None.

        Make upload_log.csv a directory so .open() raises OSError; the guard
        catches it, warns, and returns None (allow the upload)."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "01_research").mkdir(parents=True)
            (Path(td) / "01_research" / "upload_log.csv").mkdir()  # a dir, not a file
            config = {"paths": {"channel_root": td}}
            self.assertIsNone(youtube_upload._find_existing_upload(config, "2026-06-18_001"))


# ---------------------------------------------------------------------------
# main() guard behavior — the load-bearing assertion: no double-insert
# ---------------------------------------------------------------------------


def _patch_main_preamble(config: dict, video: Path, meta: Path):
    """Patch everything main() touches BEFORE the guard so we can drive it to
    the guard branch deterministically. Returns a list of patcher context mgrs."""
    snippet = {"title": "T", "description": "d", "tags": [], "categoryId": "28"}
    status = {"privacyStatus": "public"}
    return [
        mock.patch.object(youtube_upload, "load_config", return_value=config),
        mock.patch.object(youtube_upload, "find_paths", return_value=(video, None, meta)),
        mock.patch.object(youtube_upload, "_parse_metadata_response", return_value=mock.MagicMock()),
        mock.patch.object(youtube_upload, "build_snippet_and_status", return_value=(snippet, status)),
    ]


class TestMainGuard(unittest.TestCase):
    def test_existing_upload_skips_without_force(self) -> None:
        """A topic already in the log: upload_video and load_credentials never run; rc 0."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_log(tdp, [_row("2026-06-18_001", "ABC123")])
            video = tdp / "v.mp4"; video.write_bytes(b"x" * 2_000_000)
            meta = tdp / "m.txt"; meta.write_text("meta", encoding="utf-8")
            config = {"paths": {"channel_root": str(tdp), "project_root": str(tdp)}}
            argv = ["--topic-id", "2026-06-18_001", "--privacy", "public"]
            patches = _patch_main_preamble(config, video, meta)
            with patches[0], patches[1], patches[2], patches[3], \
                 mock.patch.object(youtube_upload, "load_credentials") as m_creds, \
                 mock.patch.object(youtube_upload, "upload_video") as m_upload:
                rc = youtube_upload.main(argv)
            self.assertEqual(rc, 0)
            m_upload.assert_not_called()   # the load-bearing assertion: no double-insert
            m_creds.assert_not_called()    # guard short-circuits before OAuth too

    def test_existing_upload_proceeds_with_force(self) -> None:
        """--force overrides the guard: the upload actually runs."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_log(tdp, [_row("2026-06-18_001", "ABC123")])
            video = tdp / "v.mp4"; video.write_bytes(b"x" * 2_000_000)
            meta = tdp / "m.txt"; meta.write_text("meta", encoding="utf-8")
            config = {"paths": {"channel_root": str(tdp), "project_root": str(tdp)}}
            argv = ["--topic-id", "2026-06-18_001", "--privacy", "public", "--force"]
            patches = _patch_main_preamble(config, video, meta)
            with patches[0], patches[1], patches[2], patches[3], \
                 mock.patch.object(youtube_upload, "load_credentials", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "_timeout_http", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "build", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "_run_post_upload_hooks"), \
                 mock.patch.object(youtube_upload, "upload_video", return_value="NEWVID") as m_upload:
                rc = youtube_upload.main(argv)
            self.assertEqual(rc, 0)
            m_upload.assert_called_once()

    def test_no_existing_row_allows_first_upload(self) -> None:
        """A topic NOT in the log uploads normally (guard must not block first uploads)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_log(tdp, [_row("2026-06-18_001", "ABC123")])  # different topic present
            video = tdp / "v.mp4"; video.write_bytes(b"x" * 2_000_000)
            meta = tdp / "m.txt"; meta.write_text("meta", encoding="utf-8")
            config = {"paths": {"channel_root": str(tdp), "project_root": str(tdp)}}
            argv = ["--topic-id", "2026-06-18_777", "--privacy", "public"]
            patches = _patch_main_preamble(config, video, meta)
            with patches[0], patches[1], patches[2], patches[3], \
                 mock.patch.object(youtube_upload, "load_credentials", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "_timeout_http", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "build", return_value=mock.MagicMock()), \
                 mock.patch.object(youtube_upload, "_run_post_upload_hooks"), \
                 mock.patch.object(youtube_upload, "upload_video", return_value="NEWVID") as m_upload:
                rc = youtube_upload.main(argv)
            self.assertEqual(rc, 0)
            m_upload.assert_called_once()


# ---------------------------------------------------------------------------
# CLI flag
# ---------------------------------------------------------------------------


class TestForceCLIFlag(unittest.TestCase):
    def _parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        p.add_argument("--topic-id", required=True)
        p.add_argument("--privacy", required=True, choices=("public", "unlisted", "private"))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--force", action="store_true")
        return p

    def test_force_default_false(self) -> None:
        ns = self._parser().parse_args(["--topic-id", "t", "--privacy", "public"])
        self.assertFalse(ns.force)

    def test_force_present_when_set(self) -> None:
        ns = self._parser().parse_args(["--topic-id", "t", "--privacy", "public", "--force"])
        self.assertTrue(ns.force)


if __name__ == "__main__":
    unittest.main()
