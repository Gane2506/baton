"""Invalid CSS from nested design tokens (issue #279).

A system whose tokens store colors as ``{"light": {...}, "dark": {...}}``
(the cxm shape — legacy tokens with no ``design_tokens`` key) used to
generate ``tokens.css`` with Python dict reprs as custom-property values::

    --color-dark: 'primary': '#DBA724', 'primary_hover': '#E7BD55', ...;

and the detail page's Colors table showed the same reprs as row values.
The codegen now unwraps the light/dark palettes (light under ``:root``,
dark under ``[data-theme="dark"]``) and flattens any deeper nesting
instead of stringifying dicts.
"""

from __future__ import annotations

import re
from typing import Any

from jev_agent.core.design_codegen import css_variables, generate_files

# The cxm system's token shape: legacy, no ``design_tokens`` key, colors
# nested as light/dark palettes.
_CXM_TOKENS: dict[str, Any] = {
    "app_type": "pwa",
    "theme": "both",
    "colors": {
        "light": {
            "primary": "#DBA724",
            "primary_hover": "#B88D1E",
            "primary_text": "#101418",
            "background": "#FFFFFF",
            "surface": "#F8F6F1",
            "text": "#211D12",
            "text_muted": "#81775F",
            "border": "#E7E3DA",
            "success": "#16A34A",
            "warning": "#D97706",
            "danger": "#DC2626",
        },
        "dark": {
            "primary": "#DBA724",
            "primary_hover": "#E7BD55",
            "primary_text": "#101418",
            "background": "#18150C",
            "surface": "#282315",
            "text": "#F1EFEA",
            "text_muted": "#ADA490",
            "border": "#403926",
            "success": "#22C55E",
            "warning": "#F59E0B",
            "danger": "#F87171",
        },
    },
    "type_scale": {"base": 16},
    "spacing": {"md": 16},
    "radii": {"md": 10},
}

_DECLARATION = re.compile(r"^\s+(--[\w-]+):\s*(.*?);\s*$")


def _custom_properties(css: str) -> list[tuple[str, str]]:
    """(name, value) for every custom-property declaration in ``css``."""
    properties: list[tuple[str, str]] = []
    for line in css.splitlines():
        match = _DECLARATION.match(line)
        if match:
            properties.append((match.group(1), match.group(2)))
    return properties


def _assert_valid_css(css: str) -> list[tuple[str, str]]:
    """Every custom-property value is a single valid CSS value.

    A dict repr leaks in as ``'key': 'value'`` pairs (braces stripped by
    ``_css_value``); a single CSS value never contains a ``': '`` pair, a
    brace, or a semicolon. Returns the parsed declarations for callers
    that also want to assert on specific values.
    """
    properties = _custom_properties(css)
    assert properties, "no custom properties found — expected a tokens file"
    for name, value in properties:
        assert value, f"{name} has an empty value"
        assert "{" not in value and "}" not in value, f"{name} holds a dict repr: {value!r}"
        assert "': " not in value, f"{name} holds a dict repr: {value!r}"
        assert ";" not in value, f"{name} holds multiple declarations: {value!r}"
    return properties


def test_css_variables_flattens_legacy_light_dark_tokens() -> None:
    css = css_variables(_CXM_TOKENS)
    _assert_valid_css(css)
    root_css, _, dark_css = css.partition('[data-theme="dark"]')
    values = dict(_custom_properties(root_css))
    assert "--color-light" not in values
    assert "--color-dark" not in values
    # Light palette under :root.
    assert values["--color-primary"] == "#DBA724"
    assert values["--color-primary-hover"] == "#B88D1E"
    assert values["--color-background"] == "#FFFFFF"
    # Dark palette under [data-theme="dark"], not the light values.
    dark = dict(_custom_properties(dark_css))
    assert dark["--color-primary-hover"] == "#E7BD55"
    assert dark["--color-background"] == "#18150C"
    assert dark["--color-text"] == "#F1EFEA"


def test_css_variables_flattens_deeper_nesting() -> None:
    tokens: dict[str, Any] = {
        "design_tokens": {
            "colors": {
                "light": {"primary": {"base": "#111111", "hover": "#222222"}},
                "dark": {"primary": {"base": "#333333", "hover": "#444444"}},
            }
        }
    }
    css = css_variables(tokens)
    _assert_valid_css(css)
    root_css, _, dark_css = css.partition('[data-theme="dark"]')
    properties = dict(_custom_properties(root_css))
    assert properties["--color-primary-base"] == "#111111"
    assert properties["--color-primary-hover"] == "#222222"
    dark = dict(_custom_properties(dark_css))
    assert dark["--color-primary-base"] == "#333333"
    assert dark["--color-primary-hover"] == "#444444"


def test_generate_files_tokens_css_valid_for_both_targets() -> None:
    for target in ("html-css", "react-tailwind"):
        tokens_css = generate_files(target, "cxm", _CXM_TOKENS)["tokens.css"]
        _assert_valid_css(tokens_css)
        assert "--color-primary: #DBA724;" in tokens_css
        assert '[data-theme="dark"]' in tokens_css


def test_css_variables_flat_legacy_tokens_unchanged() -> None:
    tokens: dict[str, Any] = {
        "colors": {"primary": "#e8590c", "ink": "#1a1b1e"},
        "type_scale": {"base": 16},
        "spacing": {"md": 16},
        "radii": {"md": 8},
    }
    css = css_variables(tokens)
    properties = dict(_assert_valid_css(css))
    assert properties["--color-primary"] == "#e8590c"
    assert properties["--color-ink"] == "#1a1b1e"
