"""Dashboard feature tests: browser chat, board actions, current-build KPIs (no network).

The chat backends, the run launcher and the workers are all fakes here: `JEV_FAKE_WORKERS=1`
swaps in the echo chat backend and a fake registry, and the launcher's executor is a
`Calls` recorder, so nothing touches a model, a CLI or GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.chat import META_FILE, hold_chat
from jev_agent.core.models import RunState
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


class Calls:
    """A fake run executor: records calls and answers at once (no workers run)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, RunAction]] = []

    def __call__(self, root: Path, run_id: str, action: RunAction) -> str:
        self.calls.append((root, run_id, action))
        return "run DONE"


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def client(fixture_repo: Path, calls: Calls) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=calls,
    )
    with TestClient(app) as client:
        yield client


def _dashboard(client: TestClient) -> Any:
    return cast(Any, client.app).state.dashboard


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post(
        url,
        data={**(data or {}), "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    return response


def _new_chat(client: TestClient) -> str:
    response = _post(client, "/chats/new", {"worker": "api", "learning": "", "budget_usd": "1.00"})
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def _say(client: TestClient, chat_id: str, text: str) -> None:
    response = _post(client, f"/chats/{chat_id}/send", {"text": text})
    assert response.status_code == 303
    job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "finished" and "replied" in job.message


def _brief(client: TestClient, chat_id: str) -> None:
    response = _post(client, f"/chats/{chat_id}/brief")
    assert response.status_code == 303
    job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "finished" and "brief" in job.message


def test_chat_list_has_titles_search_and_delete(client: TestClient, fixture_repo: Path) -> None:
    chat_id = _new_chat(client)
    _say(client, chat_id, "please add a search box")
    listing = client.get("/chats")
    assert chat_id in listing.text and "please add a search box" in listing.text
    assert f'action="/chats/{chat_id}/delete"' in listing.text

    other = _new_chat(client)
    _say(client, other, "unrelated idea about ships")
    found = client.get("/chats?q=search")
    assert chat_id in found.text and other not in found.text
    assert "No chats match" in client.get("/chats?q=zzz-nothing-here").text

    gone = _post(client, f"/chats/{chat_id}/delete")
    assert gone.status_code == 303
    remaining = client.get("/chats")
    assert f'action="/chats/{chat_id}/delete"' not in remaining.text  # the row is gone
    assert not (fixture_repo / ".jev" / "chats" / chat_id).exists()
    assert other in remaining.text


def test_sidebar_offers_a_new_chat_and_the_page_has_a_form(client: TestClient) -> None:
    page = client.get("/")
    assert 'class="sidebar-new"' in page.text and "New chat" in page.text
    chats = client.get("/chats")
    assert chats.status_code == 200 and 'action="/chats/new"' in chats.text


def test_create_and_continue_a_chat(client: TestClient) -> None:
    chat_id = _new_chat(client)
    assert chat_id.startswith("c-")
    assert chat_id in client.get("/chats").text

    _say(client, chat_id, "hello from the web")
    page = client.get(f"/chats/{chat_id}")
    assert "(fake model) You said: hello from the web" in page.text
    live = client.get(f"/chats/{chat_id}/live")
    assert live.status_code == 200 and "(fake model)" in live.text


def test_chat_brief_then_plan_starts_a_run(
    client: TestClient, calls: Calls, fixture_repo: Path
) -> None:
    chat_id = _new_chat(client)
    _say(client, chat_id, "add word counts")
    _brief(client, chat_id)
    assert "Fake brief" in client.get(f"/chats/{chat_id}").text

    started = _post(client, f"/chats/{chat_id}/start", {"action": "plan"})
    assert started.status_code == 303 and started.headers["location"].startswith("/runs/")
    run_id = started.headers["location"].rsplit("/", 1)[1]
    assert calls.calls and calls.calls[-1][1] == run_id and calls.calls[-1][2] == "start"
    with _store(fixture_repo) as store:
        run = store.get_run(run_id)
    assert run.plan_only is True and run.state is RunState.INTAKE
    assert f'href="/runs/{run_id}"' in client.get(f"/chats/{chat_id}").text


def test_chat_queue_adds_the_brief(client: TestClient, fixture_repo: Path) -> None:
    chat_id = _new_chat(client)
    _say(client, chat_id, "ship it")
    _brief(client, chat_id)
    queued = _post(client, f"/chats/{chat_id}/queue")
    assert queued.status_code == 303
    with _store(fixture_repo) as store:
        items = store.list_queue()
    assert items and "Fake brief" in items[-1].idea


def test_chat_actions_refuse_without_a_brief(client: TestClient) -> None:
    chat_id = _new_chat(client)
    assert _post(client, f"/chats/{chat_id}/start", {"action": "run"}).status_code == 303
    page = client.get(f"/chats/{chat_id}")
    # The user-facing message must guide, never leak the raw backend phrasing.
    assert "write a brief first" in page.text
    assert "no brief yet" not in page.text


def test_empty_chat_is_a_serene_state(client: TestClient, fixture_repo: Path) -> None:
    """Issue #156: a brand-new chat shows the greeting, the centered composer
    and repo-aware starters — and no action bar, no raw errors, no clutter."""
    chat_id = _new_chat(client)
    page = client.get(f"/chats/{chat_id}")
    assert page.status_code == 200
    assert 'data-empty="true"' in page.text
    assert "what should we build in" in page.text  # the warm greeting
    assert 'class="empty-hero"' in page.text
    assert 'id="chat-composer"' in page.text  # the one real input
    assert 'id="action-bar"' not in page.text  # hidden until the first message
    assert "no model for the api backend" not in page.text
    assert "no brief yet" not in page.text
    # The quiet persistent context strip: repo, backend, model.
    assert 'class="context-strip"' in page.text
    assert fixture_repo.name in page.text


def test_chat_with_messages_has_no_empty_hero(client: TestClient) -> None:
    """Once the conversation starts, the transcript + action bar take over and
    the context strip stays visible."""
    chat_id = _new_chat(client)
    _say(client, chat_id, "hello from the web")
    page = client.get(f"/chats/{chat_id}")
    assert 'data-empty="true"' not in page.text
    assert 'class="empty-hero"' not in page.text
    assert 'id="chat-transcript"' in page.text
    assert 'id="action-bar"' in page.text
    assert 'class="context-strip"' in page.text


def test_rail_groups_chats_by_recency_with_status(client: TestClient) -> None:
    """Issue #156: the rail groups chats (Today heading) and shows a rich
    status pill — a briefed chat reads 'Briefed' instead of a bare dot."""
    chat_id = _new_chat(client)
    _say(client, chat_id, "please add a search box")
    _brief(client, chat_id)
    page = client.get(f"/chats/{chat_id}")
    assert '<p class="rail-heading">Today</p>' in page.text
    assert "status-briefed" in page.text and ">Briefed<" in page.text


def test_new_chat_form_is_minimal_with_advanced_collapsed(client: TestClient) -> None:
    """The 6-field wall is gone: one task input + environment picker + Start in
    the main flow; budget/learning/design-system/profile hide under Advanced."""
    page = client.get("/chats")
    assert page.status_code == 200
    assert 'placeholder="Describe a task or ask a question…"' in page.text
    assert 'id="nc-worker"' in page.text and 'id="nc-model"' in page.text
    assert ">Start<" in page.text
    # Advanced options still exist, but collapsed — not in the main flow.
    assert '<details class="advanced-details">' in page.text
    advanced = page.text.split('<details class="advanced-details">', 1)[1]
    for field in ('name="learning"', 'name="design_system"', 'name="profile"', 'name="budget_usd"'):
        assert field in advanced


def test_new_chat_with_first_message_sends_it(client: TestClient) -> None:
    """Type + Enter just works: the chat is created and the first message is
    already being answered — no second step."""
    response = _post(
        client, "/chats/new", {"worker": "api", "budget_usd": "1.00", "text": "hello new chat"}
    )
    assert response.status_code == 303
    chat_id = response.headers["location"].rsplit("/", 1)[1]
    job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "finished"
    page = client.get(f"/chats/{chat_id}")
    assert "(fake model) You said: hello new chat" in page.text


@pytest.mark.parametrize("budget", ["inf", "nan", "-1", "0"])
def test_new_chat_refuses_a_budget_that_is_not_a_finite_amount(
    client: TestClient, fixture_repo: Path, budget: str
) -> None:
    response = _post(client, "/chats/new", {"worker": "api", "learning": "", "budget_usd": budget})
    assert response.status_code == 303 and "/chats/c-" not in response.headers["location"]
    assert "finite number of USD above zero" in client.get("/chats").text
    assert not (fixture_repo / ".jev" / "chats").exists() or not any(
        (fixture_repo / ".jev" / "chats").iterdir()
    )


def test_a_chat_open_in_a_terminal_is_not_changed(client: TestClient, fixture_repo: Path) -> None:
    chat_id = _new_chat(client)
    meta = fixture_repo / ".jev" / "chats" / chat_id / META_FILE
    before = meta.read_text()
    with hold_chat(fixture_repo, chat_id):  # `jev chat --resume` has it
        assert _post(client, f"/chats/{chat_id}/send", {"text": "hello"}).status_code == 303
        job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "failed" and "is open elsewhere" in job.message
    assert meta.read_text() == before


def test_board_adds_and_removes_queue_items(client: TestClient, fixture_repo: Path) -> None:
    page = client.get("/")
    assert 'action="/queue/add"' in page.text and 'formaction="/runs/new"' in page.text

    added = _post(client, "/queue/add", {"idea": "board item", "priority": "high"})
    assert added.status_code == 303
    page = client.get("/")
    assert "board item" in page.text
    with _store(fixture_repo) as store:
        item = store.list_queue()[0]
    assert f'action="/queue/{item.id}/remove"' in page.text

    assert _post(client, f"/queue/{item.id}/remove").status_code == 303
    with _store(fixture_repo) as store:
        assert store.list_queue() == []


def test_run_now_starts_a_run(client: TestClient, calls: Calls, fixture_repo: Path) -> None:
    response = _post(client, "/runs/new", {"idea": "run me now", "plan_only": "on"})
    assert response.status_code == 303 and response.headers["location"].startswith("/runs/")
    run_id = response.headers["location"].rsplit("/", 1)[1]
    with _store(fixture_repo) as store:
        run = store.get_run(run_id)
    assert run.plan_only is True and run.state is RunState.INTAKE
    assert calls.calls == [(fixture_repo, run_id, "start")]


def test_kpis_page_shows_the_current_build(client: TestClient) -> None:
    page = client.get("/kpis")
    assert page.status_code == 200
    assert "This repo" in page.text and "Median cost / run" in page.text
    assert "No runs yet." in page.text
    assert "No shipped pull requests yet" in page.text  # the PR section keeps its empty state


def test_theme_control_and_stylesheet(client: TestClient) -> None:
    page = client.get("/")
    assert '<script src="/static/theme.js">' in page.text
    assert 'id="theme"' in page.text and "Auto (OS)" in page.text
    assert client.get("/static/theme.js").status_code == 200
    css = client.get("/static/style.css").text
    assert ':root[data-theme="oled"]' in css and ':root[data-theme="solarized"]' in css
    assert ':root[data-theme="high-contrast"]' in css and "prefers-color-scheme: light" in css


# ---- UI-01: Queue page polish (issue #115) -------------------------------------
# Regression tests for the spacing/border defects found on the Queue page. CSS
# rules are asserted as served substrings (the same pattern as
# test_table_scroll_utility_css); the template assertion pins the Note-column
# fix that stopped the items table overflowing its container and slicing the
# Remove buttons at the table's right edge.


def test_queue_polish_section_heading_in_panel_head_css(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    # h2.section keeps its standalone margins, but inside a .panel-head row the
    # heading is a flex item: the row owns the spacing so the heading stays
    # centered with its action buttons (Queue "Items", Learning "Lessons").
    assert ".panel-head h2.section { margin: 0; }" in css


def test_queue_polish_toolbar_paragraph_css(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    # A <p> directly in a toolbar row is a flex item, not prose: the base p
    # bottom margin would lift it off the row's vertical center.
    assert ".panel-head > p, .actions > p { margin: 0; }" in css


def test_queue_polish_inline_hint_css(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    # A .hint captioning a button inline sat on the button's text baseline and
    # read as sinking; it is centered on the control instead.
    assert "form .btn + .hint { vertical-align: middle; }" in css


def test_queue_polish_form_grid_checkbox_css(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    # A form-grid cell holding only a checkbox ("Plan only") bottom-anchors to
    # the input row instead of floating at the top with the labels.
    assert ".form-grid > div:has(> label.check:only-child)" in css


def test_queue_items_note_column_has_no_wrap_floor(client: TestClient) -> None:
    added = _post(client, "/queue/add", {"idea": "polish item", "priority": "high"})
    assert added.status_code == 303
    html = client.get("/queue").text
    assert ">Note</th>" in html and ">Actions</th>" in html
    # The Note column's 12em .wrap floor pushed the 8-column items table 18px
    # past its container, slicing the Remove buttons at the table's right edge.
    assert '<td class="wrap small">' not in html
    assert '<td class="small"></td>' in html  # the empty note cell still renders


# ---- Cycle 3: live-region loading feedback (issue #184) ----------------------------------
# No JS test harness exists in this repo, so the live.js contract is asserted as
# served substrings (the same pattern as the queue-polish CSS tests above):
# the fetch sets aria-busy + data-live-loading and clears both on resolve, and
# a region that starts empty gets a "Loading…" placeholder until the first
# fetch replaces it. The CSS assertion pins the subtle-dim rule: opacity with a
# transition delay, so fast auto-refreshes never visibly flicker.


def test_live_region_loading_feedback_js_and_css(client: TestClient) -> None:
    js = client.get("/static/live.js").text
    assert 'el.setAttribute("aria-busy", "true")' in js
    assert 'el.setAttribute("data-live-loading", "true")' in js
    assert 'el.removeAttribute("aria-busy")' in js
    assert 'el.removeAttribute("data-live-loading")' in js
    assert "Loading…" in js
    css = client.get("/static/style.css").text
    assert "[data-live-loading] { opacity: 0.7; transition-delay: 200ms; }" in css
    assert ".live-loading" in css


# ---- Functional sweep (issue #174) ----------------------------------------------------------
# The dashboard HTTP sweep found that POST /runs/{run_id}/modify on a run waiting
# to ship without a plan crashed with AssertionError (pipeline.request_changes'
# `assert run.plan is not None`) — a 500 traceback page. The assert is now a
# PipelineError, which the modify handler turns into an actionable error flash.
# The same sweep found changes_task_card crashed with ValueError (max() on an
# empty task list); it now numbers the new task T1 instead.


def test_modify_run_without_plan_flashes_instead_of_500(
    client: TestClient, fixture_repo: Path
) -> None:
    response = _post(client, "/runs/new", {"idea": "planless run", "plan_only": "on"})
    assert response.status_code == 303
    run_id = response.headers["location"].rsplit("/", 1)[1]
    with _store(fixture_repo) as store:
        store.set_run_state(run_id, RunState.AWAITING_SHIP)  # a real pipeline always plans first

    modified = _post(client, f"/runs/{run_id}/modify", {"notes": "change the world"})
    assert modified.status_code == 303  # never a 500
    page = client.get(f"/runs/{run_id}")
    assert "has no plan" in page.text  # the error is actionable in the UI


def test_changes_task_card_with_no_cards_starts_at_t1() -> None:
    from jev_agent.core.ship import changes_task_card

    card = changes_task_card([], "  do the thing  ")
    assert card.id == "T1" and card.depends_on == [] and "do the thing" in card.description
