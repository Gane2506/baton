"""Figma in the dashboard: the Tools → APIs Figma card (token + MCP server),
the system Figma page (link, export, granular edit, sync), and import from
a Figma file on the reverse page. The Figma API is faked; no network."""

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

from jev_agent.core.design_library import load_library_system
from jev_agent.dashboard import routes_settings
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

TOKENS = {
    "colors": {"primary": "#111111", "background": "#FFFFFF"},
    "type_scale": {"base": 15},
    "spacing": {"md": 16},
    "radii": {"md": 8},
}
MARKDOWN = "# Acme\n\nA test system.\n"

FIGMA_URL = "https://www.figma.com/design/abc123/Demo-File"


def _sample_file() -> dict[str, object]:
    """A Figma file whose primary is #2662eb (changed from the system's #111111)."""
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
                        }
                    ],
                }
            ]
        },
        "styles": {"S:1": {"name": "Brand/Primary", "styleType": "FILL"}},
        "components": {"C:1": {"name": "Button"}},
    }


class _FakeFigmaClient:
    """The Figma API, faked: `validate_token` answers, `get_file` returns the sample."""

    def __init__(self, token: str, transport: object = None) -> None:
        self.token = token

    def __enter__(self) -> _FakeFigmaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def validate_token(self) -> str:
        return "fake-user"

    def get_file(self, file_key: str) -> dict[str, object]:
        assert file_key == "abc123"
        return _sample_file()


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


def _seed(name: str = "acme", **meta: object) -> int:
    from jev_agent.core.design_library import save_library_system

    extras: dict[str, object] = {"name": name, "source": "wizard"}
    extras.update(meta)
    return save_library_system(name, MARKDOWN, dict(TOKENS), extras)


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    """POST with a valid CSRF token, following redirects so flashes are visible."""
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


def _tools_html(client: TestClient) -> str:
    page = client.get("/tools")
    assert page.status_code == 200
    return page.text


# ---- Tools → APIs → Figma ---------------------------------------------------------------------


def test_tools_figma_section_renders_unconnected(client: TestClient, home: Path) -> None:
    html = _tools_html(client)
    assert ">Figma</h3>" in html
    assert "Add Figma token" in html
    assert "Add Figma MCP server" in html
    assert "Full seat" in html
    assert "figd_" in html
    assert "https://mcp.figma.com/mcp" in html
    assert "REST API only" in html


def test_tools_figma_section_with_token_offers_validate(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIGMA_API_TOKEN", "figd_test")
    html = _tools_html(client)
    assert "Validate token" in html
    assert "Add Figma token" not in html


def test_figma_validate_reports_the_account(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIGMA_API_TOKEN", "figd_test")
    monkeypatch.setattr(routes_settings, "FigmaClient", _FakeFigmaClient)
    response = _post(client, "/tools/figma/validate")
    assert "Figma connected as fake-user" in _visible(response)
    assert "Connected as fake-user" in _tools_html(client)


def test_figma_validate_without_token_is_a_plain_error(client: TestClient, home: Path) -> None:
    response = _post(client, "/tools/figma/validate")
    assert "no Figma token saved yet" in _visible(response)


def test_figma_mcp_add_connects_once(client: TestClient, home: Path, fixture_repo: Path) -> None:
    response = _post(client, "/tools/figma/mcp", {"target": "user"})
    assert "added the Figma MCP server" in _visible(response)
    html = _tools_html(client)
    assert "figma" in html
    assert "https://mcp.figma.com/mcp" in html
    assert "Add Figma MCP server" not in html
    # A second add is a plain error, not a silent overwrite.
    again = _post(client, "/tools/figma/mcp", {"target": "user"})
    assert "already exists" in _visible(again)


# ---- system Figma page ------------------------------------------------------------------------


def test_system_figma_page_renders(client: TestClient, home: Path) -> None:
    _seed()
    page = client.get("/design-systems/acme/figma")
    assert page.status_code == 200
    assert "Figma" in page.text
    assert "Link Figma file" in page.text
    assert "Generate export script" in page.text
    assert "Tweak one thing" in page.text
    assert "Sync from Figma" in page.text
    assert "Full seat" in page.text


def test_system_page_links_to_figma(client: TestClient, home: Path) -> None:
    _seed()
    page = client.get("/design-systems/acme")
    assert page.status_code == 200
    assert 'href="/design-systems/acme/figma"' in page.text


def test_figma_link_and_unlink(client: TestClient, home: Path) -> None:
    _seed()
    response = _post(
        client, "/design-systems/acme/figma", {"action": "link", "figma_url": FIGMA_URL}
    )
    assert "linked acme to Figma file abc123" in _visible(response)
    # The stored link is the canonical key-only URL (the slug is cosmetic).
    assert "https://www.figma.com/design/abc123/" in _visible(response)
    meta = json.loads(
        (home / ".jev" / "design-systems" / "acme" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["figma_file_key"] == "abc123"
    unlinked = _post(client, "/design-systems/acme/figma", {"action": "unlink"})
    assert "unlinked acme from Figma" in _visible(unlinked)
    assert "Link Figma file" in _visible(unlinked)


def test_figma_link_rejects_a_non_figma_url(client: TestClient, home: Path) -> None:
    _seed()
    response = _post(
        client,
        "/design-systems/acme/figma",
        {"action": "link", "figma_url": "https://example.com/design/abc/X"},
    )
    assert "not a Figma file URL" in _visible(response)


def test_figma_export_renders_the_script(client: TestClient, home: Path) -> None:
    _seed()
    response = _post(client, "/design-systems/acme/figma", {"action": "export"})
    text = _visible(response)
    assert "createVariableCollection" in text
    assert "Baton / acme" in text
    assert "setReactionsAsync" in text
    assert "SMART_ANIMATE" in text


def test_figma_edit_renders_a_snippet_and_rejects_bad_edits(client: TestClient, home: Path) -> None:
    _seed()
    response = _post(
        client,
        "/design-systems/acme/figma",
        {
            "action": "edit",
            "kind": "variable",
            "collection": "Baton / acme",
            "name": "color/primary",
            "mode": "Dark",
            "value": "#1D4ED8",
        },
    )
    text = _visible(response)
    assert "setValueForMode" in text
    assert "edit script ready" in text
    bad = _post(
        client,
        "/design-systems/acme/figma",
        {"action": "edit", "kind": "node-fill", "node_id": "nope", "value": "#ff0000"},
    )
    assert "bad Figma node id" in _visible(bad)


def test_figma_sync_applies_changes_as_a_new_version(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.core.figma as figma_mod

    _seed(
        figma_file_url=FIGMA_URL,
        figma_file_key="abc123",
    )
    monkeypatch.setenv("FIGMA_API_TOKEN", "figd_test")
    monkeypatch.setattr(figma_mod, "FigmaClient", _FakeFigmaClient)
    response = _post(client, "/design-systems/acme/figma", {"action": "sync"})
    text = _visible(response)
    assert "synced from Figma" in text
    assert "1 changed" in text
    system = load_library_system("acme")
    assert system is not None
    assert system.version == 2
    colors = system.tokens.get("colors")
    assert isinstance(colors, dict)
    assert colors["primary"] == "#2662eb"
    # Removed-in-Figma tokens are kept in Baton, and the sync is recorded.
    assert colors["background"] == "#FFFFFF"
    meta = json.loads(
        (home / ".jev" / "design-systems" / "acme" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["figma_last_sync"]


def test_figma_sync_when_already_in_sync(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.core.figma as figma_mod

    _seed(
        figma_file_url=FIGMA_URL,
        figma_file_key="abc123",
    )
    # The system's tokens already match the sample file exactly (the sample
    # has no text styles, so the import falls back to the starter type scale).
    from jev_agent.core.design_library import save_library_system
    from jev_agent.core.design_system import STARTER_TOKENS

    starter_type_scale = STARTER_TOKENS["type_scale"]
    assert isinstance(starter_type_scale, dict)
    save_library_system(
        "acme",
        MARKDOWN,
        {
            "colors": {"primary": "#2662eb"},
            "type_scale": dict(starter_type_scale),
            "spacing": {"xs": 8},
            "radii": {"xs": 10},
        },
        {"figma_file_url": FIGMA_URL, "figma_file_key": "abc123"},
    )
    monkeypatch.setenv("FIGMA_API_TOKEN", "figd_test")
    monkeypatch.setattr(figma_mod, "FigmaClient", _FakeFigmaClient)
    response = _post(client, "/design-systems/acme/figma", {"action": "sync"})
    assert "already in sync with Figma" in _visible(response)


def test_figma_sync_needs_a_link_and_a_token(client: TestClient, home: Path) -> None:
    _seed()
    response = _post(client, "/design-systems/acme/figma", {"action": "sync"})
    assert "link a Figma file first" in _visible(response)


# ---- reverse from Figma -----------------------------------------------------------------------


def test_reverse_page_shows_figma_import(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio/reverse")
    assert page.status_code == 200
    assert "From a Figma file" in page.text
    assert "/design-studio/reverse/figma" in page.text


def test_reverse_figma_import_creates_a_linked_system(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.core.figma as figma_mod

    monkeypatch.setenv("FIGMA_API_TOKEN", "figd_test")
    monkeypatch.setattr(figma_mod, "FigmaClient", _FakeFigmaClient)
    response = _post(
        client, "/design-studio/reverse/figma", {"figma_url": FIGMA_URL, "name": "demo-file"}
    )
    assert "imported Demo File from Figma as demo-file" in _visible(response)
    system = load_library_system("demo-file")
    assert system is not None
    colors = system.tokens.get("colors")
    assert isinstance(colors, dict)
    assert colors["primary"] == "#2662eb"
    meta = json.loads(
        (home / ".jev" / "design-systems" / "demo-file" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["source"] == "figma:abc123"
    assert meta["figma_file_key"] == "abc123"


def test_reverse_figma_import_without_token_is_a_plain_error(
    client: TestClient, home: Path
) -> None:
    response = _post(client, "/design-studio/reverse/figma", {"figma_url": FIGMA_URL})
    assert "no Figma token saved yet" in _visible(response)


def test_reverse_figma_import_rejects_a_bad_url(client: TestClient, home: Path) -> None:
    response = _post(
        client,
        "/design-studio/reverse/figma",
        {"figma_url": "https://example.com/design/abc/X"},
    )
    assert "not a Figma file URL" in _visible(response)
