"""Lower-third caption style for the long-form (16:9) track.

The Shorts word-pop captions (96px, MarginV=540, per-word scale-bounce) are
illegible and visually frantic in landscape. Long-form uses a calmer lower-third
style: smaller font, a translucent box in the bottom third, one static line per
caption group. Reuses the proven faster-whisper transcription + line-packing from
caption_word_pop; only the ASS rendering differs. Validated in the Phase-0 spike.
"""
from __future__ import annotations

import logging
from pathlib import Path

from tools.caption_word_pop import (
    _escape_ass_text,
    _format_ass_time,
    pack_into_lines,
    transcribe_words,
    warn_if_fonts_missing,
)

log = logging.getLogger(__name__)

LF_FONT = "Arial"
LF_FONT_SIZE = 54        # legible on TV/desktop at 1080p (vs the 96px Shorts word-pop)
LF_MARGIN_V = 90         # lift into the lower third
LF_MARGIN_LR = 260       # keep lines off the frame edges


def render_lower_third_ass(
    lines,
    out_path: Path,
    *,
    video_width: int = 1920,
    video_height: int = 1080,
    font_name: str = LF_FONT,
    font_size: int = LF_FONT_SIZE,
    margin_v: int = LF_MARGIN_V,
    margin_lr: int = LF_MARGIN_LR,
) -> Path:
    """Render packed Lines as static lower-third ASS captions (one Dialogue/line)."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        # BorderStyle 3 = opaque box; BackColour ~70% black; Alignment 2 = bottom-center.
        f"Style: LF,{font_name},{font_size},&H00FFFFFF,&H00000000,&HB4000000,"
        f"-1,0,3,2,0,2,{margin_lr},{margin_lr},{margin_v}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for ln in lines:
        text = _escape_ass_text(" ".join(w.text for w in ln.words).strip())
        if not text:
            continue
        events.append(
            f"Dialogue: 0,{_format_ass_time(ln.start)},{_format_ass_time(ln.end)},"
            f"LF,,0,0,0,,{text}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def generate_lower_third_captions(vo_path: Path, script, config: dict) -> Path:
    """Stage 9 (long-form): transcribe VO -> pack lines -> lower-third ASS.

    Mirrors generate_captions' word_pop setup (model dir/name/compute, wip dir) but
    emits the lower-third style. Returns the path to the written .ass file.
    """
    if not vo_path.exists():
        raise FileNotFoundError(f"Voiceover audio not found: {vo_path}")
    cap = config.get("captions", {}) or {}
    model_dir = Path(config["paths"]["models"]) / "whisper"
    model_dir.mkdir(parents=True, exist_ok=True)

    warn_if_fonts_missing()
    log.info("captions: lower_third style — transcribing %s", vo_path.name)
    words = transcribe_words(
        vo_path,
        model_name=cap["whisper_model"],
        compute_type=cap["whisper_compute_type"],
        download_root=model_dir,
    )
    lines = pack_into_lines(words)
    log.info("captions: packed %d words into %d lower-third lines", len(words), len(lines))

    wip_dir = Path(config["paths"]["channel_root"]) / "04_renders" / "_wip" / script.topic_id
    wip_dir.mkdir(parents=True, exist_ok=True)
    captions_path = wip_dir / f"{script.topic_id}_captions.ass"

    res = config.get("render", {}).get("resolution") or [1920, 1080]
    render_lower_third_ass(
        lines, captions_path,
        video_width=int(res[0]), video_height=int(res[1]),
        font_name=cap.get("font_name", LF_FONT),
        font_size=int(cap.get("lower_third_font_size", LF_FONT_SIZE)),
        margin_v=int(cap.get("lower_third_margin_v", LF_MARGIN_V)),
    )
    log.info("captions: wrote lower-third %s", captions_path)
    return captions_path
