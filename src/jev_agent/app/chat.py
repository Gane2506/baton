"""Chat work for the web UI and the CLI front ends: create, continue and act on `jev chat`
sessions (docs/chat.md).

Every function is the same code path the CLI uses (`core.chat` sessions and backends,
`app.runs` for runs, `core.queue` for the queue), so a chat started in the browser is a
normal `jev chat` session: same files, same budget, same learning profiles. Nothing here
touches a network directly: the backends do, and `JEV_FAKE_WORKERS=1` swaps in the echo one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol

from jev_agent.app.context import NO_WORKERS_MESSAGE, fake_workers_enabled, open_context, open_repo
from jev_agent.app.jobs import run_job_coroutine
from jev_agent.app.runs import create_run
from jev_agent.config.schema import JevConfig
from jev_agent.core.chat import (
    ApiChatBackend,
    BackendName,
    ChatBackend,
    ChatError,
    ChatLearning,
    ChatSession,
    ClaudeChatBackend,
    EchoChatBackend,
    chat_lessons,
    check_learning_choice,
    effective_profile,
    render_system_prompt,
)
from jev_agent.core.learning import Learning
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.queue import add_idea
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
    """The saved chat's session with its backend and prompt, as `jev chat --resume` builds it."""
    meta = ChatSession.load_meta(root, chat_id)
    backend = build_backend(repo.config, root, meta.backend, meta.model)
    profile = effective_profile(meta.learning, repo.store, repo.learning, repo.config)
    prompt = render_system_prompt(root, chat_lessons(profile, repo.learning, repo.config), profile)
    return ChatSession.resume(root, meta, backend, prompt)


def create_chat(
    root: Path,
    repo: RepoLike,
    worker: BackendName,
    model: str | None,
    choice: ChatLearning,
    profile: str | None,
    budget_usd: float,
) -> str:
    """A new chat, saved the way `jev chat` starts one; returns its id."""
    check_learning_choice(choice, repo.learning)
    backend = build_backend(repo.config, root, worker, model)
    active = effective_profile(choice, repo.store, repo.learning, repo.config)
    prompt = render_system_prompt(root, chat_lessons(active, repo.learning, repo.config), active)
    session = ChatSession.create(root, backend, prompt, choice, profile, budget_usd)
    return session.meta.chat_id


def turn(root: Path, chat_id: str, text: str) -> str:
    """One message to the model (runs in a `ChatRunner` job); returns a one-line outcome."""
    with open_repo(root) as repo:
        session = session_for(root, repo, chat_id)
        reply = run_job_coroutine(session.say(text))
        spent = session.meta.spent_usd
    if reply.ok:
        return f"replied; the chat has spent ${spent:.2f}"
    return f"the model answered with an error: {reply.text}"


def brief(root: Path, chat_id: str) -> str:
    """`/brief`: ask the model for the idea brief (runs in a `ChatRunner` job)."""
    with open_repo(root) as repo:
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
        )
    with open_repo(root) as repo:
        session_for(root, repo, chat_id).add_run(run.id, "plan-only" if plan_only else "run")
    return run.id


def queue_from_chat(root: Path, chat_id: str, priority: str = "normal") -> int:
    """`/queue`: add the chat's brief to the idea queue with the chat's flags."""
    with open_repo(root) as repo:
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
