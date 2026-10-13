"""`core/chat.py`: learning choice, command parsing, briefs, the system prompt, both backends
(with a fake runner and a mock HTTP transport) and the session's persistence and budget."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jev_agent.config.schema import JevConfig
from jev_agent.core.chat import (
    BRIEF_REQUEST,
    ApiChatBackend,
    BudgetReached,
    ChatError,
    ChatLearning,
    ChatSession,
    ClaudeChatBackend,
    Command,
    EchoChatBackend,
    Message,
    Reply,
    Turn,
    brief_title,
    chat_dir,
    choose_learning,
    effective_profile,
    extract_brief,
    learning_options,
    list_chats,
    parse_input,
    parse_learning_arg,
    parse_priority,
    render_system_prompt,
    repo_tree,
)
from jev_agent.core.learning import Learning, LearningError, Profile, set_repo_learning
from jev_agent.core.store import Store
from jev_agent.workers.subprocess_runner import CommandResult

# ---- learning choice ---------------------------------------------------------------------


def _profile(name: str) -> Profile:
    return Profile(name, "repo", True, 0, datetime.now(UTC))


def test_learning_options() -> None:
    options = learning_options("default", 1, [])
    assert [(o.key, o.label) for o in options] == [
        ("1", "With learning: default (1 lesson)"),
        ("3", "Without learning"),
    ]
    assert options[0].choice == ChatLearning(profile="default")
    assert options[1].choice == ChatLearning(off=True)
    options = learning_options("work", 3, [_profile("other")])
    assert [o.key for o in options] == ["1", "2", "3"]
    assert options[0].label == "With learning: work (3 lessons)"
    assert options[1].choice is None and "Other" in options[1].label


def test_choose_learning_precedence() -> None:
    stored, picked = ChatLearning(profile="s"), ChatLearning(profile="p")
    assert choose_learning("f", False, stored, picked) == ChatLearning(profile="f")
    assert choose_learning(None, True, stored, picked) == ChatLearning(off=True)
    assert choose_learning(None, False, stored, picked) == stored
    assert choose_learning(None, False, None, picked) == picked
    assert choose_learning(None, False).is_default
    with pytest.raises(LearningError, match="together"):
        choose_learning("f", True)
    assert ChatLearning().run_flags() == {"learning": None, "no_learning": False}
    assert ChatLearning(off=True).run_flags() == {"learning": None, "no_learning": True}
    assert ChatLearning.from_json(ChatLearning(profile="x").to_json()) == ChatLearning("x")
    assert ChatLearning.from_json("junk") == ChatLearning()


def test_effective_profile_never_creates(tmp_path: Path) -> None:
    config = JevConfig()
    with Store.open(tmp_path / "state.db") as store:
        learning = Learning.for_store(store, tmp_path / "shared.db")
        try:
            assert effective_profile(ChatLearning(), store, learning, config) is None
            set_repo_learning(store, True)
            # On, but the active profile does not exist yet: no lessons, and nothing created.
            assert effective_profile(ChatLearning(), store, learning, config) is None
            assert learning.get("default") is None
            learning.create("default")
            assert effective_profile(ChatLearning(), store, learning, config) == "default"
            assert effective_profile(ChatLearning(off=True), store, learning, config) is None
            assert effective_profile(ChatLearning("nope"), store, learning, config) is None
        finally:
            learning.close()


# ---- commands and briefs -----------------------------------------------------------------


def test_parse_input() -> None:
    assert parse_input("   ") is None
    assert parse_input(" hello ") == Message("hello")
    assert parse_input("//etc is a path") == Message("/etc is a path")
    assert parse_input("/brief") == Command("brief", (), "/brief")
    assert parse_input("/QUEUE high") == Command("queue", ("high",), "/QUEUE high")
    assert parse_input("/exit") == Command("quit", (), "/exit")
    assert parse_input("/?") == Command("help", (), "/?")
    assert parse_input("/nope x") == Command("", ("x",), "/nope x")
    assert parse_input("/") == Command("", (), "/")


def test_parse_priority_and_learning_arg() -> None:
    assert parse_priority(()) == "normal"
    assert parse_priority(("HIGH",)) == "high"
    for bad in (("urgent",), ("high", "low")):
        with pytest.raises(ChatError, match="usage"):
            parse_priority(bad)
    assert parse_learning_arg("off", "a") == ChatLearning(off=True)
    assert parse_learning_arg("default", "a") == ChatLearning()
    assert parse_learning_arg("on", "act") == ChatLearning(profile="act")
    assert parse_learning_arg("work", "a") == ChatLearning(profile="work")
    with pytest.raises(LearningError, match="not a valid profile name"):
        parse_learning_arg("bad name!", "a")


def test_extract_brief_and_title() -> None:
    assert extract_brief("Sure.\n<brief>\n# Title\nbody\n</brief>\nDone") == "# Title\nbody"
    assert extract_brief("<brief>\nunterminated") == "unterminated"
    assert extract_brief("  no markers  ") == "no markers"
    assert brief_title("\n# Add search\n\nmore") == "Add search"
    assert brief_title(None) == ""


# ---- system prompt -----------------------------------------------------------------------


def test_system_prompt_has_repo_summary_and_lessons(fixture_repo: Path) -> None:
    (fixture_repo / "src").mkdir()
    (fixture_repo / "src" / "app.py").write_text("")
    tree = repo_tree(fixture_repo)
    assert "src/\n  app.py" in tree and "tests/" in tree and ".git" not in tree
    assert repo_tree(fixture_repo, max_entries=2).endswith("... (truncated)")

    prompt = render_system_prompt(fixture_repo, [], None)
    assert "product and tech lead" in prompt and "fixture" in prompt  # README head
    assert "<brief>" in prompt and "Lessons" not in prompt
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        learning = Learning.for_store(store)
        try:
            learning.create("work")
            lesson = learning.db_for("work").add_lesson("work", "Keep diffs small.", [], [])
        finally:
            learning.close()
    prompt = render_system_prompt(fixture_repo, [lesson], "work")
    assert "learning profile work" in prompt and "- Keep diffs small." in prompt


# ---- Claude Code backend -----------------------------------------------------------------


class FakeRunner:
    def __init__(self, outputs: list[dict[str, object] | str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], str | None]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin_text: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), stdin_text))
        out = self.outputs.pop(0)
        if isinstance(out, str):
            return CommandResult(exit_code=1, stdout="", stderr=out)
        return CommandResult(exit_code=0, stdout=json.dumps(out), stderr="")


def _result(text: str, session: str = "sess-1", cost: float = 0.02) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session,
        "total_cost_usd": cost,
    }


def test_claude_backend_argv_and_resume(tmp_path: Path) -> None:
    runner = FakeRunner([_result("Hi"), _result("Again"), "No conversation found", _result("New")])
    backend = ClaudeChatBackend(tmp_path, "claude", "sonnet", runner=runner, env={})
    log = tmp_path / "log"
    reply = asyncio.run(
        backend.send("SYSTEM", [], "hello", session_id=None, budget_left_usd=0.5, log_path=log)
    )
    assert reply == Reply(True, "Hi", 0.02, "sess-1")
    argv, stdin = runner.calls[0]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--resume" not in argv and "--no-session-persistence" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--allowedTools") + 1] == "Read Glob Grep"
    assert "Edit" in argv[argv.index("--disallowedTools") + 1]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.50"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert stdin is not None and stdin.startswith("SYSTEM") and stdin.endswith("hello")

    reply = asyncio.run(
        backend.send("SYSTEM", [], "more", session_id="sess-1", budget_left_usd=1, log_path=log)
    )
    argv, stdin = runner.calls[1]
    assert argv[argv.index("--resume") + 1] == "sess-1" and stdin == "more"

    # A lost session starts a new one with the earlier turns.
    history = [Turn("user", "hello"), Turn("assistant", "Hi")]
    reply = asyncio.run(
        backend.send("SYSTEM", history, "x", session_id="gone", budget_left_usd=1, log_path=log)
    )
    assert reply.ok and reply.text == "New"
    argv, stdin = runner.calls[3]
    assert "--resume" not in argv
    assert stdin is not None and "User: hello\n\nAssistant: Hi" in stdin


def test_claude_backend_failure_is_a_reply(tmp_path: Path) -> None:
    runner = FakeRunner(["boom"])
    backend = ClaudeChatBackend(tmp_path, runner=runner, env={})
    reply = asyncio.run(
        backend.send("S", [], "m", session_id="s", budget_left_usd=0, log_path=tmp_path / "l")
    )
    assert not reply.ok and "boom" in reply.text and len(runner.calls) == 1
    assert "--max-budget-usd" not in runner.calls[0][0]


# ---- API backend -------------------------------------------------------------------------


def test_api_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-or-v1-testkey123456"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer"}}], "usage": {"cost": 0.003}}
        )

    backend = ApiChatBackend(
        "https://example.test/api/v1/", "MY_CHAT_KEY", "vendor/model",
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    log = tmp_path / "backend.log"
    monkeypatch.delenv("MY_CHAT_KEY", raising=False)
    reply = asyncio.run(
        backend.send("S", [], "m", session_id=None, budget_left_usd=1, log_path=log)
    )
    assert not reply.ok and "MY_CHAT_KEY is not set" in reply.text

    monkeypatch.setenv("MY_CHAT_KEY", "sk-or-v1-testkey123456")
    history = [Turn("user", "u1"), Turn("assistant", "a1")]
    reply = asyncio.run(
        backend.send("SYS", history, "u2", session_id=None, budget_left_usd=1, log_path=log)
    )
    assert reply == Reply(True, "Answer", 0.003)
    assert seen[0]["model"] == "vendor/model"
    assert seen[0]["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    assert "sk-or-v1-testkey" not in log.read_text()


def test_api_backend_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_CHAT_KEY", "k" * 12)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(402, json={"error": {"message": "no credits"}})
    )
    backend = ApiChatBackend("https://x.test", "MY_CHAT_KEY", "m", transport=transport)
    reply = asyncio.run(
        backend.send("S", [], "m", session_id=None, budget_left_usd=1, log_path=tmp_path / "l")
    )
    assert reply == Reply(False, "HTTP 402: no credits")


# ---- session -----------------------------------------------------------------------------


class CostlyBackend(EchoChatBackend):
    def __init__(self, cost: float) -> None:
        super().__init__("api", "m")
        self.cost = cost
        self.calls = 0

    async def send(self, *args: object, **kwargs: object) -> Reply:
        self.calls += 1
        return Reply(True, f"reply {self.calls}", self.cost)


def test_session_persistence_budget_and_brief(tmp_path: Path) -> None:
    backend = CostlyBackend(0.4)
    session = ChatSession.create(tmp_path, backend, "S", ChatLearning("w"), "box", 1.0)
    assert chat_dir(tmp_path, session.meta.chat_id).is_dir()
    asyncio.run(session.say("one"))
    asyncio.run(session.say("two"))
    assert asyncio.run(session.say("three")).ok  # spent 0.8 < 1.0: allowed, now 1.2
    with pytest.raises(BudgetReached, match=r"\$1.20 of its \$1.00"):
        asyncio.run(session.say("four"))
    assert backend.calls == 3

    meta = ChatSession.load_meta(tmp_path, session.meta.chat_id)
    assert meta.spent_usd == pytest.approx(1.2) and meta.turns == 3
    assert meta.learning == ChatLearning("w") and meta.profile == "box"
    resumed = ChatSession.resume(tmp_path, meta, EchoChatBackend("api"), "S")
    assert [t.text for t in resumed.history][:2] == ["one", "reply 1"]
    assert [m.chat_id for m in list_chats(tmp_path)] == [meta.chat_id]

    resumed.meta.budget_usd = 5
    reply = asyncio.run(resumed.make_brief())
    assert reply.ok and resumed.meta.brief and resumed.brief_is_current
    assert resumed.history[-2].text == BRIEF_REQUEST
    asyncio.run(resumed.say("more"))
    assert not resumed.brief_is_current

    with pytest.raises(ChatError, match="not a chat id"):
        chat_dir(tmp_path, "../x")
    with pytest.raises(ChatError, match="no chat"):
        ChatSession.load_meta(tmp_path, "c-20200101-000000-abcd")


def test_session_note_after_learning_change(tmp_path: Path) -> None:
    sent: list[str] = []

    class SessionBackend(EchoChatBackend):
        keeps_session = True

        async def send(self, system: str, history: object, message: str, **kw: object) -> Reply:
            sent.append(message)
            return Reply(True, "ok", None, "sid")

    session = ChatSession.create(
        tmp_path, SessionBackend("claude_code"), "S", ChatLearning(), None, 1
    )
    session.set_system_prompt("S2", "ignored: no session yet")
    asyncio.run(session.say("a"))
    session.set_system_prompt("S3", "lessons changed")
    asyncio.run(session.say("b"))
    asyncio.run(session.say("c"))
    assert sent == ["a", "[Note from jev: lessons changed]\n\nb", "c"]
    assert session.meta.unknown_cost_calls == 3 and session.meta.session_id == "sid"
