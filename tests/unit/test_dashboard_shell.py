"""Mission-control shell layout tests (workstream D1).

The shell frame — sidebar with connection status and theme switcher, center
content, live preview/terminal panes — renders on every page, and the existing
pages keep working inside it. No network, no real models.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
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


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _new_chat(client: TestClient) -> str:
    response: httpx.Response = client.post(
        "/chats/new",
        data={"worker": "api", "learning": "", "budget_usd": "1.00", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def test_shell_frame_has_sidebar_conn_and_no_global_panes(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert 'id="app-shell"' in html
    # Left: navigation, connection status slot, theme switcher slot.
    assert 'class="sidebar"' in html and "<nav" in html
    assert 'id="conn-status"' in html and "Live" in html
    assert 'id="theme"' in html and "Auto (OS)" in html
    # Panes are no longer global: only run detail opts in via the panes block.
    assert 'data-pane="preview"' not in html and 'data-pane="terminal"' not in html
    # Shell assets load alongside the existing ones.
    assert 'href="/static/shell.css"' in html and 'src="/static/app.js"' in html
    assert 'src="/static/live.js"' in html and 'src="/static/theme.js"' in html


def test_existing_pages_render_inside_shell_without_panes(client: TestClient) -> None:
    for path in (
        "/",
        "/queue",
        "/chats",
        "/agents",
        "/learning",
        "/tools",
        "/connections",
        "/spend",
        "/kpis",
        "/settings",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'id="app-shell"' in page.text, path
        assert 'data-pane="preview"' not in page.text, path


def test_chat_page_is_fullscreen_conversational_control_plane(client: TestClient) -> None:
    chat_id = _new_chat(client)
    page = client.get(f"/chats/{chat_id}")
    assert page.status_code == 200
    html = page.text
    # Fullscreen layout contract: rail, main column, work panel.
    for dom_id in (
        "chat-layout",
        "chat-rail",
        "rail-toggle",
        "chat-main",
        "chat-transcript",
        "action-bar",
        "chat-composer",
        "composer-text",
        "composer-send",
        "work-panel",
        "work-tabs",
        "sidebar-toggle",
    ):
        assert f'id="{dom_id}"' in html, dom_id
    assert "chat-fullscreen" in html
    # Transcript still fed by the existing live.js polling pattern.
    assert f'data-live="/chats/{chat_id}/live"' in html
    # No-JS fallback: plain POST forms for send and the four actions.
    assert f'action="/chats/{chat_id}/send"' in html
    assert '<textarea id="composer-text" name="text"' in html
    for action in ("brief", "start", "queue"):
        assert f'action="/chats/{chat_id}/{action}"' in html
    # The polling fragment still renders the transcript.
    live = client.get(f"/chats/{chat_id}/live")
    assert live.status_code == 200 and "transcript" in live.text.lower()


def test_shell_static_assets_and_mount_hook(client: TestClient) -> None:
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "window.JevShell" in js.text
    assert "mountPane" in js.text and "unmountPane" in js.text
    assert "data-pane-live" in js.text and "data-pane-empty" in js.text
    css = client.get("/static/shell.css")
    assert css.status_code == 200
    assert ".side-panes" in css.text and ".chat-pane" in css.text
    # Structure only: no hardcoded palette colors (variables with fallbacks).
    assert not re.search(r":\s*#[0-9a-fA-F]{3,8}\b", css.text)


def test_no_inline_scripts_or_styles(client: TestClient) -> None:
    html = client.get("/").text
    assert "<style" not in html
    tags = re.findall(r"<script[^>]*>", html)
    assert tags, "expected the vendored scripts"
    assert all("src=" in tag for tag in tags), tags
