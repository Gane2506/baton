"""WS-C: reverse-engineering evidence (`core/design_reverse.py`), the extraction output
contract, the extract.md prompt template, and `jev design reverse --dry-run`.
"""

from __future__ import annotations

import json
import math
import socket
import struct
import zlib
from collections.abc import Sequence
from http.client import HTTPMessage
from importlib.resources import files
from pathlib import Path
from typing import IO, Any, Literal, cast
from urllib.error import URLError
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from jev_agent.app import context as app_context
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.design_library import validate_library_name
from jev_agent.core.design_reverse import (
    _SafeRedirectHandler,
    _validate_fetch_url,
    evidence_to_brief,
    fetch_url_evidence,
    is_url_target,
    palette_from_png,
    parse_extraction_output,
    suggest_library_name,
    validate_extraction_tokens,
)
from jev_agent.roles.render import PromptError, render_prompt
from jev_agent.workers.fake import FakeStep, FakeWorker

runner = CliRunner()

DR = "jev_agent.core.design_reverse"  # module path for monkeypatching


def _no_fetch_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the SSRF guard so fetch tests never touch DNS (tested separately)."""
    monkeypatch.setattr(DR + "._validate_fetch_url", lambda url: url)


FIXTURE_HTML = """<!DOCTYPE html><html><head>
<title>  Acme &amp; Co  </title>
<meta name="theme-color" content="#0f172a">
<meta content="#ffffff" name="theme-color">
<style>
  body { font-family: "Inter", system-ui, sans-serif; color: #1f2937; background: #ffffff; }
  h1 { font-family: Georgia, serif; color: rgb(17, 24, 39); }
  .btn { background: #1f2937; border-color: #1F2937; }
</style>
</head><body style="font-family: 'Inter', system-ui, sans-serif; color: #1f2937;">
<p style="color: rgba(31, 41, 55, 0.8)">hi</p>
</body></html>
"""


# ---- urlopen fakes (no network in tests) -----------------------------------------------------


class _FakeHeaders:
    def __init__(self, charset: str | None) -> None:
        self._charset = charset

    def get_content_charset(self, failobj: str | None = None) -> str | None:
        return self._charset if self._charset is not None else failobj


class _FakeResponse:
    def __init__(self, body: bytes, charset: str | None = "utf-8") -> None:
        self._body = body
        self.headers = _FakeHeaders(charset)
        self.request: Any = None

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body


def _fake_urlopen_factory(body: bytes, seen: dict[str, Any]) -> Any:
    def _fake_urlopen(request: Any, timeout: Any = None) -> _FakeResponse:
        seen["request"] = request
        seen["timeout"] = timeout
        return _FakeResponse(body)

    return _fake_urlopen


# ---- fetch_url_evidence ---------------------------------------------------------------------


def test_fetch_url_evidence_parses_fixture_html(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_fetch_guard(monkeypatch)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "jev_agent.core.design_reverse.urlopen",
        _fake_urlopen_factory(FIXTURE_HTML.encode(), seen),
    )
    evidence = fetch_url_evidence("https://example.com", timeout_s=7)
    assert "error" not in evidence
    assert evidence["title"] == "Acme & Co"
    assert evidence["theme_color"] == "#0f172a"  # first theme-color meta wins
    assert evidence["fonts"] == [
        '"Inter", system-ui, sans-serif',
        "Georgia, serif",
        "'Inter', system-ui, sans-serif",
    ]  # deduped, in order of appearance
    colors = evidence["colors"]
    assert isinstance(colors, dict)
    assert colors["#1f2937"] == 4  # hex literals are lowercased before counting
    assert colors["#ffffff"] == 2
    assert colors["rgb(17, 24, 39)"] == 1
    assert colors["rgba(31, 41, 55, 0.8)"] == 1
    assert list(colors)[:2] == ["#1f2937", "#ffffff"]  # most-used first
    styles = evidence["styles"]
    assert isinstance(styles, str) and "font-family" in styles and len(styles) <= 4096
    assert seen["request"].get_header("User-agent") == "jev-design-reverse/1.0"
    assert seen["timeout"] == 7


def test_fetch_url_evidence_never_raises_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_fetch_guard(monkeypatch)

    def _boom(request: Any, timeout: Any = None) -> Any:
        raise URLError("dns blew up")

    monkeypatch.setattr("jev_agent.core.design_reverse.urlopen", _boom)
    evidence = fetch_url_evidence("https://example.com")
    assert set(evidence) == {"url", "error"}
    assert "could not fetch https://example.com" in str(evidence["error"])


def test_fetch_url_evidence_missing_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_fetch_guard(monkeypatch)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "jev_agent.core.design_reverse.urlopen",
        _fake_urlopen_factory(b"<html><body><p>no head at all</p></body></html>", seen),
    )
    evidence = fetch_url_evidence("https://example.com")
    assert evidence["title"] is None
    assert evidence["theme_color"] is None
    assert evidence["fonts"] == []
    assert evidence["colors"] == {}


# ---- fetch guard (SSRF-ish): scheme, credentials, non-public hosts --------------------------


def _fake_getaddrinfo_factory(ips: list[str]) -> Any:
    def _fake(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _fake


def _stub_dns(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_factory(ips))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/design",
        "file:///etc/passwd",
        "https://user:secret@example.com/",  # credentials would leak as Basic auth
        "https:///no-host",
    ],
)
def test_validate_fetch_url_rejects_unsafe_syntax(url: str) -> None:
    with pytest.raises(ValueError, match="refusing to fetch"):
        _validate_fetch_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254"])
def test_validate_fetch_url_rejects_non_public_hosts(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    _stub_dns(monkeypatch, [ip])
    with pytest.raises(ValueError, match="non-public"):
        _validate_fetch_url("https://internal.example/")


def test_validate_fetch_url_rejects_mixed_public_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns(monkeypatch, ["93.184.216.34", "10.0.0.1"])  # one private: refused
    with pytest.raises(ValueError, match="non-public"):
        _validate_fetch_url("https://example.com/")


def test_validate_fetch_url_accepts_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, ["93.184.216.34"])
    assert _validate_fetch_url("https://example.com/") == "https://example.com/"


def test_validate_fetch_url_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(ValueError, match="cannot resolve"):
        _validate_fetch_url("https://does-not-exist.invalid/")


def test_fetch_url_evidence_refuses_private_host_without_touching_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns(monkeypatch, ["127.0.0.1"])
    touched: list[Any] = []

    def _must_not_run(request: Any, timeout: Any = None) -> Any:
        touched.append(request)
        raise AssertionError("network must not be touched for a refused URL")

    monkeypatch.setattr(DR + ".urlopen", _must_not_run)
    evidence = fetch_url_evidence("http://localhost:9999/")
    assert set(evidence) == {"url", "error"}
    assert "refusing to fetch" in str(evidence["error"])
    assert not touched


def test_safe_redirect_handler_blocks_private_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, ["10.0.0.9"])
    handler = _SafeRedirectHandler()
    assert handler.max_redirections == 10  # the default limit is kept
    request = Request("https://example.com/")
    with pytest.raises(ValueError, match="non-public"):
        handler.redirect_request(
            request, cast(IO[bytes], None), 302, "Found", HTTPMessage(), "http://10.0.0.9/admin"
        )


def test_safe_redirect_handler_allows_public_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, ["93.184.216.34"])
    handler = _SafeRedirectHandler()
    request = Request("https://example.com/")
    follow = handler.redirect_request(
        request, cast(IO[bytes], None), 302, "Found", HTTPMessage(), "https://cdn.example.com/x"
    )
    assert isinstance(follow, Request)
    assert follow.full_url == "https://cdn.example.com/x"


# ---- PNG encoder for fixtures (struct/zlib, written in the test) ----------------------------


def _encode_png(
    width: int,
    height: int,
    rows: Sequence[Sequence[Sequence[int]]],
    *,
    color_type: int = 2,
    bit_depth: int = 8,
    interlace: int = 0,
    row_filters: list[int] | None = None,
) -> bytes:
    """Minimal truecolor/grayscale PNG encoder: one filter byte per scanline."""
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    raw = bytearray()
    for r, row in enumerate(rows):
        ftype = 0 if row_filters is None else row_filters[r]
        raw.append(ftype)
        prev = [0] * channels
        for px in row:
            vals = list(px[:channels])
            if ftype == 1:  # Sub: store each byte minus the previous pixel's
                raw.extend((vals[i] - prev[i]) & 0xFF for i in range(channels))
            else:  # None, or a bogus filter byte the decoder must reject
                raw.extend(vals)
            prev = vals

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def _write_png(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _assert_palette(actual: list[tuple[str, float]], expected: list[tuple[str, float]]) -> None:
    """Hex codes in order, coverage fractions within tolerance."""
    assert [h for h, _ in actual] == [h for h, _ in expected]
    for (_, got), (_, want) in zip(actual, expected, strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)


# ---- palette_from_png -----------------------------------------------------------------------


def test_palette_from_png_basic(tmp_path: Path) -> None:
    red, green, blue = (255, 0, 0), (0, 255, 0), (0, 0, 255)
    path = _write_png(
        tmp_path,
        "basic.png",
        _encode_png(4, 2, [[red, red, blue, blue], [red, green, green, green]]),
    )
    _assert_palette(
        palette_from_png(path),
        [("#ff0000", 3 / 8), ("#00ff00", 3 / 8), ("#0000ff", 2 / 8)],
    )


def test_palette_from_png_sub_filter_decodes(tmp_path: Path) -> None:
    red, green, blue = (255, 0, 0), (0, 255, 0), (0, 0, 255)
    path = _write_png(
        tmp_path,
        "sub.png",
        _encode_png(
            4,
            2,
            [[red, red, blue, blue], [red, green, green, green]],
            row_filters=[1, 1],
        ),
    )
    first = palette_from_png(path)[0]
    assert first[0] == "#ff0000"
    assert math.isclose(first[1], 3 / 8, rel_tol=1e-9)


def test_palette_from_png_skips_near_duplicate_shades(tmp_path: Path) -> None:
    # (240,0,0) quantizes one step away from (255,0,0): same visual red, skipped.
    rows = [[(255, 0, 0)] * 5 + [(240, 0, 0)] * 3]
    path = _write_png(tmp_path, "shades.png", _encode_png(8, 1, rows))
    _assert_palette(palette_from_png(path), [("#ff0000", 1.0)])


def test_palette_from_png_max_colors(tmp_path: Path) -> None:
    rows = [[(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]]
    path = _write_png(tmp_path, "many.png", _encode_png(4, 1, rows))
    assert len(palette_from_png(path, max_colors=2)) == 2


def test_palette_from_png_ignores_transparent_pixels(tmp_path: Path) -> None:
    rows = [[(255, 0, 0, 255), (0, 0, 0, 0), (0, 0, 255, 255), (0, 0, 255, 255)]]
    path = _write_png(tmp_path, "alpha.png", _encode_png(4, 1, rows, color_type=6))
    palette = palette_from_png(path)
    by_hex = dict(palette)
    assert math.isclose(by_hex["#ff0000"], 1 / 3, rel_tol=1e-9)
    assert math.isclose(by_hex["#0000ff"], 2 / 3, rel_tol=1e-9)
    assert math.isclose(sum(by_hex.values()), 1.0, rel_tol=1e-9)


@pytest.mark.parametrize(
    "data",
    [
        b"definitely not a png",
        _encode_png(2, 1, [[(1, 2, 3), (4, 5, 6)]], interlace=1),  # Adam7
        _encode_png(2, 1, [[(1,), (2,)]], color_type=0),  # grayscale
        _encode_png(2, 1, [[(1, 2, 3), (4, 5, 6)]], bit_depth=16),  # 16-bit
        _encode_png(2, 1, [[(1, 2, 3), (4, 5, 6)]], row_filters=[9, 9]),  # bad filter
    ],
)
def test_palette_from_png_rejects_unsupported(tmp_path: Path, data: bytes) -> None:
    path = _write_png(tmp_path, "bad.png", data)
    with pytest.raises(ValueError, match=r"(?i)(not a png|interlaced|color type|8-bit|filter)"):
        palette_from_png(path)


def test_palette_from_png_rejects_oversize_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DR + "._MAX_PNG_BYTES", 16)
    path = _write_png(tmp_path, "big.png", b"x" * 17)
    with pytest.raises(ValueError, match="limit"):
        palette_from_png(path)


def test_palette_from_png_rejects_huge_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 3x3 = 9 pixels over a stubbed limit of 4: refused before IDAT is decoded.
    monkeypatch.setattr(DR + "._MAX_PNG_PIXELS", 4)
    rows = [[(1, 2, 3)] * 3] * 3
    path = _write_png(tmp_path, "huge.png", _encode_png(3, 3, rows))
    with pytest.raises(ValueError, match="pixel"):
        palette_from_png(path)


# ---- evidence_to_brief ----------------------------------------------------------------------


def test_brief_from_url_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_fetch_guard(monkeypatch)
    monkeypatch.setattr(
        "jev_agent.core.design_reverse.urlopen",
        _fake_urlopen_factory(FIXTURE_HTML.encode(), {}),
    )
    brief = evidence_to_brief(
        {
            "target": "https://example.com",
            "kind": "url",
            "url": "https://example.com",
            "url_evidence": fetch_url_evidence("https://example.com"),
            "screenshots": None,
        }
    )
    assert "https://example.com" in brief
    assert "Acme & Co" in brief
    assert "#0f172a" in brief
    assert '"Inter", system-ui, sans-serif' in brief
    assert "#1f2937" in brief and "x4" in brief
    assert "raw CSS literals" in brief  # palette came from CSS, not screenshots


def test_brief_from_screenshot_evidence() -> None:
    brief = evidence_to_brief(
        {
            "target": "a.png, b.png",
            "kind": "screenshots",
            "url": None,
            "url_evidence": None,
            "screenshots": [
                {"path": "a.png", "palette": [("#ff0000", 0.5), ("#0000ff", 0.25)]},
                {"path": "b.png", "palette": []},
            ],
        }
    )
    assert "2 screenshot(s)" in brief
    assert "#ff0000 (50.0% of visible pixels)" in brief
    assert "(no visible colors found)" in brief
    assert "rendered screenshot pixels, not authored tokens" in brief


def test_brief_notes_fetch_failure() -> None:
    brief = evidence_to_brief(
        {
            "target": "https://down.example",
            "kind": "url",
            "url": "https://down.example",
            "url_evidence": {"url": "https://down.example", "error": "could not fetch: boom"},
            "screenshots": None,
        }
    )
    assert "Page fetch failed" in brief
    assert "do not invent tokens" in brief


# ---- parse_extraction_output ----------------------------------------------------------------


def _valid_reply() -> str:
    return """```tokens.json
{"colors": {"primary": "#1a73e8"}, "type_scale": {"md": 16},
 "spacing": {"md": 16}, "radii": {"md": 8}}
```
# Acme design system

Some markdown about the system.
"""


def test_parse_extraction_output_valid() -> None:
    tokens, markdown = parse_extraction_output(_valid_reply())
    assert tokens["colors"] == {"primary": "#1a73e8"}
    assert set(tokens) >= {"colors", "type_scale", "spacing", "radii"}
    assert markdown.startswith("# Acme design system")
    assert "tokens.json" not in markdown


def test_parse_extraction_output_keeps_text_before_the_fence() -> None:
    tokens, markdown = parse_extraction_output("A short intro.\n\n" + _valid_reply())
    assert "A short intro." in markdown
    assert tokens["spacing"] == {"md": 16}


def test_parse_extraction_output_malformed() -> None:
    with pytest.raises(ValueError, match=r"no ```tokens\.json fenced block"):
        parse_extraction_output("# just markdown, no fence")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_extraction_output("```tokens.json\n{nope\n```\n# md")
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_extraction_output('```tokens.json\n["nope"]\n```\n# md')
    with pytest.raises(ValueError, match="no 'spacing' object"):
        parse_extraction_output(
            '```tokens.json\n{"colors": {}, "type_scale": {}, "radii": {}}\n```\n# md'
        )
    with pytest.raises(ValueError, match="no design-system markdown"):
        parse_extraction_output(
            '```tokens.json\n{"colors": {}, "type_scale": {}, "spacing": {}, "radii": {}}\n```'
        )


# ---- validate_extraction_tokens --------------------------------------------------------------


def _good_tokens() -> dict[str, object]:
    return {
        "colors": {"primary": "#1A73E8", "surface": "#ffffff"},  # case-insensitive hex
        "type_scale": {"md": 16, "lg": 20.5},
        "spacing": {"md": 16},
        "radii": {"full": 9999},
    }


def test_validate_extraction_tokens_accepts_clean_tokens() -> None:
    tokens = _good_tokens()
    assert validate_extraction_tokens(tokens) is tokens


def test_validate_extraction_tokens_allows_empty_sections() -> None:
    # Thin evidence (a failed fetch): conservative empty sections are fine.
    validate_extraction_tokens({"colors": {}, "type_scale": {}, "spacing": {}, "radii": {}})


@pytest.mark.parametrize(
    ("section", "tokens", "match"),
    [
        ("colors", {"primary": "red"}, "must be a #rrggbb"),
        ("colors", {"primary": "#12345"}, "must be a #rrggbb"),
        ("colors", {"primary": "#gggggg"}, "must be a #rrggbb"),
        ("colors", {"primary": "#1a73e8ff"}, "must be a #rrggbb"),  # 8-digit: not #rrggbb
        ("colors", {"primary": 123}, "must be a #rrggbb"),
        ("colors", {"": "#ffffff"}, "invalid token name"),
        ("colors", "nope", "must be an object"),
        ("type_scale", {"md": "16px"}, "must be a finite number"),
        ("type_scale", {"md": True}, "must be a finite number"),  # bool is an int
        ("type_scale", {"md": float("nan")}, "must be a finite number"),
        ("type_scale", {"md": float("inf")}, "must be a finite number"),
        ("type_scale", {"md": -1}, "must be a finite number"),
        ("spacing", {"md": None}, "must be a finite number"),
        ("radii", {"md": [8]}, "must be a finite number"),
    ],
)
def test_validate_extraction_tokens_rejects_bad_values(
    section: str, tokens: object, match: str
) -> None:
    bad = _good_tokens()
    bad[section] = tokens
    with pytest.raises(ValueError, match=match):
        validate_extraction_tokens(bad)


def test_validate_extraction_tokens_rejects_unknown_sections() -> None:
    bad = _good_tokens()
    bad["shadows"] = {"sm": "0 1px 2px black"}
    with pytest.raises(ValueError, match=r"unknown sections.*shadows"):
        validate_extraction_tokens(bad)


def test_validate_extraction_tokens_rejects_missing_sections() -> None:
    bad = _good_tokens()
    del bad["colors"]
    with pytest.raises(ValueError, match="'colors' must be an object"):
        validate_extraction_tokens(bad)


def test_validate_extraction_tokens_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        validate_extraction_tokens(["nope"])  # type: ignore[arg-type]


# ---- name derivation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (["https://www.example.com/pricing"], "example-com"),
        (["https://example.com:8080/x"], "example-com"),
        (["http://shop.example.co.uk/"], "shop-example-co-uk"),
        (["My Homepage.PNG"], "my-homepage"),
        (["shot 1.png", "shot 2.png"], "shot-1"),
        (["https://x"], "x"),
    ],
)
def test_suggest_library_name(targets: list[str], expected: str) -> None:
    name = suggest_library_name(targets)
    assert name == expected
    validate_library_name(name)  # the suggestion always passes the library rules


def test_is_url_target() -> None:
    assert is_url_target("https://example.com")
    assert is_url_target("HTTP://example.com/x")
    assert not is_url_target("shot.png")
    assert not is_url_target("/tmp/shot.png")


# ---- extract.md prompt template --------------------------------------------------------------


def test_extract_prompt_renders_and_asks_for_tokens_then_markdown() -> None:
    extract_md = Path(str(files("jev_agent.roles").joinpath("designer", "extract.md")))
    prompt = render_prompt("designer", extract_md, brief="EVIDENCE BRIEF", lessons=[])
    assert "EVIDENCE BRIEF" in prompt
    assert "```tokens.json" in prompt
    assert prompt.index("```tokens.json") < prompt.index("design-system markdown")
    assert "Do not design screens" in prompt  # system extraction, not screen design


def test_extract_prompt_rejects_unknown_variables() -> None:
    extract_md = Path(str(files("jev_agent.roles").joinpath("designer", "extract.md")))
    with pytest.raises(PromptError):
        render_prompt("designer", extract_md)  # `brief` is required (StrictUndefined)


# ---- `jev design reverse --dry-run` ------------------------------------------------------------


def test_reverse_dry_run_prints_brief(tmp_path: Path, fixture_repo: Path) -> None:
    png = _write_png(tmp_path, "Shot One.png", _encode_png(2, 1, [[(255, 0, 0), (0, 0, 255)]]))
    result = runner.invoke(
        app, ["design", "reverse", str(png), "--dry-run", "--repo", str(fixture_repo)]
    )
    assert result.exit_code == 0, result.output
    assert "#ff0000 (50.0% of visible pixels)" in result.output
    assert "screenshot(s)" in result.output


def test_reverse_rejects_bad_targets(tmp_path: Path, fixture_repo: Path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("not an image")
    missing = tmp_path / "gone.png"

    def _run(*args: str) -> Any:
        return runner.invoke(app, ["design", "reverse", *args, "--repo", str(fixture_repo)])

    assert _run(str(txt)).exit_code == 1
    assert "only .png" in _run(str(txt)).output
    assert "image not found" in _run(str(missing)).output
    mixed = _write_png(tmp_path, "ok.png", _encode_png(1, 1, [[(1, 2, 3)]]))
    # mixed kinds are rejected before any suffix/existence checks
    assert "not both" in _run("https://example.com", str(mixed)).output
    assert "not both" in _run("https://example.com", str(txt)).output


# ---- `jev design reverse` end to end (scripted designer, no live model) ------------------------


def _fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _scripted_designer(monkeypatch: pytest.MonkeyPatch, summary: str) -> None:
    """`JEV_FAKE_WORKERS=1` with the designer role returning `summary` verbatim."""
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")

    def _fake_worker(name: str) -> FakeWorker:
        return FakeWorker(name, {"designer": FakeStep(summary=summary)})

    monkeypatch.setattr(app_context, "FakeWorker", _fake_worker)


def _library_system(home: Path, name: str) -> Path:
    return home / ".jev" / "design-systems" / name


def test_reverse_saves_validated_system(
    tmp_path: Path, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _scripted_designer(monkeypatch, _valid_reply())
    png = _write_png(tmp_path, "shot.png", _encode_png(2, 1, [[(255, 0, 0), (0, 0, 255)]]))
    result = runner.invoke(app, ["design", "reverse", str(png), "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert 'Reversed "shot" into the library' in result.output
    system_dir = _library_system(home, "shot")
    meta = json.loads((system_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "reverse:screenshots"
    tokens = json.loads((system_dir / "tokens.json").read_text(encoding="utf-8"))
    assert tokens["colors"] == {"primary": "#1a73e8"}  # the model's tokens, validated
    markdown = (system_dir / "design-system.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Acme design system")


def test_reverse_garbage_output_saves_nothing(
    tmp_path: Path, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _scripted_designer(monkeypatch, "the designer rambles with no tokens.json fence at all")
    png = _write_png(tmp_path, "shot.png", _encode_png(2, 1, [[(255, 0, 0), (0, 0, 255)]]))
    result = runner.invoke(app, ["design", "reverse", str(png), "--repo", str(fixture_repo)])
    assert result.exit_code == 1, result.output
    assert "tokens.json" in result.output  # clean contract error, no traceback
    assert not _library_system(home, "shot").exists()


def test_reverse_invalid_tokens_save_nothing(
    tmp_path: Path, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    bad_reply = (
        "```tokens.json\n"
        '{"colors": {"primary": "not-a-color"}, "type_scale": {"md": 16}, '
        '"spacing": {"md": 16}, "radii": {"md": 8}}\n'
        "```\n# Acme design system\n\nDocs.\n"
    )
    _scripted_designer(monkeypatch, bad_reply)
    png = _write_png(tmp_path, "shot.png", _encode_png(2, 1, [[(255, 0, 0), (0, 0, 255)]]))
    result = runner.invoke(app, ["design", "reverse", str(png), "--repo", str(fixture_repo)])
    assert result.exit_code == 1, result.output
    assert "failed validation" in result.output
    assert "not-a-color" in result.output
    assert not _library_system(home, "shot").exists()
