"""
style.py — loads style.json (colours, fonts, hook text, extras) for cut.py and captions.py.

Edit style.json to change the look; anything missing there falls back to DEFAULTS.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "layout": "blur",
    "accent": "#4DA8FF",           # Helix cyan
    "tint": "#1B2A5E",             # Helix navy, laid over the blurred background
    "font": "fonts/Montserrat-ExtraBold.ttf",
    "font_heavy": "fonts/Montserrat-Black.ttf",
    "caption_font_name": "Montserrat Black",
    "caption_size": 100,
    "captions_upper": True,
    "caption_highlight": True,     # the word being spoken lights up in the accent colour
    "hook_before": "WAIT FOR IT...",
    "hook_after": "CHAT WENT CRAZY",
    "brand_name": "",
    "logo": "",                    # PNG with transparency, shown at the top of every clip
    "logo_width": 560,
    "logo_y": 205,
    "watermark": "",
    "progress_bar": True,
    "fade": True,
    # Crystal render
    "crf": 17,                     # lower = sharper, bigger file (16–19 is the sweet spot)
    "preset": "slow",              # x264 effort: slow = better quality per MB than veryfast
    "max_bitrate": "20M",          # cap that TikTok / Reels / Shorts handle well
    "loudness_lufs": -14,          # loudness target for phones
    "save_clean_version": False,   # also render a copy without captions into work/
}


def load_style(path: Optional[str] = None) -> dict:
    p = Path(path) if path else HERE / "style.json"
    style = dict(DEFAULTS)
    if p.exists():
        style.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items()
                      if not k.startswith("_")})
    for key in ("font", "font_heavy", "logo"):
        f = Path(style[key]) if style.get(key) else None
        if f is not None and not f.is_absolute():
            f = HERE / f
        style[key] = str(f) if f is not None and f.exists() else None
    return style


def _rgb(color: str):
    c = color.strip().lstrip("#")
    if len(c) != 6:
        raise ValueError(f"colour must look like #RRGGBB, got {color!r}")
    return c[0:2].upper(), c[2:4].upper(), c[4:6].upper()


def ff_color(color: str) -> str:
    """#4DA8FF -> 0x4DA8FF (ffmpeg)"""
    r, g, b = _rgb(color)
    return f"0x{r}{g}{b}"


def ass_color(color: str) -> str:
    """#4DA8FF -> &H00FFA84D& (ASS is BGR)"""
    r, g, b = _rgb(color)
    return f"&H00{b}{g}{r}&"
