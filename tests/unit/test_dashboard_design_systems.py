"""The dashboard's design-systems pages: the library list, the detail view, and the
`use` / `clone` / `export` / `delete` actions (issue #118)."""

from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.core.design_library import list_library, save_library_system
from jev_agent.dashboard.app import NAV, create_app
from jev_agent.dashboard.security import DashboardSettings

TOKENS = {
    "colors": {"primary": "#e8590c", "ink": "#1a1b1e"},
    "type_scale": {"base": 16},
    "spacing": {"md": 16},
    "radii": {"md": 8},
}
MARKDOWN = (
    "# Acme Brand\n\nA warm, confident marketing system.\n\n## Components\n\nButtons and cards.\n"
)


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _seed(name: str = "acme-brand", **meta: object) -> int:
    extras: dict[str, object] = {
        "name": name,
        "source": "wizard",
        "app_type": "marketing",
        "stack": "Next.js",
    }
    extras.update(meta)
    return save_library_system(name, MARKDOWN, dict(TOKENS), extras)


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    """POST with a valid CSRF token, following redirects so flashes are visible."""
    # The TestClient returns httpx2.Response when httpx2 is installed (it
    # is, via the code-orchestrator extra -> huggingface_hub) and
    # httpx.Response otherwise; cast to the declared type (both share the
    # same response API).
    return cast(
        httpx.Response,
        client.post(
            url,
            data={"csrf_token": _csrf(client), **(data or {})},
            follow_redirects=True,
        ),
    )


def _visible(response: httpx.Response) -> str:
    """The page text as the user sees it (flashes render quotes as `&#34;`)."""
    return html.unescape(response.text)


def _repo_meta(repo: Path) -> dict[str, object]:
    raw = json.loads((repo / ".jev" / "design" / "meta.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


# ---- nav ---------------------------------------------------------------------------------


def test_nav_lists_design_systems() -> None:
    assert any(item.href == "/design-systems" and item.label == "Design Systems" for item in NAV)
    hrefs = [item.href for item in NAV]
    assert len(hrefs) == len(set(hrefs)), "duplicate href in dashboard NAV"


def test_nav_link_renders_and_is_active(client: TestClient, home: Path, fixture_repo: Path) -> None:
    board = client.get("/")
    assert board.status_code == 200
    assert 'href="/design-systems"' in board.text
    page = client.get("/design-systems")
    assert page.status_code == 200
    assert re.search(
        r'<a href="/design-systems"[^>]*class="active"[^>]*aria-current="page"', page.text
    )


# ---- list page -----------------------------------------------------------------------------


def test_empty_state_points_at_studio(client: TestClient, home: Path) -> None:
    page = client.get("/design-systems")
    assert page.status_code == 200
    assert "No design systems in the library" in page.text
    # Browser-first (issue #171): the empty state points at the Design studio page,
    # never at CLI commands.
    assert 'href="/design-studio"' in page.text
    # Cycle 3 (issue #186): the two actions link straight to their routes.
    assert 'href="/design-studio/reverse"' in page.text
    # Issue #200: the wizard start is a POST, so the empty state links at the
    # studio landing instead of GETting the state-changing start route.
    assert 'href="/design-studio/new"' not in page.text
    assert "jev design reverse" not in page.text
    assert "jev design new" not in page.text


def test_list_shows_systems_with_metadata(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    _seed("other", source="reverse:https://example.com", app_type="website", stack="-")
    page = client.get("/design-systems")
    assert page.status_code == 200
    for expected in (
        "acme-brand",
        "other",
        "wizard",
        "reverse:https://example.com",
        "marketing",
        "website",
        "Next.js",
        "v1",
    ):
        assert expected in page.text
    assert 'href="/design-systems/acme-brand"' in page.text


def test_list_search_filters_by_name(client: TestClient, home: Path) -> None:
    # Cycle 3 workstream C (issue #184): the library list searches like chats
    # do -- server-side, on the system name, case-insensitively, with a
    # clearable no-match empty state instead of the plain "no systems" one.
    _seed("acme-brand")
    _seed("other-system")
    page = client.get("/design-systems?q=ACME")
    assert "acme-brand" in page.text
    assert "other-system" not in page.text
    assert 'value="ACME"' in page.text  # the query survives in the search box
    page = client.get("/design-systems?q=zzz-no-such-system")
    assert "other-system" not in page.text
    assert "No design systems match" in page.text and "clear the search" in page.text
    assert "other-system" in client.get("/design-systems").text  # unfiltered still full


# ---- detail page -----------------------------------------------------------------------------


def test_detail_shows_tokens_and_system(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert "#e8590c" in page.text  # tokens, read-only
    assert "Buttons and cards" in page.text  # the rendered system markdown
    assert "Clone to a new name" in page.text
    assert "Export tokens" in page.text


def test_detail_flattens_light_dark_colors(client: TestClient, home: Path) -> None:
    """Issue #279: a light/dark colors dict renders as flattened rows, not reprs."""
    tokens: dict[str, object] = {
        "colors": {
            "light": {"primary": "#DBA724", "background": "#FFFFFF"},
            "dark": {"primary": "#DBA724", "background": "#18150C"},
        },
        "type_scale": {"base": 16},
        "spacing": {"md": 16},
        "radii": {"md": 8},
    }
    save_library_system("cxm", MARKDOWN, tokens, {"source": "wizard"})
    # TestClient returns httpx2.Response when httpx2 is installed (via the
    # code-orchestrator extra); cast like _post does — same response API.
    response = cast(httpx.Response, client.get("/design-systems/cxm"))
    assert response.status_code == 200
    page = _visible(response)
    assert "light.primary" in page
    assert "dark.background" in page
    assert "#18150C" in page
    assert "{'primary'" not in page
    assert "primary_hover" not in page  # no repr fragments leak through


def test_detail_unknown_name_404(client: TestClient, home: Path) -> None:
    assert client.get("/design-systems/nope").status_code == 404


def test_detail_invalid_name_404(client: TestClient, home: Path) -> None:
    assert client.get("/design-systems/bad%20name!").status_code == 404


# ---- use ---------------------------------------------------------------------------------


def test_use_installs_into_repo(client: TestClient, home: Path, fixture_repo: Path) -> None:
    _seed("acme-brand")
    response = _post(client, "/design-systems/use", {"name": "acme-brand"})
    assert response.status_code == 200
    assert 'installed "acme-brand" as the repo design system v1' in _visible(response)
    meta = _repo_meta(fixture_repo)
    assert meta["source"] == "library:acme-brand"
    assert meta["app_type"] == "marketing"  # library extras carried over
    page = client.get("/design-systems")
    assert "active" in page.text  # the active badge is shown


def test_use_unknown_system_flashes_error(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-systems/use", {"name": "nope"})
    assert response.status_code == 200
    assert 'no design system named "nope"' in _visible(response)


def test_use_rejects_path_traversal(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-systems/use", {"name": "../evil"})
    assert response.status_code == 200
    assert "invalid library design system name" in _visible(response)


# ---- clone ---------------------------------------------------------------------------------


def test_clone_copies_to_new_name(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    response = _post(
        client, "/design-systems/clone", {"name": "acme-brand", "new_name": "acme-copy"}
    )
    assert response.status_code == 200
    assert 'cloned "acme-brand" to "acme-copy"' in _visible(response)
    assert "acme-copy" in list_library()


def test_clone_to_existing_name_flashes_error(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    _seed("acme-copy")
    response = _post(
        client, "/design-systems/clone", {"name": "acme-brand", "new_name": "acme-copy"}
    )
    assert response.status_code == 200
    assert 'a design system named "acme-copy" already exists' in _visible(response)


# ---- export ---------------------------------------------------------------------------------


def test_export_w3c_download(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    response = client.post(
        "/design-systems/export",
        data={"csrf_token": _csrf(client), "name": "acme-brand", "format": "w3c"},
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="acme-brand.tokens.json"'
    )
    w3c = json.loads(response.text)
    assert w3c["color"]["primary"] == {"$value": "#e8590c", "$type": "color"}


def test_export_tailwind_and_css(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    token = _csrf(client)
    tailwind = client.post(
        "/design-systems/export",
        data={"csrf_token": token, "name": "acme-brand", "format": "tailwind"},
    )
    assert tailwind.status_code == 200
    assert "module.exports" in tailwind.text and "#e8590c" in tailwind.text
    css = client.post(
        "/design-systems/export",
        data={"csrf_token": token, "name": "acme-brand", "format": "css"},
    )
    assert css.status_code == 200
    assert ":root" in css.text and "--color-primary: #e8590c;" in css.text


def test_export_unknown_format_flashes_error(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    response = _post(client, "/design-systems/export", {"name": "acme-brand", "format": "bogus"})
    assert response.status_code == 200
    assert "unknown export format" in _visible(response)


# ---- delete (two-step, like the other destructive dashboard actions) ----------------------------


def test_delete_asks_first_then_deletes(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    confirm = _post(client, "/design-systems/delete", {"name": "acme-brand"})
    assert confirm.status_code == 200
    assert "Delete design system acme-brand?" in confirm.text
    assert "acme-brand" in list_library()  # not deleted yet
    done = _post(client, "/design-systems/delete", {"name": "acme-brand", "confirm": "yes"})
    assert done.status_code == 200
    assert 'deleted "acme-brand" from the design library' in _visible(done)
    assert list_library() == []


def test_delete_unknown_system_flashes_error(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-systems/delete", {"name": "nope"})
    assert response.status_code == 200
    assert 'no design system named "nope"' in _visible(response)


def test_delete_confirm_get_redirects_to_library(client: TestClient, home: Path) -> None:
    """Refreshing the delete confirmation (GET, issue #316) goes back to the
    library, never a 404/405 — no system is named "delete"."""
    response = client.get("/design-systems/delete", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/design-systems"


def test_delete_confirm_mentions_active_system(
    client: TestClient, home: Path, fixture_repo: Path
) -> None:
    _seed("acme-brand")
    _post(client, "/design-systems/use", {"name": "acme-brand"})
    confirm = _post(client, "/design-systems/delete", {"name": "acme-brand"})
    assert "keeps its own installed" in confirm.text


# ---- security ---------------------------------------------------------------------------------


def test_actions_require_csrf(client: TestClient, home: Path) -> None:
    _seed("acme-brand")
    for url, data in (
        ("/design-systems/use", {"name": "acme-brand"}),
        ("/design-systems/clone", {"name": "acme-brand", "new_name": "x"}),
        ("/design-systems/export", {"name": "acme-brand", "format": "w3c"}),
        ("/design-systems/showcase-refresh", {"name": "acme-brand"}),
        ("/design-systems/delete", {"name": "acme-brand"}),
    ):
        assert client.post(url, data=data).status_code == 403
    assert "acme-brand" in list_library()  # nothing happened


def test_unknown_action_404(client: TestClient, home: Path) -> None:
    response = client.post("/design-systems/bogus", data={"csrf_token": _csrf(client), "name": "x"})
    assert response.status_code == 404


# ---- showcase refresh (issue #323) ---------------------------------------------------------

_STALE_SHOWCASE = "<html><head><title>acme-brand</title></head><body>stale showcase</body></html>"


def test_showcase_refresh_rewrites_stale_saved_showcase(client: TestClient, home: Path) -> None:
    """Issue #323: a system saved before the #322 showcase CSS fix keeps
    serving its stale saved file — the refresh re-renders it with the
    current generator, touches nothing else, and matches the on-demand
    render byte for byte."""
    _seed("acme-brand")
    system_path = library_system_path("acme-brand")
    (system_path / "showcase.html").write_text(_STALE_SHOWCASE, encoding="utf-8")
    tokens_before = (system_path / "tokens.json").read_bytes()
    meta_before = (system_path / "meta.json").read_bytes()
    # The stale file is what the showcase route serves, as-is.
    assert client.get("/design-systems/acme-brand/showcase").text == _STALE_SHOWCASE

    response = _post(client, "/design-systems/showcase-refresh", {"name": "acme-brand"})
    assert response.status_code == 200
    assert 'regenerated the showcase for "acme-brand"' in _visible(response)

    refreshed = (system_path / "showcase.html").read_text(encoding="utf-8")
    assert refreshed != _STALE_SHOWCASE
    assert "@media(max-width:720px)" in refreshed  # the #322 mobile fix
    assert "clamp(" in refreshed  # the #322 type-specimen fix
    # Tokens, meta and the version are untouched — only showcase.html moves.
    assert (system_path / "tokens.json").read_bytes() == tokens_before
    assert (system_path / "meta.json").read_bytes() == meta_before
    # The refreshed file is exactly what the route renders on demand.
    (system_path / "showcase.html").unlink()
    assert client.get("/design-systems/acme-brand/showcase").text == refreshed


def test_showcase_refresh_creates_showcase_when_missing(client: TestClient, home: Path) -> None:
    """Issue #323: a system with no saved showcase at all gets one written."""
    _seed("acme-brand")
    system_path = library_system_path("acme-brand")
    assert not (system_path / "showcase.html").exists()
    response = _post(client, "/design-systems/showcase-refresh", {"name": "acme-brand"})
    assert response.status_code == 200
    assert 'regenerated the showcase for "acme-brand"' in _visible(response)
    saved = (system_path / "showcase.html").read_text(encoding="utf-8")
    assert "@media(max-width:720px)" in saved


def test_showcase_refresh_unknown_system_flashes_error(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-systems/showcase-refresh", {"name": "nope"})
    assert response.status_code == 200
    assert 'no design system named "nope"' in _visible(response)


def test_detail_header_offers_showcase_refresh(client: TestClient, home: Path) -> None:
    """Issue #323: the refresh is a visible action in the system page
    header, next to Preview showcase — browser-first, no terminal."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert 'action="/design-systems/showcase-refresh"' in page.text
    assert "Regenerate showcase" in page.text


# ---- redesign (issue #285) ---------------------------------------------------------------

from jev_agent.core.design_library import library_system_path  # noqa: E402
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens  # noqa: E402
from jev_agent.core.logo import generate_mark, mark_to_png, mark_to_svg  # noqa: E402

_SECTION_IDS = (
    "overview",
    "colors",
    "typography",
    "spacing",
    "corner-radius",
    "shadows",
    "layout",
    "z-index",
    "duration",
    "easing",
    "stagger",
    "breakpoint",
    "components",
    "assets",
    "files",
)


def test_detail_redesign_renders_every_section_inline(client: TestClient, home: Path) -> None:
    """Issue #285: the detail page is the single-page reference — all 14
    categories + Files, live components, no extra click, no inline styles."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    for section in _SECTION_IDS:
        assert f'id="{section}"' in page.text
    for label in (
        "Overview",
        "Colors",
        "Typography",
        "Spacing",
        "Corner radius",
        "Shadows",
        "Layout",
        "Z index",
        "Duration",
        "Easing",
        "Stagger",
        "Breakpoint",
        "Components",
        "Assets",
        "Files",
    ):
        assert label in page.text
    for target in (
        "copy-colors",
        "copy-typography",
        "copy-spacing",
        "copy-radii",
        "copy-shadows",
        "copy-motion",
    ):
        assert f'data-copy-target="{target}"' in page.text
    # Studio-standard scales are labelled as such, never as system tokens.
    assert page.text.count("Studio standard — not a system token") == 4
    # Live components from the reference: ticker, toast, tooltip, videomodal.
    assert "Ticker" in page.text and "Show another toast" in page.text
    assert "Tooltip" in page.text and "VideoModal" in page.text
    assert "Static preview" in page.text
    # The CSP forbids inline styles: the page must carry none
    # (a bare `style="` would also match data-ui-style, hence the space).
    assert ' style="' not in page.text
    # Library actions still live on the page (old tests' anchors).
    assert "Clone to a new name" in page.text
    assert "Export tokens" in page.text


def test_detail_rich_tokens_show_full_ramps(client: TestClient, home: Path) -> None:
    """Issue #285: a wizard system shows the rich ramps, durations, easings."""
    rich = rich_tokens(VIBE_PROFILES["minimal"], "both")
    tokens = dict(TOKENS)
    tokens["design_tokens"] = rich
    save_library_system("rich-brand", MARKDOWN, tokens, {"vibe_profile": "minimal"})
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert "primary_hover" in page.text
    assert "accent_500" in page.text
    assert "duration_fast" in page.text
    assert "ease_standard" in page.text
    assert "display_xl" in page.text


def test_duration_pct_parses_ms_and_clamps() -> None:
    """Issue #344: the dashboard duration bar is a % of its grid track at
    the showcase ladder's scale (ms / 10), never absolute px."""
    from jev_agent.dashboard.design_helpers import _duration_pct

    assert _duration_pct("90ms") == 9.0
    assert _duration_pct("700ms") == 70.0
    assert _duration_pct("60ms") == 6.0
    assert _duration_pct("125ms") == 12.5
    assert _duration_pct("5000ms") == 100.0
    assert _duration_pct("not-a-duration") == 0.0
    assert _duration_pct("") == 0.0


def test_detail_duration_bars_are_proportional_not_px(client: TestClient, home: Path) -> None:
    """Issue #344: on the detail page the Duration rows carry a percentage
    bar, not the old `ms // 2` px width — slower was 350px in a ~180px
    track at 390px, so it ran off the viewport, and the 96px name column
    let `duration_instant` paint over its value (`duration_instan90ms`)."""
    rich = rich_tokens(VIBE_PROFILES["minimal"], "both")
    tokens = dict(TOKENS)
    tokens["design_tokens"] = rich
    save_library_system("rich-brand", MARKDOWN, tokens, {"vibe_profile": "minimal"})
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    # Bars drawn to scale at ms / 10, the showcase ladder's scale.
    for name, pct in (
        ("duration_instant", "9"),
        ("duration_fast", "16"),
        ("duration_normal", "26"),
        ("duration_slow", "42"),
        ("duration_slower", "70"),
    ):
        assert f">{name}</span>" in page.text
        assert f'class="ds-duration-bar" data-bar-pct="{pct}"' in page.text
    assert 'class="ds-duration-bar" data-px-width' not in page.text
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    # The bar can never exceed its grid track, even if JS mis-scales it.
    assert (
        ".ds-duration-bar { height: 10px; border-radius: 999px; "
        "background: var(--ds-accent); opacity: 0.85; max-width: 100%; }" in css.text
    )
    # The phone name column fits the longest standard duration name
    # (`duration_instant`, ~116px in 12px monospace) on one line…
    assert (
        ".ds-scale-row { grid-template-columns: 120px 76px minmax(0, 1fr); "
        "gap: var(--s2); }" in css.text
    )
    # …and any longer custom name wraps instead of colliding with its value.
    assert (
        ".ds-scale-row .mono { font-size: 12px; color: var(--ds-muted); "
        "overflow-wrap: anywhere; }" in css.text
    )
    # Widening the name column must not push the 160px shadow chip off the
    # 390px track — it is capped at the track width too.
    assert ".ds-shadow-chip { width: 160px; height: 56px; " in css.text
    assert "border-radius: 10px; max-width: 100%; }" in css.text
    # Issue #352: the same widening shrank the easing track below the
    # 120px "Hover me" tile, so it overflowed its row by 8px — capped too.
    assert ".ds-ease-demo {\n  width: 120px;\n  max-width: 100%;" in css.text


def test_detail_color_swatches_present(client: TestClient, home: Path) -> None:
    """Issue #285: every color row carries a live swatch (data-swatch-color),
    and the stylesheet keeps the swatch column at a fixed width so it can
    never collapse to zero."""
    rich = rich_tokens(VIBE_PROFILES["minimal"], "both")
    tokens = dict(TOKENS)
    tokens["design_tokens"] = rich
    save_library_system("rich-brand", MARKDOWN, tokens, {"vibe_profile": "minimal"})
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert 'data-swatch-color="#' in page.text
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".content-inner:has(.ds-page)" in css.text
    assert "body:has(.ds-page) .shell.app-shell" in css.text
    assert "64px" in css.text


def test_detail_primary_comes_from_stored_colors(client: TestClient, home: Path) -> None:
    """Issue #285: components use the system's stored primary, not the
    fallback blue — the flat `light.primary` row name must not leak into
    the lookup."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert 'data-bg-color="#E8590C"' in page.text  # stored #e8590c, normalized
    assert 'data-bg-color="#2563EB"' not in page.text


def test_detail_assets_placeholder_without_logo(client: TestClient, home: Path) -> None:
    """Issue #285: no stored mark/favicons render as an honest placeholder,
    never a faked asset."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert "No logo yet" in page.text
    assert "No favicons yet" in page.text


def test_detail_assets_render_stored_logo(client: TestClient, home: Path) -> None:
    """Issue #285: the wizard's stored mark + favicons render in the Assets
    section (sanitized SVG, data-URI PNGs)."""
    _seed("acme-brand")
    system_path = library_system_path("acme-brand")
    mark = generate_mark("acme-brand:Acme", {"primary": "#e8590c", "primary_hover": "#c2410c"})
    (system_path / "logo.svg").write_text(mark_to_svg(mark, 512), encoding="utf-8")
    (system_path / "favicon-16.png").write_bytes(mark_to_png(mark, 16))
    (system_path / "favicon-32.png").write_bytes(mark_to_png(mark, 32))
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert "No logo yet" not in page.text
    # The mark renders as a data-URI <img> (no `| safe` anywhere — see the
    # audit test in test_issue224_minors.py). Decode it: a real SVG, with
    # no namespace prefix (`<ns0:svg>` is not recognised as SVG by HTML
    # parsers and would render as nothing).
    match = re.search(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)", page.text)
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    assert decoded.startswith("<svg ")
    assert "ns0:" not in decoded
    assert "data:image/png;base64," in page.text
    assert "Patterns" in page.text and "Social covers" in page.text


def test_detail_business_suggested_elements_inline(client: TestClient, home: Path) -> None:
    """Issue #285: proposed business elements render in Components with why."""
    _seed(
        "acme-brand",
        business_context={
            "industry": "Restaurant",
            "audience": "Hungry neighbours",
            "tone": "Warm",
            "offering": "Online ordering",
            "description": "A neighbourhood bistro with a seasonal menu.",
        },
    )
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert "Suggested for your business" in page.text
    assert "menu-card" in page.text
    assert "Why:" in page.text


def test_business_toggle_returns_to_system_page(client: TestClient, home: Path) -> None:
    """Issue #285: toggles posted from the system page return there."""
    _seed(
        "acme-brand",
        business_context={
            "industry": "Restaurant",
            "audience": "Hungry neighbours",
            "tone": "Warm",
            "offering": "Online ordering",
            "description": "A neighbourhood bistro with a seasonal menu.",
        },
    )
    response = _post(
        client,
        "/design-systems/business-elements",
        {
            "name": "acme-brand",
            "element": "menu-card",
            "action": "remove",
            "return_to": "/design-systems/acme-brand#components",
        },
    )
    assert response.status_code == 200
    assert response.request.url.path == "/design-systems/acme-brand"


def test_design_system_static_assets_served(client: TestClient, home: Path) -> None:
    """Issue #285: the new CSS/JS exist and are served (CSP: style/script-src 'self')."""
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".ds-ticker" in css.text
    js = client.get("/static/design_system.js")
    assert js.status_code == 200
    assert "ds-toast-more" in js.text


def test_code_page_restyle(client: TestClient, home: Path) -> None:
    """Issue #285: the code page keeps its function, restyled to match."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand/code?target=html-css")
    assert page.status_code == 200
    assert "design_system.css" in page.text
    assert "tokens.css" in page.text


# ---- issue #287: visual overview, scroll-spy, type specimens, preview toggle ----


def _seed_rich(name: str = "rich-brand") -> None:
    rich = rich_tokens(VIBE_PROFILES["minimal"], "both")
    tokens = dict(TOKENS)
    tokens["design_tokens"] = rich
    save_library_system(name, MARKDOWN, tokens, {"vibe_profile": "minimal"})


def test_detail_overview_is_visual_summary(client: TestClient, home: Path) -> None:
    """Issue #287: Overview is a visual summary — palette strip, type, mark,
    signature components — and the markdown notes live in the collapsed
    Notes section at the bottom instead of leading the page."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    overview = page.text.split('id="colors"')[0]
    assert "ds-overview-palette" in overview
    assert "ds-overview-type" in overview
    assert "Signature components" in overview
    assert 'href="#notes"' in page.text
    notes = page.text.split('id="notes"')[1]
    assert "System notes" in page.text
    assert "A warm, confident marketing system" in notes
    assert "A warm, confident marketing system" not in overview


def test_detail_overview_business_chips_and_collapsed_form(client: TestClient, home: Path) -> None:
    """Issue #287: business context shows as compact chips in the Overview;
    the edit form is kept, collapsed behind a disclosure."""
    _seed(
        "acme-brand",
        business_context={
            "industry": "Restaurant",
            "audience": "Hungry neighbours",
            "tone": "Warm",
            "offering": "Online ordering",
            "description": "A neighbourhood bistro with a seasonal menu.",
        },
    )
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    overview = page.text.split('id="colors"')[0]
    assert "Restaurant" in overview
    assert "Hungry neighbours" in overview
    assert "Edit business context" in overview
    assert 'id="biz-industry"' in page.text


def test_detail_typography_three_specimens(client: TestClient, home: Path) -> None:
    """Issue #287: three big specimens (display, headline, body) instead of
    the ~12-row repeat that wrapped the display sizes to 2-3 lines. The
    full scale stays in the copied JSON."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert page.text.count('class="ds-type-specimen"') == 3
    assert page.text.count("Aa — The quick brown fox") == 3
    assert "display_xl" in page.text
    css = client.get("/static/design_system.css")
    assert ".ds-type-specimen-text" in css.text
    assert ".ds-type-row" not in css.text


def test_detail_scroll_spy(client: TestClient, home: Path) -> None:
    """Issue #287: an IntersectionObserver scroll-spy highlights the sidebar
    link for the section in view; sections carry a scroll margin so anchor
    jumps land cleanly."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert "ds-nav-search" in page.text  # the search filter still exists
    js = client.get("/static/design_system.js")
    assert js.status_code == 200
    assert "IntersectionObserver" in js.text
    assert "setActiveNav" in js.text
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".ds-nav-list li a.active" in css.text
    assert "scroll-margin-top" in css.text
    # The sticky sidebar must stay visible mid-page: overflow: hidden on
    # .ds-page makes it the scroll container, so the nav scrolls away and
    # the scroll-spy highlight is invisible. overflow: clip keeps the
    # rounded-corner clipping without breaking sticky.
    assert "overflow: clip" in css.text
    # A `*/` inside a comment ends it early and swallows the next rule:
    # that exact bug killed the light-preview class (issue #287). Every
    # opener must have exactly one terminator, in both the CSS and the JS.
    assert css.text.count("/*") == css.text.count("*/")


def test_detail_preview_toggle(client: TestClient, home: Path) -> None:
    """Issue #287: a Light/Dark toggle previews the components/tokens area
    in the system's own themes. A both-theme system opens on light; the
    exact palettes travel as validated data attributes, no inline styles."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert 'data-preview-theme="light"' in page.text
    assert 'data-preview-theme="dark"' in page.text
    assert 'id="ds-content"' in page.text
    assert "ds-preview-light" in page.text
    assert 'data-preview-light-bg="#' in page.text
    assert 'data-preview-dark-bg="#' in page.text
    assert 'data-preview-light-text="#' in page.text
    assert 'data-preview-dark-text="#' in page.text
    assert ' style="' not in page.text
    js = client.get("/static/design_system.js")
    assert js.status_code == 200
    assert "applyPreview" in js.text
    assert "data-preview-theme" in js.text
    assert js.text.count("/*") == js.text.count("*/")
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".ds-content.ds-preview-light" in css.text
    assert ".ds-preview-btn" in css.text
    # The light class must actually declare the full palette — not just the
    # five JS-refined variables. surface-2, faint, and accent come from the
    # class alone; if the rule is swallowed (see the comment-balance check
    # above), inputs stay dark-on-dark in light preview.
    assert "--ds-surface-2: #ececef" in css.text
    assert "--ds-faint: #8a8a92" in css.text
    assert "--ds-accent: #65a30d" in css.text
    # The inherited text color must flip too: elements with no explicit
    # color rule (specimens, card titles, token names) inherit the content
    # area's color, and the dark page color would be invisible on light.
    assert "color: var(--ds-text)" in css.text


# ---- issue #289: richer Assets, copy per value, mobile nav collapse, header actions ----


def test_detail_copy_per_value(client: TestClient, home: Path) -> None:
    """Issue #289: individual values copy on click — color swatches/hexes,
    token rows, type-specimen specs — with a "Copied" confirmation."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    # A color row copies its hex; the overview swatch copies too.
    assert re.search(r'data-copy-value="#[0-9A-F]{6}"', page.text) is not None
    assert "ds-overview-swatch ds-copyable" in page.text
    # Spacing / radii rows copy their px value; a specimen copies its stack+size.
    assert 'data-copy-value="16px"' in page.text
    assert 'data-copy-value="display_xl: 52px,' in page.text
    # Font rows copy the stack.
    assert page.text.count('class="ds-font-row ds-copyable"') >= 1
    # Copyable rows are keyboard-reachable (role + tabindex + title).
    assert 'role="button" title="Click to copy #' in page.text
    js = client.get("/static/design_system.js")
    assert js.status_code == 200
    assert "data-copy-value" in js.text
    assert "showCopyTip" in js.text
    assert '"Copied"' in js.text
    assert "copyText" in js.text
    # Issue #292: a rejected Clipboard API write falls back to the textarea
    # path instead of reporting failure outright.
    assert "function fallback()" in js.text
    assert "function () { fallback(); }" in js.text
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".ds-copyable" in css.text
    assert ".ds-copy-tip" in css.text


def test_detail_assets_richer(client: TestClient, home: Path) -> None:
    """Issue #289: Assets are real compositions from the system's tokens —
    social covers with headline + subline + CTA in light and dark, a named
    icon set in the brand style, and logo lockups in every orientation."""
    _seed("acme-brand")
    system_path = library_system_path("acme-brand")
    mark = generate_mark("acme-brand:Acme", {"primary": "#e8590c", "primary_hover": "#c2410c"})
    (system_path / "logo.svg").write_text(mark_to_svg(mark, 512), encoding="utf-8")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    # Logo lockups: horizontal, stacked, mark-only, mono.
    assert "Lockups" in page.text
    for label in ("Horizontal", "Stacked", "Mark only", "Mono"):
        assert label in page.text
    # Social covers: headline + subline + CTA, light and dark variants.
    assert "Get started" in page.text
    assert "Open Graph · Light" in page.text
    assert "Open Graph · Dark" in page.text
    assert "Banner · Light" in page.text
    assert "Banner · Dark" in page.text
    # Icon set: the common UI icons, each named.
    for label in ("Search", "Menu", "Close", "Check", "Arrow", "Plus", "User", "Settings"):
        assert f"<figcaption>{label}</figcaption>" in page.text
    assert "ds-icon-grid" in page.text
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert ".ds-lockup" in css.text
    assert ".ds-cover-headline" in css.text
    assert ".ds-cover-cta" in css.text
    assert ".ds-icon-tile" in css.text


def test_detail_mobile_nav_collapses(client: TestClient, home: Path) -> None:
    """Issue #289: at ≤640px the in-page nav collapses to a sticky,
    horizontally scrollable chip row (search hidden), and token rows slim
    down so the page fits a 390px phone with no horizontal overflow."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    css = client.get("/static/design_system.css")
    assert css.status_code == 200
    assert "@media (max-width: 640px)" in css.text
    mobile = css.text.split("@media (max-width: 640px)")[1]
    assert ".ds-nav-list { display: contents; }" in mobile
    assert ".ds-nav-search, .ds-nav-sep { display: none; }" in mobile
    assert "overflow-x: auto" in mobile
    assert "position: sticky" in mobile
    assert "border-radius: 999px" in mobile
    # Token rows slim: the name column flexes, the hex/swatch columns shrink.
    assert "grid-template-columns: minmax(0, 1fr) 92px 40px" in mobile
    # Issue #294: the shell-grid override that collapses the live-panes
    # column must NOT apply at phone width — shell.css turns the sidebar
    # into a fixed drawer at <=1080px, and an unscoped override reserves a
    # 236px grid column for it, crushing the page to ~204px while
    # overflow: clip hides the damage from scrollWidth checks. It lives
    # inside the min-width: 1081px block, where the sidebar is in the grid.
    rule = "body:has(.ds-page) .shell.app-shell"
    assert css.text.count(rule) == 1
    idx = css.text.index(rule)
    head = css.text[:idx]
    media_idx = head.rfind("@media (min-width: 1081px)")
    assert media_idx != -1
    assert head[media_idx:].count("{") > head[media_idx:].count("}")


def test_detail_header_actions(client: TestClient, home: Path) -> None:
    """Issue #289: Clone and Export live in the page header now (the page is
    long and they were buried in Files); Delete stays buried."""
    _seed("acme-brand")
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    header = page.text.split('class="ds-layout"')[0]
    files = page.text.split('id="files"')[1]
    assert 'action="/design-systems/clone"' in header
    assert 'action="/design-systems/export"' in header
    assert 'action="/design-systems/clone"' not in files
    assert 'action="/design-systems/export"' not in files
    assert 'action="/design-systems/delete"' in files
    # The old labels survive as accessible names on the compact controls.
    assert "Clone to a new name" in page.text
    assert "Export tokens" in page.text


def test_detail_no_inline_styles_or_scripts(client: TestClient, home: Path) -> None:
    """Issue #289: the CSP forbids inline styles/scripts — the rendered
    detail page must carry none, and neither may the template source."""
    _seed_rich()
    page = client.get("/design-systems/rich-brand")
    assert page.status_code == 200
    assert ' style="' not in page.text
    # Every <script> on the page is an external file (src=) or a JSON data
    # island (type="application/json", never executed); no inline code.
    for tag in re.findall(r"<script[^>]*>", page.text):
        assert "src=" in tag or 'type="application/json"' in tag, f"inline script tag: {tag}"
    import jev_agent.dashboard

    template = Path(jev_agent.dashboard.__file__).parent / "templates" / "design_system.html"
    source = template.read_text(encoding="utf-8")
    assert "| safe" not in source
    assert 'style="' not in source
    # The template's only <script> is the external design_system.js include.
    for tag in re.findall(r"<script[^>]*>", source):
        assert "src=" in tag, f"inline script tag in template: {tag}"
