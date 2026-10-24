"""CI smoke test for the demo seed script (#57).

Copies `examples/sample-repo`, runs `jev init`, seeds it with `seed_demo.seed()`, then
GETs every dashboard page and expects 200 with no home-directory paths. Unlike the
`screenshots` CI job this needs no browser, so the seed script cannot rot silently.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples" / "demo"))

import seed_demo  # noqa: E402

from jev_agent.cli.app import app  # noqa: E402
from jev_agent.core.chat import list_chats  # noqa: E402
from jev_agent.core.pipeline import STOPPED  # noqa: E402
from jev_agent.core.store import Store  # noqa: E402
from jev_agent.dashboard.app import create_app  # noqa: E402
from jev_agent.dashboard.security import DashboardSettings  # noqa: E402

HOME = str(Path.home())

PAGES = [
    "/",
    "/fragments/board",
    "/queue",
    "/learning",
    "/chats",
    "/spend",
    "/kpis",
    "/agents",
    "/fleet",
    "/fleet/live",
    "/tools",
    "/connections",
    "/connections/live",
    "/preview/live",
    "/settings",
]


def _fake_executor(root: Path, run_id: str, action: object) -> str:
    raise AssertionError("the smoke test never starts runs")


@pytest.fixture
def demo_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "demo"
    shutil.copytree(REPO / "examples" / "sample-repo", root)
    # A hermetic home: the seed refuses to touch the real user config, and nothing it
    # writes may leak a home-directory path into the dashboard.
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    (root / ".xdg").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    result = CliRunner().invoke(app, ["init", "--repo", str(root)])
    assert result.exit_code == 0, result.output
    seed_demo.seed(root)
    return root


def _check(client: TestClient, page: str, sandbox: Path) -> None:
    response = client.get(page)
    assert response.status_code == 200, page
    # The dashboard legitimately shows paths inside the sandbox it serves (the demo repo
    # in the sidebar, the hermetic fake home). Those start with the real home whenever
    # pytest's basetemp sits under $HOME (e.g. --basetemp under ~/workspace), so strip
    # the sandbox prefix first: any home path left after that is a genuine leak of the
    # operator's own environment, not the repo being served.
    text = response.text.replace(str(sandbox), "")
    assert HOME not in text, f"{page} leaks the home directory"


def test_seed_demo_and_dashboard_pages(demo_repo: Path) -> None:
    settings = DashboardSettings(root=demo_repo, allowed_hosts=frozenset({"testserver"}))
    dash_app = create_app(settings, executor=_fake_executor)
    sandbox = demo_repo.parent
    with TestClient(dash_app) as client:
        for page in PAGES:
            _check(client, page, sandbox)
        with Store.open(demo_repo / ".jev" / "state.db") as store:
            runs = store.list_runs()
        assert runs, "the seed wrote no runs"
        run_id = next(r.id for r in runs if r.id)
        _check(client, f"/runs/{run_id}", sandbox)
        _check(client, f"/runs/{run_id}/live", sandbox)
        chats = list_chats(demo_repo)
        assert chats, "the seed saved no chats"
        _check(client, f"/chats/{chats[0].chat_id}", sandbox)
        _check(client, f"/chats/{chats[0].chat_id}/live", sandbox)
        with Store.open(demo_repo / ".jev" / "state.db") as store:
            running = [
                a
                for r in store.list_runs()
                if r.state not in STOPPED
                for a in store.list_attempts(r.id)
                if a.result is None
            ]
        assert running, "the seed left no running attempt: /fleet renders its empty state"
