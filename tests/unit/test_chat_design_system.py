"""WS-D: the design-system picker for the chat module (CLI, dashboard, run injection).

`jev_agent.core.design_library` is WS-A's parallel workstream; these tests stub it
minimally in `sys.modules`, exactly as the dependency contract allows.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.cli import chat_cmds
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.chat import (
    BRIEF_CLOSE,
    BRIEF_OPEN,
    BRIEF_REQUEST,
    ChatError,
    ChatLearning,
    ChatMeta,
    ChatSession,
    Reply,
    Turn,
    describe_design_system,
    parse_design_system,
    resolve_design_system,
    with_design_system,
)
from jev_agent.core.design_system import (
    DesignSystem,
    get_design_system,
    render_for_prompt,
    save_design_system,
)
from jev_agent.core.models import Idea
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n'

LIBRARY_MARKDOWN = "# Acme design system\n\nButtons are chunky."
LIBRARY_TOKENS: dict[str, object] = {"colors": {"primary": "#ACME00"}}


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """WS-A's `core.design_library`, stubbed minimally per the dependency contract."""
    systems = {
        "acme": DesignSystem(
            version=3,
            updated_at="2026-10-16T00:00:00",
            markdown=LIBRARY_MARKDOWN,
            tokens=LIBRARY_TOKENS,
        ),
    }
    stub = SimpleNamespace(
        list_library=lambda: ["acme"],
        load_library_system=lambda name: systems[name],  # KeyError for unknown names
    )
    monkeypatch.setitem(sys.modules, "jev_agent.core.design_library", stub)
    return stub


@pytest.fixture
def repo_system(fixture_repo: Path) -> DesignSystem:
    """A repo design system on disk, for the "repo" choice."""
    save_design_system(
        fixture_repo,
        "# Repo design system\n\nButtons are flat.",
        {"colors": {"primary": "#0000FF"}},
    )
    system = get_design_system(fixture_repo)
    assert system is not None
    return system


def _meta_dict(
    *,
    chat_id: str = "c-20261016-120000-ab12",
    design_system: str | None = "library:acme",
    learning: ChatLearning | None = None,
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "created_at": "2026-10-16T12:00:00+00:00",
        "updated_at": "2026-10-16T12:00:00+00:00",
        "learning": (learning or ChatLearning()).to_json(),
        "profile": None,
        "backend": "api",
        "model": "m",
        "design_system": design_system,
    }


# ---- ChatMeta persistence ---------------------------------------------------------------


def test_chat_meta_round_trip_with_design_system() -> None:
    meta = ChatMeta.from_json(_meta_dict(design_system="library:acme"))
    assert meta.design_system == "library:acme"
    assert ChatMeta.from_json(meta.to_json()).design_system == "library:acme"

    meta = ChatMeta.from_json(_meta_dict(design_system="repo"))
    assert ChatMeta.from_json(meta.to_json()).design_system == "repo"


def test_chat_meta_old_json_without_design_system_loads() -> None:
    """Chats saved before WS-D have no `design_system` key: they still load, with None."""
    data = _meta_dict()
    del data["design_system"]
    meta = ChatMeta.from_json(data)
    assert meta.design_system is None
    # ...and saving writes the key back, so the file converges to the new shape.
    assert ChatMeta.from_json(meta.to_json()).design_system is None
    assert "design_system" in meta.to_json()


# ---- choice parsing, resolution and labels -----------------------------------------------


def test_parse_design_system(library: SimpleNamespace) -> None:
    assert parse_design_system(None) is None
    assert parse_design_system("") is None
    assert parse_design_system("none") == "none"
    assert parse_design_system("repo") == "repo"
    assert parse_design_system("library:acme") == "library:acme"
    with pytest.raises(ChatError):
        parse_design_system("library:nope")
    with pytest.raises(ChatError):
        parse_design_system("library:")
    with pytest.raises(ChatError):
        parse_design_system("bogus")


def test_parse_design_system_without_library_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before WS-A lands, picking a library system fails loudly; the rest keeps working."""
    import jev_agent.core.chat_session as chat_session

    monkeypatch.setattr(chat_session, "_design_library", lambda: None)
    assert parse_design_system("repo") == "repo"
    assert parse_design_system("none") == "none"
    with pytest.raises(ChatError, match="not available"):
        parse_design_system("library:acme")


def test_describe_design_system() -> None:
    assert describe_design_system(None) == "None"
    assert describe_design_system("none") == "None"
    assert describe_design_system("repo") == "Repo design system"
    assert describe_design_system("library:acme") == "Library: acme"


def test_resolve_design_system(
    fixture_repo: Path, repo_system: DesignSystem, library: SimpleNamespace
) -> None:
    assert resolve_design_system(fixture_repo, None) == repo_system
    assert resolve_design_system(fixture_repo, "repo") == repo_system
    resolved = resolve_design_system(fixture_repo, "library:acme")
    assert resolved is not None and resolved.markdown == LIBRARY_MARKDOWN
    assert resolve_design_system(fixture_repo, "bogus") is None
    # "none" means explicitly no design system: nothing resolves, whatever the repo has.
    assert resolve_design_system(fixture_repo, "none") is None


def test_resolve_design_system_falls_back_when_library_entry_vanished(
    fixture_repo: Path, repo_system: DesignSystem, library: SimpleNamespace
) -> None:
    """A choice stored when the library had the system still resolves after it is gone."""
    with pytest.warns(UserWarning):
        resolved = resolve_design_system(fixture_repo, "library:gone")
    assert resolved == repo_system


def test_with_design_system_appends_rendered_text(
    fixture_repo: Path, repo_system: DesignSystem, library: SimpleNamespace
) -> None:
    prompt = with_design_system(fixture_repo, "base prompt", "library:acme")
    assert prompt.startswith("base prompt\n\n")
    assert "Acme design system" in prompt
    assert with_design_system(fixture_repo, "base prompt", None).startswith("base prompt")
    # No repo system and no choice: the prompt is untouched.
    assert with_design_system(Path("/nonexistent"), "base prompt", None) == "base prompt"


# ---- CLI -------------------------------------------------------------------------------


class ScriptedBackend:
    """Answers plain messages with "noted N" and the brief request with a fixed brief."""

    keeps_session = False

    def __init__(self, cost: float = 0.01, name: Any = "api", model: str | None = "m") -> None:
        self.name = name
        self.model = model
        self.cost = cost
        self.calls: list[tuple[str, list[Turn], str]] = []

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        self.calls.append((system, list(history), message))
        if message.endswith(BRIEF_REQUEST):
            return Reply(True, f"Here:\n{BRIEF_OPEN}\n# Add search\nGoal: search.\n{BRIEF_CLOSE}")
        return Reply(True, f"noted {len(self.calls)}", self.cost)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    with trust.open("a") as fh:
        fh.write('[trust]\nrepos = ["*"]\n')
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setattr(chat_cmds, "is_interactive", lambda: False)


@pytest.fixture
def cli_repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> ScriptedBackend:
    fake = ScriptedBackend()
    monkeypatch.setattr(chat_cmds, "make_backend", lambda ctx, name, model: fake)
    return fake


def _chat(repo: Path, *args: str, input: str = "") -> tuple[int, str]:
    result = runner.invoke(app, ["chat", *args, "--repo", str(repo)], input=input)
    return result.exit_code, " ".join(re.sub(r"[─-╿]", " ", result.output).split())


def _saved_meta(repo: Path) -> dict[str, Any]:
    (path,) = (repo / ".jev" / "chats").iterdir()
    return json.loads((path / "meta.json").read_text())  # type: ignore[no-any-return]


def test_chat_flag_persists_and_injects(
    cli_repo: Path, backend: ScriptedBackend, library: SimpleNamespace
) -> None:
    code, out = _chat(cli_repo, "--design-system", "library:acme", input="hi\n")
    assert code == 0, out
    assert _saved_meta(cli_repo)["design_system"] == "library:acme"
    assert "design system: Library: acme" in out
    assert "Acme design system" in backend.calls[0][0]


def test_chat_flag_invalid_library_fails(
    cli_repo: Path, backend: ScriptedBackend, library: SimpleNamespace
) -> None:
    code, out = _chat(cli_repo, "--design-system", "library:nope")
    assert code == 1 and "unknown design library system 'nope'" in out


def test_chat_flag_bogus_value_fails(cli_repo: Path, backend: ScriptedBackend) -> None:
    code, out = _chat(cli_repo, "--design-system", "bogus")
    assert code == 1 and "unknown design system 'bogus'" in out


def test_chat_flag_none_and_repo(
    cli_repo: Path, backend: ScriptedBackend, library: SimpleNamespace, repo_system: DesignSystem
) -> None:
    code, _ = _chat(cli_repo, "--design-system", "none", input="hi\n")
    assert code == 0
    assert _saved_meta(cli_repo)["design_system"] == "none"
    assert "Acme design system" not in backend.calls[0][0]
    assert "Repo design system" not in backend.calls[0][0]

    for path in (cli_repo / ".jev" / "chats").iterdir():
        for f in path.iterdir():
            f.unlink()
        path.rmdir()
    code, out = _chat(cli_repo, "--design-system", "repo", input="hi\n")
    assert code == 0, out
    assert _saved_meta(cli_repo)["design_system"] == "repo"
    assert "design system: Repo design system" in out
    assert "Repo design system" in backend.calls[-1][0]
    assert "Acme design system" not in backend.calls[-1][0]


def test_design_screen_on_tty(
    cli_repo: Path,
    backend: ScriptedBackend,
    library: SimpleNamespace,
    repo_system: DesignSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive picker lists None, the repo system and the library systems."""
    monkeypatch.setattr(chat_cmds, "is_interactive", lambda: True)
    # learning "1" (default), design "2" (repo), then quit.
    code, out = _chat(cli_repo, input="1\n2\n/quit\n")
    assert code == 0, out
    assert "Design system for this chat and the runs it starts:" in out
    assert "1) None" in out and "2) Repo design system" in out and "3) Library: acme" in out
    assert _saved_meta(cli_repo)["design_system"] == "repo"


def test_design_screen_without_repo_system(
    cli_repo: Path,
    backend: ScriptedBackend,
    library: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repo design system: the picker offers None and the library only."""
    monkeypatch.setattr(chat_cmds, "is_interactive", lambda: True)
    code, out = _chat(cli_repo, input="1\n2\n/quit\n")
    assert code == 0, out
    assert "Repo design system" not in out
    assert _saved_meta(cli_repo)["design_system"] == "library:acme"


def test_resume_keeps_choice_and_flag_overrides(
    cli_repo: Path,
    backend: ScriptedBackend,
    library: SimpleNamespace,
    repo_system: DesignSystem,
) -> None:
    code, _ = _chat(cli_repo, "--design-system", "library:acme", input="hi\n")
    assert code == 0
    (chat_dir,) = (cli_repo / ".jev" / "chats").iterdir()
    chat_id = chat_dir.name

    code, _ = _chat(cli_repo, "--resume", chat_id, input="again\n")
    assert code == 0
    assert _saved_meta(cli_repo)["design_system"] == "library:acme"
    assert "Acme design system" in backend.calls[-1][0]

    code, out = _chat(cli_repo, "--resume", chat_id, "--design-system", "repo", input="x\n")
    assert code == 0, out
    assert _saved_meta(cli_repo)["design_system"] == "repo"
    assert "Repo design system" in backend.calls[-1][0]


def test_chat_run_carries_design_system(
    cli_repo: Path, backend: ScriptedBackend, library: SimpleNamespace
) -> None:
    """/plan from a chat stores the chat's choice on the run (the DESIGNING injection)."""
    code, out = _chat(
        cli_repo, "--design-system", "library:acme", input="add a search box\n/brief\n/plan\n"
    )
    assert code == 0, out
    with Store.open(cli_repo / ".jev" / "state.db") as store:
        (run,) = store.list_runs()
    assert run.design_system == "library:acme"


def test_resume_none_flag_updates_choice(
    cli_repo: Path,
    backend: ScriptedBackend,
    library: SimpleNamespace,
    repo_system: DesignSystem,
) -> None:
    """`--resume` with a new `--design-system none` stores "none": no design system is
    injected into the rebuilt prompt, and the summary shows the explicit choice."""
    code, _ = _chat(cli_repo, "--design-system", "library:acme", input="hi\n")
    assert code == 0
    (chat_path,) = (cli_repo / ".jev" / "chats").iterdir()

    before = len(backend.calls)
    code, out = _chat(cli_repo, "--resume", chat_path.name, "--design-system", "none", input="x\n")
    assert code == 0, out
    assert _saved_meta(cli_repo)["design_system"] == "none"
    assert "design system: None" in out
    system = backend.calls[before][0]
    assert "Acme design system" not in system
    assert "Repo design system" not in system


def test_resume_after_library_entry_deleted_falls_back_to_repo(
    cli_repo: Path,
    backend: ScriptedBackend,
    library: SimpleNamespace,
    repo_system: DesignSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored 'library:<name>' whose entry vanished warns and falls back to the repo
    system when the chat is resumed, instead of crashing."""
    code, _ = _chat(cli_repo, "--design-system", "library:acme", input="hi\n")
    assert code == 0
    (chat_path,) = (cli_repo / ".jev" / "chats").iterdir()

    def _gone(name: str) -> DesignSystem:
        raise KeyError(name)

    monkeypatch.setattr(library, "list_library", lambda: [])
    monkeypatch.setattr(library, "load_library_system", _gone)
    before = len(backend.calls)
    with pytest.warns(UserWarning, match="unavailable"):
        code, out = _chat(cli_repo, "--resume", chat_path.name, input="again\n")
    assert code == 0, out
    system = backend.calls[before][0]
    assert "Repo design system" in system
    assert "Acme design system" not in system


def test_pipeline_none_choice_injects_no_design_context(
    fixture_repo: Path, repo_system: DesignSystem, library: SimpleNamespace
) -> None:
    """The exact composition `Pipeline._design` uses: "none" renders to no design
    context at all, while the repo default still renders the repo's system."""
    assert render_for_prompt(resolve_design_system(fixture_repo, "none")) == ""
    assert "Repo design system" in render_for_prompt(resolve_design_system(fixture_repo, None))
    assert "Repo design system" in render_for_prompt(resolve_design_system(fixture_repo, "repo"))


def test_run_record_round_trips_none_choice(cli_repo: Path) -> None:
    """`runs.design_system` (migration 19) stores "none" like any other choice."""
    with Store.open(cli_repo / ".jev" / "state.db") as store:
        run = store.create_run(
            Idea(text="add a search box", repo_path=cli_repo), design_system="none"
        )
        assert store.get_run(run.id).design_system == "none"


# ---- dashboard ----------------------------------------------------------------------------


@pytest.fixture
def dash_client(fixture_repo: Path, library: SimpleNamespace) -> Iterator[TestClient]:
    """The dashboard app with the design-library stubbed, like the CLI tests."""

    def _executor(root: Path, run_id: str, action: RunAction) -> str:
        return "run DONE"

    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=_executor,
    )
    with TestClient(app) as client:
        yield client


def _dash_csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _dash_post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.post(
        url, data={**data, "csrf_token": _dash_csrf(client)}, follow_redirects=False
    )
    return response


def _new_chat_data(**overrides: str) -> dict[str, str]:
    data = {"worker": "api", "learning": "", "budget_usd": "1.00"}
    data.update(overrides)
    return data


def test_dashboard_new_chat_form_offers_design_systems(
    dash_client: TestClient, library: SimpleNamespace, repo_system: DesignSystem
) -> None:
    page = dash_client.get("/chats")
    assert page.status_code == 200
    assert 'name="design_system"' in page.text
    assert '<option value="none">None</option>' in page.text
    assert '<option value="repo">Repo design system</option>' in page.text
    assert '<option value="library:acme">Library: acme</option>' in page.text


def test_dashboard_new_chat_form_without_repo_system(
    dash_client: TestClient, library: SimpleNamespace
) -> None:
    """No repo design system on disk: the picker offers None and the library only."""
    page = dash_client.get("/chats")
    assert page.status_code == 200
    assert '<option value="repo">' not in page.text
    assert '<option value="library:acme">Library: acme</option>' in page.text


def test_dashboard_create_chat_persists_design_choice(
    dash_client: TestClient, fixture_repo: Path, library: SimpleNamespace
) -> None:
    """The dashboard's choice is validated, persisted on the chat, and shown read-only
    on the chat detail page."""
    response = _dash_post(dash_client, "/chats/new", _new_chat_data(design_system="library:acme"))
    assert response.status_code == 303
    chat_id = response.headers["location"].rsplit("/", 1)[1]
    assert ChatSession.load_meta(fixture_repo, chat_id).design_system == "library:acme"
    detail = dash_client.get(f"/chats/{chat_id}")
    assert detail.status_code == 200
    assert "Design system: Library: acme" in detail.text


def test_dashboard_create_chat_none_choice(
    dash_client: TestClient, fixture_repo: Path, library: SimpleNamespace
) -> None:
    response = _dash_post(dash_client, "/chats/new", _new_chat_data(design_system="none"))
    assert response.status_code == 303
    chat_id = response.headers["location"].rsplit("/", 1)[1]
    assert ChatSession.load_meta(fixture_repo, chat_id).design_system == "none"
    detail = dash_client.get(f"/chats/{chat_id}")
    assert "Design system: None" in detail.text


def test_dashboard_create_chat_rejects_forged_design_system(
    dash_client: TestClient, fixture_repo: Path, library: SimpleNamespace
) -> None:
    """A forged `design_system` value fails `parse_design_system`: the user gets an
    error and no chat is created."""
    chats_dir = fixture_repo / ".jev" / "chats"
    before = set(chats_dir.iterdir()) if chats_dir.is_dir() else set()
    response = _dash_post(dash_client, "/chats/new", _new_chat_data(design_system="library:nope"))
    assert response.status_code == 303
    assert response.headers["location"] == "/chats"
    after = set(chats_dir.iterdir()) if chats_dir.is_dir() else set()
    assert after == before
