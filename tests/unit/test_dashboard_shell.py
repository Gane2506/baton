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
from typer.testing import CliRunner

from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.models import RunState
from jev_agent.core.store import Store
from jev_agent.dashboard import app as dashboard_app_module
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

_cli = CliRunner()


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


# ---- WS-B: app shell, tables, mobile nav -------------------------------------------


@pytest.fixture
def planned_run(fixture_repo: Path) -> tuple[Path, str]:
    """A repo with one run awaiting approval (planned by the fake workers)."""
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    result = _cli.invoke(
        cli_app, ["run", "add <b>search</b> & filters", "--repo", str(fixture_repo)]
    )
    assert result.exit_code == 0, result.output
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.list_runs()[0]
    assert run.state is RunState.AWAITING_APPROVAL
    return fixture_repo, run.id


@pytest.fixture
def run_client(planned_run: tuple[Path, str]) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=planned_run[0], allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def test_all_full_page_templates_extend_base() -> None:
    templates = Path(dashboard_app_module.__file__).parent / "templates"
    full_pages = sorted(
        p
        for p in templates.glob("*.html")
        if not p.name.startswith("_") and p.name not in ("base.html", "macros.html")
    )
    assert full_pages, "expected full-page templates"
    for page in full_pages:
        head = page.read_text(encoding="utf-8").splitlines()[:3]
        assert any('{% extends "base.html" %}' in line for line in head), page.name
    # The quality review is a section of the run page, not a standalone page.
    assert not (templates / "quality.html").exists()
    assert 'id="quality-review"' in (templates / "run.html").read_text(encoding="utf-8")


def test_quality_review_section_has_breadcrumb_inside_shell(
    run_client: TestClient, planned_run: tuple[Path, str]
) -> None:
    run_id = planned_run[1]
    page = run_client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    html = page.text
    # Inside the app shell, not stranded: sidebar markup present.
    assert 'id="app-shell"' in html and 'class="sidebar"' in html
    # Breadcrumb Board > run <id> > Quality review, directly above the section.
    crumb = re.search(
        r'<nav class="crumbs"[^>]*aria-label="Breadcrumb".*?</nav>\s*'
        r'<h2 class="section" id="quality-review">',
        html,
        re.S,
    )
    assert crumb, "quality review breadcrumb missing"
    trail = crumb.group(0)
    assert 'href="/"' in trail and f'href="/runs/{run_id}"' in trail
    assert 'aria-current="page">Quality review' in trail


def test_decisions_table_uses_table_scroll(
    run_client: TestClient, planned_run: tuple[Path, str]
) -> None:
    html = run_client.get(f"/runs/{planned_run[1]}").text
    assert "Decisions" in html and "D1" in html
    assert '<div class="table-wrap table-scroll"><table>' in html
    # The attempts table keeps the plain .table-wrap behavior.
    assert '<div class="table-wrap"><table>' in html


def test_kpis_shipped_prs_table_uses_table_scroll(
    run_client: TestClient, planned_run: tuple[Path, str]
) -> None:
    repo, run_id = planned_run
    with Store.open(repo / ".jev" / "state.db") as store:
        store.add_shipped_pr(
            run_id,
            remote="origin",
            repo="o/r",
            branch="jev/x",
            base="main",
            number=1,
            url=None,
            title="test pr",
            draft=False,
        )
    page = run_client.get("/kpis")
    assert page.status_code == 200
    assert "Shipped PRs" in page.text
    assert '<div class="table-wrap table-scroll"><table>' in page.text


def test_table_scroll_utility_css(client: TestClient) -> None:
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert ".table-scroll" in css.text
    assert "overflow-x: auto" in css.text
    # Edge-fade hint: covers scroll with the content, shadows pin to the edges.
    assert "background-attachment: local, local, scroll, scroll" in css.text


def test_mobile_nav_drawer_markup(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="nav-toggle"' in html
    assert 'aria-controls="app-sidebar"' in html
    assert 'id="app-sidebar"' in html
    assert 'id="nav-scrim"' in html
    assert 'class="mobile-topbar"' in html
    # Theme picker and live status stay reachable inside the drawer sidebar.
    assert 'id="theme"' in html and 'id="conn-status"' in html


def test_mobile_nav_drawer_css_and_js(client: TestClient) -> None:
    css = client.get("/static/shell.css").text
    assert "@media (max-width: 1080px)" in css
    assert ".mobile-topbar" in css and ".nav-scrim" in css
    assert "body.nav-open" in css
    # Chat fullscreen keeps its own header: no second top bar there.
    assert "chat-fullscreen .mobile-topbar" in css
    js = client.get("/static/app.js").text
    assert "nav-toggle" in js and "nav-scrim" in js
    assert "Escape" in js and "jev:nav" in js and "JevShell.nav" in js
    # Chat's own header button becomes the drawer toggle on narrow screens.
    composer = client.get("/static/composer.js").text
    assert "max-width: 1080px" in composer and "window.JevShell.nav" in composer


def test_chat_fullscreen_survives_drawer_changes(client: TestClient) -> None:
    chat_id = _new_chat(client)
    page = client.get(f"/chats/{chat_id}")
    assert page.status_code == 200
    html = page.text
    assert "chat-fullscreen" in html
    assert 'id="sidebar-toggle"' in html and 'id="chat-rail"' in html
    css = client.get("/static/chat.css").text
    # Desktop fullscreen rail contract untouched.
    assert ".chat-fullscreen .shell.app-shell" in css
    assert "grid-template-columns: 56px minmax(0, 1fr)" in css
