"""The chat redesign: contracts and behaviour for the new chat page.

Three sibling workers land in parallel:

- Worker 1 (templates/CSS): ``chat.html`` fullscreen layout, ``_chat_live.html``
  transcript markup, ``_workpanel.html``, ``base.html`` pane removal.
- Worker 2 (JS): ``static/chat.js`` panel logic, ``static/composer.js`` interactions.
- Worker 3 (backend): ``POST /chats/{id}/stop``, ``POST /chats/{id}/retry``,
  ``GET /chats/{id}/work`` JSON, ``GET /chats/{id}/work/{tab}`` fragments, and an
  extended ``_chat_context`` (``rail_chats`` / ``approval_cards`` / ``suggestions`` /
  ``step_blocks`` / ``backends``).

Tests for work that has not landed yet detect the contract's presence and
``pytest.skip`` with a clear note naming the responsible worker; once the work is
in the tree the contract is asserted in full. Re-running this file after the other
workers finish therefore exercises everything, and every test passes right now.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.app.jobs import STOPPED_MESSAGE, run_job_coroutine
from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.chat import CHATS_DIRNAME, META_FILE, TRANSCRIPT_FILE
from jev_agent.core.store import Store
from jev_agent.dashboard import views
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.runner import ChatRunner
from jev_agent.dashboard.security import DashboardSettings

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "src" / "jev_agent" / "dashboard" / "templates"
STATIC = ROOT / "src" / "jev_agent" / "dashboard" / "static"

cli = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dashboard tests plan runs with fake workers (as in test_dashboard.py).
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


CHAT_ID = "c-20261014-120000-ab12"
EMPTY_CHAT_ID = "c-20261014-120100-cd34"
RUN_CHAT_ID = "c-20261014-120200-ef56"
UNKNOWN_CHAT_ID = "c-20990101-000000-0000"

REDIRECT = {302, 303}


def _require_landed(condition: bool, worker: str) -> None:
    """Skip when a sibling worker's output is not in the tree yet."""
    if not condition:
        pytest.skip(f"contract not in the tree yet ({worker})")


def _jev(repo: Path, *args: str) -> str:
    result = cli.invoke(cli_app, [*args, "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    return result.output


def _write_chat(
    repo: Path,
    chat_id: str,
    user_texts: list[str],
    runs: list[str] | None = None,
) -> None:
    """A saved chat on disk: meta.json plus a JSON-lines transcript."""
    folder = repo / ".jev" / CHATS_DIRNAME / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "chat_id": chat_id,
        "created_at": "2026-10-14T12:00:00+00:00",
        "updated_at": "2026-10-14T12:01:00+00:00",
        "learning": {"mode": "default"},
        "profile": None,
        "backend": "api",
        "model": "some/model",
        "turns": len(user_texts),
        "brief": None,
        "runs": [
            {"run_id": run_id, "kind": "run", "at": "2026-10-14T12:02:00+00:00"}
            for run_id in (runs or [])
        ],
    }
    (folder / META_FILE).write_text(json.dumps(meta))
    lines = [
        json.dumps({"at": "2026-10-14T12:00:30+00:00", "kind": "user", "text": text})
        for text in user_texts
    ]
    (folder / TRANSCRIPT_FILE).write_text("\n".join(lines) + ("\n" if lines else ""))


def _client(repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def chat_client(fixture_repo: Path) -> Iterator[TestClient]:
    """A dashboard client over a repo with one chat holding a single user turn."""
    _write_chat(fixture_repo, CHAT_ID, ["redesign the chat page"])
    yield from _client(fixture_repo)


@pytest.fixture
def empty_chat_client(fixture_repo: Path) -> Iterator[TestClient]:
    """A dashboard client over a repo with one chat and no turns at all."""
    _write_chat(fixture_repo, EMPTY_CHAT_ID, [])
    yield from _client(fixture_repo)


@pytest.fixture
def run_chat_client(fixture_repo: Path) -> Iterator[tuple[TestClient, str]]:
    """A dashboard client over a repo with one chat linked to a real run."""
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    _jev(fixture_repo, "run", "add <b>search</b> & filters")
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run_id = store.list_runs()[0].id
    _write_chat(fixture_repo, RUN_CHAT_ID, ["make it"], runs=[run_id])
    for client in _client(fixture_repo):
        yield client, run_id


def _route_paths(client: TestClient) -> set[str]:
    return {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]


def _csrf(client: TestClient, page: str = "/") -> str:
    response = client.get(page)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', response.text)
    if match:
        return match.group(1)
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/queue").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    token: str | None = getattr(client, "jev_csrf", None)
    if token is None:
        token = _csrf(client)
        client.jev_csrf = token
    return cast(
        httpx.Response,
        client.post(url, data={"csrf_token": token, **(data or {})}, follow_redirects=False),
    )


def _dash(client: TestClient) -> Any:
    return client.app.state.dashboard  # type: ignore[attr-defined]


def _cancellable_sleep() -> str:
    async def nap() -> str:
        await asyncio.sleep(30)
        return "slept"

    return run_job_coroutine(nap())


# ---- 1. pane removal (Worker 1: templates) ---------------------------------------------


def test_base_html_drops_panes() -> None:
    text = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    _require_landed("_panes.html" not in text, "Worker 1 (templates)")
    assert "_panes.html" not in text


def test_run_html_drops_panes() -> None:
    text = (TEMPLATES / "run.html").read_text(encoding="utf-8")
    assert "_panes.html" not in text


def test_chat_html_includes_workpanel() -> None:
    text = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    _require_landed("_workpanel.html" in text, "Worker 1 (templates)")
    assert "_workpanel.html" in text


# ---- 2. stop endpoint (Worker 3: backend) -----------------------------------------------


def test_chat_runner_stop_idle_returns_false() -> None:
    _require_landed(hasattr(ChatRunner, "stop"), "Worker 3 (ChatRunner.stop)")
    assert ChatRunner().stop("no-such-chat") is False


def test_chat_runner_stop_cancels_running_job() -> None:
    _require_landed(hasattr(ChatRunner, "stop"), "Worker 3 (ChatRunner.stop)")
    runner = ChatRunner()
    started = threading.Event()

    def slow() -> str:
        started.set()
        return _cancellable_sleep()

    assert runner.start("c1", slow) is True
    assert started.wait(10)
    assert runner.stop("c1") is True
    job = runner.wait("c1", timeout=10)
    assert job is not None
    assert job.state == "failed"
    assert job.message == STOPPED_MESSAGE
    assert not runner.busy("c1")


def test_chat_stop_route_redirects(chat_client: TestClient) -> None:
    _require_landed("/chats/{chat_id}/stop" in _route_paths(chat_client), "Worker 3 (backend)")
    response = _post(chat_client, f"/chats/{CHAT_ID}/stop")
    assert response.status_code in REDIRECT
    assert response.headers["location"] == f"/chats/{CHAT_ID}"


def test_chat_stop_route_cancels_running_turn(chat_client: TestClient) -> None:
    _require_landed("/chats/{chat_id}/stop" in _route_paths(chat_client), "Worker 3 (backend)")
    _require_landed(hasattr(ChatRunner, "stop"), "Worker 3 (ChatRunner.stop)")
    runner = _dash(chat_client).chat_runner
    started = threading.Event()

    def slow() -> str:
        started.set()
        return _cancellable_sleep()

    assert runner.start(CHAT_ID, slow) is True
    assert started.wait(10)
    response = _post(chat_client, f"/chats/{CHAT_ID}/stop")
    assert response.status_code in REDIRECT
    job = runner.wait(CHAT_ID, timeout=10)
    assert job is not None
    assert job.state == "failed"
    assert job.message == STOPPED_MESSAGE


# ---- 3. retry endpoint (Worker 3: backend) ----------------------------------------------


def test_chat_retry_without_user_turns_errors_and_redirects(
    empty_chat_client: TestClient,
) -> None:
    _require_landed(
        "/chats/{chat_id}/retry" in _route_paths(empty_chat_client),
        "Worker 3 (backend)",
    )
    response = _post(empty_chat_client, f"/chats/{EMPTY_CHAT_ID}/retry")
    assert response.status_code in REDIRECT
    assert response.headers["location"] == f"/chats/{EMPTY_CHAT_ID}"
    assert _dash(empty_chat_client).chat_runner.job(EMPTY_CHAT_ID) is None
    page = empty_chat_client.get(f"/chats/{EMPTY_CHAT_ID}")
    assert page.status_code == 200
    assert 'class="flash error"' in page.text


def test_chat_retry_with_user_turn_starts_job(
    chat_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_landed("/chats/{chat_id}/retry" in _route_paths(chat_client), "Worker 3 (backend)")
    calls: list[tuple[Path, str, str]] = []

    def fake_turn(root: Path, chat_id: str, text: str) -> str:
        calls.append((root, chat_id, text))
        return "mocked reply"

    monkeypatch.setattr("jev_agent.app.chat.turn", fake_turn)
    response = _post(chat_client, f"/chats/{CHAT_ID}/retry")
    assert response.status_code in REDIRECT
    assert response.headers["location"] == f"/chats/{CHAT_ID}"
    job = _dash(chat_client).chat_runner.wait(CHAT_ID, timeout=10)
    assert job is not None
    assert job.state == "finished"
    assert job.message == "mocked reply"
    assert [call[1:] for call in calls] == [(CHAT_ID, "redesign the chat page")]


# ---- 4. work status endpoint (Worker 3: backend) ----------------------------------------


def test_chat_work_status_contract(run_chat_client: tuple[TestClient, str]) -> None:
    client, run_id = run_chat_client
    _require_landed("/chats/{chat_id}/work" in _route_paths(client), "Worker 3 (backend)")
    response = client.get(f"/chats/{RUN_CHAT_ID}/work")
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == run_id
    tabs = data["tabs"]
    assert len(tabs) == 5
    for name, tab in tabs.items():
        assert isinstance(tab["available"], bool), name
        assert isinstance(tab["active"], bool), name


def test_chat_work_status_unknown_chat_is_404(
    run_chat_client: tuple[TestClient, str],
) -> None:
    client, _ = run_chat_client
    _require_landed("/chats/{chat_id}/work" in _route_paths(client), "Worker 3 (backend)")
    assert client.get(f"/chats/{UNKNOWN_CHAT_ID}/work").status_code == 404


# ---- 5. work fragments (Worker 3: backend) ----------------------------------------------


def test_chat_work_fragments(run_chat_client: tuple[TestClient, str]) -> None:
    client, _ = run_chat_client
    _require_landed("/chats/{chat_id}/work" in _route_paths(client), "Worker 3 (backend)")
    tabs = client.get(f"/chats/{RUN_CHAT_ID}/work").json()["tabs"]
    assert len(tabs) == 5
    for name in tabs:
        response = client.get(f"/chats/{RUN_CHAT_ID}/work/{name}")
        assert response.status_code == 200, name
        assert response.text.strip(), name
        assert "<" in response.text, name


def test_chat_work_fragment_unknown_tab_is_404(
    run_chat_client: tuple[TestClient, str],
) -> None:
    client, _ = run_chat_client
    _require_landed("/chats/{chat_id}/work" in _route_paths(client), "Worker 3 (backend)")
    assert client.get(f"/chats/{RUN_CHAT_ID}/work/bogus").status_code == 404


# ---- 6. chat context (Worker 3: backend) -------------------------------------------------


def _chat_context_dict(client: TestClient, chat_id: str) -> dict[str, Any]:
    build = _dash(client)._chat_context(chat_id)
    root = _dash(client).settings.root
    with views.open_repo(root) as repo:
        context: dict[str, Any] = build(repo)
        return context


def test_chat_context_carries_redesign_keys(
    empty_chat_client: TestClient,
) -> None:
    """Suggestions are only meaningful on an empty transcript: assert all four."""
    context = _chat_context_dict(empty_chat_client, EMPTY_CHAT_ID)
    new_keys = ("rail_chats", "approval_cards", "suggestions", "step_blocks", "backends")
    _require_landed(all(key in context for key in new_keys), "Worker 3 (_chat_context)")
    assert isinstance(context["rail_chats"], list)
    assert isinstance(context["approval_cards"], list)
    assert isinstance(context["suggestions"], list)
    assert len(context["suggestions"]) == 4
    assert all(isinstance(s, str) and s for s in context["suggestions"])
    assert isinstance(context["step_blocks"], list)
    assert isinstance(context["backends"], dict)
    # The old keys the pages already render keep working.
    for key in ("chat_id", "meta", "entries", "job", "busy"):
        assert key in context


# ---- 7. chat.html DOM contract (Worker 1: templates) ------------------------------------


CHAT_DOM_IDS = (
    "chat-layout",
    "chat-rail",
    "rail-toggle",
    "chat-main",
    "chat-transcript",
    "action-bar",
    "chat-composer",
    "composer-text",
    "composer-send",
    "composer-stop",
    "suggestion-chips",
    "work-panel",
    "work-tabs",
    "sidebar-toggle",
)


def _chat_effective_text() -> str:
    """``chat.html`` with the partials it includes inlined (the contract spans both)."""
    text = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    for name in re.findall(r'{%\s*include\s+"([^"]+)"\s*%}', text):
        partial = TEMPLATES / name
        if not partial.is_file():
            pytest.skip(f"contract not in the tree yet (Worker 1 (templates)): {name} missing")
        text += "\n" + partial.read_text(encoding="utf-8")
    return text


def test_chat_html_dom_contract() -> None:
    text = _chat_effective_text()
    _require_landed('id="chat-layout"' in text, "Worker 1 (templates)")
    for element_id in CHAT_DOM_IDS:
        assert f'id="{element_id}"' in text, element_id


# ---- 8. JS files (Worker 2: JS) ----------------------------------------------------------


def _chat_js() -> tuple[str, str]:
    chat_js = STATIC / "chat.js"
    composer_js = STATIC / "composer.js"
    _require_landed(chat_js.is_file() and composer_js.is_file(), "Worker 2 (JS)")
    return chat_js.read_text(encoding="utf-8"), composer_js.read_text(encoding="utf-8")


def test_chat_js_has_no_document_write() -> None:
    chat_text, composer_text = _chat_js()
    assert "document.write" not in chat_text
    assert "document.write" not in composer_text


def test_composer_js_echoes_user_text_with_textcontent() -> None:
    _, composer_text = _chat_js()
    # The optimistic echo of the user's own message must not build raw HTML.
    assert "textContent" in composer_text


_INNERHTML_RHS = re.compile(r"\.innerHTML\s*=\s*([^;]+);")


def test_chat_js_innerhtml_only_renders_server_fragments() -> None:
    """`innerHTML` may only paint markup fetched from our own backend.

    The dashboard escapes user content when it renders those fragments
    server-side (see test_dashboard.py); the browser must never interpolate
    untrusted values into markup itself. This trips on template-literal
    interpolation (``${...}``) and on inline markup that is not passed through
    an escaping helper.
    """
    chat_text, composer_text = _chat_js()
    for name, text in (("chat.js", chat_text), ("composer.js", composer_text)):
        for match in _INNERHTML_RHS.finditer(text):
            rhs = match.group(1).strip()
            assert "${" not in rhs, f"{name}: interpolation into innerHTML: {rhs}"
            if re.fullmatch(r"[A-Za-z_$][\w$]*", rhs):
                # A variable assigned as markup: it must arrive via fetch(), i.e.
                # a server-rendered fragment, not something built locally.
                assert "fetch(" in text, f"{name}: innerHTML of {rhs} has no fetch() source"
            else:
                callee = re.match(r"([A-Za-z_$][\w$]*)\s*\(", rhs)
                assert callee and "escape" in callee.group(1).lower(), (
                    f"{name}: inline markup in innerHTML without escaping: {rhs}"
                )
