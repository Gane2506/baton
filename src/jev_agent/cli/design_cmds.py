"""`jev design ...`: the repo's design system (Claude-Styles-like, core/design_system.py).

`show` prints the current system, `init` scaffolds a starter one, and `generate` invokes
the `designer` role worker with the repo brief to produce or refresh it. Everything lives
in `<repo>/.jev/design/` and is versioned on every save.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import NO_WORKERS_MESSAGE, cli_context, fail, repo_context
from jev_agent.core.design_system import (
    STARTER_MARKDOWN,
    STARTER_TOKENS,
    DesignSystem,
    design_dir,
    get_design_system,
    render_for_prompt,
    save_design_system,
)
from jev_agent.core.harness import DEFAULT_TIMEOUT_S, Halt
from jev_agent.core.models import DesignDoc
from jev_agent.dispatch.requests import DEFAULT_EFFORT, DEFAULT_MODEL
from jev_agent.roles.schemas import ROLE_SCHEMAS
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.registry import WorkerNotAvailable

console = Console()

design_app = typer.Typer(
    name="design",
    help="The repo's design system: show it, scaffold one, or generate it with the designer.",
    no_args_is_help=True,
)


@design_app.command("show")
def show(repo: RepoOption = Path(".")) -> None:
    """Print the repo's current design system (markdown), or say there is none yet."""
    with repo_context(repo, create=False) as ctx:
        system = get_design_system(ctx.root)
        if system is None:
            console.print("No design system yet. `jev design init` scaffolds a starter one.")
            return
        console.print(f"[bold]Design system v{system.version}[/bold] ({system.updated_at})")
        console.print(system.markdown, markup=False)


@design_app.command("init")
def init(
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace the existing system instead of keeping it."),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Scaffold a starter design system with sensible defaults (versioned on later saves)."""
    with repo_context(repo) as ctx:
        if get_design_system(ctx.root) is not None and not force:
            raise fail(
                "The repo already has a design system. "
                "Pass --force to replace it, or `jev design generate` to refresh it."
            )
        version = save_design_system(ctx.root, STARTER_MARKDOWN, dict(STARTER_TOKENS))
        console.print(f"Design system v{version} written to {design_dir(ctx.root)}.")


def _brief_from_readme(root: Path) -> str:
    """The repo README as the design brief; empty when there is no README."""
    for name in ("README.md", "readme.md"):
        readme = root / name
        if readme.exists():
            try:
                return readme.read_text(encoding="utf-8")[:4000]
            except OSError:
                return ""
    return ""


def _screens_section(doc: DesignDoc, system: DesignSystem | None) -> str:
    """The DesignDoc rendered as the system's screen-specs section."""
    lines = ["## Screen specs (generated)", ""]
    ref = f" (built against design system v{system.version})" if system else ""
    lines.append(f"_{doc.title}_{ref} — {doc.summary}".rstrip(" —"))
    lines.append("")
    for screen in doc.screens:
        lines.append(f"### {screen.name}")
        if screen.description:
            lines.append(screen.description)
        if screen.components:
            lines.append("Components: " + ", ".join(screen.components))
        lines.append("")
    if doc.notes:
        lines.append("Design notes:")
        lines.extend(f"- {note}" for note in doc.notes)
    return "\n".join(lines).rstrip()


def _merge_screens(markdown: str, section: str) -> str:
    """Replace the old screen-specs section with the new one, or append it."""
    marker = "## Screen specs (generated)"
    if marker in markdown:
        head = markdown.split(marker, 1)[0].rstrip()
        return head + "\n\n" + section + "\n"
    return markdown.rstrip() + "\n\n" + section + "\n"


@design_app.command("generate")
def generate(
    brief: Annotated[
        str | None,
        typer.Option("--brief", help="What to design. Defaults to the repo's README."),
    ] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Produce (or refresh) the design system: the `designer` role worker designs from
    the brief (or the README), grounded in the current system when there is one.

    The worker's DesignDoc refreshes the stored system (a new version; the old files are
    kept as `design-system.v<n>.md` / `tokens.v<n>.json`).
    """
    with cli_context(repo) as ctx:
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        h = ctx.pipeline.h
        options = h.options("designer")
        if not options.workers:
            raise fail(
                "no available worker for role designer: enable one of its workers in "
                "config (`jev doctor` shows which are usable)"
            )
        brief_text = (brief or "").strip() or _brief_from_readme(ctx.root)
        if not brief_text:
            raise fail("nothing to design from: pass --brief or add a README.md")
        system = get_design_system(ctx.root)
        try:
            prompt = h.prompt(
                "designer",
                idea=brief_text,
                plan_json=None,
                design_system=render_for_prompt(system) if system is not None else None,
                brief=brief_text,
            )
        except Halt as exc:
            raise fail(str(exc)) from exc
        assignment = h.assignment(
            "designer",
            {
                "worker": options.workers[0],
                "model": DEFAULT_MODEL,
                "effort": DEFAULT_EFFORT,
            },
        )
        try:
            worker = h.workers.get(assignment.worker)
        except WorkerNotAvailable as exc:
            raise fail(str(exc)) from exc
        timeout = (
            getattr(getattr(h.config.workers, assignment.worker, None), "timeout_s", 0)
            or DEFAULT_TIMEOUT_S
        )
        design_dir(ctx.root).mkdir(parents=True, exist_ok=True)
        job = WorkerJob(
            role="designer",
            prompt=prompt,
            workdir=ctx.root,
            log_path=design_dir(ctx.root) / "designer.log",
            model=assignment.model,
            effort=assignment.effort,
            timeout_s=timeout,
            output_schema=ROLE_SCHEMAS["designer"],
            attempt=1,
        )
        output = asyncio.run(worker.run(job))
        doc = output.parsed
        if output.result.status != "success" or not isinstance(doc, DesignDoc):
            raise fail(
                f"the designer produced no design: {output.result.summary or output.result.status}"
            )
        tokens = dict(system.tokens) if system is not None else dict(STARTER_TOKENS)
        markdown = _merge_screens(
            system.markdown if system is not None else STARTER_MARKDOWN,
            _screens_section(doc, system),
        )
        version = save_design_system(ctx.root, markdown, tokens)
        console.print(
            f'Design system v{version} from the designer\'s "{escape(doc.title)}": '
            f"{len(doc.screens)} screen(s). The previous system is kept as v{version - 1}."
            if version > 1
            else f'Design system v{version} from the designer\'s "{escape(doc.title)}": '
            f"{len(doc.screens)} screen(s)."
        )
