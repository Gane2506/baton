"""Reverse-engineer a design system from a live website or screenshots.

Evidence gathering for `jev design reverse`: fetch a page's HTML and pull out its
title, theme color, font stacks and color literals; decode PNG screenshots into
dominant-color palettes; render the evidence as the text brief the designer role
extracts tokens from. Stdlib-only: no pipeline dependencies. URL fetches carry a
timeout, redirect limit, size cap and a guard refusing non-public hosts.
"""

from __future__ import annotations

import html as _html
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import struct
import zlib
from collections import Counter
from collections.abc import Callable
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from typing import IO, cast
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

_USER_AGENT = "jev-design-reverse/1.0"
_MAX_HTML_BYTES = 2_000_000
_MAX_STYLE_CHARS = 4096
_MAX_PNG_BYTES = 25_000_000  # screenshot files bigger than this are refused outright
_MAX_PNG_PIXELS = 50_000_000  # bounds decompressed IDAT (zip-bomb protection)

_HEX_COLOR = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-fA-F])"
)
_RGB_COLOR = re.compile(r"rgba?\([^)]*\)", re.IGNORECASE)
_FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;{}]+)", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG = re.compile(r"<meta[^>]*>", re.IGNORECASE)
_META_ATTR = re.compile(r"""([a-zA-Z_:][\w:.-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
_TOKENS_FENCE = re.compile(r"```tokens\.json[ \t]*\r?\n(.*?)```", re.DOTALL)
_NAME_BAD_CHARS = re.compile(r"[^a-z0-9]+")

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Sections the extraction contract requires inside the ```tokens.json block.
REQUIRED_TOKEN_SECTIONS = ("colors", "type_scale", "spacing", "radii")


def is_url_target(target: str) -> bool:
    """True when `target` looks like an http(s) URL rather than a file path."""
    return target.lower().startswith(("http://", "https://"))


def suggest_library_name(targets: list[str]) -> str:
    """Derive a library-safe name from a URL host or the first image's stem.

    Lowercased, runs of non-alphanumerics become `-`, capped at 64 chars — the
    result always passes `validate_library_name` (falls back to "reversed" when
    nothing usable remains, including an empty target list).
    """
    if not targets:
        return "reversed"
    if len(targets) == 1 and is_url_target(targets[0]):
        host = urlsplit(targets[0]).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        raw = host
    else:
        raw = Path(targets[0]).stem
    candidate = _NAME_BAD_CHARS.sub("-", raw.lower()).strip("-")[:64]
    return candidate or "reversed"


def _is_global_ip(ip: str) -> bool:
    """True only for globally routable addresses (no loopback, LAN, link-local,
    cloud-metadata, multicast, reserved or unspecified ranges)."""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _validate_fetch_url(url: str) -> str:
    """Refuse fetch targets that are unsafe or unreachable-by-policy.

    Only http/https, no embedded credentials (urllib would send them as Basic
    auth), and the host must resolve with *every* address globally routable —
    localhost, LAN hosts, link-local (incl. cloud metadata endpoints) and other
    special ranges are refused, since the fetch runs with the user's network
    identity. Returns the URL unchanged; raises ValueError with the reason.
    This is the early check only: the actual connection re-validates and
    pins its own resolution (`_validated_socket`), so a DNS rebinding
    between this check and the fetch cannot steer the socket.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"refusing to fetch {url!r}: only http(s) URLs are supported")
    if parts.username or parts.password:
        raise ValueError(f"refusing to fetch {url!r}: URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise ValueError(f"refusing to fetch {url!r}: no hostname")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"refusing to fetch {url!r}: cannot resolve {host}: {exc}") from exc
    resolved = sorted({str(info[4][0]) for info in infos})
    bad = [ip for ip in resolved if not _is_global_ip(ip)]
    if bad:
        raise ValueError(
            f"refusing to fetch {url!r}: {host} resolves to non-public address(es): "
            + ", ".join(bad)
        )
    return url


def _validated_socket(host: str, port: int, timeout: float | None) -> socket.socket:
    """Resolve `host` and connect to one of its globally routable addresses.

    Resolution and connection happen in this one step, and the socket is
    opened to the resolved IP literal — not the hostname. That closes the
    TOCTOU in the old flow, where `_validate_fetch_url` resolved the host
    and the fetch then resolved it again: a rebinding DNS answer between
    the two could steer the fetch at a private address. A host whose
    resolution yields no public address is refused here, even if the
    pre-check passed on a stale answer.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"cannot resolve {host}: {exc}") from exc
    public = sorted({str(info[4][0]) for info in infos if _is_global_ip(str(info[4][0]))})
    if not public:
        raise ValueError(f"refusing to connect to {host}: no globally routable address")
    return socket.create_connection((public[0], port), timeout)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that connects to the validated IP, not the hostname."""

    def connect(self) -> None:
        self.sock = _validated_socket(self.host, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to the validated IP, with SNI/Host kept as the
    original hostname (the certificate must still match the hostname)."""

    def connect(self) -> None:
        if getattr(self, "_tunnel_host", None):
            super().connect()
            return
        sock = _validated_socket(self.host, self.port, self.timeout)
        context = getattr(self, "_context", None) or ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


class _SafeHTTPHandler(HTTPHandler):
    """HTTP via the pinned connection (see `_validated_socket`)."""

    def http_open(self, req: Request) -> HTTPResponse:
        return self.do_open(_PinnedHTTPConnection, req)


class _SafeHTTPSHandler(HTTPSHandler):
    """HTTPS via the pinned connection (see `_validated_socket`)."""

    def https_open(self, req: Request) -> HTTPResponse:
        return self.do_open(
            _PinnedHTTPSConnection,
            req,
            context=getattr(self, "_context", None),
            check_hostname=getattr(self, "_check_hostname", True),
        )


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Follow redirects (max 10, like the default handler) but refuse any hop
    whose URL fails the fetch guard — a redirect to an internal address is
    treated exactly like fetching one directly."""

    max_redirections = 10

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        _validate_fetch_url(newurl)  # ValueError: reported, not raised, by the caller
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request: Request, timeout: float | None = None) -> HTTPResponse:
    """Open `request` through the SSRF-safe redirect handler and the pinned
    connections (`_validated_socket`): the socket connects to a validated
    public IP, never to a re-resolved hostname.

    A module-level function (rather than `urllib.request.urlopen` directly) so
    tests can stub the network without touching the redirect policy.
    """
    return cast(
        HTTPResponse,
        build_opener(_SafeRedirectHandler(), _SafeHTTPHandler(), _SafeHTTPSHandler()).open(
            request, timeout=timeout
        ),
    )


def fetch_url_evidence(url: str, timeout_s: int = 15) -> dict[str, object]:
    """Fetch `url` and extract design evidence from its HTML.

    Returns the title, `<meta name="theme-color">`, every `font-family:`
    declaration (deduplicated, in order of appearance), every hex/rgb color
    literal (counted, most-used first) and the inline `<style>` blocks (first
    4KB). The fetch has a 15s timeout, follows at most 10 redirects, reads at
    most 2MB of HTML, and refuses non-public hosts (see `_validate_fetch_url`).
    Never raises on network failure: returns `{"url": ..., "error": ...}`
    and the caller fails gracefully with the message.
    """
    try:
        _validate_fetch_url(url)
        request = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read(_MAX_HTML_BYTES + 1)
            charset = response.headers.get_content_charset(failobj="utf-8")
            page = raw[:_MAX_HTML_BYTES].decode(charset, errors="replace")
    except Exception as exc:  # network, DNS, TLS, bad URL: reported, not raised
        return {"url": url, "error": f"could not fetch {url}: {exc}"}
    return {
        "url": url,
        "title": _extract_title(page),
        "theme_color": _extract_theme_color(page),
        "fonts": _extract_fonts(page),
        "colors": _count_colors(page),
        "styles": _extract_styles(page),
    }


def _extract_title(page: str) -> str | None:
    match = _TITLE.search(page)
    if not match:
        return None
    title = _html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return title or None


def _extract_theme_color(page: str) -> str | None:
    for tag in _META_TAG.findall(page):
        attrs: dict[str, str] = {}
        for attr in _META_ATTR.finditer(tag):
            value = next(g for g in attr.groups()[2:] if g is not None)
            attrs[attr.group(1).lower()] = value
        if attrs.get("name", "").lower() == "theme-color" and attrs.get("content"):
            return attrs["content"].strip()
    return None


def _extract_fonts(page: str) -> list[str]:
    seen: list[str] = []
    for raw in _FONT_FAMILY.findall(page):
        stack = re.sub(r"\s+", " ", raw).strip().rstrip(";").strip()
        if stack and stack not in seen:
            seen.append(stack)
    return seen


def _extract_styles(page: str) -> str:
    return "\n".join(_STYLE_BLOCK.findall(page))[:_MAX_STYLE_CHARS]


def _count_colors(page: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for literal in _HEX_COLOR.findall(page):
        counts[literal.lower()] += 1
    for literal in _RGB_COLOR.findall(page):
        counts[re.sub(r"\s+", " ", literal).strip().lower()] += 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def palette_from_png(path: Path, max_colors: int = 8) -> list[tuple[str, float]]:
    """Dominant colors of a PNG as (hex, coverage fraction), most-covered first.

    Decoded with stdlib only (zlib): 8-bit non-interlaced truecolor (type 2) and
    truecolor+alpha (type 6). Anything else raises ValueError with a clear reason.
    Fully transparent pixels are ignored. Colors are quantized to 5 bits/channel;
    near-duplicate shades (within one quantization step) merge into the first match.
    """
    data = path.read_bytes()  # OSError propagates: the caller reports it
    if len(data) > _MAX_PNG_BYTES:
        raise ValueError(f"{path}: screenshot is {len(data)} bytes (limit {_MAX_PNG_BYTES})")
    if data[:8] != _PNG_SIGNATURE:
        raise ValueError(f"{path} is not a PNG file")
    width, height, color_type, idat = _read_png_header(data, path)
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError(f"{path}: corrupt PNG image data: {exc}") from exc
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise ValueError(f"{path}: corrupt PNG: {len(raw)} image bytes, expected {expected}")
    counts: Counter[tuple[int, int, int]] = Counter()
    total = 0
    previous = bytearray(stride)
    for row in range(height):
        offset = row * (stride + 1)
        scanline = bytearray(raw[offset + 1 : offset + 1 + stride])
        _unfilter(raw[offset], scanline, previous, channels)
        for x in range(width):
            i = x * channels
            if channels == 4 and scanline[i + 3] == 0:
                continue  # fully transparent: not a visible color
            counts[(scanline[i] >> 3, scanline[i + 1] >> 3, scanline[i + 2] >> 3)] += 1
            total += 1
        previous = scanline
    if total == 0:
        return []
    # (quantized rgb, pixel count); near-duplicate shades merge into the first match.
    selected: list[tuple[tuple[int, int, int], int]] = []
    for (qr, qg, qb), count in counts.most_common():
        match: int | None = None
        for i, ((r, g, b), _) in enumerate(selected):
            if max(abs(qr - r), abs(qg - g), abs(qb - b)) <= 1:
                match = i
                break
        if match is None:
            if len(selected) >= max_colors:
                break
            selected.append(((qr, qg, qb), count))
        else:
            (rgb, have) = selected[match]
            selected[match] = (rgb, have + count)
    return [(_quantized_hex(qr, qg, qb), count / total) for (qr, qg, qb), count in selected]


def _read_png_header(data: bytes, path: Path) -> tuple[int, int, int, bytearray]:
    """IHDR fields plus the concatenated IDAT bytes; ValueError on anything odd."""
    offset = 8
    width = height = 0
    color_type = -1
    idat = bytearray()
    seen_ihdr = False
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise ValueError(f"{path}: truncated PNG chunk {chunk_type!r}")
        chunk = data[start:end]
        offset = end + 4  # skip the CRC
        if chunk_type == b"IHDR":
            if seen_ihdr:
                raise ValueError(f"{path}: corrupt PNG: duplicate IHDR")
            seen_ihdr = True
            if length != 13:
                raise ValueError(f"{path}: corrupt PNG: bad IHDR length")
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if width == 0 or height == 0:
                raise ValueError(f"{path}: corrupt PNG: zero dimension")
            if width * height > _MAX_PNG_PIXELS:
                raise ValueError(
                    f"{path}: screenshot is {width}x{height} "
                    f"(over the {_MAX_PNG_PIXELS}-pixel limit)"
                )
            if bit_depth != 8:
                raise ValueError(f"{path}: unsupported PNG: {bit_depth}-bit (only 8-bit)")
            if color_type not in (2, 6):
                raise ValueError(
                    f"{path}: unsupported PNG color type {color_type} (only truecolor 2/6)"
                )
            if comp != 0 or filt != 0:
                raise ValueError(f"{path}: unsupported PNG compression/filter method")
            if interlace != 0:
                raise ValueError(f"{path}: interlaced PNGs are not supported")
        elif chunk_type == b"IDAT":
            idat += chunk
        elif chunk_type == b"IEND":
            break
    if not seen_ihdr:
        raise ValueError(f"{path}: corrupt PNG: missing IHDR")
    if not idat:
        raise ValueError(f"{path}: corrupt PNG: no image data")
    return width, height, color_type, idat


def _unfilter(filter_type: int, scanline: bytearray, previous: bytearray, channels: int) -> None:
    """Reverse one PNG scanline filter in place (filters 0-4; else ValueError)."""
    if filter_type == 0:  # None
        return
    if filter_type == 1:  # Sub
        for i in range(channels, len(scanline)):
            scanline[i] = (scanline[i] + scanline[i - channels]) & 0xFF
    elif filter_type == 2:  # Up
        for i in range(len(scanline)):
            scanline[i] = (scanline[i] + previous[i]) & 0xFF
    elif filter_type == 3:  # Average
        for i in range(len(scanline)):
            left = scanline[i - channels] if i >= channels else 0
            scanline[i] = (scanline[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(len(scanline)):
            left = scanline[i - channels] if i >= channels else 0
            up = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            scanline[i] = (scanline[i] + _paeth(left, up, upper_left)) & 0xFF
    else:
        raise ValueError(f"unknown PNG filter type {filter_type}")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _expand5(q: int) -> int:
    """A 5-bit channel back to 8 bits (replicate the top bits into the low ones)."""
    return (q << 3) | (q >> 2)


def _quantized_hex(qr: int, qg: int, qb: int) -> str:
    return f"#{_expand5(qr):02x}{_expand5(qg):02x}{_expand5(qb):02x}"


def evidence_to_brief(evidence: dict[str, object]) -> str:
    """Render gathered evidence as the text brief the designer extracts from."""
    target = str(evidence.get("target", "unknown target"))
    lines = [f"## Design evidence: reverse-engineered from {target}", ""]
    if evidence.get("kind") == "url":
        lines.extend(_url_brief_lines(evidence, target))
    else:
        lines.extend(_screenshot_brief_lines(evidence, target))
    return "\n".join(lines).rstrip()


def _url_brief_lines(evidence: dict[str, object], target: str) -> list[str]:
    lines = [f"Source: live website {evidence.get('url') or target}", ""]
    url_evidence = evidence.get("url_evidence")
    if not isinstance(url_evidence, dict) or "error" in url_evidence:
        detail = (
            url_evidence.get("error") if isinstance(url_evidence, dict) else "no evidence gathered"
        )
        return [
            *lines,
            f"Page fetch failed: {detail}",
            "",
            "No visual evidence is available; say so in the markdown and do not invent tokens.",
        ]
    title = url_evidence.get("title")
    lines.append(f"Page title: {title}" if title else "Page title: (none found)")
    theme = url_evidence.get("theme_color")
    if theme:
        lines.append(f'Theme color (<meta name="theme-color">): {theme}')
    else:
        lines.append("Theme color: not declared")
    lines.append("")
    fonts = url_evidence.get("fonts")
    if isinstance(fonts, list) and fonts:
        lines.append("Font stacks observed (in order of appearance):")
        lines.extend(f"- {font}" for font in fonts[:10])
    else:
        lines.append("Font stacks observed: none found in the page CSS.")
    lines.append("")
    colors = url_evidence.get("colors")
    if isinstance(colors, dict) and colors:
        lines.append(f"Color literals in the page CSS ({len(colors)} unique, most-used first):")
        lines.extend(f"- {literal} (x{count})" for literal, count in list(colors.items())[:12])
    else:
        lines.append("Color literals in the page CSS: none found.")
    lines += [
        "",
        "Note: the colors above are raw CSS literals; treat the most-used as token candidates",
        "and the theme-color as the brand anchor when one is declared.",
    ]
    return lines


def _screenshot_brief_lines(evidence: dict[str, object], target: str) -> list[str]:
    shots = evidence.get("screenshots")
    names: list[str] = []
    palettes: list[tuple[str, list[tuple[str, float]]]] = []
    if isinstance(shots, list):
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            name = str(shot.get("path", "screenshot"))
            names.append(name)
            palette = shot.get("palette")
            palettes.append((name, list(palette) if isinstance(palette, list) else []))
    lines = [f"Source: {len(names)} screenshot(s): {', '.join(names) or target}", ""]
    for name, palette in palettes:
        lines.append(f"Dominant colors in {name} (by pixel coverage):")
        if palette:
            for hex_color, coverage in palette:
                lines.append(f"- {hex_color} ({coverage:.1%} of visible pixels)")
        else:
            lines.append("- (no visible colors found)")
        lines.append("")
    lines += [
        "Note: this palette comes from rendered screenshot pixels, not authored tokens;",
        "use it as the observed color story and derive sensible semantic token names.",
    ]
    return lines


def parse_extraction_output(text: str) -> tuple[dict[str, object], str]:
    """Split the designer's reply into (tokens, markdown).

    The reply must open with a fenced ```tokens.json block holding a JSON object
    with `colors`, `type_scale`, `spacing` and `radii` sections; everything else
    is the design-system markdown. Raises ValueError with a clear message when
    the reply breaks the contract — the caller must save nothing partial.
    """
    match = _TOKENS_FENCE.search(text)
    if match is None:
        raise ValueError("the designer output has no ```tokens.json fenced block")
    try:
        tokens = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"the ```tokens.json block is not valid JSON: {exc}") from exc
    if not isinstance(tokens, dict):
        raise ValueError("the ```tokens.json block must be a JSON object")
    for section in REQUIRED_TOKEN_SECTIONS:
        if not isinstance(tokens.get(section), dict):
            raise ValueError(f"the ```tokens.json block has no {section!r} object")
    markdown = (text[: match.start()] + text[match.end() :]).strip()
    if not markdown:
        raise ValueError(
            "the designer output has no design-system markdown after the ```tokens.json block"
        )
    return tokens, markdown


def _is_hex6(value: object) -> bool:
    """A `#rrggbb` color literal, exactly as extract.md requires."""
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def _is_px_number(value: object) -> bool:
    """A finite, non-negative px number — bools explicitly rejected (`True`
    is an `int`, and `json` also accepts `NaN`/`Infinity`)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _validate_token_map(
    value: object, section: str, is_valid: Callable[[object], bool], expectation: str
) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"extracted tokens: {section!r} must be an object")
    for key, val in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"extracted tokens: {section!r} has an invalid token name {key!r}")
        if not is_valid(val):
            raise ValueError(
                f"extracted tokens: {section}.{key} must be {expectation}, got {val!r}"
            )


def validate_extraction_tokens(tokens: dict[str, object]) -> dict[str, object]:
    """Validate the designer's extracted tokens before they reach the library.

    `colors` values must be `#rrggbb` hex; `type_scale`, `spacing` and `radii`
    map names to finite, non-negative numbers (px). The contract is exactly the
    four sections extract.md promises — unknown top-level sections are rejected
    rather than written unvalidated. Raises ValueError with the first problem;
    returns the tokens unchanged when they are clean.
    """
    if not isinstance(tokens, dict):
        raise ValueError("extracted tokens must be a JSON object")
    unknown = sorted(key for key in tokens if key not in REQUIRED_TOKEN_SECTIONS)
    if unknown:
        raise ValueError(
            "extracted tokens have unknown sections (expected only "
            f"{', '.join(REQUIRED_TOKEN_SECTIONS)}): {', '.join(unknown)}"
        )
    _validate_token_map(
        tokens.get("colors"), "colors", _is_hex6, "a #rrggbb hex color like #1a73e8"
    )
    for section in ("type_scale", "spacing", "radii"):
        _validate_token_map(tokens.get(section), section, _is_px_number, "a finite number (px)")
    return tokens
