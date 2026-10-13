"""`jev chat`: scripted sessions with a fake chat backend and fake workers (no model, no CLI)."""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jev_agent.cli import chat_cmds
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config.schema import JevConfig
from jev_agent.core.chat import (
    BRIEF_CLOSE,
    BRIEF_OPEN,
    BRIEF_REQUEST,
    ChatError,
    Reply,
    Turn,
    hold_chat,
)
from jev_agent.core.learning import Learning
from jev_agent.core.store import Store

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n'


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
    # These tests model the user's own repository: trust it, so its jev.toml may set the
    # guarded keys (servers, tools, permissions; docs/security.md).
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    with trust.open("a") as fh:  # keep the suite's user config (tests/conftest.py)
        fh.write('[trust]\nrepos = ["*"]\n')
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setattr(chat_cmds, "is_interactive", lambda: False)


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
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


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _meta(repo: Path) -> dict[str, Any]:
    (path,) = (repo / ".jev" / "chats").iterdir()
    data: dict[str, Any] = json.loads((path / "meta.json").read_text())
    return data


def _profile_with_lesson(repo: Path, name: str, text: str) -> None:
    with _store(repo) as store:
        learning = Learning.for_store(store)
        try:
            learning.create(name)
            learning.db_for(name).add_lesson(name, text, ["planner"], [])
        finally:
            learning.close()


def test_chat_brief_plan_with_learning_profile(repo: Path, backend: ScriptedBackend) -> None:
    _profile_with_lesson(repo, "work", "Keep diffs small.")
    code, out = _chat(repo, "--learning", "work", input="add a search box\n/brief\n/plan\n")
    assert code == 0, out
    assert "learning: profile work; 1 lesson(s) from work in the model's instructions" in out
    assert "noted 1" in out and "Add search" in out
    assert "AWAITING_APPROVAL" in out and "jev status" in out and "Chat saved" in out
    system, _, _ = backend.calls[0]
    assert "- Keep diffs small." in system and "product and tech lead" in system

    with _store(repo) as store:
        (run,) = store.list_runs()
    assert run.learning_profile == "work"
    assert run.idea.text == "# Add search\nGoal: search."
    meta = _meta(repo)
    assert meta["runs"] == [{"run_id": run.id, "kind": "plan", "at": meta["runs"][0]["at"]}]
    assert meta["brief"] == run.idea.text and meta["learning"] == {"profile": "work", "off": False}


def test_plan_writes_a_brief_first_and_refreshes_a_stale_one(
    repo: Path, backend: ScriptedBackend
) -> None:
    code, out = _chat(repo, input="idea\n/plan\nmore detail\n/run\n")
    assert code == 0, out
    assert "No brief yet" in out and "moved on since the last brief" in out
    assert sum(1 for _, _, m in backend.calls if m == BRIEF_REQUEST) == 2
    with _store(repo) as store:
        runs = store.list_runs()
    assert len(runs) == 2


def test_queue_adds_item_with_priority(repo: Path, backend: ScriptedBackend) -> None:
    _profile_with_lesson(repo, "work", "x")
    code, out = _chat(
        repo, "--learning", "work", input="idea\n/queue urgent\n/queue low\n/status\n"
    )
    assert code == 0, out
    assert "usage: /queue [high|normal|low]" in out and "queued item 1 (low)" in out
    assert "queue item 1 (low)" in out
    with _store(repo) as store:
        (item,) = store.list_queue()
        assert store.list_runs() == []
    assert item.priority == "low" and item.learning_profile == "work" and not item.no_learning
    assert item.idea == "# Add search\nGoal: search."
    assert _meta(repo)["queue_items"][0]["id"] == 1


def test_learning_toggle_affects_next_launch(repo: Path, backend: ScriptedBackend) -> None:
    _profile_with_lesson(repo, "work", "Keep diffs small.")
    script = "idea\n/learning\n/learning off\n/plan\n/learning work\n/plan\n/learning nope\n"
    code, out = _chat(repo, "--learning", "work", input=script)
    assert code == 0, out
    assert "change with /learning" in out and "learning: off" in out
    assert "no learning profile named 'nope'" in out
    with _store(repo) as store:
        runs = sorted(store.list_runs(), key=lambda r: r.created_at)
    assert [r.learning_profile for r in runs] == [None, "work"]
    assert _meta(repo)["learning"] == {"profile": "work", "off": False}


def test_no_learning_launches_without_profile(repo: Path, backend: ScriptedBackend) -> None:
    _profile_with_lesson(repo, "default", "Lesson.")
    assert runner.invoke(app, ["learning", "enable", "--repo", str(repo)]).exit_code == 0
    code, out = _chat(repo, "--no-learning", input="idea\n/plan\n")
    assert code == 0, out
    assert "learning: off" in out and "Lesson." not in backend.calls[0][0]
    with _store(repo) as store:
        (run,) = store.list_runs()
    assert run.learning_profile is None

    # Without flags (and no TTY) the repo default applies: learning is on, profile default.
    code, out = _chat(repo, input="idea\n/plan\n")
    assert code == 0 and "repo default (now default)" in out
    with _store(repo) as store:
        assert {r.learning_profile for r in store.list_runs()} == {None, "default"}


def test_transcript_redacted(
    repo: Path, backend: ScriptedBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    code, out = _chat(repo, input="my token is supersecretvalue123 and sk-abcdefgh12345\n")
    assert code == 0, out
    (path,) = (repo / ".jev" / "chats").iterdir()
    text = (path / "transcript.jsonl").read_text()
    assert "supersecretvalue123" not in text and "sk-abcdefgh12345" not in text
    assert "[REDACTED]" in text
    kinds = [json.loads(line)["kind"] for line in text.splitlines()]
    assert kinds == ["user", "assistant"]


def test_resume_restores_state_and_list(repo: Path, backend: ScriptedBackend) -> None:
    code, out = _chat(repo, "--no-learning", "--budget-usd", "2", input="first idea\n/brief\n")
    assert code == 0, out
    chat_id = _meta(repo)["chat_id"]

    code, out = _chat(repo, "--list")
    assert code == 0 and chat_id in out and "Add search" in out and "off" in out

    backend.calls.clear()
    code, out = _chat(repo, "--resume", chat_id, input="second\n/status\n")
    assert code == 0, out
    assert f"chat {chat_id} resumed" in out and "2 earlier exchange(s) restored" in out
    assert "learning: off" in out and "budget $2.00" in out
    _, history, message = backend.calls[0]
    assert message == "second" and history[0] == Turn("user", "first idea")
    meta = _meta(repo)
    assert meta["turns"] == 3 and meta["learning"]["off"] is True

    with hold_chat(repo, chat_id):  # the dashboard is answering it
        code, out = _chat(repo, "--resume", chat_id, input="third\n")
    assert code == 1 and "is open elsewhere" in out
    assert _meta(repo)["turns"] == 3

    code, out = _chat(repo, "--resume", "c-20200101-000000-abcd")
    assert code == 1 and "no chat" in out
    code, out = _chat(repo, "--resume", "nonsense")
    assert code == 1 and "not a chat id" in out


def test_budget_stops_model_calls(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ScriptedBackend(cost=0.6)
    monkeypatch.setattr(chat_cmds, "make_backend", lambda ctx, name, model: fake)
    code, out = _chat(repo, "--budget-usd", "1", input="a\nb\nc\n/cost\n")
    assert code == 0, out
    assert "noted 2" in out and "noted 3" not in out
    assert "budget: this chat has spent $1.20 of its $1.00 budget" in out
    assert "spent $1.2000 of this chat's $1.00 budget" in out
    assert len(fake.calls) == 2


def test_unknown_profile_refused_at_start(repo: Path, backend: ScriptedBackend) -> None:
    code, out = _chat(repo, "--profile", "box", input="idea\n")
    assert code == 1
    assert "no workspace profile named 'box'" in out


def test_profile_passed_to_runs(repo: Path, backend: ScriptedBackend) -> None:
    (repo / "jev.toml").write_text(
        CONFIG + '[servers.web]\nhost = "web1"\n[profiles.box]\nservers = ["web"]\n'
    )
    code, out = _chat(repo, "--profile", "box", input="idea\n/plan\n/queue\n")
    assert code == 0, out
    assert "workspace profile: box" in out
    with _store(repo) as store:
        (run,) = store.list_runs()
        (item,) = store.list_queue()
    saved = json.loads((repo / ".jev" / "runs" / run.id / "profile.json").read_text())
    assert saved["name"] == "box" and saved["servers"] == ["web"]
    assert item.profile == "box"
    assert _meta(repo)["profile"] == "box"


def test_start_screen(
    repo: Path, backend: ScriptedBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_cmds, "is_interactive", lambda: True)
    _profile_with_lesson(repo, "other", "Other lesson.")
    code, out = _chat(repo, input="3\n")
    assert code == 0, out
    assert "1) With learning: default (0 lessons)" in out and "2) Other learning profile" in out
    assert _meta(repo)["learning"] == {"profile": None, "off": True}

    for path in (repo / ".jev" / "chats").iterdir():
        for f in path.iterdir():
            f.unlink()
        path.rmdir()
    code, out = _chat(repo, input="2\n1\nhi\n")
    assert code == 0, out
    assert "1) other (repo, 1 lessons)" in out and "1 lesson(s) from other" in out
    assert "Other lesson." in backend.calls[-1][0]
    assert _meta(repo)["learning"] == {"profile": "other", "off": False}

    # A flag skips the start screen.
    code, out = _chat(repo, "--no-learning")
    assert code == 0 and "Choose" not in out


def test_flag_errors(repo: Path, backend: ScriptedBackend) -> None:
    code, out = _chat(repo, "--learning", "a", "--no-learning")
    assert code == 1 and "cannot be used together" in out
    code, out = _chat(repo, "--learning", "nope")
    assert code == 1 and "jev learning create nope" in out
    code, out = _chat(repo, "--profile", " ")
    assert code == 1 and "--profile needs a name" in out


def test_unknown_command_and_help(repo: Path, backend: ScriptedBackend) -> None:
    code, out = _chat(repo, input="/nope\n/help\n//slash text\n/quit\nnot sent\n")
    assert code == 0, out
    assert "unknown command /nope" in out and "/queue" in out
    assert [m for _, _, m in backend.calls] == ["/slash text"]


def test_echo_backend_under_fake_workers(repo: Path) -> None:
    code, out = _chat(repo, input="hello\n/brief\n")
    assert code == 0, out
    assert "(fake model) You said: hello" in out and "Fake brief" in out


# ---- backend and model choice ------------------------------------------------------------


def test_pick_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FAKE_WORKERS_ENV)
    config = JevConfig()
    assert chat_cmds.pick_backend(config, None) == "claude_code"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k" * 10)
    assert chat_cmds.pick_backend(config, None) == "api"
    assert chat_cmds.pick_backend(config, None, stored="claude_code") == "claude_code"
    assert chat_cmds.pick_backend(config, "claude_code") == "claude_code"
    with pytest.raises(ChatError, match="--worker must be"):
        chat_cmds.pick_backend(config, "codex")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ChatError, match="OPENROUTER_API_KEY"):
        chat_cmds.pick_backend(config, "api")
    assert chat_cmds.pick_backend(config, None, stored="api") == "claude_code"
    off = JevConfig.model_validate({"workers": {"claude_code": {"enabled": False}}})
    with pytest.raises(ChatError, match="disabled"):
        chat_cmds.pick_backend(off, "claude_code")
    with pytest.raises(ChatError, match="no chat model"):
        chat_cmds.pick_backend(off, None)


def test_pick_model() -> None:
    config = JevConfig.model_validate({"roles": {"planner": {"models": ["opus", "vendor/big"]}}})
    assert chat_cmds.pick_model(config, "api", "flag/model") == "flag/model"
    assert chat_cmds.pick_model(config, "api", None, stored="old/one") == "old/one"
    assert chat_cmds.pick_model(config, "api", None) == "vendor/big"
    assert chat_cmds.pick_model(config, "claude_code", None) == "opus"
    with_default = JevConfig.model_validate({"workers": {"api": {"default_model": "d/m"}}})
    assert chat_cmds.pick_model(with_default, "api", None) == "d/m"
    assert chat_cmds.pick_model(JevConfig(), "claude_code", None) is None


def test_quality_command_sets_the_level_of_launched_runs(
    repo: Path, backend: ScriptedBackend
) -> None:
    script = "idea\n/quality\n/quality ultra\n/quality production\n/plan\n/queue\n"
    script += "/quality default\n/status\n/plan\n"
    code, out = _chat(repo, input=script)
    assert code == 0, out
    assert "quality: none (from jev.toml)" in out and "change with /quality" in out
    assert "unknown quality level 'ultra'" in out and "quality: production" in out
    with _store(repo) as store:
        runs = sorted(store.list_runs(), key=lambda r: r.created_at)
        (item,) = store.list_queue()
    assert [r.quality_level for r in runs] == ["production", "none"]
    assert item.quality == "production"
    assert _meta(repo)["quality"] is None
