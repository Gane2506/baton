"""Theme profiles: every profile (+ auto) resolves to a complete variable set.

The dashboard's color system is CSS-variables-only: the :root block is the
default (dark) profile, each named profile overrides the full set under
:root[data-theme="..."], and "auto" follows the OS through a
prefers-color-scheme media query. theme.js exposes the JevTheme API that the
sidebar uses to switch profiles.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STYLE = PROJECT_ROOT / "src" / "jev_agent" / "dashboard" / "static" / "style.css"
THEME_JS = PROJECT_ROOT / "src" / "jev_agent" / "dashboard" / "static" / "theme.js"

COLOR_VARS = [
    "--bg",
    "--surface",
    "--surface-2",
    "--hover",
    "--fg",
    "--muted",
    "--faint",
    "--border",
    "--border-strong",
    "--accent",
    "--accent-ink",
    "--accent-soft",
    "--ok",
    "--warn",
    "--bad",
    "--info",
    "--bar",
    "--code-bg",
]

PROFILES = ["dark", "light", "oled", "solarized", "high-contrast"]


def _no_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css: str) -> list[tuple[str, str]]:
    """Yield (selector, body) for top-level CSS rules, brace-matched."""
    css = _no_comments(css)
    results: list[tuple[str, str]] = []
    i = 0
    while i < len(css):
        open_at = css.find("{", i)
        if open_at < 0:
            break
        selector = css[i:open_at].strip()
        depth = 0
        j = open_at
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        results.append((selector, css[open_at + 1 : j]))
        i = j + 1
    return results


def _declared_vars(body: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", body))


def _profile_body(css: str, profile: str) -> str:
    blocks = _blocks(css)
    if profile == "dark":
        # dark is the default: the plain :root block
        for selector, body in blocks:
            if selector == ":root":
                return body
        raise AssertionError("no plain :root block (dark default) in style.css")
    for selector, body in blocks:
        if selector == f':root[data-theme="{profile}"]':
            return body
    raise AssertionError(f"no [data-theme={profile!r}] block in style.css")


def _var_value(body: str, var: str) -> str:
    match = re.search(rf"{re.escape(var)}\s*:\s*([^;]+);", body)
    assert match, f"{var} not declared"
    return match.group(1).strip()


def test_all_profiles_declare_complete_variable_set() -> None:
    css = STYLE.read_text(encoding="utf-8")
    for profile in PROFILES:
        body = _profile_body(css, profile)
        missing = [v for v in COLOR_VARS if v not in _declared_vars(body)]
        assert not missing, f"profile {profile!r} missing variables: {missing}"


def test_auto_follows_os_to_light_variable_set() -> None:
    css = STYLE.read_text(encoding="utf-8")
    media = [body for selector, body in _blocks(css) if "prefers-color-scheme" in selector]
    assert media, "no prefers-color-scheme block (auto theme) in style.css"
    inner = [body for selector, body in _blocks(media[0]) if selector == ":root:not([data-theme])"]
    assert inner, "auto theme must apply on :root without a data-theme attribute"
    missing = [v for v in COLOR_VARS if v not in _declared_vars(inner[0])]
    assert not missing, f"auto (light) missing variables: {missing}"


def test_auto_light_stays_in_sync_with_light_profile() -> None:
    css = STYLE.read_text(encoding="utf-8")
    media = [body for selector, body in _blocks(css) if "prefers-color-scheme: light" in selector]
    auto_body = _blocks(media[0])[0][1]
    light_body = _profile_body(css, "light")
    for var in COLOR_VARS:
        assert _var_value(auto_body, var) == _var_value(light_body, var), (
            f"auto light block drifted from [data-theme=light] on {var}"
        )


def test_no_hardcoded_colors_outside_profile_blocks() -> None:
    css = STYLE.read_text(encoding="utf-8")
    profile_selectors = {":root", ":root:not([data-theme])"} | {
        f':root[data-theme="{p}"]' for p in PROFILES
    }
    for selector, body in _blocks(css):
        if selector.startswith("@media") and "prefers-color-scheme" in selector:
            for inner_selector, _ in _blocks(body):
                assert inner_selector in profile_selectors, inner_selector
            continue
        if selector in profile_selectors:
            continue
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), (
            f"hardcoded color outside a theme profile block: {selector}"
        )


def test_theme_js_exposes_jev_theme_api() -> None:
    src = THEME_JS.read_text(encoding="utf-8")
    assert "window.JevTheme" in src
    for name in ("set", "list", "current", "effective"):
        assert re.search(rf"\b{name}\s*:", src), f"JevTheme.{name} missing"
    for profile in ["auto", "dark", "light", "oled", "solarized", "high-contrast"]:
        assert f'"{profile}"' in src, f"profile {profile!r} not listed in theme.js"
    assert '"jev-theme"' in src, "localStorage key jev-theme not used"


def test_theme_js_still_supports_plain_theme_select() -> None:
    src = THEME_JS.read_text(encoding="utf-8")
    assert 'id !== "theme"' in src
    assert 'querySelectorAll("select#theme")' in src or "getElementById" in src
