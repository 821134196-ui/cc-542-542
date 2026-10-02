"""本地亮度照片：用标准库生成 PNG（画面亮度随流明变化），无第三方依赖。"""
import struct
import zlib


def _clamp(v):
    return 0 if v < 0 else 255 if v > 255 else int(v)


def render_brightness_png(path, lumens, max_lumens=12000.0, label=""):
    """生成 240x90 的灰度光斑 PNG，亮度比例 = lumens / max_lumens。

    亮度不足时整体偏暗并偏红，正常时为暖白光，便于试映前后成对比对。
    """
    w, h = 240, 90
    ratio = max(0.0, min(1.0, lumens / max_lumens))
    low = ratio < 0.7
    cx, cy, r = w / 2, h / 2, h * 0.46
    base = 40 + 200 * ratio

    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            glow = max(0.0, 1.0 - d / r)
            v = _clamp(18 + base * glow)
            if low:
                rgb = (v, _clamp(v * 0.45), _clamp(v * 0.4))  # 暗红
            else:
                rgb = (v, _clamp(v * 0.92), _clamp(v * 0.72))  # 暖白/金
            row += bytes(rgb)
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")

    with open(path, "wb") as f:
        f.write(png)
    return path
