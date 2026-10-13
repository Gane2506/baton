"""`jev dashboard`: pages, actions and the security model, through Starlette's in-process test
client (no listening socket, no real workers or network)."""

from __future__ import annotations

import json
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.cli import dashboard_cmd
from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.chat import CHATS_DIRNAME, META_FILE, TRANSCRIPT_FILE
from jev_agent.core.learning import Learning
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.render import (
    LABEL_CHAR_PX,
    bar_chart,
    fit_label,
    render_bar_chart,
    render_markdown,
)
from jev_agent.dashboard.runner import RunAction, RunLauncher
from jev_agent.dashboard.security import (
    DashboardConfigError,
    DashboardSettings,
    is_loopback,
)
from jev_agent.dashboard.views import spend_by_day
from jev_agent.quality.models import GateResult

cli = CliRunner()
SECRET = "sk-or-v1-dashboard-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _jev(repo: Path, *args: str) -> str:
    result = cli.invoke(cli_app, [*args, "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    return result.output


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


@pytest.fixture
def planned(fixture_repo: Path) -> tuple[Path, str]:
    """A repo with one run awaiting approval (planned by the fake workers)."""
    _jev(fixture_repo, "run", "add <b>search</b> & filters")
    with _store(fixture_repo) as store:
        run = store.list_runs()[0]
    assert run.state is RunState.AWAITING_APPROVAL
    return fixture_repo, run.id


class Calls:
    """A fake executor: records calls, optionally blocks until released."""

    def __init__(self, block: bool = False) -> None:
        self.calls: list[tuple[Path, str, RunAction]] = []
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(self, root: Path, run_id: str, action: RunAction) -> str:
        self.calls.append((root, run_id, action))
        self.release.wait(10)
        return "run DONE"


def _client(repo: Path, executor: Calls | None = None, **settings: object) -> Iterator[TestClient]:
    options: dict[str, object] = {"allowed_hosts": frozenset({"testserver"})}
    options.update(settings)
    app = create_app(
        DashboardSettings(root=repo, **options),  # type: ignore[arg-type]
        executor=executor or Calls(),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(planned: tuple[Path, str]) -> Iterator[TestClient]:
    yield from _client(planned[0])


def _csrf(client: TestClient, page: str = "/") -> str:
    response = client.get(page)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', response.text)
    if match:
        return match.group(1)
    # Pages without forms: take it from the queue page, which always has one.
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/queue").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    token: str | None = getattr(client, "jev_csrf", None)  # one per session: fetch it once
    if token is None:
        token = _csrf(client, "/queue")
        client.jev_csrf = token
    response: httpx.Response = client.post(
        url, data={"csrf_token": token, **(data or {})}, follow_redirects=False
    )
    return response


def _launcher(client: TestClient) -> RunLauncher:
    launcher = client.app.state.dashboard.launcher  # type: ignore[attr-defined]
    assert isinstance(launcher, RunLauncher)
    return launcher


# ---- pages -------------------------------------------------------------------------------


def test_board_shows_runs_and_queue_escaped(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.add_queue_item("queued <script>alert(1)</script> idea", "high")
    page = client.get("/")
    assert page.status_code == 200
    for column in ("Backlog", "Planning", "Awaiting approval", "Running", "Done", "Blocked"):
        assert column in page.text
    assert run_id in page.text and f'href="/runs/{run_id}"' in page.text
    assert "add &lt;b&gt;search&lt;/b&gt; &amp; filters" in page.text
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;alert(1)" in page.text
    assert 'data-live="/fragments/board"' in page.text
    fragment = client.get("/fragments/board")
    assert fragment.status_code == 200 and run_id in fragment.text
    assert "<html" not in fragment.text


def test_security_headers_and_static(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert '<script src="/static/live.js"' in response.text
    assert "http://" not in response.text.split("<main>")[0]  # no external assets
    assert client.get("/static/live.js").status_code == 200
    assert "prefers-color-scheme: dark" in client.get("/static/style.css").text
    assert '<link rel="icon" href="/static/favicon.svg"' in response.text
    assert '<img src="/static/favicon.svg"' in response.text
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200 and favicon.headers["content-type"].startswith("image/svg")


def test_run_page_shows_plan_decisions_attempts_and_files(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    run_id = planned[1]
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    text = page.text
    assert "AWAITING_APPROVAL" in text and "Fake task" in text
    assert "Decisions" in text and "D1" in text and "rules" in text
    assert "FALLBACK" not in text and "jev: raised DispatchError" in text  # fallback reason
    assert "planner" in text and "validator" in text  # attempts
    assert f'action="/runs/{run_id}/approve"' in text
    assert f'action="/runs/{run_id}/cancel"' in text
    assert f'action="/runs/{run_id}/resume"' not in text
    assert "approve" in text and "Validation" in text
    assert f"/runs/{run_id}/files/run/" in text  # log links
    live = client.get(f"/runs/{run_id}/live")
    assert live.status_code == 200 and "Fake task" in live.text
    assert client.get("/runs/20200101-000000-abcd").status_code == 404
    assert client.get("/runs/not-a-run").status_code == 404


def test_run_page_renders_quality_review_sanitized(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.add_quality_report(run_id, GateResult(level="basic", scope="integration"))
    review = repo / ".jev" / "runs" / run_id / "quality" / "review.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(
        "# Review\n\n**bold** finding\n\n<script>alert('x')</script>\n\n"
        "[click](javascript:alert(1)) <img src=x onerror=alert(1)>\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    text = client.get(f"/runs/{run_id}").text
    assert "<strong>bold</strong>" in text and "<table>" in text
    assert "<script>alert" not in text and "&lt;script&gt;" in text
    main = text.split("<main>", 1)[1]  # the header carries the logo <img>
    assert "<img" not in main and "<img src=x" not in text and 'href="javascript:' not in text
    assert "basic: passed" in client.get("/").text  # quality status on the card


def test_render_markdown_strips_html_and_bad_links() -> None:
    html = str(render_markdown("<script>x()</script>\n\n[a](javascript:x()) [b](https://e.org)"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html and 'href="https://e.org"' in html


def test_queue_page_and_actions(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, _ = planned
    page = client.get("/queue")
    assert page.status_code == 200 and "Scheduler" in page.text and "not running" in page.text
    response = _post(
        client,
        "/queue/add",
        {"idea": "first idea", "priority": "normal", "plan_only": "on", "learning": "none"},
    )
    assert response.status_code == 303
    _post(client, "/queue/add", {"idea": "second idea", "priority": "low", "quality": "basic"})
    page = client.get("/queue")
    assert "queued item 1 (normal)" in page.text  # the one-time message
    assert "plan-only no-learning" in page.text and "quality=basic" in page.text
    with _store(repo) as store:
        assert [i.idea for i in store.list_queue()] == ["first idea", "second idea"]

    _post(client, "/queue/2/move", {"direction": "top"})
    with _store(repo) as store:
        items = store.list_queue()
        assert [i.idea for i in items] == ["second idea", "first idea"]
        assert items[0].priority == "normal"
    assert "low -&gt; normal" in client.get("/queue").text

    _post(client, "/queue/1/remove")
    with _store(repo) as store:
        assert [i.id for i in store.list_queue()] == [2]

    # Errors come back as messages, nothing changes.
    _post(client, "/queue/add", {"idea": "   "})
    assert "the idea is empty" in client.get("/queue").text
    _post(client, "/queue/add", {"idea": "x", "learning": "nope"})
    assert "no learning profile named" in client.get("/queue").text
    _post(client, "/queue/add", {"idea": "x", "quality": "ultra"})
    assert "unknown quality level" in client.get("/queue").text
    _post(client, "/queue/99/remove")
    assert "queue item 99 not found" in client.get("/queue").text
    _post(client, "/queue/2/move", {"direction": "sideways"})
    assert "move needs a direction" in client.get("/queue").text
    with _store(repo) as store:
        assert len(store.list_queue()) == 1


def test_queue_page_shows_scheduler_holder(client: TestClient, planned: tuple[Path, str]) -> None:
    import os
    import socket

    repo, _ = planned
    lock = repo / ".jev" / "scheduler.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": "2026-10-12T10:00:00+00:00",
                "current_item": 7,
            }
        )
    )
    text = client.get("/queue").text
    assert f"pid {os.getpid()}" in text and "#7" in text


def test_approve_starts_background_job_once(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    calls = Calls(block=True)
    for client in _client(repo, calls):
        response = _post(client, f"/runs/{run_id}/approve")
        assert response.status_code == 303
        assert response.headers["location"] == f"/runs/{run_id}"
        assert "approve started in the background" in client.get(f"/runs/{run_id}").text
        # A second start while the first is running is refused (one job per run).
        _post(client, f"/runs/{run_id}/approve")
        assert "already being worked on" in client.get(f"/runs/{run_id}").text
        assert "working in the background" in client.get(f"/runs/{run_id}/live").text
        calls.release.set()
        job = _launcher(client).wait(run_id)
        assert job is not None and job.state == "finished" and job.message == "run DONE"
        assert calls.calls == [(repo, run_id, "approve")]
        assert "run DONE" in client.get(f"/runs/{run_id}/live").text


def test_approve_and_resume_refused_in_wrong_state(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    _, run_id = planned
    _post(client, f"/runs/{run_id}/resume")
    assert "cannot be resumed" in client.get(f"/runs/{run_id}").text
    _post(client, f"/runs/{run_id}/cancel")
    text = client.get(f"/runs/{run_id}").text
    assert "CANCELLED" in text and "branches and worktrees are kept" in text
    _post(client, f"/runs/{run_id}/approve")
    assert "only a run awaiting approval" in client.get(f"/runs/{run_id}").text
    _post(client, f"/runs/{run_id}/cancel")
    assert "already CANCELLED" in client.get(f"/runs/{run_id}").text
    assert _launcher(client).job(run_id) is None
    assert client.post(f"/runs/{run_id}/explode").status_code in (403, 404)


def test_approve_runs_the_real_cli_code_path(planned: tuple[Path, str]) -> None:
    """The default executor is `jev approve`'s path; with fake workers the run finishes."""
    repo, run_id = planned
    app = create_app(DashboardSettings(root=repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as client:
        _post(client, f"/runs/{run_id}/approve")
        job = _launcher(client).wait(run_id, timeout=60)
        assert job is not None and job.state == "finished", job
        assert job.message.startswith("run DONE")
    with _store(repo) as store:
        assert store.get_run(run_id).state is RunState.DONE
    for client in _client(repo):
        text = client.get(f"/runs/{run_id}").text
        assert f"jev/{run_id}/result" in text and "coder" in text


def test_executor_errors_are_reported(planned: tuple[Path, str]) -> None:
    repo, run_id = planned

    def boom(root: Path, rid: str, action: RunAction) -> str:
        raise PipelineError("no workers here")

    launcher = RunLauncher(repo, boom)
    assert launcher.start(run_id, "resume")
    job = launcher.wait(run_id)
    assert job is not None and job.state == "failed" and job.message == "no workers here"
    assert not launcher.busy(run_id)
    assert launcher.start(run_id, "resume")  # the lock was released
    launcher.wait(run_id)


def test_learning_page_and_actions(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, _ = planned
    assert "Learning is" in client.get("/learning").text
    _post(client, "/learning/create", {"name": "web"})
    _post(client, "/learning/create", {"name": "bad name!"})
    page = client.get("/learning").text
    assert "created profile web (repo)" in page and "not a valid profile name" in page
    _post(client, "/learning/use", {"profile": "web"})
    _post(client, "/learning/enable")
    with _store(repo) as store:
        learning = Learning.for_store(store)
        lesson = learning.repo.add_lesson("web", "Run the <tests> first", ["coder"], ["tests"])
        learning.repo.add_lesson("web", "Keep diffs small", ["coder"], [])
        learning.close()
    page = client.get("/learning?profile=web").text
    assert "Run the &lt;tests&gt; first" in page and "Keep diffs small" in page
    assert "on" in page and "<strong>web</strong>" in page

    _post(client, "/learning/forget", {"lesson": lesson.id})
    assert f"retired {lesson.id} from web" in client.get("/learning?profile=web").text
    assert "Run the &lt;tests&gt; first" in client.get("/learning?profile=web&all=1").text

    _post(client, "/learning/disable", {"profile": "web"})
    _post(client, "/learning/enable", {"profile": "web"})
    _post(client, "/learning/disable")
    assert "learning is off for this repo" in client.get("/learning").text


def test_learning_flush_requires_confirmation(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    repo, _ = planned
    with _store(repo) as store:
        learning = Learning.for_store(store)
        learning.create("web")
        learning.repo.add_lesson("web", "one", ["coder"], [])
        learning.repo.add_lesson("web", "two", ["coder"], [])
        learning.close()

    def count() -> int:
        with _store(repo) as store:
            learning = Learning.for_store(store)
            try:
                return len(learning.lessons("web"))
            finally:
                learning.close()

    confirm = _post(client, "/learning/flush", {"profile": "web"})
    assert confirm.status_code == 200
    assert "Delete 2 lesson(s)" in confirm.text
    assert 'name="confirm" value="yes"' in confirm.text
    assert count() == 2
    _post(client, "/learning/flush", {"profile": "web", "confirm": "no"})
    assert count() == 2
    done = _post(client, "/learning/flush", {"profile": "web", "confirm": "yes"})
    assert done.status_code == 303
    assert count() == 0
    assert "deleted 2 lesson(s) from web" in client.get("/learning?profile=web").text
    _post(client, "/learning/flush", {"profile": "ghost"})
    assert "no learning profile named" in client.get("/learning").text


def test_chats_pages(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    chat_id = "c-20261012-153012-ab12"
    folder = repo / ".jev" / CHATS_DIRNAME / chat_id
    folder.mkdir(parents=True)
    meta = {
        "chat_id": chat_id,
        "created_at": "2026-10-12T15:30:12+00:00",
        "updated_at": "2026-10-12T15:40:00+00:00",
        "learning": {"mode": "default"},
        "profile": None,
        "backend": "api",
        "model": "some/model",
        "turns": 1,
        "brief": "Add <search> to the CLI",
        "runs": [{"run_id": run_id, "kind": "plan", "at": "2026-10-12T15:41:00+00:00"}],
    }
    (folder / META_FILE).write_text(json.dumps(meta))
    (folder / TRANSCRIPT_FILE).write_text(
        json.dumps({"at": "2026-10-12T15:30:20+00:00", "kind": "user", "text": "hi <b>there</b>"})
        + "\nnot json\n"
        + json.dumps({"at": "2026-10-12T15:30:30+00:00", "kind": "assistant", "text": "hello"})
        + "\n"
    )
    listing = client.get("/chats")
    assert listing.status_code == 200 and chat_id in listing.text
    assert "Add &lt;search&gt; to the CLI" in listing.text
    page = client.get(f"/chats/{chat_id}")
    assert page.status_code == 200
    assert "hi &lt;b&gt;there&lt;/b&gt;" in page.text and "hello" in page.text
    assert f'href="/runs/{run_id}"' in page.text
    assert client.get("/chats/c-20990101-000000-0000").status_code == 404
    assert client.get("/chats/..%2F..%2Fstate.db").status_code == 404
    assert client.get("/chats/nonsense").status_code == 404


def test_spend_page(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.add_run_spend(run_id, 1.25)
    page = client.get("/spend")
    assert page.status_code == 200
    assert "<svg" in page.text and "$1.25" in page.text and f'href="/runs/{run_id}"' in page.text


def test_spend_helpers() -> None:
    chart = bar_chart([("a", 2.0, None), ("b", 1.0, "/runs/x"), ("c", 0.0, None)])
    assert chart.bars[0].width == chart.bar_area and chart.bars[1].width == chart.bar_area / 2
    assert bar_chart([("a", 0.0, None)]).bars[0].width == 0
    assert spend_by_day([], days=3)[-1][1] == 0.0 and len(spend_by_day([], days=3)) == 3


LONG_LABEL = "20261011-005528-f3a0 Stream large files through the parser in chunks <fast>"


def _svg_rows(chart_svg: str) -> list[ET.Element]:
    root = ET.fromstring(chart_svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert root.tag in ("svg", f"{ns}svg")
    return [el for el in root if el.tag.endswith("g")]


def test_fit_label_truncates_with_ellipsis() -> None:
    assert fit_label("short", 100) == "short"
    cut = fit_label(LONG_LABEL, 200)
    assert cut.endswith("\u2026") and len(cut) * LABEL_CHAR_PX <= 200
    assert LONG_LABEL.startswith(cut[:-1])
    assert fit_label("a  b\n c", 100) == "a b c"


def test_bar_chart_labels_never_overlap_bars_or_values() -> None:
    rows = [(LONG_LABEL, 1.12, "/runs/x"), ("b", 0.5, None), (LONG_LABEL + " zero", 0.0, None)]
    chart = bar_chart(rows, label_width=190)
    for bar in chart.bars:
        assert bar.text_end <= chart.label_width  # label ends before the bar starts
        assert bar.value_x >= bar.x + bar.width  # value after the bar's end
        assert bar.value_x > bar.text_end  # and after the label, also for $0.00
        assert bar.value_x + len(bar.value_text) * 7.3 <= chart.width  # value fits
    zero = chart.bars[2]
    assert zero.width == 0 and zero.value_x == chart.label_width + 6
    assert zero.text.endswith("\u2026") and zero.label == LONG_LABEL + " zero"


def test_render_bar_chart_svg_has_full_label_tooltips() -> None:
    rows = [(LONG_LABEL, 1.12, "/runs/x"), ("2026-10-01", 0.0, None)]
    chart = bar_chart(rows, label_width=190)
    svg = str(render_bar_chart(chart))
    assert "<fast>" not in svg and "&lt;fast&gt;" in svg  # escaped
    groups = _svg_rows(svg)
    assert len(groups) == 2
    for group, bar in zip(groups, chart.bars, strict=True):
        title = next(el for el in group if el.tag.endswith("title"))
        assert title.text == f"{bar.label}: {bar.value_text}"
        value = [el for el in group if el.tag.endswith("text") and el.get("class") == "value"]
        rect = next(el for el in group if el.tag.endswith("rect"))
        bar_end = float(rect.get("x", "0")) + float(rect.get("width", "0"))
        assert float(value[0].get("x", "0")) >= bar_end
        assert float(value[0].get("x", "0")) > bar.text_end
    link = next(el for el in groups[0] if el.tag.endswith("a"))
    assert link.get("href") == "/runs/x"
    assert "".join(link.itertext()) == chart.bars[0].text
    assert str(render_bar_chart(bar_chart([]), "Nothing.")) == '<p class="muted">Nothing.</p>'


def test_spend_page_truncates_long_run_labels(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    _, run_id = planned
    page = client.get("/spend")
    assert page.status_code == 200
    assert f"<title>{run_id} " in page.text


def test_settings_never_renders_secret_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    page = client.get("/settings")
    assert page.status_code == 200
    assert "OPENROUTER_API_KEY" in page.text and SECRET not in page.text
    assert "dispatcher.primary" in page.text and "quality.level" in page.text
    assert "Doctor" in page.text and "git repo" in page.text and "config" in page.text
    for path in ("/", "/queue", "/learning", "/chats", "/spend"):
        assert SECRET not in client.get(path).text


# ---- security ----------------------------------------------------------------------------


def test_csrf_rejections(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    token = _csrf(client, "/queue")
    # No token, a wrong token, or a cross-origin request: refused, nothing changes.
    assert client.post("/queue/add", data={"idea": "x"}).status_code == 403
    assert client.post("/queue/add", data={"idea": "x", "csrf_token": "0" * 64}).status_code == 403
    cross = client.post(
        "/queue/add",
        data={"idea": "x", "csrf_token": token},
        headers={"Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert (
        client.post(
            "/queue/add",
            data={"idea": "x", "csrf_token": token},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/queue/add",
            data={"idea": "x", "csrf_token": token},
            headers={"Referer": "http://evil.example/page"},
        ).status_code
        == 403
    )
    assert client.post(f"/runs/{run_id}/approve", data={}).status_code == 403
    # Another session's token does not work either.
    for other in _client(repo):
        stolen = _csrf(other, "/queue")
    fresh = TestClient(client.app)
    assert fresh.post("/queue/add", data={"idea": "x", "csrf_token": stolen}).status_code == 403
    with _store(repo) as store:
        assert store.list_queue() == []
    # Same-origin with the right token works; actions are POST only.
    ok = client.post(
        "/queue/add",
        data={"idea": "x", "csrf_token": token},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert client.get("/queue/add").status_code == 405
    assert client.get(f"/runs/{run_id}/approve").status_code == 405


def test_token_required_off_loopback(planned: tuple[Path, str]) -> None:
    repo, _ = planned
    with pytest.raises(DashboardConfigError, match="needs a token"):
        create_app(DashboardSettings(root=repo, host="0.0.0.0"))
    token = "t" * 32
    for client in _client(repo, host="0.0.0.0", token=token):
        assert client.get("/").status_code == 401
        assert client.get("/static/style.css").status_code == 401
        assert client.get("/?token=wrong").status_code == 401
        assert client.get("/", headers={"X-Jev-Token": token}).status_code == 200
        redirect = client.get(f"/queue?all=1&token={token}", follow_redirects=False)
        assert redirect.status_code == 303 and redirect.headers["location"] == "/queue?all=1"
        assert "jev_token" in redirect.headers["set-cookie"]
        assert "HttpOnly" in redirect.headers["set-cookie"]
        assert client.get("/queue?all=1").status_code == 200  # the cookie now
        assert token not in client.get("/settings").text


def test_token_on_loopback_when_given(planned: tuple[Path, str]) -> None:
    repo, _ = planned
    for client in _client(repo, token="s" * 20):
        assert client.get("/").status_code == 401


def test_loopback_rejects_foreign_host_header(client: TestClient) -> None:
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/", headers={"Host": "[::1]:8765"}).status_code == 200


def test_is_loopback() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.2")
    assert not is_loopback("example.com")


def test_log_files_served_read_only_and_traversal_safe(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    repo, run_id = planned
    base = repo / ".jev" / "runs" / run_id
    logs = sorted(p for p in base.rglob("*.log"))
    assert logs
    relative = logs[0].relative_to(base).as_posix()
    response = client.get(f"/runs/{run_id}/files/{relative}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    (base / "evil.html").write_text("<script>alert(1)</script>")
    html = client.get(f"/runs/{run_id}/files/evil.html")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/plain")
    (repo / "outside.txt").write_text("secret")
    (base / "link.txt").symlink_to(repo / "outside.txt")
    for bad in (
        "../../state.db",
        "..%2F..%2Fstate.db",
        "%2e%2e/%2e%2e/state.db",
        "run/../../../state.db",
        "link.txt",
        "",
        "run",
        "/etc/passwd",
        "%2Fetc%2Fpasswd",
    ):
        assert client.get(f"/runs/{run_id}/files/{bad}").status_code == 404, bad
    assert client.get("/runs/..%2F..%2F/files/state.db").status_code == 404
    assert client.post(f"/runs/{run_id}/files/{relative}").status_code in (403, 405)


# ---- the command -------------------------------------------------------------------------


def test_cli_prints_install_hint_without_extra(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard_cmd, "missing_dependencies", lambda: ["starlette"])
    result = cli.invoke(cli_app, ["dashboard", "--no-open", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "baton-agent-orchestrator[dashboard]" in output and "uv tool install" in output


def test_cli_generates_token_off_loopback(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    served: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(kw))
    monkeypatch.delenv(dashboard_cmd.TOKEN_ENV, raising=False)
    result = cli.invoke(
        cli_app,
        [
            "dashboard",
            "--no-open",
            "--host",
            "0.0.0.0",
            "--port",
            "8799",
            "--repo",
            str(fixture_repo),
        ],
    )
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "warning:" in output and "ssh -L 8799:127.0.0.1:8799" in output
    assert re.search(r"http://127\.0\.0\.1:8799/\?token=[\w-]{20,}", output)
    assert served == [{"host": "0.0.0.0", "port": 8799, "log_level": "warning"}]

    served.clear()
    result = cli.invoke(cli_app, ["dashboard", "--no-open", "--repo", str(fixture_repo)])
    assert result.exit_code == 0 and "?token=" not in result.output
    assert "http://127.0.0.1:8765/" in result.output
    assert served[0]["host"] == "127.0.0.1"

    result = cli.invoke(
        cli_app, ["dashboard", "--no-open", "--token", "short", "--repo", str(fixture_repo)]
    )
    assert result.exit_code == 1 and "at least 16" in result.output
