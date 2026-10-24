"""Tools page markup tests (workstream 2: design-system pass over /tools).

The page renders its own template with Title Case labels via field_label(),
real <details class="disclosure"> add expanders, .btn button classes, and
section headers with plain-language blurbs. No network, no real models.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _tools_html(client: TestClient) -> str:
    page = client.get("/tools")
    assert page.status_code == 200
    text: str = page.text
    return text


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/tools").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.post(
        url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False
    )
    return response


def test_tools_sections_have_headers_and_blurbs(client: TestClient) -> None:
    html = _tools_html(client)
    assert 'id="s-tools-mcp"' in html
    assert 'id="s-tools-skills"' in html
    assert 'id="s-tools-plugins"' in html
    assert ">MCP servers</h2>" in html
    assert ">Skills</h2>" in html
    assert ">Plugins (Claude Code only)</h2>" in html
    assert 'aria-label="Tools sections"' in html


def test_tools_add_expandable_is_disclosure_button(client: TestClient) -> None:
    html = _tools_html(client)
    assert html.count('<details class="disclosure">') == 3
    assert "Add to Tools: MCP servers" in html
    assert "Add to Tools: skills" in html
    assert "Add to Tools: plugins (Claude Code only)" in html
    # The legacy text-link expander is gone from this page.
    assert 'class="entity add-entity"' not in html


def test_tools_labels_are_title_case(client: TestClient) -> None:
    html = _tools_html(client)
    for label in (
        "Name",
        "Save to",
        "Command",
        "Args",
        "Env from",
        "URL",
        "Transport",
        "Bearer token env",
        "Path",
        "Allowed tools",
    ):
        assert f">{label}</label>" in html, label
    # No raw config keys as visible labels in the add forms.
    for raw in (
        ">name</label>",
        ">save to</label>",
        ">env_from</label>",
        ">bearer_token_env</label>",
    ):
        assert raw not in html, raw


def test_tools_add_buttons_use_primary_style(client: TestClient) -> None:
    html = _tools_html(client)
    assert html.count('<button class="btn btn-primary" type="submit">Add</button>') == 3


def test_tools_entry_rows_use_title_case_labels_and_btn_classes(client: TestClient) -> None:
    added = _post(
        client,
        "/settings/entity",
        {
            "section": "tools.mcp",
            "action": "add",
            "name": "github",
            "target": "user",
            "keys": "command,args,env_from",
            "kinds": "str,list,list",
            "f_command": "npx",
            "f_args": "-y\n@modelcontextprotocol/server-github",
            "f_env_from": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
    )
    assert added.status_code == 303
    html = _tools_html(client)
    assert ">Command</label>" in html
    assert ">Env from</label>" in html
    assert '<button class="btn btn-sm btn-secondary" type="submit">Save</button>' in html
    assert "Remove github" in html
