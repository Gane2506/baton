"""`jev chat`: talk an idea through with a model, then launch a run from the brief (docs/chat.md).

This module holds everything that is not terminal I/O, so it can be tested without a TTY:

- **Learning choice** (`ChatLearning`, `learning_options`, `choose_learning`): which lessons the
  chat model sees and which learning flags a launched run gets. Three states: the repo default
  (resolved at launch, like `jev run` without flags), one named profile, or off.
- **Commands** (`parse_input`): `/brief`, `/plan`, `/run`, `/queue`, ... or plain text.
- **Backends** (`ChatBackend`): `ApiChatBackend` (OpenAI-compatible chat completions, by default
  OpenRouter) and `ClaudeChatBackend` (`claude -p --resume <session>`, read-only tools). Tests
  use fakes.
- **Persistence** (`ChatSession`): `.jev/chats/<chat_id>/transcript.jsonl` (redacted) and
  `meta.json` (learning choice, workspace profile, backend, session id, runs, cost, budget).

The chat's spend is its own: it is shown and capped by the chat budget, and counts toward no
run.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from jinja2 import Environment, StrictUndefined

from jev_agent.config import STATE_DIRNAME
from jev_agent.config.schema import JevConfig
from jev_agent.core.learning import (
    Learning,
    LearningError,
    Lesson,
    Profile,
    check_profile_name,
    context_terms,
    learning_status,
    select_lessons,
)
from jev_agent.core.store import Store
from jev_agent.runlog.redact import REDACTED, redact
from jev_agent.workers.claude_code import classify_claude_code, parse_result, usage_from
from jev_agent.workers.subprocess_runner import CommandRunner, append_log_sections, run_command

CHATS_DIRNAME = "chats"
TRANSCRIPT_FILE = "transcript.jsonl"
META_FILE = "meta.json"
BACKEND_LOG_FILE = "backend.log"
DEFAULT_BUDGET_USD = 1.00
DEFAULT_TIMEOUT_S = 600
README_HEAD_CHARS = 2000
TREE_MAX_ENTRIES = 60
_CHAT_ID = re.compile(r"^c-\d{8}-\d{6}-[0-9a-f]{4}$")

BackendName = Literal["api", "claude_code"]
BACKENDS: tuple[BackendName, ...] = ("api", "claude_code")
Priority = Literal["high", "normal", "low"]
PRIORITIES: tuple[Priority, ...] = ("high", "normal", "low")

BRIEF_OPEN = "<brief>"
BRIEF_CLOSE = "</brief>"
BRIEF_REQUEST = (
    "Write the idea brief for jev's planner now, from everything we agreed so far. Use the "
    "format from your instructions and put the whole brief between a line `<brief>` and a "
    "line `</brief>`. Where something is still open, choose the simplest reasonable option and "
    "list it under Assumptions."
)


class ChatError(Exception):
    """A chat cannot be opened, resumed or continued. The message is user-facing."""


class BudgetReached(ChatError):
    """The chat has spent its budget; no more model calls."""


def new_chat_id() -> str:
    """A short, sortable chat id: `c-` plus UTC time and 4 random hex chars."""
    return f"c-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---- learning choice ---------------------------------------------------------------------


@dataclass(frozen=True)
class ChatLearning:
    """The chat's learning choice. `profile` set: that profile; `off`: no learning; neither:
    the repo default (whatever `jev run` without flags would use)."""

    profile: str | None = None
    off: bool = False

    def __post_init__(self) -> None:
        if self.profile is not None and self.off:
            raise LearningError("--learning and --no-learning cannot be used together")

    @property
    def is_default(self) -> bool:
        return self.profile is None and not self.off

    def run_flags(self) -> dict[str, Any]:
        """`learning` / `no_learning` keyword arguments for `start_run` (and the queue)."""
        return {"learning": self.profile, "no_learning": self.off}

    def describe(self) -> str:
        if self.off:
            return "off"
        if self.profile is not None:
            return f"profile {self.profile}"
        return "repo default"

    def to_json(self) -> dict[str, Any]:
        return {"profile": self.profile, "off": self.off}

    @classmethod
    def from_json(cls, data: Any) -> ChatLearning:
        if not isinstance(data, dict):
            return cls()
        profile = data.get("profile")
        return cls(profile if isinstance(profile, str) else None, bool(data.get("off")))


@dataclass(frozen=True)
class LearningOption:
    key: str  # what the user types: "1", "2", "3"
    label: str
    choice: ChatLearning | None  # None: "pick another profile" (a second question follows)


def learning_options(
    active: str, active_lessons: int, others: Sequence[Profile]
) -> list[LearningOption]:
    """The start screen's choices. Option 2 is offered only when other profiles exist."""
    count = f"{active_lessons} lesson{'s' if active_lessons != 1 else ''}"
    options = [
        LearningOption("1", f"With learning: {active} ({count})", ChatLearning(profile=active))
    ]
    if others:
        options.append(LearningOption("2", "Other learning profile...", None))
    options.append(LearningOption("3", "Without learning", ChatLearning(off=True)))
    return options


def choose_learning(
    flag_profile: str | None,
    no_learning: bool,
    stored: ChatLearning | None = None,
    picked: ChatLearning | None = None,
) -> ChatLearning:
    """The chat's learning choice. Precedence: flags > the resumed chat's stored choice > what
    was picked on the start screen > the repo default."""
    if flag_profile is not None or no_learning:
        return ChatLearning(flag_profile, no_learning)
    if stored is not None:
        return stored
    if picked is not None:
        return picked
    return ChatLearning()


def effective_profile(
    choice: ChatLearning, store: Store, learning: Learning, config: JevConfig
) -> str | None:
    """The profile whose lessons the chat model sees, or None. Never creates a profile (unlike
    `resolve_learning`, which a launched run still goes through)."""
    if choice.off:
        return None
    if choice.profile is not None:
        return choice.profile if learning.get(choice.profile) is not None else None
    status = learning_status(store, config)
    if not status.enabled:
        return None
    profile = learning.get(status.profile)
    return profile.name if profile is not None and profile.enabled else None


def check_learning_choice(choice: ChatLearning, learning: Learning) -> None:
    """Raise `LearningError` if the choice names a profile that does not exist."""
    if choice.profile is not None and learning.get(check_profile_name(choice.profile)) is None:
        raise LearningError(
            f"no learning profile named {choice.profile!r}; "
            f"create it with `jev learning create {choice.profile}`"
        )


def chat_lessons(
    profile: str | None, learning: Learning, config: JevConfig, texts: Sequence[str] = ()
) -> list[Lesson]:
    """Planner lessons for the chat's system prompt (the same selection a run's planner gets)."""
    if profile is None:
        return []
    try:
        lessons = learning.lessons(profile)
    except LearningError:
        return []
    terms = context_terms(texts)
    settings = config.learning
    return select_lessons(lessons, "planner", terms, settings.max_lessons, settings.max_chars)


# ---- commands ----------------------------------------------------------------------------

COMMANDS: dict[str, str] = {
    "brief": "Have the model write the current idea brief, and show it.",
    "plan": "Start a plan-only run from the brief (`jev run --plan-only`).",
    "run": "Start a run from the brief; it stops at plan approval if jev.toml asks for it.",
    "queue": "Add the brief to the idea queue: /queue [high|normal|low] (default normal).",
    "learning": "Show the learning choice; change it: /learning on|off|default|PROFILE.",
    "quality": "Show the quality gate level for runs; change it: /quality LEVEL|default.",
    "status": "Show this chat: backend, learning, brief, runs launched.",
    "cost": "Show what this chat has spent, and its budget.",
    "help": "List the commands.",
    "quit": "Leave the chat (it is saved; `jev chat --resume ID` continues it).",
}
_ALIASES = {"exit": "quit", "q": "quit", "?": "help"}


@dataclass(frozen=True)
class Command:
    name: str  # a key of COMMANDS, or "" for an unknown command
    args: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class Message:
    text: str


def parse_input(line: str) -> Command | Message | None:
    """One line of input: a `/command`, a message for the model, or None (blank). A line that
    starts with `//` is a message starting with `/`."""
    text = line.strip()
    if not text:
        return None
    if text.startswith("//"):
        return Message(text[1:])
    if not text.startswith("/"):
        return Message(text)
    parts = text[1:].split()
    if not parts:
        return Command("", raw=text)
    name = _ALIASES.get(parts[0].lower(), parts[0].lower())
    return Command(name if name in COMMANDS else "", tuple(parts[1:]), text)


def parse_priority(args: Sequence[str]) -> Priority:
    """`/queue` arguments to a priority; raises `ChatError` for anything else."""
    if not args:
        return "normal"
    value = args[0].lower()
    if len(args) > 1 or value not in PRIORITIES:
        raise ChatError("usage: /queue [high|normal|low]")
    return value


def parse_learning_arg(arg: str, active: str) -> ChatLearning:
    """`/learning on|off|default|PROFILE` to a choice (`on` is the repo's active profile)."""
    value = arg.strip()
    if value.lower() == "off":
        return ChatLearning(off=True)
    if value.lower() == "default":
        return ChatLearning()
    if value.lower() == "on":
        return ChatLearning(profile=active)
    return ChatLearning(profile=check_profile_name(value))


def brief_title(brief: str | None) -> str:
    """The brief's first non-empty line, without a Markdown heading mark."""
    for line in (brief or "").splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()
    return ""


def extract_brief(reply: str) -> str:
    """The text between `<brief>` and `</brief>`; the whole reply if the markers are missing."""
    start = reply.find(BRIEF_OPEN)
    if start == -1:
        return reply.strip()
    body = reply[start + len(BRIEF_OPEN) :]
    end = body.find(BRIEF_CLOSE)
    return (body if end == -1 else body[:end]).strip()


# ---- repo summary and system prompt -------------------------------------------------------

_SKIP_DIRS = frozenset({".git", ".jev", "node_modules", "__pycache__", ".venv", "venv", "dist"})


def repo_tree(root: Path, max_entries: int = TREE_MAX_ENTRIES) -> str:
    """Top-level entries (directories end with `/`, with their direct children), truncated."""
    lines: list[str] = []

    def entries(path: Path) -> list[Path]:
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return []
        return [c for c in children if c.name not in _SKIP_DIRS and not c.name.startswith(".")]

    for entry in entries(root):
        if len(lines) >= max_entries:
            break
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            for child in entries(entry):
                if len(lines) >= max_entries:
                    break
                lines.append(f"  {child.name}{'/' if child.is_dir() else ''}")
        else:
            lines.append(entry.name)
    if len(lines) >= max_entries:
        lines.append("... (truncated)")
    return "\n".join(lines)


def readme_head(root: Path, max_chars: int = README_HEAD_CHARS) -> str | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"
    return None


_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def render_system_prompt(
    root: Path, lessons: Sequence[Lesson], learning_profile: str | None
) -> str:
    """The chat model's instructions (`roles/chat/prompt.md`), with the repo summary and the
    planner lessons of the chat's profile."""
    source = files("jev_agent.roles").joinpath("chat", "prompt.md").read_text(encoding="utf-8")
    return _env.from_string(source).render(
        repo_path=str(root),
        repo_tree=repo_tree(root),
        readme=readme_head(root),
        lessons=list(lessons),
        learning_profile=learning_profile,
        brief_open=BRIEF_OPEN,
        brief_close=BRIEF_CLOSE,
    )


# ---- backends ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass
class Reply:
    ok: bool
    text: str  # the model's answer, or a one-line error
    cost_usd: float | None = None
    session_id: str | None = None


class ChatBackend(Protocol):
    name: BackendName
    model: str | None
    # True when the backend keeps the conversation itself (a session id); history is then only
    # sent when there is no session yet.
    keeps_session: bool

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply: ...


def _history_text(history: Sequence[Turn]) -> str:
    return "\n\n".join(f"{t.role.capitalize()}: {t.text}" for t in history)


class ApiChatBackend:
    """`POST {base_url}/chat/completions` with the whole conversation each turn (stateless)."""

    name: BackendName = "api"
    keeps_session = False

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model: str | None = model
        self.timeout_s = timeout_s
        self._transport = transport

    def build_body(self, system: str, history: Sequence[Turn], message: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": message})
        return {"model": self.model, "messages": messages}

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
        key = os.environ.get(self.api_key_env, "")
        if not key:
            return Reply(False, f"{self.api_key_env} is not set")
        body = self.build_body(system, history, message)
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/Gane2506/baton",
            "X-OpenRouter-Title": "Baton Agent Orchestrator",
        }
        sections = [("request", f"POST {url}\nmodel: {self.model}\nmessage: {message}")]
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    url, json=body, headers=headers, timeout=self.timeout_s
                )
        except httpx.HTTPError as exc:
            reply = Reply(False, f"network error: {type(exc).__name__}")
        else:
            reply = _api_reply(response)
            sections.append(("response", f"HTTP {response.status_code}\n{response.text}"))
        sections.append(("result", f"ok: {reply.ok}\ncost_usd: {reply.cost_usd}"))
        append_log_sections(log_path, [(t, b.replace(key, REDACTED)) for t, b in sections])
        reply.text = redact(reply.text.replace(key, REDACTED))
        return reply


def _api_reply(response: httpx.Response) -> Reply:
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    if not response.is_success:
        message = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            message = str(data["error"].get("message", ""))
        return Reply(False, f"HTTP {response.status_code}: {message or response.reason_phrase}")
    if not isinstance(data, dict):
        return Reply(False, "response body is not a JSON object")
    usage = data.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    cost_usd = float(cost) if isinstance(cost, int | float) else None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        return Reply(False, "response has no message content", cost_usd)
    return Reply(True, content, cost_usd)


# What `claude --resume` says when it cannot find the session.
SESSION_GONE = re.compile(r"no conversation found|session[\w ]*not found", re.IGNORECASE)


class ClaudeChatBackend:
    """Claude Code headless: `claude -p --output-format json`, continued with `--resume`.

    Read-only: `--permission-mode dontAsk` with only Read, Glob and Grep allowed and the
    file-writing tools disallowed; the cwd is the repo. The first call sends the instructions
    (and any earlier turns, e.g. after switching backends) on stdin; later calls send just the
    message with `--resume <session_id>`. Each call is capped with `--max-budget-usd` at what
    is left of the chat budget.
    """

    name: BackendName = "claude_code"
    keeps_session = True
    READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
    DENIED_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash")

    def __init__(
        self,
        root: Path,
        command: str = "claude",
        model: str | None = None,
        *,
        bare: bool = False,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        runner: CommandRunner = run_command,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.command = command
        self.model = model
        self.bare = bare
        self.timeout_s = timeout_s
        self.runner = runner
        self._env = env

    def build_argv(self, session_id: str | None, budget_left_usd: float) -> list[str]:
        argv = [self.command, "-p", "--output-format", "json"]
        if self.bare:
            argv.append("--bare")
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--resume", session_id]
        argv += [
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            " ".join(self.READ_ONLY_TOOLS),
            "--disallowedTools",
            " ".join(self.DENIED_TOOLS),
        ]
        if budget_left_usd > 0:
            argv += ["--max-budget-usd", f"{budget_left_usd:.2f}"]
        return argv

    @staticmethod
    def first_prompt(system: str, history: Sequence[Turn], message: str) -> str:
        parts = [system.rstrip()]
        if history:
            parts.append("# Conversation so far\n\n" + _history_text(history))
        parts.append("# The user says\n\n" + message)
        return "\n\n---\n\n".join(parts)

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
        if session_id:
            reply = await self._call(session_id, message, budget_left_usd, log_path)
            if reply.ok or not SESSION_GONE.search(reply.text):
                return reply
            # The session is gone (another machine, cleared cache): start a new one.
        prompt = self.first_prompt(system, history, message)
        return await self._call(None, prompt, budget_left_usd, log_path)

    async def _call(
        self, session_id: str | None, prompt: str, budget_left_usd: float, log_path: Path
    ) -> Reply:
        env = dict(os.environ if self._env is None else self._env)
        argv = self.build_argv(session_id, budget_left_usd)
        try:
            result = await self.runner(
                argv, self.root, env, self.timeout_s, prompt, log_path=log_path
            )
        except OSError as exc:
            return Reply(False, f"could not run {self.command}: {exc}")
        payload = parse_result(result.stdout)
        status = classify_claude_code(result, payload)
        cost, _ = usage_from(payload)
        text = payload.get("result") if payload else None
        new_session = payload.get("session_id") if payload else None
        sid = new_session if isinstance(new_session, str) else session_id
        if status != "success" or not isinstance(text, str):
            detail = text if isinstance(text, str) else (result.stderr.strip() or "no output")
            summary = " ".join(detail.split())[:300]
            return Reply(False, redact(f"claude {status}: {summary}", env), cost, sid)
        return Reply(True, redact(text, env), cost, sid)


# ---- persistence and the conversation ---------------------------------------------------


@dataclass
class ChatMeta:
    chat_id: str
    created_at: str
    updated_at: str
    learning: ChatLearning
    profile: str | None  # workspace profile, passed to launched runs
    backend: BackendName
    model: str | None
    session_id: str | None = None
    budget_usd: float = DEFAULT_BUDGET_USD
    spent_usd: float = 0.0
    unknown_cost_calls: int = 0  # model calls that reported no cost
    turns: int = 0  # messages answered by the model
    brief: str | None = None
    brief_turns: int = 0  # `turns` when the brief was written; more turns make it stale
    runs: list[dict[str, str]] = field(default_factory=list)  # {"run_id", "kind", "at"}
    queue_items: list[dict[str, Any]] = field(default_factory=list)  # {"id", "priority", "at"}
    quality: str | None = None  # quality level for launched runs; None: [quality] level

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["learning"] = self.learning.to_json()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMeta:
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known}
        values["learning"] = ChatLearning.from_json(data.get("learning"))
        if values.get("backend") not in BACKENDS:
            raise ChatError(f"chat {data.get('chat_id')!r} has an unknown backend")
        return cls(**values)


def chats_dir(root: Path) -> Path:
    return root / STATE_DIRNAME / CHATS_DIRNAME


def chat_dir(root: Path, chat_id: str) -> Path:
    if not _CHAT_ID.match(chat_id):
        raise ChatError(f"{chat_id!r} is not a chat id (they look like c-20261012-153012-ab12)")
    return chats_dir(root) / chat_id


def list_chats(root: Path) -> list[ChatMeta]:
    """Saved chats, newest first. Unreadable ones are skipped."""
    found: list[ChatMeta] = []
    base = chats_dir(root)
    if not base.is_dir():
        return found
    for path in sorted(base.iterdir(), reverse=True):
        try:
            found.append(ChatMeta.from_json(json.loads((path / META_FILE).read_text("utf-8"))))
        except (OSError, ValueError, TypeError, ChatError):
            continue
    return found


class ChatSession:
    """One chat: its metadata, transcript and backend. Every change is saved at once."""

    def __init__(
        self, root: Path, meta: ChatMeta, backend: ChatBackend, system_prompt: str
    ) -> None:
        self.root = root
        self.meta = meta
        self.backend = backend
        self.system_prompt = system_prompt
        self.dir = chat_dir(root, meta.chat_id)
        self.history: list[Turn] = []
        # Sent ahead of the next message when the instructions change mid-session (Claude Code
        # keeps the original instructions in its session).
        self.pending_note: str | None = None

    @classmethod
    def create(
        cls,
        root: Path,
        backend: ChatBackend,
        system_prompt: str,
        learning: ChatLearning,
        profile: str | None,
        budget_usd: float,
    ) -> ChatSession:
        now = _now()
        meta = ChatMeta(
            chat_id=new_chat_id(),
            created_at=now,
            updated_at=now,
            learning=learning,
            profile=profile,
            backend=backend.name,
            model=backend.model,
            budget_usd=budget_usd,
        )
        session = cls(root, meta, backend, system_prompt)
        session.dir.mkdir(parents=True, exist_ok=True)
        session.save()
        return session

    @staticmethod
    def load_meta(root: Path, chat_id: str) -> ChatMeta:
        path = chat_dir(root, chat_id) / META_FILE
        try:
            return ChatMeta.from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise ChatError(f"no chat {chat_id!r}; `jev chat --list` shows saved chats") from exc
        except (ValueError, TypeError) as exc:
            raise ChatError(f"chat {chat_id!r} cannot be read: {exc}") from exc

    @classmethod
    def resume(
        cls, root: Path, meta: ChatMeta, backend: ChatBackend, system_prompt: str
    ) -> ChatSession:
        """Reopen a saved chat. A different backend than before starts a new model session
        (the earlier turns are sent along); the transcript is kept either way."""
        session = cls(root, meta, backend, system_prompt)
        if backend.name != meta.backend:
            meta.session_id = None
        meta.backend, meta.model = backend.name, backend.model
        session.history = session.read_history()
        session.save()
        return session

    # ---- files ---------------------------------------------------------------------------

    @property
    def transcript_path(self) -> Path:
        return self.dir / TRANSCRIPT_FILE

    @property
    def log_path(self) -> Path:
        return self.dir / BACKEND_LOG_FILE

    def save(self) -> None:
        self.meta.updated_at = _now()
        self.dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.meta.to_json(), indent=2)
        (self.dir / META_FILE).write_text(redact(text) + "\n", encoding="utf-8")

    def record(self, kind: str, text: str, **extra: Any) -> None:
        """Append one redacted entry to the transcript."""
        entry = {"at": _now(), "kind": kind, "text": redact(text), **extra}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def read_history(self) -> list[Turn]:
        """The user/assistant turns from the transcript, in order."""
        turns: list[Turn] = []
        if not self.transcript_path.exists():
            return turns
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("kind") in ("user", "assistant"):
                turns.append(Turn(entry["kind"], str(entry.get("text", ""))))
        return turns

    # ---- talking -------------------------------------------------------------------------

    @property
    def budget_left(self) -> float:
        return max(self.meta.budget_usd - self.meta.spent_usd, 0.0)

    def set_system_prompt(self, prompt: str, note: str) -> None:
        """New instructions (e.g. other lessons after `/learning`). A backend that keeps a
        session gets `note` ahead of the next message, since its session has the old ones."""
        self.system_prompt = prompt
        if self.backend.keeps_session and self.meta.session_id:
            self.pending_note = note

    async def say(self, message: str) -> Reply:
        """Send `message` to the model. Raises `BudgetReached` when the budget is spent."""
        if self.meta.spent_usd >= self.meta.budget_usd:
            raise BudgetReached(
                f"this chat has spent ${self.meta.spent_usd:.2f} of its "
                f"${self.meta.budget_usd:.2f} budget; no more model calls "
                "(resume with a higher --budget-usd to continue)"
            )
        sent = message
        if self.pending_note:
            sent = f"[Note from jev: {self.pending_note}]\n\n{message}"
        reply = await self.backend.send(
            self.system_prompt,
            self.history,
            sent,
            session_id=self.meta.session_id,
            budget_left_usd=self.budget_left,
            log_path=self.log_path,
        )
        if reply.cost_usd is not None:
            self.meta.spent_usd = round(self.meta.spent_usd + reply.cost_usd, 6)
        elif reply.ok:
            self.meta.unknown_cost_calls += 1
        if reply.session_id:
            self.meta.session_id = reply.session_id
        if reply.ok:
            self.pending_note = None
            self.history += [Turn("user", message), Turn("assistant", reply.text)]
            self.record("user", message)
            self.record("assistant", reply.text, cost_usd=reply.cost_usd)
            self.meta.turns += 1
        else:
            self.record("error", reply.text, message=message)
        self.save()
        return reply

    async def make_brief(self) -> Reply:
        """Ask for the idea brief; on success `meta.brief` holds it (markers removed)."""
        reply = await self.say(BRIEF_REQUEST)
        if reply.ok:
            self.meta.brief = extract_brief(reply.text)
            self.meta.brief_turns = self.meta.turns
            self.record("brief", self.meta.brief)
            self.save()
        return reply

    @property
    def brief_is_current(self) -> bool:
        """A brief exists and nothing was said since it was written."""
        return bool(self.meta.brief) and self.meta.turns <= self.meta.brief_turns

    # ---- bookkeeping ---------------------------------------------------------------------

    def set_learning(self, choice: ChatLearning) -> None:
        self.meta.learning = choice
        self.record("event", f"learning: {choice.describe()}")
        self.save()

    def set_quality(self, level: str | None) -> None:
        self.meta.quality = level
        self.record("event", f"quality: {level or 'default'}")
        self.save()

    def add_run(self, run_id: str, kind: str) -> None:
        self.meta.runs.append({"run_id": run_id, "kind": kind, "at": _now()})
        self.record("event", f"{kind} run {run_id} started", run_id=run_id)
        self.save()

    def add_queue_item(self, item_id: int, priority: str) -> None:
        self.meta.queue_items.append({"id": item_id, "priority": priority, "at": _now()})
        self.record("event", f"queued item {item_id} ({priority})", queue_item=item_id)
        self.save()


__all__ = [
    "BACKENDS",
    "BRIEF_REQUEST",
    "COMMANDS",
    "DEFAULT_BUDGET_USD",
    "ApiChatBackend",
    "BudgetReached",
    "ChatBackend",
    "ChatError",
    "ChatLearning",
    "ChatMeta",
    "ChatSession",
    "ClaudeChatBackend",
    "Command",
    "EchoChatBackend",
    "LearningOption",
    "Message",
    "Reply",
    "Turn",
    "brief_title",
    "chat_lessons",
    "check_learning_choice",
    "choose_learning",
    "effective_profile",
    "extract_brief",
    "learning_options",
    "list_chats",
    "parse_input",
    "parse_learning_arg",
    "parse_priority",
    "render_system_prompt",
    "repo_tree",
]


class EchoChatBackend:
    """A stand-in model for `JEV_FAKE_WORKERS=1`: echoes messages and writes a fixed brief.
    Never calls a model or a CLI."""

    keeps_session = False

    def __init__(self, name: BackendName, model: str | None = None) -> None:
        self.name: BackendName = name
        self.model = model

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
        if message.endswith(BRIEF_REQUEST):
            ideas = [t.text for t in history if t.role == "user"] or ["(no idea yet)"]
            body = "\n".join(f"- {text}" for text in ideas)
            brief = f"# Fake brief\n\n## Goal\n{ideas[0]}\n\n## Notes\n{body}"
            return Reply(True, f"{BRIEF_OPEN}\n{brief}\n{BRIEF_CLOSE}", 0.0)
        return Reply(True, f"(fake model) You said: {message}", 0.0)
