"""`jev dashboard`: pages, actions and the security model, through Starlette's in-process test
client (no listening socket, no real workers or network)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.app import chat
from jev_agent.cli import dashboard_cmd
from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.chat import CHATS_DIRNAME, META_FILE, TRANSCRIPT_FILE
from jev_agent.core.learning import Learning
from jev_agent.core.models import Idea, RunState
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.store import RunRecord, Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.render import (
    LABEL_CHAR_PX,
    daily_bars,
    fit_label,
    render_daily_bars,
    render_markdown,
)
from jev_agent.dashboard.runner import (
    STOPPED_MESSAGE,
    Executor,
    RunAction,
    RunLauncher,
    execute_run,
    run_job_coroutine,
)
from jev_agent.dashboard.security import (
    DashboardConfigError,
    DashboardSettings,
    is_loopback,
)
from jev_agent.dashboard.views import spend_by_day, spend_view_for_runs
from jev_agent.quality.models import GateResult
from jev_agent.workers.subprocess_runner import run_command

cli = CliRunner()
SECRET = "-".join(["obviously", "not", "a", "real", "credential"])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


def _jev(repo: Path, *args: str) -> str:
    result = cli.invoke(cli_app, [*args, "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    return result.output


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _trust(repo: Path) -> None:
    """Trust a fixture repo in the isolated test user config.

    `[ship]` is a guarded key: an untrusted repo's jev.toml may not set it (trust gate,
    audit 2026-10-16), so the fixture repo is opted in, mirroring a user who trusts
    their own checkout. Idempotent within a test.
    """
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"
    if "[trust]" not in user_config.read_text():
        with user_config.open("a") as fh:
            fh.write(f'\n[trust]\nrepos = ["{repo}"]\n')


@pytest.fixture
def planned(fixture_repo: Path) -> tuple[Path, str]:
    """A repo with one run awaiting approval (planned by the fake workers)."""
    # The jev dispatcher with its key unset: every decision falls back to rules, with a warning.
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
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


def _client(
    repo: Path, executor: Executor | None = None, **settings: object
) -> Iterator[TestClient]:
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


def test_nav_has_no_duplicate_entries() -> None:
    from jev_agent.dashboard.app import NAV

    hrefs = [item.href for item in NAV]
    assert len(hrefs) == len(set(hrefs)), "duplicate href in dashboard NAV"


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
    assert "prefers-color-scheme: light" in client.get("/static/style.css").text
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


def test_live_fragment_skips_the_log_dir_walk(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    """#52 remainder: the 3s live poll rebuilt the full run view, including an os.walk of
    the log dir. The fragment must stay walk-free; the full page still walks."""
    run_id = planned[1]
    with mock.patch("os.walk") as walk:
        live = client.get(f"/runs/{run_id}/live")
        assert live.status_code == 200
        assert "Fake task" in live.text  # the fragment still renders everything it shows
        walk.assert_not_called()
    # The full run page still lists the log files: the walk was skipped, not broken.
    with mock.patch("os.walk", wraps=os.walk) as walk:
        page = client.get(f"/runs/{run_id}")
        assert page.status_code == 200
        assert walk.call_count >= 1


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


def test_fragment_error_keeps_flash_messages(client: TestClient) -> None:
    # A live poll that fails (rendering error.html) must not eat the session's flash
    # messages: they survive until the next successful full-page render (#52).
    response = _post(client, "/queue/add", {"idea": "flash idea"})
    assert response.status_code == 303
    failed_poll = client.get("/runs/nope/live")
    assert failed_poll.status_code == 404
    page = client.get("/queue")
    assert page.status_code == 200
    assert "queued item 1 (normal)" in page.text  # the flash survived the failed poll
    assert "queued item 1 (normal)" not in client.get("/queue").text  # shown once


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


def _executing_with_open_attempt(repo: Path, run_id: str, age: timedelta | None = None) -> None:
    """Put the run in EXECUTING with a coder attempt that has no result yet (a worker that is
    running, or whose process died), started `age` ago (default: just now)."""
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.EXECUTING)
        task = store.list_tasks(run_id)[0]
        attempt_id = store.start_attempt(run_id, task.id, 1, "coder", "claude_code")
        if age is not None:
            started = (datetime.now(UTC) - age).isoformat(timespec="microseconds")
            store.connection.execute(
                "UPDATE attempts SET started_at = ? WHERE id = ?", (started, attempt_id)
            )


def test_resume_needs_force_while_an_attempt_may_be_running(planned: tuple[Path, str]) -> None:
    """Issue #15: an open attempt within its worker's timeout means another jev process may be
    running the run; a plain Resume is not offered and is refused without force."""
    repo, run_id = planned
    _executing_with_open_attempt(repo, run_id)
    calls = Calls()
    for client in _client(repo, calls):
        text = client.get(f"/runs/{run_id}").text
        assert "Force resume" in text and 'name="force" value="1"' in text
        assert "resuming starts a second one" in text
        assert ">Resume</button>" not in text
        _post(client, f"/runs/{run_id}/resume")
        text = client.get(f"/runs/{run_id}").text
        assert "may be running this run" in text and "coder attempt" in text
        assert calls.calls == []
        _post(client, f"/runs/{run_id}/resume", {"force": "1"})
        assert "resume started in the background" in client.get(f"/runs/{run_id}").text
        _launcher(client).wait(run_id)
        assert calls.calls == [(repo, run_id, "resume")]


def test_interrupted_run_still_offers_plain_resume(planned: tuple[Path, str]) -> None:
    """An open attempt older than its worker's timeout (plus a margin) is from a dead process."""
    repo, run_id = planned
    _executing_with_open_attempt(repo, run_id, age=timedelta(hours=2))
    calls = Calls()
    for client in _client(repo, calls):
        text = client.get(f"/runs/{run_id}").text
        assert ">Resume</button>" in text and "Force resume" not in text
        _post(client, f"/runs/{run_id}/resume")
        assert "resume started in the background" in client.get(f"/runs/{run_id}").text
        _launcher(client).wait(run_id)
        assert calls.calls == [(repo, run_id, "resume")]


def test_resume_needs_force_while_the_scheduler_runs_the_item(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.EXECUTING)
        run = store.get_run(run_id)
        item = store.add_queue_item(run.idea.text)
        assert store.claim_queue_item(item.id)
        # The run was created after the scheduler claimed the item (its run_id is set later).
        claimed = (run.created_at - timedelta(seconds=5)).isoformat(timespec="microseconds")
        store.connection.execute("UPDATE queue SET updated_at = ? WHERE id = ?", (claimed, item.id))
    lock = repo / ".jev" / "scheduler.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": "2026-10-12T10:00:00+00:00",
                "current_item": item.id,
            }
        )
    )
    calls = Calls()
    for client in _client(repo, calls):
        assert "Force resume" in client.get(f"/runs/{run_id}").text
        _post(client, f"/runs/{run_id}/resume")
        text = client.get(f"/runs/{run_id}").text
        assert "may be running this run" in text and f"queue item #{item.id}" in text
        assert calls.calls == []
        lock.unlink()  # the scheduler stopped: a plain resume is fine again
        assert ">Resume</button>" in client.get(f"/runs/{run_id}").text


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


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


def test_shutdown_cancels_running_jobs_and_their_processes(planned: tuple[Path, str]) -> None:
    """Stopping the server stops the jobs it started: the job's task is cancelled, which kills
    the agent's process group (issue #13)."""
    repo, run_id = planned
    pid_file = repo / ".jev" / "child.pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    def agent(root: Path, rid: str, action: RunAction) -> str:
        argv = [sys.executable, "-c", code]
        result = run_job_coroutine(run_command(argv, root, dict(os.environ), 60))
        return f"agent exited {result.exit_code}"

    for client in _client(repo, executor=agent):
        launcher = _launcher(client)
        assert launcher.start(run_id, "resume")
        _wait_for(lambda: pid_file.exists() and bool(pid_file.read_text()))
    # leaving the client ran the app's lifespan shutdown
    job = launcher.job(run_id)
    assert job is not None and job.state == "failed" and job.message == STOPPED_MESSAGE
    assert not launcher.busy(run_id)
    pid = int(pid_file.read_text())
    _wait_for(lambda: not _process_alive(pid))


def test_shutdown_before_the_coroutine_starts(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    entered = threading.Event()
    go = threading.Event()

    def slow_start(root: Path, rid: str, action: RunAction) -> str:
        entered.set()
        go.wait(10)  # shutdown is requested while the executor has not reached its coroutine
        return run_job_coroutine(asyncio.sleep(0, result="never"))

    launcher = RunLauncher(repo, slow_start)
    assert launcher.start(run_id, "approve")
    entered.wait(10)
    stopper = threading.Thread(target=launcher.shutdown, args=(10.0,))
    stopper.start()
    _wait_for(lambda: launcher._cancels[run_id].requested)
    go.set()
    stopper.join(10)
    job = launcher.job(run_id)
    assert job is not None and job.message == STOPPED_MESSAGE


def test_run_job_coroutine_outside_a_job_is_asyncio_run() -> None:
    assert run_job_coroutine(asyncio.sleep(0, result=42)) == 42


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if not Path("/proc").is_dir():  # macOS: kill(0) succeeded, so it runs
        return True
    try:  # a zombie still answers kill(0); it may also be reaped right now
        state = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False
    return "\nState:\tZ" not in state


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


def _repo_toggle_buttons(page_text: str) -> tuple[int, int]:
    """`(on buttons, off buttons)` for the repo-level learning switch."""
    return page_text.count(">Turn learning on</button>"), page_text.count(
        ">Turn learning off</button>"
    )


def test_learning_toggle_is_one_button_reflecting_state(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    page = client.get("/learning").text
    assert _repo_toggle_buttons(page) == (1, 0)  # off: only "Turn learning on"

    _post(client, "/learning/enable")
    page = client.get("/learning").text
    assert _repo_toggle_buttons(page) == (0, 1)  # on: only "Turn learning off"

    _post(client, "/learning/disable")
    page = client.get("/learning").text
    assert _repo_toggle_buttons(page) == (1, 0)


def test_kpis_labels_unfinished_runs(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.FAILED)
    page = client.get("/kpis").text
    assert "Unfinished" in page
    assert "Stopped" not in page
    assert '<div class="stat-value">1</div>' in page  # one unfinished run


def test_board_has_legend_and_labeled_done_control(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    page = client.get("/").text
    assert "States legend" in page
    for column in ("Backlog", "Planning", "Awaiting approval", "Running", "Done", "Blocked"):
        assert column in page
    # the done-runs switch is a labeled control, not a bare link
    assert 'name="all"' in page and "Show every done run" in page
    assert "Show the last 10 done runs" not in page
    assert "/?all=1" not in page


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
    assert "Delete 2 lessons" in confirm.text
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


def test_chats_new_form_guiding_notice_without_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backend ready: the New chat form guides, never leads with a red error."""
    monkeypatch.setattr(
        chat,
        "backend_readiness",
        lambda config: {
            "api": (False, "OPENROUTER_API_KEY is unset"),
            "claude_code": (False, "claude is not on PATH"),
        },
    )
    page = client.get("/chats")
    assert page.status_code == 200
    assert "No chat backend is set up yet" in page.text
    assert 'class="notice"' in page.text
    assert 'href="/connections"' in page.text
    assert "Claude Code" in page.text
    assert "Codex" in page.text
    # The red error wall is gone…
    assert "flash error" not in page.text
    assert "No chat backend is ready" not in page.text
    # …and the form keeps every field with the same behavior: the minimal row
    # (task input + backend + model + Start) plus the Advanced section.
    assert 'action="/chats/new"' in page.text
    assert 'name="text"' in page.text and "Describe a task or ask a question" in page.text
    assert 'class="advanced-details"' in page.text
    for field in (
        'name="worker"',
        'name="model"',
        'name="learning"',
        'name="design_system"',
        'name="profile"',
        'name="budget_usd"',
    ):
        assert field in page.text
    assert ">Start<" in page.text


def test_chats_new_form_no_notice_with_backend_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready backend: no 'no backend' guidance block, and the worker option is selectable."""
    monkeypatch.setattr(
        chat,
        "backend_readiness",
        lambda config: {
            "api": (True, "OPENROUTER_API_KEY is set"),
            "claude_code": (False, "claude is not on PATH"),
        },
    )
    page = client.get("/chats")
    assert page.status_code == 200
    assert "No chat backend is set up yet" not in page.text
    assert 'value="api" selected' in page.text


def test_spend_page(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.add_run_spend(run_id, 1.25)
    page = client.get("/spend")
    assert page.status_code == 200
    assert "<svg" in page.text and "$1.25" in page.text and f'href="/runs/{run_id}"' in page.text


def test_spend_helpers() -> None:
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


def test_spend_page_truncates_long_ideas_in_costliest_table(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    repo, _ = planned
    long_idea = "Rebuild the entire reporting pipeline from scratch " * 3
    assert len(long_idea) > 64
    with _store(repo) as store:
        run = store.create_run(Idea(text=long_idea, repo_path=repo))
        store.add_run_spend(run.id, 9.99)
    page = client.get("/spend")
    assert page.status_code == 200
    # The cell shows the truncated idea; the full text stays in the tooltip.
    assert f'title="{long_idea}"' in page.text
    assert "…" in page.text


def _run_record(
    run_id: str, spend: float, created_at: datetime, idea: str = "test idea"
) -> RunRecord:
    return RunRecord(
        id=run_id,
        idea=Idea(text=idea, repo_path=Path("/tmp")),
        state=RunState.DONE,
        plan=None,
        plan_round=1,
        spend_usd=spend,
        created_at=created_at,
        updated_at=created_at,
    )


def test_spend_by_day_buckets_runs_by_utc_start_day() -> None:
    runs = [
        _run_record("a", 1.0, datetime(2026, 10, 14, 10, 0, tzinfo=UTC)),
        _run_record("b", 2.0, datetime(2026, 10, 14, 23, 30, tzinfo=UTC)),
        _run_record("c", 4.0, datetime(2026, 10, 10, 8, 0, tzinfo=UTC)),
    ]
    days = dict(spend_by_day(runs, days=30, today=date(2026, 10, 15)))
    assert len(days) == 30
    assert days[date(2026, 10, 14)] == 3.0  # same UTC day sums
    assert days[date(2026, 10, 10)] == 4.0
    assert days[date(2026, 10, 15)] == 0.0  # days without runs are present at $0
    assert days[date(2026, 9, 16)] == 0.0  # oldest day of the window


def test_spend_by_day_uses_utc_not_local_time() -> None:
    # 00:30 at +02:00 is still the previous day in UTC.
    plus_two = timezone(timedelta(hours=2))
    run = _run_record("a", 1.5, datetime(2026, 10, 15, 0, 30, tzinfo=plus_two))
    days = dict(spend_by_day([run], days=3, today=date(2026, 10, 15)))
    assert days[date(2026, 10, 14)] == 1.5
    assert days[date(2026, 10, 15)] == 0.0


def test_spend_by_day_ignores_runs_outside_the_window() -> None:
    run = _run_record("old", 9.0, datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    days = spend_by_day([run], days=30, today=date(2026, 10, 15))
    assert len(days) == 30 and all(usd == 0.0 for _, usd in days)


def test_spend_view_for_runs_week_month_and_avg() -> None:
    today = date(2026, 10, 15)
    runs = [
        _run_record("today", 1.25, datetime(2026, 10, 15, 9, 0, tzinfo=UTC)),
        _run_record("week", 2.50, datetime(2026, 10, 10, 9, 0, tzinfo=UTC)),
        _run_record("month", 4.00, datetime(2026, 9, 20, 9, 0, tzinfo=UTC)),
        _run_record("old", 8.00, datetime(2026, 8, 1, 9, 0, tzinfo=UTC)),
    ]
    view = spend_view_for_runs(runs, today=today)
    assert view.total == 15.75
    assert view.week_usd == 3.75  # today + 5-days-ago only
    assert view.month_usd == 7.75  # everything inside the 30-day window
    assert view.avg_usd == pytest.approx(15.75 / 4)
    assert view.has_spend
    assert [r.id for r in view.costliest] == ["old", "month", "week", "today"]
    assert len(view.day_totals) == 30


def test_spend_view_for_runs_empty() -> None:
    view = spend_view_for_runs([], today=date(2026, 10, 15))
    assert (view.total, view.week_usd, view.month_usd, view.avg_usd) == (0.0, 0.0, 0.0, 0.0)
    assert not view.has_spend and view.costliest == [] and len(view.day_totals) == 30


def test_daily_bars_scales_columns_to_max() -> None:
    days = [(date(2026, 10, 13) + timedelta(days=i), v) for i, v in enumerate([0.0, 2.0, 4.0])]
    chart = daily_bars(days)
    assert len(chart.bars) == 3
    tallest = max(bar.height for bar in chart.bars)
    assert chart.bars[2].height == tallest
    assert chart.bars[1].height == pytest.approx(tallest / 2)
    assert chart.bars[0].height == 3.0  # the $0.00 stub keeps the day visible
    assert all(bar.y + bar.height == pytest.approx(chart.baseline) for bar in chart.bars)


def test_render_daily_bars_has_one_tooltip_per_day() -> None:
    pair = [(date(2026, 10, 14), 1.25), (date(2026, 10, 15), 0.0)]
    svg = str(render_daily_bars(daily_bars(pair)))
    assert svg.count("<title>") == 2
    assert "<title>2026-10-14: $1.25</title>" in svg
    assert "<title>2026-10-15: $0.00</title>" in svg
    assert 'class="col zero"' in svg and 'class="col"' in svg
    assert "Oct 14" in svg  # sparse, human-readable tick labels


def test_render_daily_bars_empty() -> None:
    assert str(render_daily_bars(daily_bars([]))) == '<p class="muted">No data yet.</p>'


@pytest.fixture
def empty_client(fixture_repo: Path) -> Iterator[TestClient]:
    """A dashboard client for a repo with no runs at all."""
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    yield from _client(fixture_repo)


def test_spend_page_summary_cards(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.add_run_spend(run_id, 1.25)
        older = store.create_run(Idea(text="older run", repo_path=repo))
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="microseconds")
        store.connection.execute(
            "UPDATE runs SET created_at=?, updated_at=?, spend_usd=? WHERE id=?",
            (ten_days_ago, ten_days_ago, 2.50, older.id),
        )
    page = client.get("/spend")
    assert page.status_code == 200
    text = page.text
    for label in ("Total spend", "This week", "This month", "Avg per run"):
        assert label in text
    assert "$3.75" in text  # total and month: both runs
    assert "$1.25" in text  # this week: only the recent run
    assert f"${3.75 / 2:.2f}" in text  # avg per run
    assert "No spend recorded yet" not in text


def test_spend_page_empty_state_teaches(empty_client: TestClient) -> None:
    page = empty_client.get("/spend")
    assert page.status_code == 200
    text = page.text
    assert "No spend recorded yet" in text  # the $0.00 cards read as intentional
    assert "No spend in the last 30 days" in text  # under the chart
    assert "<svg" in text  # the 30-day axis still renders
    assert "No runs yet" in text
    assert "Start one with" in text and "jev run" in text


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


# ---- jev ship and the KPIs page ----------------------------------------------------------


def _shipped(repo: Path, run_id: str, number: int, **sync: object) -> None:
    with _store(repo) as store:
        pr = store.add_shipped_pr(
            run_id,
            remote="origin",
            repo="acme/widgets",
            branch=f"jev/{run_id}-{number}",
            base="main",
            number=number,
            url=f"https://github.com/acme/widgets/pull/{number}",
            title="add <b>search</b>",
            draft=True,
            pushed_commits=2,
        )
        if sync:
            fields: dict[str, Any] = {
                "state": "open",
                "draft": False,
                "merged_at": None,
                "closed_at": None,
                "review_comments": 1,
                "reviews": 1,
                "ci_status": "success",
                "commits_after_open": 0,
                "additions": 10,
                "deletions": 1,
                "changed_files": 2,
                "reverted": None,
                "raw": None,
            }
            fields.update(sync)
            store.update_shipped_pr(pr.id, **fields)


def test_kpis_page_empty_state(client: TestClient) -> None:
    text = client.get("/kpis").text
    assert 'href="/kpis" class="active"' in text
    assert "No shipped pull requests yet" in text and "jev ship RUN_ID" in text
    assert "enabled = true" in text  # shipping is off by default


def test_kpis_page_lists_prs_cards_and_trends(
    client: TestClient, planned: tuple[Path, str]
) -> None:
    repo, run_id = planned
    for number in (11, 12, 13):
        _shipped(repo, run_id, number)
    _shipped(repo, run_id, 14, state="merged", merged_at=datetime.now(UTC) + timedelta(hours=5))
    response = client.get("/kpis?last=3")
    assert response.status_code == 200
    text = response.text
    assert "Median cost per PR" in text and "Merged" in text and "1 of 3" in text
    assert "https://github.com/acme/widgets/pull/14" in text and ">#11<" not in text
    assert "add &lt;b&gt;search&lt;/b&gt;" in text and "<b>search</b>" not in text
    assert text.count('<svg class="trend"') >= 5
    # The KPIs page carries no executable inline JS: the command palette's data
    # block and the vendored scripts are the only script tags allowed.
    scripts = re.findall(r"<script[^>]*>", text.split("</head>")[1])
    assert all('type="application/json"' in s or "src=" in s for s in scripts), scripts
    assert client.get("/kpis?last=oops").status_code == 200


def test_render_trend_chart_marks_regressions_and_escapes() -> None:
    from jev_agent.core.benchmarks import KPI_BY_KEY, trend
    from jev_agent.dashboard.render import render_trend_chart

    series = [(f"#{n}<x>", v) for n, v in enumerate([1.0, 1.1, 0.9, 1.0, 3.0])]
    chart = str(render_trend_chart(trend(KPI_BY_KEY["cost_usd"], series)))
    root = ET.fromstring(chart)
    circles = [el for el in root.iter() if el.tag.endswith("circle")]
    assert len(circles) == 5
    assert [c.get("class") for c in circles].count("pt bad") == 1
    assert circles[-1].get("class") == "pt bad"
    assert "#4&lt;x&gt;" in chart and "<x>" not in chart
    assert 'class="median"' in chart and "style=" not in chart
    empty = str(render_trend_chart(trend(KPI_BY_KEY["cost_usd"], [("#1", None)])))
    assert "No data yet" in empty and "<svg" not in empty
    single = str(render_trend_chart(trend(KPI_BY_KEY["cost_usd"], [("#1", 2.0)])))
    ET.fromstring(single)


def test_ship_button_confirmation_and_background_job(
    planned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_agent.core.kpi import RunKpis
    from jev_agent.core.ship import ShipError, ShipPlan
    from jev_agent.dashboard import app as app_module

    repo, run_id = planned
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.DONE)
    calls = Calls()
    for client in _client(repo, calls):
        assert "Ship as PR" not in client.get(f"/runs/{run_id}").text  # off by default
        (repo / "jev.toml").write_text("[ship]\nenabled = true\n")
        _trust(repo)  # `[ship]` is a guarded key (trust gate, audit 2026-10-16)
        assert "Ship as PR" in client.get(f"/runs/{run_id}").text

        def refuse(*args: object, **kwargs: object) -> ShipPlan:
            raise ShipError("this repo has no remote 'origin'")

        monkeypatch.setattr(app_module, "prepare_ship", refuse)
        response = _post(client, f"/runs/{run_id}/ship")
        assert response.status_code == 303
        assert "no remote" in client.get(f"/runs/{run_id}").text

        plan = ShipPlan(
            run_id=run_id,
            remote="origin",
            remote_url="git@github.com:acme/widgets.git",
            repo="acme/widgets",
            branch=f"jev/{run_id}",
            base="main",
            title="add <b>search</b>",
            body="## Idea\n\n> add <script>x</script>\n",
            draft=True,
            labels=[],
            head_oid="0" * 40,
            pushed_commits=1,
            kpis=RunKpis.model_validate(
                {
                    "run_id": run_id,
                    "total_cost_usd": 0,
                    "wall_clock_s": 1,
                    "tasks": 1,
                    "tasks_done": 1,
                    "attempts_total": 1,
                    "attempts_per_task": 1,
                    "first_pass_rate": 1,
                    "retries": 0,
                    "escalations": 0,
                    "reassigns": 0,
                    "fix_rounds": 0,
                    "quality_level": "none",
                }
            ),
        )

        def ready(*args: object, plan: ShipPlan = plan, **kwargs: object) -> ShipPlan:
            return plan

        monkeypatch.setattr(app_module, "prepare_ship", ready)
        response = _post(client, f"/runs/{run_id}/ship")
        assert response.status_code == 200
        text = response.text
        assert "Push and open the PR" in text and 'name="confirm" value="yes"' in text
        assert "add &lt;b&gt;search&lt;/b&gt;" in text and "<script>x" not in text
        assert calls.calls == []  # nothing runs before the confirmation

        response = _post(client, f"/runs/{run_id}/ship", {"confirm": "yes"})
        assert response.status_code == 303
        _launcher(client).wait(run_id)
        assert calls.calls == [(repo, run_id, "ship")]

        _shipped(repo, run_id, 42)
        live = client.get(f"/runs/{run_id}/live").text
        assert "https://github.com/acme/widgets/pull/42" in live
        assert "Ship as PR</button>" not in live and "already shipped" not in live


# ---- job errors and warnings (#51) ---------------------------------------------------------


def _default_job(root: Path, run_id: str, action: RunAction) -> str:
    launcher = RunLauncher(root)  # the real executor: execute_run
    assert launcher.start(run_id, action)
    job = launcher.wait(run_id)
    assert job is not None and job.state == "failed"
    return job.message


def test_job_shows_not_a_repo_error(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    message = _default_job(tmp_path / "plain", "20261013-000000-abcd", "resume")
    assert "is not inside a git repository" in message


def test_job_shows_config_error(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    (repo / "jev.toml").write_text("[workers.api]\nbogus = 1\n")
    message = _default_job(repo, run_id, "approve")
    assert "workers.api.bogus: unknown key" in message


def test_job_shows_newer_state_db_error(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    with _store(repo) as store:
        store.connection.execute("PRAGMA user_version = 999")
    actions: list[RunAction] = ["approve", "ship"]
    for action in actions:
        message = _default_job(repo, run_id, action)
        assert "database has schema version 999" in message


def test_job_message_includes_run_start_warnings(
    planned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, run_id = planned
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')  # its key is unset
    launcher = RunLauncher(repo)
    assert launcher.start(run_id, "approve")
    job = launcher.wait(run_id, timeout=60)
    assert job is not None and job.state == "finished", job and job.message
    assert job.message.startswith("run ")
    assert "; warning: " in job.message
    assert "are not set, so Jev cannot be reached" in job.message


def test_resume_needs_force_while_another_process_holds_the_lease(
    planned: tuple[Path, str],
) -> None:
    """Issue #61: a live run lease means another jev process drives the run, even between
    worker attempts (no open attempt)."""
    repo, run_id = planned
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.EXECUTING)
        store.claim_run_lease(run_id, "t", os.getpid(), socket.gethostname(), lambda _: False)
    calls = Calls()
    for client in _client(repo, calls):
        assert "Force resume" in client.get(f"/runs/{run_id}").text
        _post(client, f"/runs/{run_id}/resume")
        text = client.get(f"/runs/{run_id}").text
        assert "may be running this run" in text and f"pid {os.getpid()} on " in text
        assert calls.calls == []


@pytest.fixture
def waiting_to_ship(fixture_repo: Path) -> tuple[Path, str]:
    """A run both votes approved at D6, waiting for a person before it ships."""
    (fixture_repo / "jev.toml").write_text(
        '[dispatcher]\nprimary = "rules"\n[ship]\nenabled = true\nafter_approval = "ask"\n'
    )
    _trust(fixture_repo)  # `[ship]` is a guarded key (trust gate, audit 2026-10-16)
    _jev(fixture_repo, "run", "add search")
    with _store(fixture_repo) as store:
        run_id = store.list_runs()[0].id
    _jev(fixture_repo, "approve", run_id)
    with _store(fixture_repo) as store:
        assert store.get_run(run_id).state is RunState.AWAITING_SHIP
    return fixture_repo, run_id


def test_run_waiting_to_ship_offers_approve_modify_and_stop(
    waiting_to_ship: tuple[Path, str],
) -> None:
    repo, run_id = waiting_to_ship
    calls = Calls()
    for client in _client(repo, calls):
        page = client.get(f"/runs/{run_id}").text
        assert "Ready to ship: your call" in page and "add search" in page
        assert "D6 Vote" in page and "action=ship" in page
        for action in ("approve", "modify", "stop"):
            assert f'action="/runs/{run_id}/{action}"' in page

        _post(client, f"/runs/{run_id}/modify", {"notes": "  "})
        assert "the notes are empty" in client.get(f"/runs/{run_id}").text
        _post(client, f"/runs/{run_id}/modify", {"notes": "Add a docstring."})
        _launcher(client).wait(run_id)
        with _store(repo) as store:
            run = store.get_run(run_id)
            task = store.list_tasks(run_id)[-1]
        assert run.state is RunState.EXECUTING
        assert "Add a docstring." in task.card.description
        assert calls.calls == [(repo, run_id, "resume")]

        _post(client, f"/runs/{run_id}/stop")  # no longer waiting to ship
        assert "only a run waiting to ship" in client.get(f"/runs/{run_id}").text


def test_stop_and_approve_jobs_for_a_run_waiting_to_ship(
    waiting_to_ship: tuple[Path, str],
) -> None:
    repo, run_id = waiting_to_ship
    calls = Calls()
    for client in _client(repo, calls):
        _post(client, f"/runs/{run_id}/stop")
        _launcher(client).wait(run_id)
        _post(client, f"/runs/{run_id}/approve")
        _launcher(client).wait(run_id)
    assert calls.calls == [(repo, run_id, "stop"), (repo, run_id, "approve")]
    # The real code path: stop ends the run DONE, not shipped.
    assert "not shipped" in execute_run(repo, run_id, "stop")
    with _store(repo) as store:
        assert store.get_run(run_id).state is RunState.DONE


def test_queue_form_ship_choice(client: TestClient, planned: tuple[Path, str]) -> None:
    repo, _ = planned
    assert "ask me first (--ask-before-ship)" in client.get("/queue").text
    _post(client, "/queue/add", {"idea": "auto idea", "ship_mode": "merge"})
    assert "needs shipping on" in client.get("/queue").text
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n[ship]\nenabled = true\n')
    _trust(repo)  # `[ship]` is a guarded key (trust gate, audit 2026-10-16)
    _post(client, "/queue/add", {"idea": "auto idea", "ship_mode": "merge"})
    _post(client, "/queue/add", {"idea": "bad idea", "ship_mode": "later"})
    page = client.get("/queue").text
    assert "auto-ship" in page and "unknown ship mode" in page
    with _store(repo) as store:
        assert [(i.idea, i.ship_mode) for i in store.list_queue()] == [("auto idea", "merge")]
    settings = client.get("/settings").text
    assert "ship.after_approval" in settings and "ask" in settings


# ---- approval card cost estimate and budget gate (issue #106) -----------------


def _tiny_budget(repo: Path) -> None:
    """A run budget no estimate can fit under: the gate must refuse approval."""
    with open(repo / "jev.toml", "a") as handle:
        handle.write("[budget]\nper_run_usd = 0.0001\nper_task_usd = 0.0001\n")


def test_approval_card_shows_cost_estimate(planned: tuple[Path, str]) -> None:
    repo, run_id = planned
    for client in _client(repo):
        text = client.get(f"/runs/{run_id}").text
    assert "Estimated cost:" in text
    assert "of $5.00 run budget" in text  # the default `[budget] per_run_usd`
    assert "Approve and run" in text


def test_approve_refuses_when_estimate_exceeds_budget(
    planned: tuple[Path, str],
) -> None:
    repo, run_id = planned
    calls = Calls()
    _tiny_budget(repo)
    for client in _client(repo, calls):
        response = _post(client, f"/runs/{run_id}/approve")
        assert response.status_code == 303
        text = client.get(f"/runs/{run_id}").text
    assert "not approving" in text
    assert "Approve anyway (over budget)" in text
    # Nothing launched: the refusal is advisory, the approval gate still stands.
    assert calls.calls == []
    with _store(repo) as store:
        assert store.get_run(run_id).state is RunState.AWAITING_APPROVAL


def test_approve_force_overrides_the_budget_refusal(
    planned: tuple[Path, str],
) -> None:
    repo, run_id = planned
    calls = Calls()
    _tiny_budget(repo)
    for client in _client(repo, calls):
        _post(client, f"/runs/{run_id}/approve", data={"force": "1"})
    assert calls.calls == [(repo, run_id, "approve")]


def test_approve_warn_carries_the_warning_into_confirmation(
    planned: tuple[Path, str],
) -> None:
    """High end between warn and refuse fractions: the card warns, the confirm dialog
    carries the warning, and approving proceeds."""
    import re

    from jev_agent.config import load_config
    from jev_agent.core.budget import estimate_run_cost

    repo, run_id = planned
    with _store(repo) as store:
        run = store.get_run(run_id)
        estimate = estimate_run_cost(store, run, load_config(repo).dispatcher.cost)
    cap = estimate.high_usd / 0.9  # warn fires (0.8 * cap < high), refuse does not
    with open(repo / "jev.toml", "a") as handle:
        handle.write(f"[budget]\nper_run_usd = {cap:.4f}\nper_task_usd = {cap:.4f}\n")
    calls = Calls()
    for client in _client(repo, calls):
        text = client.get(f"/runs/{run_id}").text
        match = re.search(rf'action="/runs/{run_id}/approve" data-confirm="([^{chr(34)}]+)"', text)
        assert match is not None, text
        assert "is over 80% of the run budget" in match.group(1)
        _post(client, f"/runs/{run_id}/approve")
    assert calls.calls == [(repo, run_id, "approve")]


def test_select_chevron_rule_in_stylesheet(client: TestClient) -> None:
    """Every native select gets the custom chevron: no cramped native arrow.

    Guards the global rule in style.css — appearance:none, gradient chevron
    layers with a theme-aware CSS variable color, and right padding clearing
    the chevron (issue #130).
    """
    css = client.get("/static/style.css").text
    assert "select, select:disabled {" in css
    assert "--select-chevron:" in css
    assert "appearance: none" in css
    assert "padding-right: 34px" in css
    assert "linear-gradient(45deg" in css
    assert "linear-gradient(135deg" in css


def test_chats_api_model_picker_shows_when_no_default_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API key set but no default model: the inline picker appears (issue #149)."""
    monkeypatch.setattr(
        chat,
        "backend_readiness",
        lambda config: {
            "api": (True, "OPENROUTER_API_KEY is set"),
            "claude_code": (False, "claude is not on PATH"),
        },
    )
    page = client.get("/chats")
    assert page.status_code == 200
    # The picker notice appears; the raw "no model" error never does.
    assert "Pick your API model" in page.text
    assert 'action="/chats/api-model"' in page.text
    assert "no model for the api backend" not in page.text
