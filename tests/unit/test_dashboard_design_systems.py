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
