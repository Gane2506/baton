"""Abstract geometric logo-mark generator (stdlib only — no Pillow).

`generate_mark(seed, palette)` picks one of five templates deterministically from
the seed hash and returns a list of primitives in a 100x100 coordinate space.
`mark_to_svg` renders them as a standalone SVG; `mark_to_png` rasterizes them
with a minimal stdlib PNG encoder (zlib + struct, RGBA, non-interlaced) using
2x supersampling for antialiasing.

The mark never contains text: there is no text primitive, and the wordmark is
styled text (font stacks) documented in the design-system markdown instead.
"""

from __future__ import annotations

import binascii
import hashlib
import math
import struct
import zlib
from dataclasses import dataclass

SPACE = 100.0  # primitives live in a 100x100 box
_SUPERSAMPLE = 2  # rasterize at 2x, then box-average down for antialiasing


@dataclass(frozen=True)
class Circle:
    """circle(cx, cy, r, fill): a filled circle."""

    cx: float
    cy: float
    r: float
    fill: str


@dataclass(frozen=True)
class RoundedRect:
    """rounded_rect(x, y, w, h, r, fill): a filled rectangle with corner radius r."""

    x: float
    y: float
    w: float
    h: float
    r: float
    fill: str


@dataclass(frozen=True)
class Ring:
    """ring(cx, cy, r, stroke, fill): a circle outline; `fill` is the stroke color."""

    cx: float
    cy: float
    r: float
    stroke: float
    fill: str


@dataclass(frozen=True)
class Triangle:
    """A filled triangle (used only by the apex template)."""

    x1: float
    y1: float
    x2: float
    y2: float
    x3: float
    y3: float
    fill: str


Primitive = Circle | RoundedRect | Ring | Triangle

_TEMPLATES: tuple[str, ...] = ("rings", "orbit", "signal", "bloom", "apex")


def mark_template(seed: str) -> str:
    """Which of the five templates `generate_mark` picks for this seed."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return _TEMPLATES[digest[0] % len(_TEMPLATES)]


def _tint(hex_color: str) -> str:
    """Mix a `#RRGGBB` color 45% toward white for a lighter accent."""
    text = hex_color.strip().lstrip("#")
    r = int(text[0:2], 16)
    g = int(text[2:4], 16)
    b = int(text[4:6], 16)

    def mix(v: int) -> int:
        return round(v + (255 - v) * 0.45)

    return f"#{mix(r):02X}{mix(g):02X}{mix(b):02X}"


def _build_rings(primary: str, secondary: str, accent: str, digest: bytes) -> list[Primitive]:
    jitter = (digest[1] % 7) - 3
    return [
        Ring(50, 50, 36 + jitter, 8, primary),
        Ring(50, 50, 23 + jitter, 6, secondary),
        Circle(50, 50, 10, accent),
    ]


def _build_orbit(primary: str, secondary: str, accent: str, digest: bytes) -> list[Primitive]:
    jitter = (digest[1] % 9) - 4
    return [
        Ring(50, 50, 34, 4, secondary),
        Circle(50, 50, 21, primary),
        Circle(74 + jitter, 28 - jitter, 9, accent),
    ]


def _build_signal(primary: str, secondary: str, accent: str, digest: bytes) -> list[Primitive]:
    bars: list[Primitive] = []
    for i, height in enumerate((22, 34, 46, 58)):
        bars.append(
            RoundedRect(
                16 + i * 17,
                82 - height,
                12,
                height,
                6,
                primary if i % 2 == 0 else secondary,
            )
        )
    return bars


def _build_bloom(primary: str, secondary: str, accent: str, digest: bytes) -> list[Primitive]:
    return [
        RoundedRect(22, 22, 56, 56, 18, primary),
        Circle(50, 50, 11, accent),
    ]


def _build_apex(primary: str, secondary: str, accent: str, digest: bytes) -> list[Primitive]:
    return [
        Triangle(50, 12, 88, 84, 12, 84, primary),
        Triangle(50, 42, 70, 76, 30, 76, accent),
    ]


_BUILDERS = {
    "rings": _build_rings,
    "orbit": _build_orbit,
    "signal": _build_signal,
    "bloom": _build_bloom,
    "apex": _build_apex,
}


def generate_mark(seed: str, palette: dict[str, str]) -> list[Primitive]:
    """Build the abstract mark for `seed`: one of five geometric templates chosen
    deterministically by the seed hash, colored from the palette.

    `palette` needs at least "primary" (falls back to a default blue); the
    accent is a lightened tint of the primary. The same seed always yields the
    same mark.
    """
    name = mark_template(seed)
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    primary = palette.get("primary") or "#2563EB"
    secondary = palette.get("primary_hover") or primary
    accent = _tint(primary)
    return _BUILDERS[name](primary, secondary, accent, digest)


def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _svg_shape(primitive: Primitive) -> str:
    if isinstance(primitive, Circle):
        return (
            f'<circle cx="{_num(primitive.cx)}" cy="{_num(primitive.cy)}" '
            f'r="{_num(primitive.r)}" fill="{primitive.fill}"/>'
        )
    if isinstance(primitive, RoundedRect):
        return (
            f'<rect x="{_num(primitive.x)}" y="{_num(primitive.y)}" '
            f'width="{_num(primitive.w)}" height="{_num(primitive.h)}" '
            f'rx="{_num(primitive.r)}" fill="{primitive.fill}"/>'
        )
    if isinstance(primitive, Ring):
        return (
            f'<circle cx="{_num(primitive.cx)}" cy="{_num(primitive.cy)}" '
            f'r="{_num(primitive.r)}" fill="none" '
            f'stroke="{primitive.fill}" stroke-width="{_num(primitive.stroke)}"/>'
        )
    return (
        f'<polygon points="{_num(primitive.x1)},{_num(primitive.y1)} '
        f"{_num(primitive.x2)},{_num(primitive.y2)} "
        f'{_num(primitive.x3)},{_num(primitive.y3)}" fill="{primitive.fill}"/>'
    )


def mark_to_svg(primitives: list[Primitive], size: int = 512) -> str:
    """The mark as a clean standalone SVG (`size` x `size` px, 100x100 viewBox)."""
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    body = "\n".join("  " + _svg_shape(p) for p in primitives)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 100 100" role="img">\n{body}\n</svg>\n'
    )


def _parse_hex(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _bbox(primitive: Primitive) -> tuple[float, float, float, float]:
    """Tight bounding box in 100-space, to skip pixels a shape cannot touch."""
    if isinstance(primitive, Circle):
        return (
            primitive.cx - primitive.r,
            primitive.cy - primitive.r,
            primitive.cx + primitive.r,
            primitive.cy + primitive.r,
        )
    if isinstance(primitive, Ring):
        e = primitive.r + primitive.stroke / 2
        return (primitive.cx - e, primitive.cy - e, primitive.cx + e, primitive.cy + e)
    if isinstance(primitive, RoundedRect):
        return (
            primitive.x,
            primitive.y,
            primitive.x + primitive.w,
            primitive.y + primitive.h,
        )
    xs = (primitive.x1, primitive.x2, primitive.x3)
    ys = (primitive.y1, primitive.y2, primitive.y3)
    return (min(xs), min(ys), max(xs), max(ys))


def _covers(primitive: Primitive, px: float, py: float) -> bool:
    """Whether the point (px, py) in 100-space is covered by the primitive."""
    if isinstance(primitive, Circle):
        return (px - primitive.cx) ** 2 + (py - primitive.cy) ** 2 <= primitive.r**2
    if isinstance(primitive, Ring):
        dist = math.sqrt((px - primitive.cx) ** 2 + (py - primitive.cy) ** 2)
        return abs(dist - primitive.r) <= primitive.stroke / 2
    if isinstance(primitive, RoundedRect):
        if not (primitive.x <= px <= primitive.x + primitive.w):
            return False
        if not (primitive.y <= py <= primitive.y + primitive.h):
            return False
        qx = min(max(px, primitive.x + primitive.r), primitive.x + primitive.w - primitive.r)
        qy = min(max(py, primitive.y + primitive.r), primitive.y + primitive.h - primitive.r)
        return (qx - px) ** 2 + (qy - py) ** 2 <= primitive.r**2
    # Triangle: the point is inside when all edge signs agree.

    def sign(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
        return (ax - cx) * (by - cy) - (bx - cx) * (ay - cy)

    d1 = sign(px, py, primitive.x1, primitive.y1, primitive.x2, primitive.y2)
    d2 = sign(px, py, primitive.x2, primitive.y2, primitive.x3, primitive.y3)
    d3 = sign(px, py, primitive.x3, primitive.y3, primitive.x1, primitive.y1)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", binascii.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _encode_png(rgba: bytes, width: int, height: int) -> bytes:
    """Minimal PNG encoder: 8-bit RGBA, non-interlaced, filter type 0 per row."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += rgba[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def mark_to_png(primitives: list[Primitive], size: int) -> bytes:
    """Rasterize the mark to `size` x `size` RGBA PNG bytes.

    Rasterizes at 2x and box-averages down for antialiasing; the background is
    transparent and shapes are painted in order (later shapes cover earlier
    ones). Pure stdlib (zlib + struct + binascii).
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    if not primitives:
        raise ValueError("need at least one primitive")
    hires = size * _SUPERSAMPLE
    scale = hires / SPACE
    buf = bytearray(hires * hires * 4)
    jobs: list[tuple[Primitive, tuple[int, int, int], int, int, int, int]] = []
    for primitive in primitives:
        x0, y0, x1, y1 = _bbox(primitive)
        jobs.append(
            (
                primitive,
                _parse_hex(primitive.fill),
                max(0, int(x0 * scale)),
                max(0, int(y0 * scale)),
                min(hires, int(x1 * scale) + 1),
                min(hires, int(y1 * scale) + 1),
            )
        )
    for primitive, (r, g, b), ix0, iy0, ix1, iy1 in jobs:
        for iy in range(iy0, iy1):
            py = (iy + 0.5) / scale
            row = iy * hires * 4
            for ix in range(ix0, ix1):
                if _covers(primitive, (ix + 0.5) / scale, py):
                    o = row + ix * 4
                    buf[o] = r
                    buf[o + 1] = g
                    buf[o + 2] = b
                    buf[o + 3] = 255
    # Box-average each 2x2 block down to the target size.
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            rs = gs = bs = a = 0
            for dy in range(_SUPERSAMPLE):
                base = ((y * _SUPERSAMPLE + dy) * hires + x * _SUPERSAMPLE) * 4
                for dx in range(_SUPERSAMPLE):
                    o = base + dx * 4
                    rs += buf[o]
                    gs += buf[o + 1]
                    bs += buf[o + 2]
                    a += buf[o + 3]
            o2 = (y * size + x) * 4
            out[o2] = (rs + 2) // 4
            out[o2 + 1] = (gs + 2) // 4
            out[o2 + 2] = (bs + 2) // 4
            out[o2 + 3] = (a + 2) // 4
    return _encode_png(bytes(out), size, size)
