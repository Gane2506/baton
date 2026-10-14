"""`jev chat`: talk an idea through with a model, then plan, run or queue it (docs/chat.md).

The conversation logic lives in `core/chat.py`; this module is the terminal side: the start
screen, the prompt loop and the `/commands`. Runs are started with `run_cmds.start_run`, the
same code path as `jev run`, and queue items with the queue store API, as `jev queue add` does.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table

from jev_agent.app.chat import chat_title
from jev_agent.app.runs import start_run
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import print_outcome, print_warnings
from jev_agent.cli.text import short
from jev_agent.cli.wiring import (
    FAKE_WORKERS_ENV,
    NO_WORKERS_MESSAGE,
    CliContext,
    cli_context,
    fail,
    repo_context,
)
from jev_agent.config.schema import JevConfig
from jev_agent.config.servers_schema import NO_PROFILE
from jev_agent.core.chat import (
    BACKENDS,
    COMMANDS,
    DEFAULT_BUDGET_USD,
    ApiChatBackend,
    BackendName,
    BudgetReached,
    ChatBackend,
    ChatError,
    ChatLearning,
    ChatMeta,
    ChatSession,
    ClaudeChatBackend,
    Command,
    EchoChatBackend,
    Reply,
    brief_title,
    chat_lessons,
    check_learning_choice,
    choose_learning,
    effective_profile,
    hold_chat,
    learning_options,
    list_chats,
    parse_input,
    parse_learning_arg,
    parse_priority,
    render_system_prompt,
)
from jev_agent.core.learning import Learning, LearningError, Lesson, learning_status
from jev_agent.core.lease import RunInUseError
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.store import NotFoundError
from jev_agent.quality.levels import LEVELS, parse_level
from jev_agent.servers.profiles import ProfileError, get_profile

console = Console()


def is_interactive() -> bool:
    """Whether to show the start screen (tests patch this)."""
    return sys.stdin.isatty()


# ---- backend and model -------------------------------------------------------------------


def api_usable(config: JevConfig) -> bool:
    api = config.workers.api
    return api.enabled and bool(os.environ.get(api.api_key_env, "").strip())


def pick_backend(
    config: JevConfig, flag: str | None, stored: BackendName | None = None
) -> BackendName:
    """`--worker` > the resumed chat's backend (if still usable) > the api worker when its key
    is set and it is enabled > Claude Code. Raises `ChatError` when the choice is not usable."""
    claude_enabled = config.workers.claude_code.enabled
    fake = os.environ.get(FAKE_WORKERS_ENV) == "1"
    if flag is not None:
        if flag not in BACKENDS:
            raise ChatError(f"--worker must be one of: {', '.join(BACKENDS)}")
        if flag == "api" and not (fake or api_usable(config)):
            api = config.workers.api
            raise ChatError(
                "the api worker needs `[workers.api] enabled = true` and "
                f"{api.api_key_env} set in the environment"
            )
        if flag == "claude_code" and not claude_enabled:
            raise ChatError("`[workers.claude_code]` is disabled in jev.toml")
        return flag
    if stored == "api" and (fake or api_usable(config)):
        return "api"
    if stored == "claude_code" and claude_enabled:
        return "claude_code"
    if api_usable(config):
        return "api"
    if claude_enabled:
        return "claude_code"
    raise ChatError(
        "no chat model: set "
        f"{config.workers.api.api_key_env} (with `[workers.api]` enabled) or enable "
        "`[workers.claude_code]`"
    )


def pick_model(
    config: JevConfig, backend: BackendName, flag: str | None, stored: str | None = None
) -> str | None:
    """`--model` > the resumed chat's model > (api) `[workers.api] default_model`, then the
    first OpenRouter-style id (`vendor/model`) in `[roles.planner] models`; (Claude Code) the
    first planner model without a `/`, else Claude Code's default."""
    if flag:
        return flag
    if stored:
        return stored
    planner = config.roles.planner.models
    if backend == "api":
        return config.workers.api.default_model or next((m for m in planner if "/" in m), None)
    return next((m for m in planner if "/" not in m), None)


def make_backend(ctx: CliContext, backend: BackendName, model: str | None) -> ChatBackend:
    """The chat backend. With `JEV_FAKE_WORKERS=1`, an echoing fake (tests patch this)."""
    if os.environ.get(FAKE_WORKERS_ENV) == "1":
        return EchoChatBackend(backend, model)
    workers = ctx.config.workers
    if backend == "api":
        if not model:
            raise ChatError(
                "no model for the api backend: pass --model, or set `[workers.api] "
                "default_model` or an OpenRouter model in `[roles.planner] models`"
            )
        return ApiChatBackend(
            workers.api.base_url, workers.api.api_key_env, model, timeout_s=workers.api.timeout_s
        )
    claude = workers.claude_code
    return ClaudeChatBackend(
        ctx.root, claude.command, model, bare=claude.bare, timeout_s=claude.timeout_s
    )


# ---- learning ----------------------------------------------------------------------------


def _learning(ctx: CliContext) -> Learning:
    assert ctx.learning is not None
    return ctx.learning


def start_screen(ctx: CliContext) -> ChatLearning:
    """Ask which lessons to use (only on a TTY, when no learning flag was given)."""
    learning = _learning(ctx)
    active = learning_status(ctx.store, ctx.config).profile
    profile = learning.get(active)
    others = [p for p in learning.profiles() if p.name != active]
    options = learning_options(active, profile.lessons if profile else 0, others)
    console.print("[bold]Learning for this chat and the runs it starts:[/bold]")
    for option in options:
        console.print(f"  {option.key}) {escape(option.label)}")
    key = Prompt.ask("Choose", choices=[o.key for o in options], default="1", console=console)
    chosen = next(o for o in options if o.key == key)
    if chosen.choice is not None:
        if chosen.choice.profile is not None:
            learning.ensure(chosen.choice.profile)
        return chosen.choice
    for i, p in enumerate(others, 1):
        state = "" if p.enabled else ", disabled"
        console.print(f"  {i}) {escape(p.name)} ({p.scope}, {p.lessons} lessons{state})")
    number = Prompt.ask(
        "Profile", choices=[str(i) for i in range(1, len(others) + 1)], console=console
    )
    return ChatLearning(profile=others[int(number) - 1].name)


def lessons_and_prompt(
    ctx: CliContext, choice: ChatLearning, texts: list[str]
) -> tuple[str | None, list[Lesson], str]:
    learning = _learning(ctx)
    profile = effective_profile(choice, ctx.store, learning, ctx.config)
    lessons = chat_lessons(profile, learning, ctx.config, texts)
    return profile, lessons, render_system_prompt(ctx.root, lessons, profile)


def describe_learning(choice: ChatLearning, profile: str | None, lessons: list[Lesson]) -> str:
    text = choice.describe()
    if choice.is_default:
        text += f" (now {profile})" if profile else " (now off)"
    if profile is not None:
        text += f"; {len(lessons)} lesson(s) from {profile} in the model's instructions"
    return text


# ---- the chat loop -----------------------------------------------------------------------


class ChatLoop:
    """The prompt loop of one chat. `handle` returns False when the chat should end."""

    def __init__(self, ctx: CliContext, session: ChatSession) -> None:
        self.ctx = ctx
        self.session = session

    def _texts(self) -> list[str]:
        return [t.text for t in self.session.history if t.role == "user"]

    def _talk(self, message: str) -> Reply | None:
        try:
            with console.status("thinking..."):
                reply = asyncio.run(self.session.say(message))
        except BudgetReached as exc:
            console.print(f"[yellow]budget:[/yellow] {escape(str(exc))}")
            return None
        self._show(reply)
        return reply

    def _show(self, reply: Reply) -> None:
        if reply.ok:
            console.print(Markdown(reply.text))
        else:
            console.print(f"[red]model error:[/red] {escape(reply.text)}")

    def brief(self, show: bool = True) -> str | None:
        try:
            with console.status("writing the brief..."):
                reply = asyncio.run(self.session.make_brief())
        except BudgetReached as exc:
            console.print(f"[yellow]budget:[/yellow] {escape(str(exc))}")
            return None
        if not reply.ok:
            self._show(reply)
            return None
        if show:
            console.rule("brief")
            console.print(Markdown(self.session.meta.brief or ""))
            console.rule()
        return self.session.meta.brief

    def _current_brief(self) -> str | None:
        """The latest brief; written now if there is none or the chat moved on since."""
        if self.session.brief_is_current:
            return self.session.meta.brief
        if self.session.meta.brief:
            console.print("The chat moved on since the last brief; writing a new one first.")
        else:
            console.print("No brief yet; asking the model for one first.")
        return self.brief()

    def launch(self, kind: str) -> None:
        """`/plan` (plan-only) or `/run`: start a run from the brief, like `jev run`."""
        if not self.ctx.has_workers:
            console.print(f"[red]error:[/red] {escape(NO_WORKERS_MESSAGE)}")
            return
        meta = self.session.meta
        brief = self._current_brief()
        if brief is None:
            return
        print_warnings(self.ctx)
        console.print(f"Starting a {'plan-only ' if kind == 'plan' else ''}run "
                      f"(learning: {escape(meta.learning.describe())})...")  # fmt: skip
        try:
            record = asyncio.run(
                start_run(
                    self.ctx,
                    brief,
                    plan_only=kind == "plan",
                    **meta.learning.run_flags(),
                    quality=meta.quality,
                    # Without a profile, launched runs use the repo default like `jev run`.
                    profile=meta.profile,
                )
            )
        except (LearningError, PipelineError, RunInUseError, ValueError) as exc:
            console.print(f"[red]error:[/red] {escape(str(exc))}")
            return
        except KeyboardInterrupt:
            console.print("Interrupted. `jev status` shows the run; `jev resume RUN` continues it.")
            return
        self.session.add_run(record.id, kind)
        print_outcome(record)
        console.print(f"  details: `jev status {record.id}`")

    def queue(self, args: tuple[str, ...]) -> None:
        try:
            priority = parse_priority(args)
        except ChatError as exc:
            console.print(f"[red]error:[/red] {escape(str(exc))}")
            return
        brief = self._current_brief()
        if brief is None:
            return
        meta = self.session.meta
        item = self.ctx.store.add_queue_item(
            brief,
            priority,
            learning_profile=meta.learning.profile,
            no_learning=meta.learning.off,
            profile=meta.profile,
            quality=None if meta.quality is None else parse_level(meta.quality),
        )
        self.session.add_queue_item(item.id, item.priority)
        console.print(
            f"queued item [bold]{item.id}[/bold] ({item.priority}): "
            f"{escape(short(item.idea, 60))}; `jev scheduler start` runs the queue"
        )

    def learning(self, args: tuple[str, ...]) -> None:
        learning = _learning(self.ctx)
        if args:
            active = learning_status(self.ctx.store, self.ctx.config).profile
            try:
                choice = parse_learning_arg(" ".join(args), active)
                if choice.profile is not None and choice.profile == active:
                    learning.ensure(active)
                check_learning_choice(choice, learning)
            except LearningError as exc:
                console.print(f"[red]error:[/red] {escape(str(exc))}")
                return
            self.session.set_learning(choice)
        choice = self.session.meta.learning
        profile, lessons, prompt = lessons_and_prompt(self.ctx, choice, self._texts())
        if args:
            note = (
                "the learning choice changed; lessons from earlier runs to use from now on:\n"
                + "\n".join(f"- {lesson.text}" for lesson in lessons)
                if lessons
                else "the learning choice changed; ignore the lessons from earlier runs."
            )
            self.session.set_system_prompt(prompt, note)
        console.print(f"learning: {escape(describe_learning(choice, profile, lessons))}")
        if not args:
            console.print(
                "  change with /learning on|off|default|PROFILE (for the rest of the chat)"
            )

    def quality(self, args: tuple[str, ...]) -> None:
        """`/quality`: show the level runs from this chat get; `/quality LEVEL|default`."""
        if args:
            arg = " ".join(args).strip().lower()
            if arg == "default":
                self.session.set_quality(None)
            else:
                try:
                    self.session.set_quality(parse_level(arg))
                except ValueError as exc:
                    console.print(f"[red]error:[/red] {escape(str(exc))}")
                    return
        console.print(f"quality: {escape(self.quality_line())}")
        if not args:
            console.print(f"  change with /quality {'|'.join(LEVELS)}|default")

    def quality_line(self) -> str:
        level = self.session.meta.quality
        if level is None:
            return f"{self.ctx.config.quality.level} (from jev.toml)"
        return level

    def status(self) -> None:
        meta = self.session.meta
        _, lessons, _ = lessons_and_prompt(self.ctx, meta.learning, self._texts())
        profile = effective_profile(
            meta.learning, self.ctx.store, _learning(self.ctx), self.ctx.config
        )
        console.print(f"chat [bold]{meta.chat_id}[/bold]: {meta.turns} answered message(s)")
        console.print(f"  backend: {meta.backend} (model: {escape(meta.model or 'default')})")
        console.print(f"  learning: {escape(describe_learning(meta.learning, profile, lessons))}")
        console.print(f"  workspace profile: {escape(meta.profile or 'none')}")
        console.print(f"  quality: {escape(self.quality_line())}")
        title = brief_title(meta.brief) or "not written yet (/brief)"
        if meta.brief and not self.session.brief_is_current:
            title += " (older than the latest messages)"
        console.print(f"  brief: {escape(title)}")
        for run in meta.runs:
            try:
                state = str(self.ctx.store.get_run(run["run_id"]).state)
            except NotFoundError:
                state = "deleted"
            console.print(f"  {run['kind']} run {run['run_id']}: {state}")
        for item in meta.queue_items:
            console.print(f"  queue item {item['id']} ({item['priority']})")
        self.cost()

    def cost(self) -> None:
        meta = self.session.meta
        line = f"spent ${meta.spent_usd:.4f} of this chat's ${meta.budget_usd:.2f} budget"
        if meta.unknown_cost_calls:
            line += f" ({meta.unknown_cost_calls} call(s) reported no cost)"
        console.print(line + "; chat spend counts toward no run")

    def help(self) -> None:
        table = Table(title="chat commands", show_header=False)
        table.add_column("command", no_wrap=True)
        table.add_column("does", overflow="fold")
        for name, text in COMMANDS.items():
            table.add_row(f"/{name}", escape(text))
        console.print(table)
        console.print("Anything else goes to the model. Start a line with // to send a '/'.")

    def handle(self, line: str) -> bool:
        parsed = parse_input(line)
        if parsed is None:
            return True
        if not isinstance(parsed, Command):
            self._talk(parsed.text)
            return True
        name, args = parsed.name, parsed.args
        if name == "":
            console.print(f"unknown command {escape(parsed.raw)}; /help lists the commands")
        elif name == "quit":
            return False
        elif name == "brief":
            self.brief()
        elif name in ("plan", "run"):
            self.launch(name)
        elif name == "queue":
            self.queue(args)
        elif name == "learning":
            self.learning(args)
        elif name == "quality":
            self.quality(args)
        elif name == "status":
            self.status()
        elif name == "cost":
            self.cost()
        else:
            self.help()
        return True

    def run(self) -> None:
        while True:
            try:
                line = console.input("[bold cyan]you>[/bold cyan] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not self.handle(line):
                break
        console.print(
            f"Chat saved. Continue it with `jev chat --resume {self.session.meta.chat_id}`."
        )


# ---- the command -------------------------------------------------------------------------


def print_chat_list(root: Path) -> None:
    chats = list_chats(root)
    if not chats:
        console.print("No saved chats. Start one with `jev chat`.")
        return
    table = Table(title="chats")
    table.add_column("id", no_wrap=True)
    for column in ("updated", "backend", "learning", "turns", "runs", "spent", "title"):
        table.add_column(column, overflow="fold")
    for meta in chats:
        title = chat_title(meta, root) or "-"
        table.add_row(
            meta.chat_id,
            meta.updated_at.replace("+00:00", "Z"),
            meta.backend,
            escape(meta.learning.describe()),
            str(meta.turns),
            str(len(meta.runs)),
            f"${meta.spent_usd:.2f}",
            escape(short(title, 50)),
        )
    console.print(table)


def open_session(
    ctx: CliContext,
    *,
    learning: str | None,
    no_learning: bool,
    profile: str | None,
    worker: str | None,
    model: str | None,
    resume: str | None,
    budget_usd: float | None,
) -> ChatSession:
    """Create or resume the chat for these flags (flags win over a resumed chat's settings)."""
    stored: ChatMeta | None = None
    if resume is not None:
        stored = ChatSession.load_meta(ctx.root, resume)
    picked = None
    if stored is None and learning is None and not no_learning and is_interactive():
        picked = start_screen(ctx)
    choice = choose_learning(learning, no_learning, stored.learning if stored else None, picked)
    check_learning_choice(choice, _learning(ctx))
    backend_name = pick_backend(ctx.config, worker, stored.backend if stored else None)
    same_backend = stored is not None and stored.backend == backend_name
    chosen_model = pick_model(
        ctx.config, backend_name, model, stored.model if same_backend and stored else None
    )
    backend = make_backend(ctx, backend_name, chosen_model)
    if stored is None:
        _, _, prompt = lessons_and_prompt(ctx, choice, [])
        return ChatSession.create(
            ctx.root,
            backend,
            prompt,
            choice,
            profile,
            DEFAULT_BUDGET_USD if budget_usd is None else budget_usd,
        )
    stored.learning = choice
    if profile is not None:
        stored.profile = profile
    if budget_usd is not None:
        stored.budget_usd = budget_usd
    session = ChatSession(ctx.root, stored, backend, "")
    _, _, prompt = lessons_and_prompt(ctx, choice, [t.text for t in session.read_history()])
    return ChatSession.resume(ctx.root, stored, backend, prompt)


def chat(
    learning: Annotated[
        str | None,
        typer.Option(
            "--learning",
            metavar="PROFILE",
            help="Use this learning profile for the chat's lessons and the runs it starts.",
        ),
    ] = None,
    no_learning: Annotated[
        bool, typer.Option("--no-learning", help="No lessons in the chat or its runs.")
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", metavar="NAME", help="Workspace profile for launched runs."),
    ] = None,
    worker: Annotated[
        str | None,
        typer.Option(
            "--worker",
            metavar="api|claude_code",
            help="Chat backend (default: api if its key is set, else Claude Code).",
        ),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", metavar="MODEL", help="Model for the chat backend.")
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", metavar="CHAT_ID", help="Continue a saved chat."),
    ] = None,
    list_: Annotated[bool, typer.Option("--list", help="List saved chats and exit.")] = False,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            "--budget-usd",
            min=0.0,
            metavar="USD",
            help=f"Stop model calls once this chat has spent this much (default "
            f"{DEFAULT_BUDGET_USD:.2f}; a resumed chat keeps its own).",
        ),
    ] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Talk an idea through with a model, then /plan, /run or /queue the brief."""
    if list_:  # read-only: no workers, no model list, no new .jev/ (#26)
        with repo_context(repo, create=False) as listing:
            print_chat_list(listing.root)
        return
    with cli_context(repo) as ctx:
        if learning is not None and no_learning:
            raise fail("--learning and --no-learning cannot be used together")
        if profile is not None and not profile.strip():
            raise fail("--profile needs a name")
        if profile is not None and profile != NO_PROFILE:
            try:
                get_profile(ctx.config, profile)
            except ProfileError as exc:
                raise fail(str(exc)) from exc
        with ExitStack() as held:  # the chat's lock, for as long as this terminal has it
            try:
                if resume is not None:  # held before loading: the saved state must not move
                    held.enter_context(hold_chat(ctx.root, resume))
                session = open_session(
                    ctx,
                    learning=learning,
                    no_learning=no_learning,
                    profile=profile,
                    worker=worker,
                    model=model,
                    resume=resume,
                    budget_usd=budget_usd,
                )
                if resume is None:
                    held.enter_context(hold_chat(ctx.root, session.meta.chat_id))
            except (ChatError, LearningError) as exc:
                raise fail(str(exc)) from exc
            meta = session.meta
            verb = "resumed" if resume else "started"
            console.print(
                f"chat [bold]{meta.chat_id}[/bold] {verb}: {meta.backend} "
                f"(model: {escape(meta.model or 'default')}), budget ${meta.budget_usd:.2f}"
            )
            profile_name, lessons, _ = lessons_and_prompt(
                ctx, meta.learning, [t.text for t in session.history]
            )
            console.print(
                f"learning: {escape(describe_learning(meta.learning, profile_name, lessons))}"
            )
            if meta.profile:
                console.print(f"workspace profile: {escape(meta.profile)}")
            if resume and session.history:
                console.print(f"{len(session.history) // 2} earlier exchange(s) restored.")
            console.print("Describe your idea. /help lists the commands; /quit leaves.")
            ChatLoop(ctx, session).run()
