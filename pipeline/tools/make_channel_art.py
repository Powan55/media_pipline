"""
Generate ShadowVerse YouTube channel art (profile picture + banner) with Pillow.

No GPU, no model downloads. Pure 2D primitives over Segoe UI Black / Bold so
the result is deterministic and regenerable. If you later install ComfyUI +
Flux, replace this with an AI-rendered version; the brand constants here
match what the metadata bundles already use for cover frames.

Outputs:
    C:\\ContentOps\\channels\\ShadowVerse\\00_brand\\profile_picture.png  (800x800)
    C:\\ContentOps\\channels\\ShadowVerse\\00_brand\\banner.png            (2048x1152)
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brand")

BG = (11, 15, 26)         # #0B0F1A dark slate
ACCENT = (124, 92, 255)   # #7C5CFF violet (matches cover-frame metadata)
INK = (245, 245, 250)     # near-white

FONT_BOLD = Path(r"C:\Windows\Fonts\segoeuib.ttf")  # Segoe UI Bold (upright)

OUT_DIR = Path(r"C:\ContentOps\channels\ShadowVerse\00_brand")


def _require(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"required font missing: {path}")
    return path


def fit_font(text: str, font_path: Path, max_width: int, max_size: int = 400) -> ImageFont.FreeTypeFont:
    """Largest font size where `text` measures <= max_width pixels."""
    _require(font_path)
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    lo, hi = 10, max_size
    best = ImageFont.truetype(str(font_path), size=lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(str(font_path), size=mid)
        bbox = draw.textbbox((0, 0), text, font=f)
        if bbox[2] - bbox[0] <= max_width:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def make_profile_picture() -> Path:
    """800x800 profile: solid slate, single violet 'S' centered."""
    size = 800
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)

    f = ImageFont.truetype(str(_require(FONT_BOLD)), size=720)
    text = "S"
    bbox = draw.textbbox((0, 0), text, font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) // 2 - bbox[0]
    y = (size - h) // 2 - bbox[1]
    draw.text((x, y), text, font=f, fill=ACCENT)

    out = OUT_DIR / "profile_picture.png"
    img.save(out, "PNG")
    log.info("wrote %s (%dx%d)", out, size, size)
    return out


def make_banner() -> Path:
    """
    2048x1152 banner. All text fits inside the 1235x338 TV-safe area at the
    center, so it renders cleanly across mobile, tablet, desktop, and TV.
    """
    W, H = 2048, 1152
    safe_w = 1235  # YouTube TV-safe area width
    target_head_w = int(safe_w * 0.92)  # leave a margin

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    headline = "TACTICAL AI WORKFLOWS"
    subhead = "for devs who read the docs"

    f_head = fit_font(headline, FONT_BOLD, max_width=target_head_w, max_size=200)
    f_sub = ImageFont.truetype(str(_require(FONT_BOLD)), size=52)

    bbox_h = draw.textbbox((0, 0), headline, font=f_head)
    head_w = bbox_h[2] - bbox_h[0]
    head_h = bbox_h[3] - bbox_h[1]

    bbox_s = draw.textbbox((0, 0), subhead, font=f_sub)
    sub_w = bbox_s[2] - bbox_s[0]
    sub_h = bbox_s[3] - bbox_s[1]

    underline_thickness = 6
    gap_head_to_underline = 22
    gap_underline_to_sub = 28

    block_h = head_h + gap_head_to_underline + underline_thickness + gap_underline_to_sub + sub_h
    block_top = (H - block_h) // 2

    head_x = (W - head_w) // 2 - bbox_h[0]
    head_y = block_top - bbox_h[1]
    draw.text((head_x, head_y), headline, font=f_head, fill=INK)

    underline_y = block_top + head_h + gap_head_to_underline
    underline_w = int(head_w * 0.55)
    underline_x0 = (W - underline_w) // 2
    underline_x1 = underline_x0 + underline_w
    draw.rectangle(
        [underline_x0, underline_y, underline_x1, underline_y + underline_thickness],
        fill=ACCENT,
    )

    sub_y_top = underline_y + underline_thickness + gap_underline_to_sub
    sub_x = (W - sub_w) // 2 - bbox_s[0]
    sub_y = sub_y_top - bbox_s[1]
    draw.text((sub_x, sub_y), subhead, font=f_sub, fill=INK)

    out = OUT_DIR / "banner.png"
    img.save(out, "PNG")
    log.info("wrote %s (%dx%d)", out, W, H)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_profile_picture()
    make_banner()


if __name__ == "__main__":
    main()
