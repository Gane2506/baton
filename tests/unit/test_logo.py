"""Tests for the stdlib-only geometric logo-mark generator (`core/logo.py`)."""

from __future__ import annotations

import binascii
import struct
import xml.etree.ElementTree as ET
import zlib

import pytest

from jev_agent.core.logo import (
    Circle,
    Primitive,
    Ring,
    RoundedRect,
    Triangle,
    generate_mark,
    mark_template,
    mark_to_png,
    mark_to_svg,
)

_PALETTE = {"primary": "#2563EB", "primary_hover": "#1D4ED8"}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _inferred_template(mark: list[Primitive]) -> str:
    """Which template a mark came from, from its primitive structure."""
    if any(isinstance(p, Triangle) for p in mark):
        return "apex"
    rects = [p for p in mark if isinstance(p, RoundedRect)]
    if len(rects) == 4:
        return "signal"
    if len(rects) == 1:
        return "bloom"
    rings = [p for p in mark if isinstance(p, Ring)]
    circles = [p for p in mark if isinstance(p, Circle)]
    if len(rings) == 2 and len(circles) == 1:
        return "rings"
    assert len(rings) == 1 and len(circles) == 2
    return "orbit"


def test_generate_mark_is_deterministic() -> None:
    first = generate_mark("acme:Acme", _PALETTE)
    second = generate_mark("acme:Acme", _PALETTE)
    assert first == second
    assert first  # never empty


def test_all_five_templates_are_reachable() -> None:
    seen = {_inferred_template(generate_mark(f"seed-{i}", _PALETTE)) for i in range(500)}
    assert seen == {"rings", "orbit", "signal", "bloom", "apex"}


def test_template_name_matches_generated_mark() -> None:
    for i in range(50):
        seed = f"product-{i}"
        assert _inferred_template(generate_mark(seed, _PALETTE)) == mark_template(seed)


def test_marks_stay_inside_the_canvas() -> None:
    for i in range(50):
        mark = generate_mark(f"bounds-{i}", _PALETTE)
        for primitive in mark:
            for value in _coords(primitive):
                assert 0 <= value <= 100, (primitive, value)


def _coords(primitive: Primitive) -> list[float]:
    if isinstance(primitive, Circle):
        return [
            primitive.cx - primitive.r,
            primitive.cx + primitive.r,
            primitive.cy - primitive.r,
            primitive.cy + primitive.r,
        ]
    if isinstance(primitive, RoundedRect):
        return [primitive.x, primitive.x + primitive.w, primitive.y, primitive.y + primitive.h]
    if isinstance(primitive, Ring):
        e = primitive.r + primitive.stroke / 2
        return [primitive.cx - e, primitive.cx + e, primitive.cy - e, primitive.cy + e]
    assert isinstance(primitive, Triangle)
    return [primitive.x1, primitive.x2, primitive.x3, primitive.y1, primitive.y2, primitive.y3]


def test_svg_parses_as_xml() -> None:
    mark = generate_mark("acme:Acme", _PALETTE)
    svg = mark_to_svg(mark, 512)
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib["width"] == "512"
    assert root.attrib["height"] == "512"
    assert root.attrib["viewBox"] == "0 0 100 100"


def test_svg_never_renders_text() -> None:
    """The wordmark rule: the mark is abstract geometry; the wordmark is styled
    text documented in the markdown, never baked into the mark."""
    for i in range(50):
        svg = mark_to_svg(generate_mark(f"wordmark-{i}", _PALETTE)).lower()
        assert "<text" not in svg
        assert "font" not in svg


def _parse_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == _PNG_MAGIC, "missing PNG signature"
    chunks: list[tuple[bytes, bytes]] = []
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_type = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (stored_crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert stored_crc == binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        chunks.append((chunk_type, payload))
        pos += 12 + length
    return chunks


@pytest.mark.parametrize("size", [16, 32, 180, 192, 512])
def test_png_is_valid(size: int) -> None:
    mark = generate_mark("acme:Acme", _PALETTE)
    chunks = _parse_chunks(mark_to_png(mark, size))
    types = [t for t, _ in chunks]
    assert types[0] == b"IHDR"
    assert b"IDAT" in types
    assert types[-1] == b"IEND"
    ihdr = dict(chunks)[b"IHDR"]
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    assert (width, height) == (size, size)
    assert bit_depth == 8
    assert color_type == 6  # RGBA
    assert compression == 0 and filter_method == 0 and interlace == 0


def test_png_idat_decompresses_to_full_rgba_scanlines() -> None:
    size = 64
    mark = generate_mark("acme:Acme", _PALETTE)
    chunks = _parse_chunks(mark_to_png(mark, size))
    raw = zlib.decompress(b"".join(payload for t, payload in chunks if t == b"IDAT"))
    assert len(raw) == size * (1 + size * 4)
    alphas = raw[3::4]
    assert any(a == 255 for a in alphas), "the mark should paint opaque pixels"
    assert any(a == 0 for a in alphas), "the background should stay transparent"


def test_png_rejects_bad_input() -> None:
    mark = generate_mark("acme:Acme", _PALETTE)
    with pytest.raises(ValueError):
        mark_to_png(mark, 0)
    with pytest.raises(ValueError):
        mark_to_png(mark, -4)
    with pytest.raises(ValueError):
        mark_to_png([], 32)
    with pytest.raises(ValueError):
        mark_to_svg(mark, 0)


# ---- per-template coverage ----------------------------------------------------------------------


def _seed_for_template(template: str) -> str:
    for i in range(2000):
        seed = f"template-hunt-{i}"
        if mark_template(seed) == template:
            return seed
    raise AssertionError(f"no seed found for template {template!r}")


def test_svg_parses_and_has_no_text_for_every_template() -> None:
    for template in ("rings", "orbit", "signal", "bloom", "apex"):
        mark = generate_mark(_seed_for_template(template), _PALETTE)
        svg = mark_to_svg(mark, 256)
        root = ET.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        lowered = svg.lower()
        assert "<text" not in lowered
        assert "font" not in lowered


def test_png_valid_for_every_template() -> None:
    for template in ("rings", "orbit", "signal", "bloom", "apex"):
        data = mark_to_png(generate_mark(_seed_for_template(template), _PALETTE), 32)
        assert data[:8] == _PNG_MAGIC
        width, height = struct.unpack(">II", data[16:24])  # IHDR width/height
        assert (width, height) == (32, 32)


def test_same_seed_renders_identical_svg() -> None:
    first = mark_to_svg(generate_mark("acme:Acme", _PALETTE))
    assert mark_to_svg(generate_mark("acme:Acme", _PALETTE)) == first
