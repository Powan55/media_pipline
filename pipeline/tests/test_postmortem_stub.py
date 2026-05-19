"""Unit tests for tools/postmortem_stub.py.

All tests use tmp_path fixtures. The real ``Channels\\ShadowVerse\\postmortems\\``
is NEVER touched.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure the pipeline repo root is importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.postmortem_stub import (  # noqa: E402
    NOT_CAPTURED,
    PostmortemData,
    _SafeFormatDict,
    _extract_hook_formula,
    _extract_thumbnail_pattern,
    _extract_youtube_title,
    _lookup_upload_row,
    _render,
    generate_postmortem,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal scaffold of channel_root + project_root in tmp_path
# ---------------------------------------------------------------------------


def _make_master(channel_root: Path, topic_id: str) -> Path:
    """Create a non-empty placeholder master mp4 + QA marker for a topic."""
    final_master = channel_root / "04_renders" / "_final_master"
    final_master.mkdir(parents=True, exist_ok=True)
    master = final_master / f"{topic_id}_master.mp4"
    # Tests don't probe duration; ffprobe will fail on this stub and that's fine.
    master.write_bytes(b"\x00not-a-real-mp4")
    (final_master / f"{topic_id}_master_QA_APPROVED.marker").write_text("ok", encoding="utf-8")
    return master


def _make_metadata(channel_root: Path, topic_id: str, title: str, pattern: str) -> Path:
    """Create a metadata_RESPONSE.txt mimicking pipeline output."""
    topic_dir = channel_root / "02_scripts" / "_drafts" / topic_id
    topic_dir.mkdir(parents=True, exist_ok=True)
    metadata = topic_dir / "metadata_RESPONSE.txt"
    metadata.write_text(
        "## YOUTUBE SHORTS\n"
        f"Title: {title}\n"
        "Description: Whatever the description is.\n"
        "Tags: a, b, c\n"
        "Hashtags: #Shorts #AI\n"
        "\n"
        "## TIKTOK\n"
        "Caption: tt cap\n"
        "Hashtags: #x\n"
        "\n"
        "## INSTAGRAM REELS\n"
        "Caption: ig cap\n"
        "Hashtags: #y\n"
        "\n"
        "## COVER / THUMBNAIL CONCEPT\n"
        f"Pattern: {pattern}\n"
        "Text overlay: BIG TEXT\n"
        "Background: solid violet\n"
        "Accent color: violet\n",
        encoding="utf-8",
    )
    return metadata


def _make_script_final(channel_root: Path, topic_id: str, hook_formula: str | None) -> Path:
    """Create a script_FINAL.txt; include a hook_formula line if provided."""
    topic_dir = channel_root / "02_scripts" / "_drafts" / topic_id
    topic_dir.mkdir(parents=True, exist_ok=True)
    script = topic_dir / "script_FINAL.txt"
    body = "CHOSEN: HOOK_A\n\nSCRIPT:\n\nLine one of the script.\n"
    if hook_formula is not None:
        body = f"hook_formula: {hook_formula}\n\n" + body
    script.write_text(body, encoding="utf-8")
    return script


def _make_upload_log(
    channel_root: Path, rows: list[dict[str, str]],
) -> Path:
    """Create an upload_log.csv with the exact schema youtube_upload.py writes."""
    log_path = channel_root / "01_research" / "upload_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"])
        for r in rows:
            writer.writerow([
                r.get("uploaded_at", ""),
                r.get("topic_id", ""),
                r.get("video_id", ""),
                r.get("url", ""),
                r.get("privacy", ""),
                r.get("title", ""),
            ])
    return log_path


@pytest.fixture()
def scaffold(tmp_path: Path) -> dict[str, Path]:
    """Build a minimal channel_root + project_root tree for one topic."""
    channel_root = tmp_path / "channel"
    project_root = tmp_path / "project"
    (project_root / "Channels" / "ShadowVerse" / "postmortems").mkdir(parents=True)
    return {"channel_root": channel_root, "project_root": project_root}


# ---------------------------------------------------------------------------
# Acceptance criteria tests
# ---------------------------------------------------------------------------


def test_generate_with_template_substitutes(scaffold: dict[str, Path]) -> None:
    """AC2: when _TEMPLATE.md exists, treat as str.format() template."""
    topic_id = "2026-05-06_003"
    _make_master(scaffold["channel_root"], topic_id)
    _make_metadata(scaffold["channel_root"], topic_id, "Test Title", "big_number")
    _make_script_final(scaffold["channel_root"], topic_id, "PROBLEM_PROMISE_PROOF")

    template = (
        scaffold["project_root"] / "Channels" / "ShadowVerse" / "postmortems" / "_TEMPLATE.md"
    )
    template.write_text(
        "# Postmortem — {topic_id}\n"
        "Slug: {slug}\n"
        "Hook: {hook_formula}\n"
        "Pattern: {thumbnail_pattern}\n",
        encoding="utf-8",
    )

    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
    )
    text = out.read_text(encoding="utf-8")
    assert topic_id in text
    assert "Test Title" in text
    assert "PROBLEM_PROMISE_PROOF" in text
    assert "big_number" in text


def test_generate_falls_back_to_default_when_template_missing(
    scaffold: dict[str, Path],
) -> None:
    """AC3: with no _TEMPLATE.md, falls back to inline default template."""
    topic_id = "2026-05-07_001"
    _make_master(scaffold["channel_root"], topic_id)
    # No metadata, no script — slug should default to topic_id.

    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
    )
    text = out.read_text(encoding="utf-8")
    assert "Day 1 baseline" in text
    assert "What I tried" in text
    assert "What I'd do differently" in text
    assert topic_id in text
    # Slug fallback: when no metadata Title, slug == topic_id.
    assert f"**Slug:** {topic_id}" in text


def test_generate_pulls_upload_data_from_csv(scaffold: dict[str, Path]) -> None:
    """AC4: upload_log row populates upload_date + video_url."""
    topic_id = "2026-05-06_003"
    _make_master(scaffold["channel_root"], topic_id)
    _make_metadata(scaffold["channel_root"], topic_id, "T", "big_text_claim")
    _make_upload_log(
        scaffold["channel_root"],
        [{
            "uploaded_at": "2026-05-08T12:34:56+00:00",
            "topic_id": topic_id,
            "video_id": "abc123XYZ",
            "url": "https://www.youtube.com/watch?v=abc123XYZ",
            "privacy": "public",
            "title": "T",
        }],
    )

    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
    )
    text = out.read_text(encoding="utf-8")
    assert "abc123XYZ" in text
    assert "https://www.youtube.com/watch?v=abc123XYZ" in text
    # Upload date should be formatted, not the literal sentinel.
    assert "2026-05-08" in text
    assert NOT_CAPTURED not in text.split("**Upload date:**", 1)[1].split("\n", 1)[0]


def test_generate_handles_missing_csv_gracefully(scaffold: dict[str, Path]) -> None:
    """AC4: no upload_log.csv → upload_date / video_url render as not-yet-captured."""
    topic_id = "2026-05-07_001"
    _make_master(scaffold["channel_root"], topic_id)

    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
    )
    text = out.read_text(encoding="utf-8")
    # Upload-related lines should both render the not-captured sentinel.
    upload_line = [
        line for line in text.splitlines() if line.startswith("**Upload date:**")
    ]
    url_line = [
        line for line in text.splitlines() if line.startswith("**Video URL:**")
    ]
    assert upload_line and NOT_CAPTURED in upload_line[0]
    assert url_line and NOT_CAPTURED in url_line[0]


def test_generate_handles_csv_topic_id_not_found(scaffold: dict[str, Path]) -> None:
    """AC4: topic_id not in CSV → upload_date / video_url stay None."""
    topic_id = "2026-05-07_001"
    _make_master(scaffold["channel_root"], topic_id)
    _make_upload_log(
        scaffold["channel_root"],
        [{
            "uploaded_at": "2026-05-01T12:34:56+00:00",
            "topic_id": "some_other_topic",
            "video_id": "zzz",
            "url": "https://www.youtube.com/watch?v=zzz",
            "privacy": "public",
            "title": "irrelevant",
        }],
    )

    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
    )
    text = out.read_text(encoding="utf-8")
    assert "zzz" not in text


def test_refuses_overwrite_without_flag(scaffold: dict[str, Path]) -> None:
    """AC5: pre-existing target → FileExistsError unless overwrite=True."""
    topic_id = "2026-05-07_001"
    _make_master(scaffold["channel_root"], topic_id)

    target = (
        scaffold["project_root"]
        / "Channels" / "ShadowVerse" / "postmortems" / f"{topic_id}.md"
    )
    target.write_text("preexisting content", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate_postmortem(
            topic_id,
            channel_root=scaffold["channel_root"],
            project_root=scaffold["project_root"],
        )

    # With overwrite=True, succeeds and replaces content.
    out = generate_postmortem(
        topic_id,
        channel_root=scaffold["channel_root"],
        project_root=scaffold["project_root"],
        overwrite=True,
    )
    new_text = out.read_text(encoding="utf-8")
    assert "preexisting content" not in new_text
    assert topic_id in new_text


def test_raises_when_master_missing(scaffold: dict[str, Path]) -> None:
    """generate_postmortem refuses to run on a topic with no master mp4."""
    with pytest.raises(FileNotFoundError):
        generate_postmortem(
            "nonexistent_topic",
            channel_root=scaffold["channel_root"],
            project_root=scaffold["project_root"],
        )


def test_backfill_processes_all_marked_topics(scaffold: dict[str, Path]) -> None:
    """AC6: backfill walks QA markers and generates one postmortem per topic.

    Scaffold 3 topics, only 2 have QA markers → 2 postmortems generated.
    """
    channel_root = scaffold["channel_root"]
    project_root = scaffold["project_root"]
    final_master = channel_root / "04_renders" / "_final_master"

    # Topics A and B: with masters and markers
    for tid in ("2026-05-07_001", "2026-05-07_002"):
        _make_master(channel_root, tid)

    # Topic C: master exists but NO QA marker → not picked up by backfill
    final_master.mkdir(parents=True, exist_ok=True)
    (final_master / "2026-05-07_003_master.mp4").write_bytes(b"x")
    # (no marker file)

    # Run main with --backfill
    rc = main([
        "--backfill",
        "--channel-root", str(channel_root),
        "--project-root", str(project_root),
    ])
    assert rc == 0

    pm_dir = project_root / "Channels" / "ShadowVerse" / "postmortems"
    generated = sorted(p.name for p in pm_dir.glob("*.md") if not p.name.startswith("_"))
    assert generated == ["2026-05-07_001.md", "2026-05-07_002.md"]


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


def test_safe_format_dict_returns_sentinel_for_missing() -> None:
    """Unknown placeholder keys (e.g. weekly template's {title}) → NOT_CAPTURED."""
    fields = _SafeFormatDict({"topic_id": "X"})
    rendered = "Topic: {topic_id} / Title: {title}".format_map(fields)
    assert "Topic: X" in rendered
    assert NOT_CAPTURED in rendered


def test_render_substitutes_none_fields_with_sentinel() -> None:
    data = PostmortemData(
        topic_id="t1",
        slug="t1",
        render_date=datetime(2026, 5, 7, 10, 0, 0),
        upload_date=None,
        video_url=None,
        hook_formula=None,
        thumbnail_pattern=None,
        duration_s=None,
    )
    out = _render("URL={video_url}|Hook={hook_formula}|Dur={duration_s}", data)
    assert out == f"URL={NOT_CAPTURED}|Hook={NOT_CAPTURED}|Dur={NOT_CAPTURED}"


def test_extract_youtube_title(tmp_path: Path) -> None:
    p = tmp_path / "metadata_RESPONSE.txt"
    p.write_text(
        "## YOUTUBE SHORTS\nTitle: Hello World\nDescription: x\n",
        encoding="utf-8",
    )
    assert _extract_youtube_title(p) == "Hello World"


def test_extract_thumbnail_pattern(tmp_path: Path) -> None:
    p = tmp_path / "metadata_RESPONSE.txt"
    p.write_text(
        "## YOUTUBE SHORTS\nTitle: t\n\n## COVER / THUMBNAIL CONCEPT\nPattern: big_number\n",
        encoding="utf-8",
    )
    assert _extract_thumbnail_pattern(p) == "big_number"


def test_extract_hook_formula_present(tmp_path: Path) -> None:
    p = tmp_path / "script_FINAL.txt"
    p.write_text("hook_formula: PROBLEM_PROMISE_PROOF\n\nSCRIPT:\n", encoding="utf-8")
    assert _extract_hook_formula(p) == "PROBLEM_PROMISE_PROOF"


def test_extract_hook_formula_absent(tmp_path: Path) -> None:
    p = tmp_path / "script_FINAL.txt"
    p.write_text("CHOSEN: HOOK_A\n\nSCRIPT:\n", encoding="utf-8")
    assert _extract_hook_formula(p) is None


def test_lookup_upload_row_picks_latest(tmp_path: Path) -> None:
    """When CSV has multiple rows for one topic_id, latest row wins."""
    log = tmp_path / "upload_log.csv"
    with log.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"])
        w.writerow([
            "2026-05-01T12:00:00+00:00", "T", "id_old",
            "https://www.youtube.com/watch?v=id_old", "private", "T",
        ])
        w.writerow([
            "2026-05-08T12:00:00+00:00", "T", "id_new",
            "https://www.youtube.com/watch?v=id_new", "public", "T",
        ])

    upload_date, url = _lookup_upload_row(log, "T")
    assert url == "https://www.youtube.com/watch?v=id_new"
    assert upload_date == datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_lookup_upload_row_missing_file_returns_none(tmp_path: Path) -> None:
    upload_date, url = _lookup_upload_row(tmp_path / "does_not_exist.csv", "T")
    assert upload_date is None and url is None


def test_lookup_upload_row_unparseable_timestamp(tmp_path: Path) -> None:
    """Bad timestamp → upload_date stays None but url still extracted."""
    log = tmp_path / "upload_log.csv"
    with log.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uploaded_at", "topic_id", "video_id", "url", "privacy", "title"])
        w.writerow([
            "not-a-timestamp", "T", "id_x",
            "https://www.youtube.com/watch?v=id_x", "public", "T",
        ])
    upload_date, url = _lookup_upload_row(log, "T")
    assert url == "https://www.youtube.com/watch?v=id_x"
    assert upload_date is None
