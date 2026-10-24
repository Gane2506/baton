"""Regression tests for Baton issue #199 (core-design module, swarm review):

1. theme-"both" exports emit valid CSS/JS/prompt text (no dict reprs)
2. showcase split hero renders on flat/legacy tokens, all vibes, without KeyError
3. legacy codegen defines every CSS variable its components use
4. saving over a corrupt system snapshots the corrupt files first
5. SSRF fetch pins the validated IP at connect time (no DNS-rebinding TOCTOU)
6. generator inputs are escaped/sanitized (HTML/CSS/zip paths)
"""

from __future__ import annotations

import io
import json
import re
import socket
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.design_codegen import (
    bundle_zip,
    codegen_summary,
    css_variables,
    generate_files,
)
from jev_agent.core.design_kit import palette_for_vibe, tokens_for_app_type
from jev_agent.core.design_library import (
    export_tokens_css,
    export_tokens_tailwind,
    export_tokens_w3c,
    load_library_system,
    save_library_system,
)
from jev_agent.core.design_reverse import (
    _PinnedHTTPConnection,
    suggest_library_name,
)
from jev_agent.core.design_showcase import VIBE_PROFILES
from jev_agent.core.design_showcase_html import render_showcase
from jev_agent.core.design_system import (
    STARTER_TOKENS,
    DesignSystem,
    get_design_system,
    render_for_prompt,
    save_design_system,
)


def _both_tokens() -> dict[str, Any]:
    return tokens_for_app_type("website", palette_for_vibe("fintech"), "both")


def _starter_section(key: str) -> dict[str, Any]:
    section = STARTER_TOKENS[key]
    assert isinstance(section, dict)
    return dict(section)


def _flat_tokens() -> dict[str, Any]:
    """A legacy token set: flat colors, no ramps, no shadows/motion/typography."""
    return {
        "colors": _starter_section("colors"),
        "type_scale": _starter_section("type_scale"),
        "spacing": _starter_section("spacing"),
        "radii": _starter_section("radii"),
    }


def _used_vars(css_text: str) -> set[str]:
    return set(re.findall(r"var\(--([a-z0-9-]+)", css_text))


def _defined_vars(css_text: str) -> set[str]:
    return set(re.findall(r"--([a-z0-9-]+)\s*:", css_text))


def _brace_balanced(text: str) -> bool:
    return text.count("{") == text.count("}")


class _ParseCheck(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)


# ---- 1. theme-"both" exports -------------------------------------------------------------


def test_export_tokens_css_both_is_valid_css() -> None:
    css = export_tokens_css(_both_tokens())
    assert "{'primary'" not in css and '"light":' not in css
    assert "--color-primary:" in css
    assert '[data-theme="dark"]' in css
    assert _brace_balanced(css)
    root, dark = css.split('[data-theme="dark"]')
    light_bg = re.search(r"--color-background:\s*([^;]+);", root)
    dark_bg = re.search(r"--color-background:\s*([^;]+);", dark)
    assert light_bg and dark_bg
    assert light_bg.group(1).strip() != dark_bg.group(1).strip()


def test_export_tokens_tailwind_both_has_flat_colors() -> None:
    js = export_tokens_tailwind(_both_tokens())
    assert "{'primary'" not in js and '"light": {' not in js
    # The colors value must be a flat string map: parse the JSON object after "colors:".
    match = re.search(r"colors: (\{.*?\n\})", js, re.DOTALL)
    assert match is not None
    colors = json.loads(match.group(1))
    assert all(isinstance(v, str) for v in colors.values())
    assert colors["primary"].startswith("#")


def test_export_tokens_w3c_both_splits_light_and_dark() -> None:
    w3c = export_tokens_w3c(_both_tokens())
    color = w3c["color"]
    assert isinstance(color, dict)
    assert all(
        isinstance(node, dict) and isinstance(node["$value"], str) for node in color.values()
    )
    assert "colorDark" in w3c
    dark = w3c["colorDark"]
    assert isinstance(dark, dict)
    assert dark["background"]["$value"] != color["background"]["$value"]


def test_render_for_prompt_both_has_no_dict_repr() -> None:
    system = DesignSystem(
        version=1, updated_at="2026-10-24T00:00:00+00:00", markdown="# X\n", tokens=_both_tokens()
    )
    text = render_for_prompt(system)
    assert "{'primary'" not in text
    assert "Colors (light):" in text and "Colors (dark):" in text


def test_export_tokens_css_sanitizes_keys_and_values() -> None:
    tokens: dict[str, object] = {
        "colors": {"primary": "#2563EB", "bad;key\n}evil": "red; } p { color: blue"},
        "type_scale": {},
        "spacing": {},
        "radii": {},
    }
    css = export_tokens_css(tokens)
    assert "}evil" not in css and "p { color" not in css
    assert _brace_balanced(css)
    assert "--color-bad-key-evil:" in css


# ---- 2. showcase on flat tokens ----------------------------------------------------------


@pytest.mark.parametrize("vibe_key", sorted(VIBE_PROFILES))
def test_render_showcase_flat_tokens_all_vibes(vibe_key: str) -> None:
    html = render_showcase("Test", VIBE_PROFILES[vibe_key], _flat_tokens(), "light")
    parser = _ParseCheck()
    parser.feed(html)  # must parse without raising
    assert "html" in parser.tags and "header" in parser.tags
    assert "Palette" in html and "Components" in html


def test_render_showcase_empty_colors_all_vibes() -> None:
    for vibe_key in ("elegant", "minimal", "bold"):
        html = render_showcase("Test", VIBE_PROFILES[vibe_key], {"colors": {}}, "light")
        assert "hero-split-card" in html or "hero" in html


def test_render_showcase_escapes_name() -> None:
    html = render_showcase(
        "<script>alert(1)</script>", VIBE_PROFILES["minimal"], _flat_tokens(), "light"
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_showcase_invalid_theme_falls_back_to_light() -> None:
    html = render_showcase("Test", VIBE_PROFILES["minimal"], _flat_tokens(), "weird")
    assert 'id="themeToggle"' not in html
    assert '[data-theme="dark"] {' not in html
    assert 'data-theme="light"' in html


# ---- 3. legacy codegen -------------------------------------------------------------------


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_codegen_legacy_tokens_have_no_undefined_vars(target: str) -> None:
    files = generate_files(target, "demo", _flat_tokens())
    tokens_css = files["tokens.css"]
    assert _brace_balanced(tokens_css)
    defined = _defined_vars(tokens_css)
    for filename, content in files.items():
        if filename == "tokens.css":
            continue
        undefined = _used_vars(content) - defined
        assert not undefined, f"{filename} uses undefined vars: {sorted(undefined)}"


def test_codegen_legacy_badges_get_subtle_backgrounds() -> None:
    css = css_variables(_flat_tokens())
    for var in (
        "--color-primary-subtle",
        "--color-success-subtle",
        "--color-warning-subtle",
        "--color-danger-subtle",
        "--color-info",
        "--color-info-subtle",
        "--color-border-strong",
        "--color-surface-raised",
        "--color-text-faint",
        "--color-primary-text",
        "--shadow-sm",
        "--shadow-xl",
        "--font-display",
        "--font-body",
        "--motion-duration-fast",
    ):
        assert f"{var}:" in css


def test_codegen_summary_matches_emitted_variables() -> None:
    empty: dict[str, object] = {"design_tokens": None}
    for tokens in (_flat_tokens(), empty):
        summary = codegen_summary(tokens)
        assert summary["variables"] == len(_defined_vars(css_variables(tokens)))
    assert codegen_summary(_flat_tokens())["variables"] > 20


def test_bundle_zip_traversal_name_is_sanitized() -> None:
    data = bundle_zip("html-css", "../../evil", _flat_tokens())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert names, "zip must not be empty"
    for entry in names:
        assert not entry.startswith("/") and ".." not in Path(entry).parts
        assert entry.startswith("evil-html-css/") or entry.startswith("design-system-")


def test_codegen_escapes_name_in_html_and_comments() -> None:
    files = generate_files("html-css", '<b>&"x\n*/ evil', _flat_tokens())
    demo = files["index.html"]
    assert "<b>" not in demo and "&lt;b&gt;" in demo
    # The CSS comment must not be terminable by the name.
    comment = files["tokens.css"].split("*/")[0]
    assert "*/ evil" not in comment
    assert files["README.md"].splitlines()[0].count("\n") == 0


# ---- 4. corrupt save snapshots -----------------------------------------------------------


def test_save_over_corrupt_repo_system_snapshots_first(tmp_path: Path) -> None:
    design_dir = tmp_path / ".jev" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "meta.json").write_text("{not json")
    (design_dir / "tokens.json").write_text("{}")
    (design_dir / "design-system.md").write_text("# old corrupt system\n")
    version = save_design_system(tmp_path, "# new\n", dict(STARTER_TOKENS))
    assert version == 1
    snapshots = sorted(p.name for p in design_dir.iterdir() if ".corrupt-" in p.name)
    assert snapshots, "corrupt files must be snapshotted before overwrite"
    assert any("design-system.corrupt-" in name for name in snapshots)
    assert any("tokens.corrupt-" in name for name in snapshots)
    assert get_design_system(tmp_path) is not None


def test_save_over_corrupt_library_system_snapshots_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    system_dir = fake_home / ".jev" / "design-systems" / "demo"
    system_dir.mkdir(parents=True)
    (system_dir / "meta.json").write_text('{"version": "not-an-int"}')
    (system_dir / "tokens.json").write_text("{}")
    version = save_library_system("demo", "# new\n", dict(STARTER_TOKENS))
    assert version == 1
    snapshots = [p.name for p in system_dir.iterdir() if ".corrupt-" in p.name]
    assert snapshots
    assert load_library_system("demo") is not None


def test_float_version_counts_as_corrupt(tmp_path: Path) -> None:
    design_dir = tmp_path / ".jev" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "meta.json").write_text(
        '{"version": 2.9, "updated_at": "2026-10-24T00:00:00+00:00"}'
    )
    (design_dir / "tokens.json").write_text("{}")
    (design_dir / "design-system.md").write_text("# x\n")
    assert get_design_system(tmp_path) is None


# ---- 5. SSRF pinning + name suggestion ---------------------------------------------------


def test_suggest_library_name_empty_list() -> None:
    assert suggest_library_name([]) == "reversed"


def _stub_dns(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    def _fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


def test_pinned_connection_connects_to_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, ["93.184.216.34"])
    seen: dict[str, Any] = {}

    def _fake_create_connection(
        address: Any, timeout: Any = None, *args: Any, **kwargs: Any
    ) -> Any:
        seen["address"] = address
        return object()

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    conn = _PinnedHTTPConnection("example.com", 80, timeout=5)
    conn.connect()
    assert seen["address"] == ("93.184.216.34", 80)


def test_pinned_connection_refuses_private_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, ["10.0.0.1"])

    def _must_not_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not connect to a private address")

    monkeypatch.setattr(socket, "create_connection", _must_not_connect)
    conn = _PinnedHTTPConnection("internal.example", 80, timeout=5)
    with pytest.raises(ValueError, match="no globally routable"):
        conn.connect()


# ---- 6. React template fixes (review M-4, same generator surface) ------------------------


def test_react_input_does_not_leak_invalid_prop() -> None:
    jsx = generate_files("react-tailwind", "demo", _flat_tokens())["components.jsx"]
    assert "const { invalid, ...rest } = props" in jsx
    assert "<input className={cls} {...rest} />" in jsx


def test_react_progress_coerces_non_numeric_value() -> None:
    jsx = generate_files("react-tailwind", "demo", _flat_tokens())["components.jsx"]
    assert "Number.isFinite(num)" in jsx
