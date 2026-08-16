#!/usr/bin/env python3
"""Render PhySc-agent banner PNG with PIL: pixel-exact block chars + gold gradient."""
from PIL import Image, ImageDraw, ImageFont

# Project's own block-char art (from src/phxsc/splash.py), two words side by side
PHXSC_ART = [
    "██████╗ ██╗  ██╗██╗   ██╗███████╗ ██████╗  █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██╔══██╗██║  ██║╚██╗ ██╔╝██╔════╝██╔════╝ ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "██████╔╝███████║ ╚████╔╝ ███████╗██║      ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    "██╔═══╝ ██╔══██║  ╚██╔╝  ╚════██║██║      ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    "██║     ██║  ██║   ██║   ███████║╚██████╗ ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    "╚═╝     ╚═╝  ╚═╝   ╚═╝   ╚══════╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ",
]
AGENT_ART = [
    "",
    "",
    "",
    "",
    "",
    "",
]
BANNER = [p.rstrip() + "  " + a.rstrip() for p, a in zip(PHXSC_ART, AGENT_ART)]

# Gold ramp: one color per two lines (bright gold → amber → bronze), row-uniform
GOLD_RAMP = ["#FFD700", "#FFD700", "#FFBF00", "#FFBF00", "#CD7F32", "#CD7F32"]
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"  # unused alias kept
FONT_SIZE = 22
GRID_W = 13  # integer grid per char: glyph 14px, 1px overlap covered by next paste
TAGLINE = "Local-first academic agent for physics & materials science research"
TAG_SIZE = 13

font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
tag_font = ImageFont.truetype(FONT_PATH, TAG_SIZE)

char_w = font.getlength("█")
bbox = font.getbbox("█")
block_h = bbox[3] - bbox[1]
line_h = block_h + 1  # 1px seam to avoid glyph collision

max_w = max(len(l) for l in BANNER)
art_w = max_w * GRID_W
art_h = len(BANNER) * line_h

PAD_X, PAD_TOP = 28, 26
W = art_w + PAD_X * 2
H = PAD_TOP + art_h + 18 + int(TAG_SIZE * 1.6) + 22

img = Image.new("RGB", (W, H))
draw = ImageDraw.Draw(img)

# Vertical background gradient #0B0E14 → #101826
top, bottom = (11, 14, 20), (16, 24, 38)
for y in range(H):
    t = y / max(H - 1, 1)
    col = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    draw.line([(0, y), (W, y)], fill=col)

# Art lines — render every glyph into the SAME fixed cell.
# Do NOT crop to the glyph bbox: bbox-cropping destroys the font's
# in-cell bearing and makes box-drawing characters drift relative to
# the 13px character grid.
GRID_W = 13
CELL_H = block_h
_glyph_cache: dict = {}
for i, row in enumerate(BANNER):
    color = GOLD_RAMP[i]
    y = PAD_TOP + i * line_h
    for ci, ch in enumerate(row):
        if ch == " ":
            continue

        gly = _glyph_cache.get(ch)
        if gly is None:
            # Keep the full cell. The font's original bearing/baseline is
            # preserved because the glyph is NOT cropped to getbbox().
            tmp = Image.new("L", (GRID_W, CELL_H), 0)
            ImageDraw.Draw(tmp).text((0, 0), ch, font=font, fill=255)
            gly = tmp
            _glyph_cache[ch] = gly

        img.paste(color, (PAD_X + ci * GRID_W, y), gly)

# Tagline
tag_y = PAD_TOP + art_h + 18
draw.text((PAD_X, tag_y), TAGLINE, font=tag_font, fill="#8B8682")

# Bottom gold accent bar
bar_y = H - 10
for x in range(PAD_X, W - PAD_X):
    t = (x - PAD_X) / max(W - PAD_X * 2 - 1, 1)
    col = tuple(int(205 + (255 - 205) * t) for _ in range(1))  # placeholder
    # fade #CD7F32 → #FFD700 left to right
    r = int(205 + (255 - 205) * t)
    g = int(127 + (215 - 127) * t)
    b = int(50 + (0 - 50) * t)
    draw.line([(x, bar_y), (x, bar_y + 1)], fill=(r, g, b))

import sys
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/phxsc-banner.png"
img.save(out, "PNG")
print(f"written {out}: {W}x{H} (char_w={char_w:.1f}, block_h={block_h}, line_h={line_h})")
