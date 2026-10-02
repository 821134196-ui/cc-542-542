"""亮度照片：试映前后各拍一张，存本地 data/photos/，以 SVG 模拟银幕测光画面。"""
from __future__ import annotations

import os

from .adapter import MIN_BRIGHTNESS_FL, NEW_LAMP_BRIGHTNESS_FL

_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
  <rect width="320" height="200" fill="#050507"/>
  <rect x="30" y="24" width="260" height="130" rx="4" fill="{screen}" stroke="#2a2a33"/>
  <rect x="30" y="24" width="260" height="130" rx="4" fill="url(#g)"/>
  <defs><radialGradient id="g" cx="50%" cy="50%" r="75%">
    <stop offset="0%" stop-color="#ffffff" stop-opacity="{glow}"/>
    <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>
  </radialGradient></defs>
  <text x="160" y="180" fill="{label_color}" font-family="monospace" font-size="15"
        text-anchor="middle">{label} · {fl:.1f} fL</text>
</svg>
"""


def _screen_color(fl: float) -> tuple[str, str, str]:
    """按亮度给出银幕底色、光晕透明度、标签色。"""
    ratio = max(0.0, min(1.0, fl / NEW_LAMP_BRIGHTNESS_FL))
    v = int(40 + 200 * ratio)          # 40..240 灰阶
    glow = round(0.15 + 0.55 * ratio, 2)
    color = f"#{v:02x}{v:02x}{v - 8 if v > 8 else 0:02x}"
    label = "#3fb950" if fl >= MIN_BRIGHTNESS_FL else "#e5484d"
    return color, str(glow), label


def save_brightness_photo(photos_dir: str, name: str, fl: float, label: str) -> str:
    """生成一张亮度照片，返回相对路径（photos/xxx.svg）。"""
    os.makedirs(photos_dir, exist_ok=True)
    color, glow, label_color = _screen_color(fl)
    fname = f"{name}.svg"
    with open(os.path.join(photos_dir, fname), "w", encoding="utf-8") as f:
        f.write(_SVG.format(screen=color, glow=glow, label_color=label_color,
                            label=label, fl=fl))
    return f"photos/{fname}"
