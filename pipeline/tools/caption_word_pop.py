"""Word-pop ASS caption generator for ShadowVerse Shorts (T3, R5 retention lever).

Replaces the static-block 1-3 word caption emitter in ``pipeline.generate_captions``
with word-by-word caption events: each word "pops" with a scale-bounce as it is
spoken (active word yellow + 96 px peak; inactive words white + 84 px). The
animation rides on libass overlay tags ``\\t(0,80,\\fscx125\\fscy125)\\t(80,140,
\\fscx100\\fscy100)``.

Visual spec sourced from ``audit_2026-05-07/phase2_finishing.md`` §1.7-§1.8 — the
Style/Pop block, MarginV=540, color hexes, and bounce timings are all anchored
there. Don't drift them without rereading that section.

Usage::

    python tools/caption_word_pop.py --audio path/to/vo.wav --out path/to/cap.ass
    python tools/caption_word_pop.py --audio vo.wav --out cap.ass --no-bounce

Importable surface (Stage 09 wiring in Wave 3 will import these)::

    from tools.caption_word_pop import (
        Word, Line,
        transcribe_words, pack_into_lines, render_ass,
    )

Whisper init flags ``condition_on_previous_text=False, vad_filter=False`` are
deliberate — see footgun in ``_pipeline/CLAUDE.md``. Don't revert.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    pass


log = logging.getLogger("caption_word_pop")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    """One whisper-emitted word timestamp.

    ``start`` and ``end`` are seconds from start of audio; ``text`` is the
    raw token (whisper sometimes emits leading whitespace — we strip on
    construction in :func:`transcribe_words`).
    """

    start: float
    end: float
    text: str


@dataclass
class Line:
    """A screen-fitting group of contiguous words rendered as a single caption.

    One libass ``Dialogue:`` event will be emitted per word in :attr:`words`,
    each one marking that word as the active (popping) word and the rest of the
    line as visible-but-inactive.
    """

    words: tuple[Word, ...] = field(default_factory=tuple)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def char_count(self) -> int:
        # Count the rendered string length (words joined by spaces).
        return sum(len(w.text) for w in self.words) + max(0, len(self.words) - 1)


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def transcribe_words(
    audio_path: Path,
    *,
    model_name: str = "large-v3",
    compute_type: str = "float16",
    language: str = "en",
    device: str = "cuda",
    download_root: Path | None = None,
    beam_size: int = 5,
) -> list[Word]:
    """Run faster-whisper with ``word_timestamps=True`` and return a flat list.

    Words are sorted by start time. Whisper occasionally emits empty or
    whitespace-only tokens at segment boundaries — those are skipped.

    The flags ``condition_on_previous_text=False`` and ``vad_filter=False``
    match the existing ``pipeline.generate_captions`` footgun-mitigation
    (see ``_pipeline/CLAUDE.md``); do not revert.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Voiceover audio not found: {audio_path}")

    # Lazy-import so the module is testable without the heavy dep.
    from faster_whisper import WhisperModel

    log.info(
        "loading faster-whisper model=%s compute_type=%s device=%s",
        model_name,
        compute_type,
        device,
    )
    kwargs: dict[str, object] = {"device": device, "compute_type": compute_type}
    if download_root is not None:
        kwargs["download_root"] = str(download_root)
    model = WhisperModel(model_name, **kwargs)

    log.info("transcribing %s (word_timestamps=True)", audio_path.name)
    segments, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        beam_size=beam_size,
        language=language,
        vad_filter=False,
        condition_on_previous_text=False,
    )

    words: list[Word] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            text = (w.word or "").strip()
            if not text:
                continue
            words.append(Word(start=float(w.start), end=float(w.end), text=text))

    words.sort(key=lambda w: w.start)
    duration_s = float(info.duration or 0.0)
    log.info(
        "transcription: %d words (lang=%s, duration=%.1fs)",
        len(words),
        info.language,
        duration_s,
    )
    if not words:
        raise RuntimeError(
            f"Whisper produced no word-level segments for {audio_path}. "
            "Check that the audio is non-silent."
        )
    # Sanity-check word density: a 30s narrated short typically yields >50
    # words (~1.67 wps). Anything below ~1.0 wps suggests whisper choked on
    # background noise or a non-speech track. Raise loudly per
    # `feedback_engineering_principles.md`.
    if duration_s >= 5.0:
        wps = len(words) / duration_s
        if wps < 1.0:
            raise RuntimeError(
                f"Word-level rate too low: {len(words)} words over "
                f"{duration_s:.1f}s = {wps:.2f} wps (<1.0). Check audio."
            )
    return words


# ---------------------------------------------------------------------------
# Line packing
# ---------------------------------------------------------------------------


def pack_into_lines(
    words: list[Word],
    *,
    max_words_per_line: int = 4,
    max_line_duration_s: float = 2.5,
    max_chars_per_line: int = 22,
    max_inter_word_gap_s: float = 0.6,
) -> list[Line]:
    """Group word timestamps into screen-fitting lines.

    A new line starts when any of these are true on the *next* word:

    1. ``max_words_per_line`` already reached on the current line.
    2. Adding the next word would exceed ``max_chars_per_line`` (rendered
       width of the line, words joined by single spaces).
    3. Adding the next word would push the line's duration past
       ``max_line_duration_s``.
    4. The inter-word silence gap exceeds ``max_inter_word_gap_s`` — natural
       breath / pause boundary.

    Empty input returns ``[]``. Single-word lines are allowed (e.g. when a
    word is itself >= ``max_chars_per_line``).
    """
    if not words:
        return []

    lines: list[Line] = []
    current: list[Word] = []

    def render_chars(buf: list[Word], extra: Word | None = None) -> int:
        items = buf if extra is None else [*buf, extra]
        return sum(len(w.text) for w in items) + max(0, len(items) - 1)

    def flush() -> None:
        if current:
            lines.append(Line(words=tuple(current)))
            current.clear()

    for w in words:
        if not current:
            current.append(w)
            continue

        gap = w.start - current[-1].end
        line_dur_with = w.end - current[0].start
        chars_with = render_chars(current, w)

        force_break = (
            len(current) >= max_words_per_line
            or chars_with > max_chars_per_line
            or line_dur_with > max_line_duration_s
            or gap > max_inter_word_gap_s
        )
        if force_break:
            flush()
        current.append(w)

    flush()
    return lines


# ---------------------------------------------------------------------------
# ASS rendering
# ---------------------------------------------------------------------------


def _format_ass_time(seconds: float) -> str:
    """ASS subtitle time format: ``H:MM:SS.cc`` (centiseconds).

    Mirrors ``pipeline._format_ass_time`` — same shape so existing libass
    consumers keep parsing identically.
    """
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:  # rounding edge — bump the second.
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Escape characters libass treats as override-block delimiters."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
    )


def _build_header(
    *,
    video_width: int,
    video_height: int,
    primary_font: str,
    fallback_chain: tuple[str, ...],
    font_size_inactive: int,
    font_size_active_settle: int,
    color_inactive: str,
    color_active: str,
    outline_px: int,
    margin_v: int,
) -> str:
    """Assemble [Script Info] + [V4+ Styles] + [Events] header.

    Two styles are declared: ``Default`` (white, inactive size) and ``Pop``
    (yellow, active settle size). Per-word Dialogue events ride the Default
    line and use ``\\rPop`` to re-skin the active word.

    libass parses Style fields as comma-separated, so the Fontname field
    holds ONLY the primary font. The full fallback chain is emitted as a
    comment line above the Style block AND used by libass at runtime via
    fontconfig (Windows: ``WINDOWSFONTDIR``; cross-platform: ``fonts.conf``).
    """
    # libass V4+ Style fields, in order:
    # Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,
    # BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing,
    # Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR,
    # MarginV, Encoding
    shadow_color = "&H4D000000"  # 70% translucent black drop shadow
    outline_color = "&H00000000"
    secondary = "&H000000FF"
    margin_l = 60
    margin_r = 60
    bold = -1  # libass: -1 = on, 0 = off
    style_default = (
        f"Style: Default,{primary_font},{font_size_inactive},"
        f"{color_inactive},{secondary},{outline_color},{shadow_color},"
        f"{bold},0,0,0,100,100,2,0,1,{outline_px},4,2,"
        f"{margin_l},{margin_r},{margin_v},1"
    )
    style_pop = (
        f"Style: Pop,{primary_font},{font_size_active_settle},"
        f"{color_active},{secondary},{outline_color},{shadow_color},"
        f"{bold},0,0,0,100,100,2,0,1,{outline_px},4,2,"
        f"{margin_l},{margin_r},{margin_v},1"
    )
    fallback_comment_chain = ",".join((primary_font, *fallback_chain))

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        f"; Font fallback chain (resolved by fontconfig at render): {fallback_comment_chain}\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_default}\n"
        f"{style_pop}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def _build_active_word_text(
    line: Line,
    active_idx: int,
    *,
    pop_attack_ms: int,
    pop_settle_ms: int,
    enable_bounce: bool,
    peak_scale_pct: int,
    settle_scale_pct: int,
) -> str:
    """Compose a Dialogue ``Text`` field with one popping active word.

    All inactive words on the line render in Default style (white, 84 px).
    The active word at ``active_idx`` is wrapped in ``{\\rPop ...}word{\\r}``
    so it picks up the Pop style (yellow, 96 px) plus an animated scale tween.

    With ``enable_bounce=False``, the ``\\t(...)`` scale tween is dropped —
    the active word still pops yellow but stays at settle size (used by the
    A/B test cycle on Day 23-24 of the 30/60/90 plan).
    """
    parts: list[str] = []
    for idx, word in enumerate(line.words):
        if idx == active_idx:
            if enable_bounce:
                t1_end = pop_attack_ms
                t2_end = pop_attack_ms + pop_settle_ms
                anim = (
                    f"\\rPop"
                    f"\\t(0,{t1_end},\\fscx{peak_scale_pct}\\fscy{peak_scale_pct})"
                    f"\\t({t1_end},{t2_end},"
                    f"\\fscx{settle_scale_pct}\\fscy{settle_scale_pct})"
                )
            else:
                anim = (
                    f"\\rPop\\fscx{settle_scale_pct}\\fscy{settle_scale_pct}"
                )
            parts.append("{" + anim + "}" + _escape_ass_text(word.text) + "{\\r}")
        else:
            parts.append(_escape_ass_text(word.text))
    return " ".join(parts)


def render_ass(
    lines: list[Line],
    out_path: Path,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    font_primary: str = "Montserrat Black",
    font_fallbacks: tuple[str, ...] = ("Anton", "Bebas Neue", "Impact", "Arial Black"),
    font_size_inactive: int = 84,
    font_size_active_peak: int = 110,
    font_size_active_settle: int = 96,
    color_inactive: str = "&H00FFFFFF",
    color_active: str = "&H0000E6FF",
    outline_px: int = 6,
    margin_v: int = 540,
    pop_attack_ms: int = 80,
    pop_settle_ms: int = 60,
    peak_scale_pct: int = 125,
    settle_scale_pct: int = 100,
    enable_bounce: bool = True,
    fade_in_ms: int = 40,
) -> Path:
    """Emit a libass-renderable ``.ass`` file with word-pop animation.

    For each :class:`Line` and each word index in that line, one ``Dialogue:``
    event is emitted spanning the active word's ``(start, end)`` interval.
    The active word pops in Pop style (yellow + 96 px settle) with a
    125% -> 100% scale tween; the rest of the line renders in Default style
    (white + 84 px) on the same event.

    Returns the path written.
    """
    if font_size_inactive <= 0:
        raise ValueError("font_size_inactive must be > 0")
    # The active word inherits the Pop style's font_size_active_settle baseline.
    # peak_scale_pct=125 + settle_scale_pct=100 yields the literal
    # \fscx125\fscy125 attack tag from the spec example in
    # phase2_finishing.md §1.8. font_size_active_peak is informational; the
    # actual peak on screen = font_size_active_settle * peak_scale_pct / 100.
    _ = font_size_active_peak  # informational — peak is via peak_scale_pct over Pop baseline

    header = _build_header(
        video_width=video_width,
        video_height=video_height,
        primary_font=font_primary,
        fallback_chain=font_fallbacks,
        font_size_inactive=font_size_inactive,
        font_size_active_settle=font_size_active_settle,
        color_inactive=color_inactive,
        color_active=color_active,
        outline_px=outline_px,
        margin_v=margin_v,
    )

    body: list[str] = []
    for line in lines:
        for idx, word in enumerate(line.words):
            text_field = _build_active_word_text(
                line,
                idx,
                pop_attack_ms=pop_attack_ms,
                pop_settle_ms=pop_settle_ms,
                enable_bounce=enable_bounce,
                peak_scale_pct=peak_scale_pct,
                settle_scale_pct=settle_scale_pct,
            )
            if fade_in_ms > 0:
                text_field = "{" + f"\\fad({fade_in_ms},0)" + "}" + text_field
            body.append(
                f"Dialogue: 0,"
                f"{_format_ass_time(word.start)},{_format_ass_time(word.end)},"
                f"Default,,0,0,0,,{text_field}"
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(body) + "\n", encoding="utf-8")
    log.info("wrote captions: %s (%d events across %d lines)",
             out_path, len(body), len(lines))
    return out_path


# ---------------------------------------------------------------------------
# Font availability check (warning-only)
# ---------------------------------------------------------------------------


_DEFAULT_WINDOWS_FONT_DIR = Path(r"C:\Windows\Fonts")


def warn_if_fonts_missing(
    font_dir: Path = _DEFAULT_WINDOWS_FONT_DIR,
    expected: tuple[str, ...] = ("Montserrat-Black.ttf", "Anton-Regular.ttf"),
) -> None:
    """Log a WARNING for any expected font file not found in ``font_dir``.

    Non-fatal: libass falls through the fallback chain (Impact / Arial Black
    are guaranteed on Windows). Day 3.2 of the 30/60/90 plan installs these.
    """
    if not font_dir.exists():
        log.warning("font dir %s does not exist; skipping font check", font_dir)
        return
    for name in expected:
        if not (font_dir / name).exists():
            log.warning(
                "font %s not installed in %s; libass will fall through fallback chain",
                name,
                font_dir,
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="caption_word_pop",
        description="Generate word-pop ASS captions from a voiceover WAV.",
    )
    ap.add_argument("--audio", required=True, type=Path, help="Path to input voiceover WAV.")
    ap.add_argument("--out", required=True, type=Path, help="Path to output .ass file.")
    ap.add_argument("--model", default="large-v3", help="faster-whisper model name.")
    ap.add_argument("--language", default="en", help="Language code passed to whisper.")
    ap.add_argument(
        "--no-bounce",
        action="store_true",
        help="Emit ASS without the scale-bounce tween (settle size only) for A/B testing.",
    )
    ap.add_argument(
        "--device",
        default="cuda",
        help="Whisper device (cuda/cpu).",
    )
    ap.add_argument(
        "--compute-type",
        default="float16",
        help="Whisper compute_type (float16/int8/etc).",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level name.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    warn_if_fonts_missing()

    try:
        words = transcribe_words(
            args.audio,
            model_name=args.model,
            compute_type=args.compute_type,
            language=args.language,
            device=args.device,
        )
    except FileNotFoundError as exc:
        log.error("audio not found: %s", exc)
        return 2
    except RuntimeError as exc:
        log.error("transcription failed: %s", exc)
        return 3

    lines = pack_into_lines(words)
    log.info("packed %d words into %d caption lines", len(words), len(lines))
    render_ass(lines, args.out, enable_bounce=not args.no_bounce)
    return 0


if __name__ == "__main__":
    sys.exit(main())
