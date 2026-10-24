"""`jev chat`'s persistence and conversation: `ChatSession` (metadata, transcript and backend),
the saved-chat helpers (`list_chats`, `read_transcript`, `hold_chat`) and the chat budget."""

from __future__ import annotations

import fcntl
import importlib
import json
import math
import os
import re
import secrets
import sys
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jev_agent.config import STATE_DIRNAME
from jev_agent.core.chat_backends import BACKENDS, BackendName, Turn
from jev_agent.core.chat_learning import ChatLearning
from jev_agent.core.chat_prompts import BRIEF_REQUEST, extract_brief
from jev_agent.core.design_system import DesignSystem, get_design_system, render_for_prompt
from jev_agent.core.learning import LearningError
from jev_agent.runlog.redact import redact

if TYPE_CHECKING:
    from jev_agent.core.chat_backends import ChatBackend, Reply

CHATS_DIRNAME = "chats"
TRANSCRIPT_FILE = "transcript.jsonl"
META_FILE = "meta.json"
BACKEND_LOG_FILE = "backend.log"
LOCK_FILE = "chat.lock"
DEFAULT_BUDGET_USD = 1.00
# Issue #202: what a successful model call that reports no cost is charged. Providers
# without a cost field used to leave `spent_usd` untouched forever, so the budget never
# tripped no matter how many calls ran. A cent is a floor, not an estimate — deliberately
# small so unknown-cost providers stay usable, but the budget now trips after enough
# such calls instead of never. The count is still kept in `unknown_cost_calls`.
UNKNOWN_COST_USD = 0.01
_CHAT_ID = re.compile(r"^c-\d{8}-\d{6}-[0-9a-f]{4}$")
_CREATE_ID_RETRIES = 10  # fresh `new_chat_id()` attempts before `create()` gives up


class ChatError(Exception):
    """A chat cannot be opened, resumed or continued. The message is user-facing."""


class BudgetReached(ChatError):
    """The chat has spent its budget; no more model calls."""


def new_chat_id() -> str:
    """A short, sortable chat id: `c-` plus UTC time and 4 random hex chars."""
    return f"c-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    # The design system this chat (and the runs it starts) uses: "library:<name>",
    # "repo", "none" (explicitly no design system), or None for no explicit choice
    # (the repo default, as before). Old chats without the key load with None:
    # `from_json` drops unknown keys and the default fills the rest.
    design_system: str | None = None

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
        # Issue #202: a hand-edited meta.json with a string (or boolean, NaN, ...) in a
        # numeric field used to load with the wrong type and break budget arithmetic
        # later. Reject it here, so `list_chats` skips that one chat and `load_meta`
        # reports it, instead of crashing some unrelated operation.
        for name in ("budget_usd", "spent_usd"):
            value = values.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ChatError(f"chat {data.get('chat_id')!r} has a non-numeric {name}: {value!r}")
        for name in ("turns", "brief_turns", "unknown_cost_calls"):
            value = values.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ChatError(f"chat {data.get('chat_id')!r} has a non-integer {name}: {value!r}")
        return cls(**values)


def _design_library() -> Any | None:
    """`jev_agent.core.design_library` (the library WS-A builds), or None when that module
    is absent. Imported late so the chat still works in a build without the library."""
    try:
        return importlib.import_module("jev_agent.core.design_library")
    except ImportError:
        return None


def _warn_design(message: str) -> None:
    warnings.warn(message, stacklevel=3)
    print(f"warning: {message}", file=sys.stderr)


def list_design_library() -> list[str]:
    """The design library's system names; empty when the library module is absent."""
    library = _design_library()
    if library is None:
        return []
    return list(library.list_library())


def parse_design_system(value: str | None) -> str | None:
    """Validate a `--design-system` flag or the New chat form's `design_system` field.

    Accepts `library:<name>` (the name must exist in the design library), `repo` and
    `none`; returns the choice to store on `ChatMeta.design_system` (`"library:<name>"`,
    `"repo"`, `"none"`, or None for "no choice", which keeps the repo default).
    Raises `ChatError` for anything else.
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value == "none":
        return "none"
    if value == "repo":
        return "repo"
    if value.startswith("library:"):
        name = value[len("library:") :]
        if not name:
            raise ChatError("a design system choice needs a library name after 'library:'")
        library = _design_library()
        if library is None:
            raise ChatError("the design library is not available in this build of jev")
        names = list_design_library()
        if name not in names:
            known = ", ".join(names) if names else "the library is empty"
            raise ChatError(f"unknown design library system {name!r} (known: {known})")
        return f"library:{name}"
    raise ChatError(f"unknown design system {value!r}: use 'library:<name>', 'repo' or 'none'")


def resolve_design_system(root: Path, choice: str | None) -> DesignSystem | None:
    """The design system a chat's (or run's) `design_system` choice means, for prompt
    injection. None is "no choice", which behaves exactly as before (the repo's own
    system); `"repo"` is the repo's system explicitly; `"none"` means no design system
    at all; `"library:<name>"` is the named library system, preferred over the repo
    default. A library entry that vanished since the choice was stored warns and falls
    back to the repo's system. Never raises."""
    if choice == "none":
        return None
    if choice is None or choice == "repo":
        return get_design_system(root)
    if choice.startswith("library:"):
        name = choice[len("library:") :]
        library = _design_library()
        if library is not None:
            try:
                system: DesignSystem | None = library.load_library_system(name)
            except Exception as exc:
                _warn_design(
                    f"design library system {name!r} is unavailable and will be ignored ({exc})"
                )
            else:
                return system
        return get_design_system(root)
    return None


def describe_design_system(choice: str | None) -> str:
    """The human label for a stored choice: "None", "Repo design system", "Library: <name>"."""
    if choice is None or choice == "none":
        return "None"
    if choice == "repo":
        return "Repo design system"
    if choice.startswith("library:"):
        return f"Library: {choice[len('library:') :]}"
    return choice


def with_design_system(root: Path, prompt: str, choice: str | None) -> str:
    """Append the resolved design system to a chat's system prompt (unchanged when unset)."""
    rendered = render_for_prompt(resolve_design_system(root, choice))
    return f"{prompt}\n\n{rendered}" if rendered else prompt


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
        except (OSError, ValueError, TypeError, ChatError, LearningError):
            # LearningError: a stored learning choice `ChatLearning.from_json` could not
            # make sense of (issue #202) — one corrupt chat must not break the listing
            # of all the others.
            continue
    return found


def read_transcript(root: Path, chat_id: str) -> list[dict[str, Any]]:
    """Every entry of a saved chat's transcript (already redacted), oldest first; lines that
    cannot be parsed are skipped. Raises `ChatError` for a malformed id."""
    path = chat_dir(root, chat_id) / TRANSCRIPT_FILE
    entries: list[dict[str, Any]] = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


class ChatInUse(ChatError):
    """Another process (a terminal `jev chat` or a dashboard reply) has this chat open."""


@contextmanager
def hold_chat(root: Path, chat_id: str) -> Iterator[None]:
    """Hold the saved chat's lock for the block: one process at a time loads, changes and
    saves a chat, so a terminal and the dashboard never overwrite each other's `meta.json`.
    An exclusive `flock` on `chat.lock`; the kernel drops it when the holder dies. Raises
    `ChatInUse` at once when it is taken, and `ChatError` for an unknown chat."""
    directory = chat_dir(root, chat_id)
    if not (directory / META_FILE).is_file():
        raise ChatError(f"no chat {chat_id!r}; `jev chat --list` shows saved chats")
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ChatInUse(
                f"chat {chat_id} is open elsewhere (a terminal `jev chat` or the dashboard); "
                "try again when that is done"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ChatSession:
    """One chat: its metadata, transcript and backend. Every change is saved at once."""

    def __init__(
        self,
        root: Path,
        meta: ChatMeta,
        backend: ChatBackend,
        system_prompt: str,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.root = root
        self._clock = _now if clock is None else clock
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
        *,
        clock: Callable[[], str] | None = None,
        chat_id: str | None = None,
        design_system: str | None = None,
    ) -> ChatSession:
        clock = _now if clock is None else clock
        now = clock()
        # A generated id can collide with an existing chat (same second, same 4 random hex
        # chars): retry with a fresh id instead of silently merging two chats into one
        # directory. An explicitly chosen id keeps the old merge behavior.
        attempts = _CREATE_ID_RETRIES if chat_id is None else 1
        for _ in range(attempts):
            resolved = new_chat_id() if chat_id is None else chat_id
            meta = ChatMeta(
                chat_id=resolved,
                created_at=now,
                updated_at=now,
                learning=learning,
                profile=profile,
                backend=backend.name,
                model=backend.model,
                budget_usd=budget_usd,
                design_system=design_system,
            )
            session = cls(root, meta, backend, system_prompt, clock=clock)
            try:
                session.dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                if chat_id is not None:
                    session.dir.mkdir(parents=True, exist_ok=True)
                    session.save()
                    return session
                continue
            session.save()
            return session
        raise ChatError("could not pick an unused chat id; please try again")

    @staticmethod
    def load_meta(root: Path, chat_id: str) -> ChatMeta:
        path = chat_dir(root, chat_id) / META_FILE
        try:
            return ChatMeta.from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise ChatError(f"no chat {chat_id!r}; `jev chat --list` shows saved chats") from exc
        except (ValueError, TypeError, LearningError) as exc:
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
        self.meta.updated_at = self._clock()
        self.dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.meta.to_json(), indent=2)
        # Write-temp-then-rename: a crash mid-write never leaves a half-written meta.json.
        tmp = self.dir / f".{META_FILE}.{os.getpid()}.tmp"
        tmp.write_text(redact(text) + "\n", encoding="utf-8")
        os.replace(tmp, self.dir / META_FILE)

    def record(self, kind: str, text: str, **extra: Any) -> None:
        """Append one redacted entry to the transcript. String extras are redacted too: the
        `say()` error path stores the user's message as an extra, and it must not persist
        raw (e.g. a pasted API key) when a backend call fails."""
        entry = {
            "at": self._clock(),
            "kind": kind,
            "text": redact(text),
            **{k: redact(v) if isinstance(v, str) else v for k, v in extra.items()},
        }
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
            # Issue #202: a call that reports no cost still consumes budget, or a
            # provider without a cost field would never trip it (see UNKNOWN_COST_USD).
            self.meta.unknown_cost_calls += 1
            self.meta.spent_usd = round(self.meta.spent_usd + UNKNOWN_COST_USD, 6)
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
        self.meta.runs.append({"run_id": run_id, "kind": kind, "at": self._clock()})
        self.record("event", f"{kind} run {run_id} started", run_id=run_id)
        self.save()

    def add_queue_item(self, item_id: int, priority: str) -> None:
        self.meta.queue_items.append({"id": item_id, "priority": priority, "at": self._clock()})
        self.record("event", f"queued item {item_id} ({priority})", queue_item=item_id)
        self.save()


__all__ = [
    "BACKEND_LOG_FILE",
    "CHATS_DIRNAME",
    "DEFAULT_BUDGET_USD",
    "LOCK_FILE",
    "META_FILE",
    "TRANSCRIPT_FILE",
    "UNKNOWN_COST_USD",
    "_CHAT_ID",
    "BudgetReached",
    "ChatError",
    "ChatInUse",
    "ChatMeta",
    "ChatSession",
    "_now",
    "chat_dir",
    "chats_dir",
    "describe_design_system",
    "hold_chat",
    "list_chats",
    "list_design_library",
    "new_chat_id",
    "parse_design_system",
    "read_transcript",
    "resolve_design_system",
    "with_design_system",
]
