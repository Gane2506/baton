"""Two-level navigation tests: submenu panels and the icon-collapsed sidebar.

The sidebar entries are `NavItem`s (app.NAV); an entry with `children` renders
a toggle button that opens a submenu panel adjacent to the sidebar, and the
sidebar collapses to icon-only while the panel is open. No network, no models.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.dashboard.app import NavChild, NavItem, create_app
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


# ---- data model ------------------------------------------------------------


def test_nav_items_declare_children_declaratively() -> None:
    from jev_agent.dashboard.app import NAV

    chats = next(item for item in NAV if item.href == "/chats")
    assert isinstance(chats, NavItem)
    assert [(c.href, c.label, c.method) for c in chats.children] == [
        ("/chats", "All chats", "get"),
        ("/chats/new", "New chat", "post"),
    ]
    # Every other entry is a plain link: no children, same shape as before.
    for item in NAV:
        if item is chats:
            continue
        assert item.children == ()


def test_nav_child_defaults_to_get_link() -> None:
    assert NavChild("/x", "X").method == "get"


def test_submenu_open_only_on_parent_or_declared_child_pages() -> None:
    from jev_agent.dashboard.app import NAV

    chats = next(item for item in NAV if item.href == "/chats")
    assert chats.submenu_open("/chats")
    assert chats.submenu_open("/chats/new")
    # A deeper page keeps the parent highlighted but does not auto-open the
    # panel (in particular, chat detail pages keep their own rail behavior).
    assert not chats.submenu_open("/chats/abc123")
    assert chats.is_active("/chats/abc123")
    assert not chats.submenu_open("/queue")


def test_nav_active_logic_unchanged_for_plain_entries() -> None:
    from jev_agent.dashboard.app import NAV

    board = next(item for item in NAV if item.href == "/")
    assert board.is_active("/")
    assert board.is_active("/runs/abc")
    assert not board.is_active("/queue")
    queue = next(item for item in NAV if item.href == "/queue")
    assert queue.is_active("/queue")
    assert not queue.is_active("/")


# ---- server rendering ------------------------------------------------------


def test_chats_renders_as_submenu_toggle(client: TestClient) -> None:
    html = client.get("/").text
    # Parent with children is a button, not a link to /chats.
    assert "data-submenu-toggle" in html
    assert 'aria-controls="submenu-chats"' in html
    # Closed on a non-child page: body has no collapsed class, panel hidden.
    body_class = html.split("<body", 1)[1].split(">", 1)[0]
    assert "submenu-open" not in body_class
    assert 'id="submenu-chats"' in html and 'aria-expanded="false"' in html
    assert '<div class="submenu-panel" id="submenu-chats"' in html
    # Submenu children: All chats links to /chats, New chat posts to /chats/new.
    assert 'href="/chats"' in html and ">All chats<" in html
    assert 'action="/chats/new"' in html and ">New chat<" in html
    # Accessibility: labelled navigation landmark, expanded state announced.
    assert 'role="navigation" aria-label="Chats submenu"' in html


def test_chats_page_renders_submenu_open_and_sidebar_collapsed(
    client: TestClient,
) -> None:
    html = client.get("/chats").text
    body_class = html.split("<body", 1)[1].split(">", 1)[0]
    assert "submenu-open" in body_class
    assert 'aria-expanded="true"' in html
    panel = html.split('id="submenu-chats"', 1)[1].split(">", 1)[0]
    assert "hidden" not in panel
    # The current child is marked current.
    assert 'class="submenu-link active" aria-current="page"' in html
    # Collapsed sidebar still tooltips via title attributes.
    assert 'title="Chats"' in html and 'title="Queue"' in html


def test_non_child_page_renders_no_collapse(client: TestClient) -> None:
    for path in ("/", "/queue", "/settings"):
        html = client.get(path).text
        body_class = html.split("<body", 1)[1].split(">", 1)[0]
        assert "submenu-open" not in body_class, path
        assert 'aria-expanded="false"' in html, path


def test_chat_detail_page_keeps_parent_active_without_opening_panel(
    client: TestClient,
) -> None:
    html = client.get("/chats/does-not-exist").text
    assert html  # page renders (404 body still inside the shell)
    body_class = html.split("<body", 1)[1].split(">", 1)[0]
    assert "submenu-open" not in body_class


# ---- static assets ---------------------------------------------------------


def test_submenu_css(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    assert ".submenu-panel" in css and ".nav-parent" in css
    assert "body.submenu-open" in css
    assert "--sidebar-w: 60px" in css
    # The collapse is desktop-only; the drawer keeps full width on mobile.
    assert "@media (min-width: 1081px)" in css
    # No hardcoded palette colors: the collapsed state must work in every style.
    assert ".nav-chevron" in css


def test_submenu_panel_inline_in_mobile_drawer(client: TestClient) -> None:
    css = client.get("/static/shell.css").text
    assert ".submenu-panel" in css
    assert "position: static" in css


def test_chat_fullscreen_rail_covers_parent_button(client: TestClient) -> None:
    css = client.get("/static/chat.css").text
    assert "button.nav-parent" in css
    assert ".chat-fullscreen .submenu-panel" in css


def test_submenu_js(client: TestClient) -> None:
    js = client.get("/static/app.js").text
    assert "data-submenu-toggle" in js
    assert "jev:submenu" in js
    assert "aria-expanded" in js
    # Escape closes the panel first, before the mobile drawer's own handler.
    assert "stopPropagation" in js
    # Focus moves into the panel on open and back to the toggle on close.
    assert ".focus()" in js


def test_mobile_drawer_markup_still_intact(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="nav-toggle"' in html and 'id="nav-scrim"' in html
    assert "body.nav-open" in client.get("/static/shell.css").text
