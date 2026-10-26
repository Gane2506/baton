"""Figma core: URL parsing, the REST client, file→tokens mapping, sync,
and the `use_figma` Plugin API code generation (variables, styles,
components, prototype reactions, granular edits). No network: the client
tests use `httpx.MockTransport`."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from jev_agent.core.figma import (
    FigmaClient,
    FigmaEdit,
    FigmaError,
    apply_sync,
    edit_from_form,
    file_to_tokens,
    parse_figma_url,
    reconcile_tokens,
    render_use_figma_edit,
    render_use_figma_script,
    rgba_to_hex,
    suggest_library_name,
)


def _sample_file() -> dict[str, object]:
    """A small Figma file: one named paint style, one unstyled fill, one
    named text style, auto-layout spacing and a corner radius."""
    return {
        "name": "Demo File",
        "document": {
            "children": [
                {
                    "name": "Frame",
                    "children": [
                        {
                            "name": "Btn",
                            "fills": [
                                {
                                    "type": "SOLID",
                                    "color": {"r": 0.149, "g": 0.384, "b": 0.921},
                                    "visible": True,
                                }
                            ],
                            "styles": {"fill": "S:1"},
                            "cornerRadius": 10,
                            "itemSpacing": 8,
                            "paddingLeft": 16,
                        },
                        {
                            "name": "Ghost",
                            "fills": [
                                {
                                    "type": "SOLID",
                                    "color": {"r": 0.0, "g": 0.0, "b": 0.0},
                                    "visible": True,
                                }
                            ],
                        },
                        {
                            "name": "Title",
                            "style": {"fontFamily": "Inter", "fontSize": 28},
                            "styles": {"text": "S:2"},
                        },
                    ],
                }
            ]
        },
        "styles": {
            "S:1": {"name": "Brand/Primary", "styleType": "FILL"},
            "S:2": {"name": "Heading", "styleType": "TEXT"},
        },
        "components": {"C:1": {"name": "Button"}, "C:2": {"name": "Card"}},
    }


# ---- URL parsing ---------------------------------------------------------------------------


def test_parse_figma_url_design_file_proto_and_node_id() -> None:
    ref = parse_figma_url("https://www.figma.com/design/abc123/My-File?node-id=12-34")
    assert ref.file_key == "abc123"
    assert ref.node_id == "12:34"
    assert ref.url == "https://www.figma.com/design/abc123/?node-id=12-34"
    assert parse_figma_url("https://www.figma.com/file/xyz9/Slug").file_key == "xyz9"
    assert parse_figma_url("https://www.figma.com/proto/xyz9/Slug").file_key == "xyz9"
    encoded = parse_figma_url("https://figma.com/design/k1/X?node-id=7%3A8")
    assert encoded.node_id == "7:8"
    assert parse_figma_url("https://www.figma.com/design/k1/X").node_id is None


def test_parse_figma_url_rejects_non_figma_and_bad_ids() -> None:
    for bad in (
        "https://example.com/design/abc/X",
        "https://www.figma.com/abc",
        "https://www.figma.com/design/",
        "not a url",
        "",
    ):
        with pytest.raises(FigmaError):
            parse_figma_url(bad)
    with pytest.raises(FigmaError, match="node id"):
        parse_figma_url("https://www.figma.com/design/k1/X?node-id=abc")


def test_suggest_library_name() -> None:
    assert suggest_library_name("My Cool File!") == "my-cool-file"
    assert suggest_library_name("") == "figma-import"
    assert suggest_library_name("!!!") == "figma-import"


def test_rgba_to_hex() -> None:
    assert rgba_to_hex({"r": 1, "g": 0.5, "b": 0}) == "#ff8000"
    assert rgba_to_hex({"r": 0, "g": 0, "b": 0, "a": 0.5}) == "#000000"
    assert rgba_to_hex({"r": 2, "g": 0, "b": 0}) is None
    assert rgba_to_hex({"r": 0.5}) is None
    assert rgba_to_hex("not a color") is None


# ---- REST client (MockTransport) ------------------------------------------------------------


def _section(tokens: dict[str, object], key: str) -> dict[str, object]:
    """One token section as a dict (asserted, so mypy can index it)."""
    section = tokens.get(key)
    assert isinstance(section, dict)
    return section


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_client_me_and_validate_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/me"
        assert request.headers["X-Figma-Token"] == "figd_test"
        return httpx.Response(200, json={"handle": "gane", "email": "g@example.com"})

    with FigmaClient("figd_test", transport=_transport(handler)) as client:
        assert client.validate_token() == "gane"
        assert client.me()["email"] == "g@example.com"


def test_client_validate_token_falls_back_to_email() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"email": "g@example.com"})

    with FigmaClient("figd_test", transport=_transport(handler)) as client:
        assert client.validate_token() == "g@example.com"


def test_client_401_is_a_plain_language_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": 401, "err": "Invalid token"})

    with (
        FigmaClient("bad", transport=_transport(handler)) as client,
        pytest.raises(FigmaError, match="rejected the token"),
    ):
        client.me()


def test_client_403_404_429() -> None:
    for status_code, fragment in ((403, "refused"), (404, "no such file"), (429, "rate-limited")):

        def handler(request: httpx.Request, status: int = status_code) -> httpx.Response:
            return httpx.Response(status, json={})

        with (
            FigmaClient("figd_test", transport=_transport(handler)) as client,
            pytest.raises(FigmaError, match=fragment),
        ):
            client.get_file("abc")


def test_client_requires_a_token() -> None:
    with pytest.raises(FigmaError, match="no Figma token"):
        FigmaClient("  ")


def test_client_variables_403_names_the_enterprise_gate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    with (
        FigmaClient("figd_test", transport=_transport(handler)) as client,
        pytest.raises(FigmaError, match="Enterprise-only"),
    ):
        client.get_local_variables("abc")


def test_client_get_file_nodes_passes_ids() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"nodes": {}})

    with FigmaClient("figd_test", transport=_transport(handler)) as client:
        assert client.get_file_nodes("abc", ["1:2", "3:4"]) == {"nodes": {}}
    assert "ids=1%3A2%2C3%3A4" in seen[0] or "ids=1:2,3:4" in seen[0]


# ---- file -> tokens -------------------------------------------------------------------------


def test_file_to_tokens_maps_styles_fills_type_spacing_radii() -> None:
    imported = file_to_tokens(_sample_file())
    assert imported.file_name == "Demo File"
    assert _section(imported.tokens, "colors")["primary"] == "#2662eb"
    # The unstyled black fill joins as a numbered color, not a named one.
    assert "#000000" in _section(imported.tokens, "colors").values()
    assert _section(imported.tokens, "type_scale")["heading"] == 28
    assert imported.tokens["spacing"] == {"xs": 8, "sm": 16}
    assert imported.tokens["radii"] == {"xs": 10}
    assert imported.component_names == ["Button", "Card"]
    assert imported.fonts == ["Inter"]
    assert imported.style_names == ["Brand/Primary", "Heading"]
    assert imported.fallbacks == []


def test_file_to_tokens_falls_back_and_says_so() -> None:
    imported = file_to_tokens({"name": "Empty", "document": {"children": []}})
    assert _section(imported.tokens, "colors")["primary"] == "#2563EB"
    assert imported.tokens["spacing"] == {"xs": 4, "sm": 8, "md": 16, "lg": 24, "xl": 32, "2xl": 48}
    assert any("spacing" in note for note in imported.fallbacks)
    assert any("radii" in note for note in imported.fallbacks)
    assert "Defaults used" in imported.markdown


def test_file_to_tokens_ignores_hidden_and_non_solid_fills() -> None:
    file_json: dict[str, object] = {
        "name": "F",
        "document": {
            "children": [
                {
                    "name": "A",
                    "fills": [
                        {"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0}, "visible": False},
                        {"type": "GRADIENT_LINEAR", "color": {"r": 0, "g": 1, "b": 0}},
                    ],
                }
            ]
        },
    }
    imported = file_to_tokens(file_json)
    assert "#ff0000" not in _section(imported.tokens, "colors").values()
    assert "#00ff00" not in _section(imported.tokens, "colors").values()


# ---- sync -----------------------------------------------------------------------------------


def test_reconcile_and_apply() -> None:
    current: dict[str, object] = {
        "colors": {"primary": "#111111", "gone": "#000000"},
        "type_scale": {"base": 15},
        "spacing": {},
        "radii": {},
    }
    incoming: dict[str, object] = {
        "colors": {"primary": "#2661eb", "accent": "#00ff00"},
        "type_scale": {"base": 15},
        "spacing": {"md": 16},
        "radii": {},
    }
    diff = reconcile_tokens(current, incoming)
    assert diff.summary() == "2 added, 1 changed, 1 removed in Figma (kept in Baton)"
    assert diff.added == {"colors": {"accent": "#00ff00"}, "spacing": {"md": 16}}
    assert diff.changed == {"colors": {"primary": ("#111111", "#2661eb")}}
    assert diff.removed == {"colors": {"gone": "#000000"}}
    merged = apply_sync(current, diff)
    assert merged["colors"] == {"primary": "#2661eb", "gone": "#000000", "accent": "#00ff00"}
    assert merged["spacing"] == {"md": 16}
    # The input is never mutated — the caller saves the merged dict as a new version.
    assert _section(current, "colors")["primary"] == "#111111"


def test_reconcile_empty_when_in_sync() -> None:
    tokens: dict[str, object] = {
        "colors": {"a": "#000000"},
        "type_scale": {},
        "spacing": {},
        "radii": {},
    }
    diff = reconcile_tokens(tokens, dict(tokens))
    assert diff.empty()
    assert diff.summary() == "already in sync"


# ---- `use_figma` script generation ----------------------------------------------------------


def test_render_export_script_covers_variables_styles_components_prototype() -> None:
    tokens = {
        "colors": {
            "light": {"primary": "#2563EB", "background": "#FFFFFF"},
            "dark": {"primary": "#1D4ED8", "background": "#0F172A"},
        },
        "type_scale": {"base": 15, "lg": 18},
        "spacing": {"md": 16},
        "radii": {"md": 10},
    }
    script = render_use_figma_script("acme", tokens, file_key="abc123")
    assert 'figma.variables.createVariableCollection("Baton / acme")' in script
    assert "collection.renameMode(lightMode.modeId, 'Light')" in script
    assert "collection.addMode('Dark')" in script
    assert "createVariable(\"color/primary\", collection, 'COLOR')" in script
    assert "setValueForMode(lightMode.modeId" in script
    assert "setValueForMode(darkMode.modeId" in script
    assert "figma.createPaintStyle()" in script
    assert "figma.createTextStyle()" in script
    assert "figma.createComponent()" in script
    assert "setReactionsAsync" in script
    assert "SMART_ANIMATE" in script
    assert "https://www.figma.com/design/abc123/" in script
    assert "REST API cannot run" in script


def test_render_export_script_escapes_hostile_names() -> None:
    tokens: dict[str, object] = {
        "colors": {"primary": "#2563EB"},
        "type_scale": {},
        "spacing": {},
        "radii": {},
    }
    script = render_use_figma_script('a"; evil();//', tokens)
    # The name is a JSON-escaped string literal wherever it is code — the raw
    # text appears only in the header comment, which cannot execute.
    assert 'createVariableCollection("Baton / a\\"; evil();//")' in script
    assert [line for line in script.splitlines() if 'a"; evil' in line] == [
        '// System: a"; evil();//'
    ]
    # A newline in the name cannot end the header comment and inject a line.
    injected = render_use_figma_script("x\nmalicious();", tokens)
    assert "\nmalicious();" not in injected
    assert "// System: x malicious();" in injected


def test_render_export_script_flat_colors() -> None:
    tokens: dict[str, object] = {
        "colors": {"primary": "#2563EB"},
        "type_scale": {},
        "spacing": {},
        "radii": {},
    }
    script = render_use_figma_script("flat", tokens)
    assert 'createVariable("color/primary", collection' in script
    # No dark mode values when the system has no dark palette.
    assert script.count("setValueForMode(darkMode.modeId") == 0


# ---- granular edits -------------------------------------------------------------------------


def test_render_edit_variable_color_and_number() -> None:
    color = render_use_figma_edit(
        FigmaEdit(
            kind="variable",
            collection="Baton / acme",
            name="color/primary",
            mode="Dark",
            value="#1D4ED8",
        )
    )
    assert "getLocalVariableCollections" in color
    assert "setValueForMode(mode.modeId" in color
    assert '"Dark"' in color
    number = render_use_figma_edit(
        FigmaEdit(kind="variable", collection="Baton / acme", name="spacing/md", value="16")
    )
    assert "16" in number


def test_render_edit_node_kinds() -> None:
    fill = render_use_figma_edit(FigmaEdit(kind="node-fill", node_id="12:34", value="#ff0000"))
    assert 'getNodeByIdAsync("12:34")' in fill
    assert "node.fills" in fill
    text = render_use_figma_edit(FigmaEdit(kind="node-text", node_id="12:34", value="Hello"))
    assert "node.characters" in text
    assert "loadFontAsync" in text
    size = render_use_figma_edit(
        FigmaEdit(kind="node-size", node_id="12:34", width=320, height=180)
    )
    assert "node.resize(320" in size
    radius = render_use_figma_edit(FigmaEdit(kind="corner-radius", node_id="12:34", value="12"))
    assert "node.cornerRadius = 12" in radius
    reaction = render_use_figma_edit(
        FigmaEdit(
            kind="reaction",
            node_id="12:34",
            destination_id="56:78",
            trigger="ON_HOVER",
            transition="DISSOLVE",
        )
    )
    assert "setReactionsAsync" in reaction
    assert '"ON_HOVER"' in reaction
    assert '"DISSOLVE"' in reaction
    # Every snippet inspects first and verifies after.
    for snippet in (fill, text, size, radius, reaction):
        assert "if (!node) throw" in snippet
        assert "get_screenshot" in snippet


def test_render_edit_rejects_bad_inputs() -> None:
    cases = [
        FigmaEdit(kind="mystery"),
        FigmaEdit(kind="variable", collection="", name="color/primary", value="#000000"),
        FigmaEdit(
            kind="variable", collection="C", name="color/primary", mode="Sepia", value="#000000"
        ),
        FigmaEdit(kind="variable", collection="C", name="color/primary", value="banana"),
        FigmaEdit(kind="node-fill", node_id="abc", value="#000000"),
        FigmaEdit(kind="node-fill", node_id="1:2", value="red"),
        FigmaEdit(kind="node-text", node_id="1:2", value="  "),
        FigmaEdit(kind="node-size", node_id="1:2", width=0, height=10),
        FigmaEdit(kind="corner-radius", node_id="1:2", value="big"),
        FigmaEdit(kind="reaction", node_id="1:2", destination_id="nope"),
        FigmaEdit(kind="reaction", node_id="1:2", destination_id="3:4", trigger="ON_BLINK"),
        FigmaEdit(kind="reaction", node_id="1:2", destination_id="3:4", transition="TELEPORT"),
    ]
    for edit in cases:
        with pytest.raises(FigmaError):
            render_use_figma_edit(edit)


def test_edit_from_form() -> None:
    edit = edit_from_form(
        "variable",
        {
            "collection": "Baton / acme",
            "name": "color/primary",
            "mode": "Light",
            "value": "#2563EB",
        },
    )
    assert edit.kind == "variable"
    assert edit.name == "color/primary"
    with pytest.raises(FigmaError, match="numbers"):
        edit_from_form("node-size", {"node_id": "1:2", "width": "wide", "height": "10"})
