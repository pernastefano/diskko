"""Generate the app icons into app/web/static/icons using only the standard library.

A disc: a ring with a dot in the middle, on the dark graphite of the theme (the same style as
TRAKKO's "T with a dot"). Usage: python scripts/generate_icons.py
"""

import struct
import zlib
from pathlib import Path

ICONS_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "icons"

BACKGROUND = (0x1F, 0x23, 0x27)
FOREGROUND = (0xED, 0xE9, 0xE2)
ACCENT = (0xF2, 0xA4, 0x31)
CORNER_RADIUS = 0.22
RING_OUTER, RING_INNER, DOT = 0.30, 0.20, 0.075  # fractions of the icon: inside the safe zone
SUPERSAMPLING = 3

# (file name, size in px, shape)
ICONS = [
    ("icon-192.png", 192, "rounded"),
    ("icon-512.png", 512, "rounded"),
    ("icon-maskable-512.png", 512, "full"),
    ("apple-touch-icon.png", 180, "full"),
]


def inside_rounded_square(u: float, v: float) -> bool:
    cx = min(max(u, CORNER_RADIUS), 1 - CORNER_RADIUS)
    cy = min(max(v, CORNER_RADIUS), 1 - CORNER_RADIUS)
    return (u - cx) ** 2 + (v - cy) ** 2 <= CORNER_RADIUS**2


def shade(u: float, v: float, shape: str) -> tuple[int, int, int, int]:
    """RGBA at (u, v) in [0, 1]."""
    if shape == "rounded" and not inside_rounded_square(u, v):
        return 0, 0, 0, 0
    distance2 = (u - 0.5) ** 2 + (v - 0.5) ** 2
    if distance2 <= DOT**2:
        return (*ACCENT, 255)
    if RING_INNER**2 <= distance2 <= RING_OUTER**2:
        return (*FOREGROUND, 255)
    return (*BACKGROUND, 255)


def render(size: int, shape: str) -> bytes:
    rows = []
    offsets = [(i + 0.5) / SUPERSAMPLING for i in range(SUPERSAMPLING)]
    samples = SUPERSAMPLING**2
    for y in range(size):
        row = bytearray([0])  # PNG filter type 0
        for x in range(size):
            r = g = b = a = 0
            for dy in offsets:
                for dx in offsets:
                    sr, sg, sb, sa = shade((x + dx) / size, (y + dy) / size, shape)
                    r, g, b, a = r + sr * sa, g + sg * sa, b + sb * sa, a + sa
            if a == 0:
                row += bytes(4)
            else:
                row += bytes([round(r / a), round(g / a), round(b / a), round(a / samples)])
        rows.append(bytes(row))
    return b"".join(rows)


def chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def write_png(path: Path, size: int, shape: str) -> None:
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(render(size, shape), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, shape in ICONS:
        write_png(ICONS_DIR / name, size, shape)
        print(f"wrote {name} ({size}x{size}, {shape})")


if __name__ == "__main__":
    main()
