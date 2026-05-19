"""Pillow-based thumbnail renderer for ShadowVerse YouTube Shorts.

Implements 3 of the 8 patterns from `prompts/library/thumbnail_patterns.md`:
  - big_text_claim   : 1-3 word massive claim, channel default
  - big_number       : huge violet number + small uppercase label below
  - crossed_out      : top term struck through with violet, recommendation below

The other 5 patterns (terminal_with_result, comparison_frame, tool_logo_spotlight,
question_hook, before_after_stack) can be added incrementally; the dispatcher
falls back to big_text_claim for unknown / unimplemented patterns and logs a
warning so we know to add them when first requested.

Brand constants kept in sync with `tools/make_channel_art.py` and the COVER
section of `prompts/06_metadata_generation.md`. Output is 1080×1920 PNG to match
the master / variants aspect — displays clean as a Shorts thumbnail and scales
gracefully on any other YouTube surface.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("thumbnail")

# Brand constants (kept in sync with tools/make_channel_art.py)
BG = (11, 15, 26)         # #0B0F1A dark slate
ACCENT = (124, 92, 255)   # #7C5CFF violet
INK = (245, 245, 250)     # near-white text
MUTED = (180, 180, 200)   # secondary text (channel handle)

FONT_BOLD = Path(r"C:\Windows\Fonts\segoeuib.ttf")  # Segoe UI Bold (upright)

W, H = 1080, 1920  # Shorts-aspect thumbnail dimensions


def _font(size: int) -> ImageFont.FreeTypeFont:
    if not FONT_BOLD.exists():
        raise FileNotFoundError(f"Required font missing: {FONT_BOLD}")
    return ImageFont.truetype(str(FONT_BOLD), size=size)


def _fit_font(
    text: str,
    max_width: int,
    max_height: int,
    *,
    max_size: int = 700,
) -> ImageFont.FreeTypeFont:
    """Largest font size where `text` fits within max_width × max_height."""
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    lo, hi = 20, max_size
    best = _font(lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(mid)
        bbox = draw.textbbox((0, 0), text, font=f)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_width and h <= max_height:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _draw_brand(draw: ImageDraw.ImageDraw) -> None:
    """Top-left brand stack: violet S + tiny channel handle."""
    f_mark = _font(140)
    draw.text((60, 60), "S", font=f_mark, fill=ACCENT)
    f_handle = _font(36)
    draw.text((60, 230), "@ShadowVerseTec", font=f_handle, fill=MUTED)


def render_big_text_claim(text: str, out_path: Path) -> Path:
    """Solid dark-slate background, 1-3 words massive bold text, violet underline.

    The default for our channel — Fireship-flavored, brand-consistent, fast to render.
    """
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw)

    target_w = int(W * 0.88)
    target_h = int(H * 0.55)
    f = _fit_font(text, target_w, target_h, max_size=520)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (W - tw) // 2 - bbox[0]
    ty = (H - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=f, fill=INK)

    underline_w = int(tw * 0.6)
    underline_x0 = (W - underline_w) // 2
    underline_y = ty + bbox[1] + th + 30
    draw.rectangle(
        [underline_x0, underline_y, underline_x0 + underline_w, underline_y + 12],
        fill=ACCENT,
    )

    img.save(out_path, "PNG")
    log.info("wrote big_text_claim: %s", out_path)
    return out_path


def render_big_number(number: str, label: str, out_path: Path) -> Path:
    """Single huge violet number on dark slate, with 2-3 word uppercase label below.

    Use for videos with a measurable claim ("11x FASTER", "$200 SAVED").
    """
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw)

    # Number takes most of the visual weight
    f_num = _fit_font(number, int(W * 0.78), int(H * 0.55), max_size=900)
    bbox_n = draw.textbbox((0, 0), number, font=f_num)
    nw, nh = bbox_n[2] - bbox_n[0], bbox_n[3] - bbox_n[1]

    label_upper = (label or "").upper().strip()
    if label_upper:
        f_lab = _fit_font(label_upper, int(W * 0.85), int(H * 0.10), max_size=160)
        bbox_l = draw.textbbox((0, 0), label_upper, font=f_lab)
        lw, lh = bbox_l[2] - bbox_l[0], bbox_l[3] - bbox_l[1]
    else:
        f_lab = None
        lw = lh = 0
        bbox_l = (0, 0, 0, 0)

    gap = 60 if label_upper else 0
    block_h = nh + gap + lh
    block_top = (H - block_h) // 2

    nx = (W - nw) // 2 - bbox_n[0]
    ny = block_top - bbox_n[1]
    draw.text((nx, ny), number, font=f_num, fill=ACCENT)

    if label_upper and f_lab is not None:
        lx = (W - lw) // 2 - bbox_l[0]
        ly = block_top + nh + gap - bbox_l[1]
        draw.text((lx, ly), label_upper, font=f_lab, fill=INK)

    img.save(out_path, "PNG")
    log.info("wrote big_number: %s", out_path)
    return out_path


def render_crossed_out(rejected: str, recommended: str, out_path: Path) -> Path:
    """Two-stack: rejected term struck through with a violet diagonal, recommendation below."""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw)

    f_top = _fit_font(rejected, int(W * 0.78), int(H * 0.22), max_size=440)
    bbox_t = draw.textbbox((0, 0), rejected, font=f_top)
    tw, th = bbox_t[2] - bbox_t[0], bbox_t[3] - bbox_t[1]

    f_bot = _fit_font(recommended, int(W * 0.85), int(H * 0.30), max_size=620)
    bbox_b = draw.textbbox((0, 0), recommended, font=f_bot)
    bw, bh = bbox_b[2] - bbox_b[0], bbox_b[3] - bbox_b[1]

    gap = 120
    block_h = th + gap + bh
    block_top = (H - block_h) // 2

    # Rejected term
    tx = (W - tw) // 2 - bbox_t[0]
    ty = block_top - bbox_t[1]
    draw.text((tx, ty), rejected, font=f_top, fill=INK)
    # Strikethrough as a thick diagonal violet line through the term's vertical center
    strike_y_center = ty + bbox_t[1] + th // 2
    pad = 30
    draw.line(
        [
            (tx + bbox_t[0] - pad, strike_y_center + th // 5),
            (tx + bbox_t[0] + tw + pad, strike_y_center - th // 5),
        ],
        fill=ACCENT,
        width=14,
    )

    # Recommended term
    bx = (W - bw) // 2 - bbox_b[0]
    by = block_top + th + gap - bbox_b[1]
    draw.text((bx, by), recommended, font=f_bot, fill=INK)
    # Violet underline beneath the recommendation
    underline_y = by + bbox_b[1] + bh + 25
    underline_w = int(bw * 0.7)
    underline_x = (W - underline_w) // 2
    draw.rectangle(
        [underline_x, underline_y, underline_x + underline_w, underline_y + 12],
        fill=ACCENT,
    )

    img.save(out_path, "PNG")
    log.info("wrote crossed_out: %s", out_path)
    return out_path


def render(pattern: str, text_overlay: str, out_path: Path) -> Path:
    """Top-level dispatcher used by `pipeline.generate_thumbnail`.

    Pattern semantics for non-trivial inputs:
      - big_number: text_overlay is `<number> <label>` (e.g., "11x FASTER");
        splits on first whitespace.
      - crossed_out: text_overlay is `<rejected> -> <recommended>` (e.g.,
        "pip -> uv"); falls back to a whitespace split if `->` is absent.
      - any unrecognized pattern: logs a warning and renders big_text_claim.
    """
    name = (pattern or "big_text_claim").strip().lower()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if name == "big_number":
        raw = text_overlay.strip()
        # Treat "/" as the explicit number/label separator when it sits adjacent
        # to whitespace or at the edge of the string ("0 / FOO", "99 /", "/ FOO").
        # A "/" inside a word like "100/sec FASTER" is left intact, and we fall
        # back to a whitespace split for the legacy "<number> <label>" form
        # (e.g., "11x FASTER").
        if re.search(r"\s/|/\s|^/|/$", raw):
            parts = re.split(r"\s*/\s*", raw, maxsplit=1)
            number = parts[0].strip() or "?"
            label = parts[1].strip() if len(parts) > 1 else ""
        else:
            parts = raw.split(None, 1)
            number = parts[0] if parts else "?"
            label = parts[1] if len(parts) > 1 else ""
        return render_big_number(number, label, out_path)

    if name == "crossed_out":
        if "->" in text_overlay:
            left, right = text_overlay.split("->", 1)
            rejected, recommended = left.strip(), right.strip()
        else:
            parts = text_overlay.strip().split(None, 1)
            rejected = parts[0] if parts else "?"
            recommended = parts[1] if len(parts) > 1 else rejected
        return render_crossed_out(rejected, recommended, out_path)

    if name != "big_text_claim":
        log.warning(
            "thumbnail pattern %r is not yet implemented in make_thumbnail.py — falling back to big_text_claim",
            name,
        )
    return render_big_text_claim(text_overlay, out_path)


def main() -> None:
    """CLI for offline testing — renders all 3 patterns to the channel's _thumbnails dir."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(r"C:\ContentOps\channels\ShadowVerse\04_renders\_thumbnails")
    out_dir.mkdir(parents=True, exist_ok=True)
    render("big_text_claim", "PROMPTS BELONG IN FILES.", out_dir / "test_big_text_claim.png")
    render("big_number", "11x FASTER", out_dir / "test_big_number.png")
    render("crossed_out", "pip -> uv", out_dir / "test_crossed_out.png")


if __name__ == "__main__":
    main()
