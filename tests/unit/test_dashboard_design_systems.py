"""The dashboard's design-systems pages: the library list, the detail view, and the
`use` / `clone` / `export` / `delete` actions (issue #118)."""

from __future__ import annotations

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
        ("/design-systems/delete", {"name": "acme-brand"}),
    ):
        assert client.post(url, data=data).status_code == 403
    assert "acme-brand" in list_library()  # nothing happened


def test_unknown_action_404(client: TestClient, home: Path) -> None:
    response = client.post("/design-systems/bogus", data={"csrf_token": _csrf(client), "name": "x"})
    assert response.status_code == 404


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
    assert ".content-inner:has(> .ds-page)" in css.text
    assert "64px" in css.text


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
    assert "<circle" in page.text or "<rect" in page.text or "<path" in page.text
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
