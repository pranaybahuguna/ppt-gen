"""Draw each box's id onto a rendered slide picture (runs in the sandbox).

The vision model is shown this marked picture, so it can say "box 9 is a heading" and the
answer points at an exact shape. Boxes that belong to the first repeated item are drawn in
red; other labelled boxes in blue. Nothing about the slide itself changes.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from .validate import FALLBACK_FONTS


def _font(size):
    for f in FALLBACK_FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def mark(png_in: str, png_out: str, slide: dict, slide_size: dict, ids: list[int], item_ids: list[int]):
    im = Image.open(png_in).convert("RGB")
    W, H = im.size
    sx, sy = W / slide_size["w"], H / slide_size["h"]
    d = ImageDraw.Draw(im)
    font = _font(max(11, W // 90))
    for shp in slide["shapes"]:
        sid = int(shp["ref"].rsplit("shape:", 1)[1])
        if sid not in ids or not shp.get("geom"):
            continue
        g = shp["geom"]
        box = [g["x"] * sx, g["y"] * sy, (g["x"] + g["w"]) * sx, (g["y"] + max(g["h"], 1)) * sy]
        col = (215, 30, 30) if sid in item_ids else (35, 85, 215)
        d.rectangle(box, outline=col, width=2)
        label = str(sid)
        tw = d.textlength(label, font=font)
        x, y = max(0, box[2] - tw - 4), box[1]
        d.rectangle([x - 2, y, x + tw + 2, y + font.size + 3], fill=col)
        d.text((x, y), label, font=font, fill="white")
    im.save(png_out)
    return png_out
