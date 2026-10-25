"""Chat work for the web UI and the CLI front ends: create, continue and act on `jev chat`
sessions (docs/chat.md).

Every function is the same code path the CLI uses (`core.chat` sessions and backends,
`app.runs` for runs, `core.queue` for the queue), so a chat started in the browser is a
normal `jev chat` session: same files, same budget, same learning profiles. Nothing here
touches a network directly: the backends do, and `JEV_FAKE_WORKERS=1` swaps in the echo one.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Protocol

from jev_agent.app.context import NO_WORKERS_MESSAGE, fake_workers_enabled, open_context, open_repo
from jev_agent.app.jobs import run_job_coroutine
from jev_agent.app.runs import create_run
from jev_agent.config.schema import JevConfig
from jev_agent.core.chat import (
    META_FILE,
    TRANSCRIPT_FILE,
    ApiChatBackend,
    BackendName,
    ChatBackend,
    ChatError,
    ChatLearning,
    ChatMeta,
    ChatSession,
    ClaudeChatBackend,
    EchoChatBackend,
    brief_title,
    chat_dir,
    chat_lessons,
    check_learning_choice,
    effective_profile,
    hold_chat,
    render_system_prompt,
    with_design_system,
)
from jev_agent.core.learning import Learning
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.queue import add_idea
from jev_agent.core.skills import describe_for_prompt, find_skills
from jev_agent.core.store import Store


class RepoLike(Protocol):
    """What the chat needs from an open repo (`app.context.RepoContext`, `views.Repo`)."""

    root: Path
    config: JevConfig
    store: Store
    learning: Learning


def backend_readiness(config: JevConfig) -> dict[str, tuple[bool, str]]:
    """Which chat backends are usable, with a one-line reason (the New chat form)."""
    api = config.workers.api
    claude = config.workers.claude_code
    api_ok = bool(os.environ.get(api.api_key_env))
    claude_ok = shutil.which(claude.command) is not None
    return {
        "api": (api_ok, f"{api.api_key_env} is set" if api_ok else f"{api.api_key_env} is unset"),
        "claude_code": (
            claude_ok,
            f"{claude.command} is on PATH" if claude_ok else f"{claude.command} is not on PATH",
        ),
    }


def build_backend(
    config: JevConfig, root: Path, worker: BackendName, model: str | None
) -> ChatBackend:
    """The backend `jev chat` would use for this worker and model (the echo one in tests)."""
    if fake_workers_enabled():
        return EchoChatBackend(worker, model)
    if worker == "claude_code":
        claude = config.workers.claude_code
        if shutil.which(claude.command) is None:
            raise ChatError(
                f"the {claude.command} CLI is not on PATH; install it or use the api backend"
            )
        return ClaudeChatBackend(
            root,
            command=claude.command,
            model=model,
            bare=claude.bare,
            timeout_s=claude.timeout_s,
        )
    api = config.workers.api
    if not os.environ.get(api.api_key_env):
        raise ChatError(f"{api.api_key_env} is not set; set it or use the claude_code backend")
    chosen = model or api.default_model
    if not chosen:
        raise ChatError(
            "no model for the api backend: pass one, or set [workers.api] default_model"
        )
    return ApiChatBackend(
        api.base_url,
        api.api_key_env,
        chosen,
        timeout_s=api.timeout_s,
    )


def session_for(root: Path, repo: RepoLike, chat_id: str) -> ChatSession:
    """The saved chat's session with its backend and prompt, as `jev chat --resume` builds it.
    Callers that change the chat hold `hold_chat` around it, as the terminal does."""
    meta = ChatSession.load_meta(root, chat_id)
    backend = build_backend(repo.config, root, meta.backend, meta.model)
    profile = effective_profile(meta.learning, repo.store, repo.learning, repo.config)
    prompt = render_system_prompt(root, chat_lessons(profile, repo.learning, repo.config), profile)
    prompt = with_design_system(root, prompt, meta.design_system)
    return ChatSession.resume(root, meta, backend, prompt)


def create_chat(
    root: Path,
    repo: RepoLike,
    worker: BackendName,
    model: str | None,
    choice: ChatLearning,
    profile: str | None,
    budget_usd: float,
    design_system: str | None = None,
) -> str:
    """A new chat, saved the way `jev chat` starts one; returns its id. `design_system`
    is the chat's design-system choice ("library:<name>", "repo", "none", or None)."""
    check_learning_choice(choice, repo.learning)
    backend = build_backend(repo.config, root, worker, model)
    active = effective_profile(choice, repo.store, repo.learning, repo.config)
    prompt = render_system_prompt(root, chat_lessons(active, repo.learning, repo.config), active)
    prompt = with_design_system(root, prompt, design_system)
    session = ChatSession.create(
        root, backend, prompt, choice, profile, budget_usd, design_system=design_system
    )
    return session.meta.chat_id


def switch_backend(root: Path, chat_id: str, worker: BackendName, model: str | None) -> BackendName:
    """Move a saved chat to another backend (issue #277): a chat whose backend is
    unavailable — e.g. `claude_code` on a machine without the `claude` CLI — is
    otherwise a dead end, since every send fails and the UI offers no way out.

    The new backend is validated first via `build_backend` (missing CLI, missing
    API key, or no API model all raise `ChatError` and leave the chat untouched).
    The switch goes through `ChatSession.resume`, so a different backend starts a
    new model session (`meta.session_id` cleared) while the transcript is kept.
    Returns the backend the chat now uses. Refuses a switch to the backend the
    chat already uses."""
    with hold_chat(root, chat_id), open_repo(root) as repo:
        meta = ChatSession.load_meta(root, chat_id)
        if worker == meta.backend:
            raise ChatError(f"chat {chat_id} already uses the {worker} backend")
        backend = build_backend(repo.config, root, worker, model)
        profile = effective_profile(meta.learning, repo.store, repo.learning, repo.config)
        prompt = render_system_prompt(
            root, chat_lessons(profile, repo.learning, repo.config), profile
        )
        prompt = with_design_system(root, prompt, meta.design_system)
        ChatSession.resume(root, meta, backend, prompt)
    return backend.name


def delete_chat(root: Path, chat_id: str) -> None:
    """Delete a saved chat's files; refused while another process has it open (`ChatInUse`,
    a `ChatError` — a terminal `jev chat` or a reply in flight). Runs it started are kept."""
    with hold_chat(root, chat_id):
        directory = chat_dir(root, chat_id)
        if not (directory / META_FILE).is_file():
            raise ChatError(f"no chat {chat_id!r}; `jev chat --list` shows saved chats")
        shutil.rmtree(directory)


# Titles in chat lists stay one line and short.
TITLE_MAX_CHARS = 70


def first_user_message(root: Path, chat_id: str, max_lines: int = 400) -> str | None:
    """The opening message of a saved chat, for lists. Reads at most `max_lines` transcript
    lines and stops at the first user turn; `None` when there is none or it cannot be read."""
    try:
        path = chat_dir(root, chat_id) / TRANSCRIPT_FILE
    except ChatError:
        return None
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(entry, dict)
                    and entry.get("kind") == "user"
                    and str(entry.get("text", "")).strip()
                ):
                    return str(entry["text"]).strip()
    except OSError:
        return None
    return None


def chat_title(meta: ChatMeta, root: Path) -> str:
    """What to call a chat in a list: its brief, else its opening message, else ''."""
    text = brief_title(meta.brief) or first_user_message(root, meta.chat_id) or ""
    line = " ".join(text.split())
    return line if len(line) <= TITLE_MAX_CHARS else line[: TITLE_MAX_CHARS - 3].rstrip() + "..."


def turn(root: Path, chat_id: str, text: str) -> str:
    """One message to the model (runs in a `ChatRunner` job); returns a one-line outcome."""
    with hold_chat(root, chat_id), open_repo(root) as repo:
        session = session_for(root, repo, chat_id)
        # Find-skills pass: match the message against Baton's skill registry so the
        # model knows what Baton can do, and the UI can show what was found.
        skill_matches = find_skills(text)
        sent = text
        if skill_matches:
            session.record(
                "skills",
                "; ".join(m.skill.name for m in skill_matches),
                titles="; ".join(m.skill.title for m in skill_matches),
            )
            sent = f"[Skills found: {describe_for_prompt(skill_matches)}]\n\n{text}"
        reply = run_job_coroutine(session.say(sent))
        spent = session.meta.spent_usd
    if reply.ok:
        return f"replied; the chat has spent ${spent:.2f}"
    return f"the model answered with an error: {reply.text}"


def brief(root: Path, chat_id: str) -> str:
    """`/brief`: ask the model for the idea brief (runs in a `ChatRunner` job)."""
    with hold_chat(root, chat_id), open_repo(root) as repo:
        session = session_for(root, repo, chat_id)
        reply = run_job_coroutine(session.make_brief())
    if reply.ok:
        return "brief written"
    return f"could not write the brief: {reply.text}"


def start_run_from_chat(root: Path, chat_id: str, *, plan_only: bool) -> str:
    """`/plan` and `/run`: create a run from the chat's brief (the caller advances it with a
    `start` job) and record it on the chat. Returns the run id."""
    with open_context(root) as ctx:
        if not ctx.has_workers:
            raise PipelineError(NO_WORKERS_MESSAGE)
        meta = ChatSession.load_meta(root, chat_id)
        if not meta.brief:
            raise ChatError("no brief yet; write one first (the Write brief button)")
        flags = meta.learning.run_flags()
        run = create_run(
            ctx,
            meta.brief,
            plan_only=plan_only,
            learning=flags["learning"],
            no_learning=flags["no_learning"],
            quality=meta.quality,
            profile=meta.profile,
            design_system=meta.design_system,
        )
    with hold_chat(root, chat_id), open_repo(root) as repo:
        session_for(root, repo, chat_id).add_run(run.id, "plan-only" if plan_only else "run")
    return run.id


def queue_from_chat(root: Path, chat_id: str, priority: str = "normal") -> int:
    """`/queue`: add the chat's brief to the idea queue with the chat's flags.

    The whole thing runs under `hold_chat`: recording the queue item on the
    chat mutates its meta, and doing that outside the lock let a reply in
    flight interleave with the queue recording (issue #197). Holding the lock
    first also means a chat open elsewhere fails before anything is queued,
    instead of leaving an orphan queue item behind a failed recording.
    """
    with hold_chat(root, chat_id), open_repo(root) as repo:
        meta = ChatSession.load_meta(root, chat_id)
        if not meta.brief:
            raise ChatError("no brief yet; write one first (the Write brief button)")
        flags = meta.learning.run_flags()
        item = add_idea(
            repo.store,
            repo.learning,
            meta.brief,
            priority,
            plan_only=False,
            learning_profile=flags["learning"],
            no_learning=flags["no_learning"],
            profile=meta.profile,
            quality=meta.quality,
            ship_mode=None,
        )
        session_for(root, repo, chat_id).add_queue_item(item.id, priority)
    return item.id
