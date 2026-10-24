"""`jev design ...`: the repo's design system (Claude-Styles-like, core/design_system.py).

`show` prints the current system, `init` scaffolds a starter one, `new` runs an
interactive wizard that saves a complete system (tokens, components, logo, favicons)
into the library, and `generate` invokes
the `designer` role worker with the repo brief to produce or refresh it. Everything lives
in `<repo>/.jev/design/` and is versioned on every save.

The library subcommands (`list`, `use`, `clone`, `delete`, `export`) manage the
user-level design system library at `~/.jev/design-systems/` (core/design_library.py):
named, versioned systems that any repo can adopt with `jev design use <name>`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table

from jev_agent.app.design_reverse import extract_with_designer
from jev_agent.app.errors import JevError
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import NO_WORKERS_MESSAGE, cli_context, fail, repo_context
from jev_agent.core.design_kit import (
    APP_TYPES,
    STACKS,
    design_system_markdown,
    palette_for_vibe,
    tokens_for_app_type,
)
from jev_agent.core.design_library import (
    delete_library_system,
    export_tokens_css,
    export_tokens_tailwind,
    export_tokens_w3c,
    library_dir,
    library_system_path,
    list_library,
    load_library_system,
    save_library_system,
    validate_library_name,
)
from jev_agent.core.design_reverse import (
    evidence_to_brief,
    fetch_url_evidence,
    is_url_target,
    palette_from_png,
    suggest_library_name,
)
from jev_agent.core.design_system import (
    META_JSON,
    STARTER_MARKDOWN,
    STARTER_TOKENS,
    DesignSystem,
    _write_atomic,
    design_dir,
    get_design_system,
    render_for_prompt,
    save_design_system,
)
from jev_agent.core.harness import DEFAULT_TIMEOUT_S, Halt
from jev_agent.core.logo import generate_mark, mark_template, mark_to_png, mark_to_svg
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
        try:
            output = asyncio.run(worker.run(job))
        except Exception as exc:  # CLI boundary: report the failure, don't traceback
            # Workers map agent failures to a result status, but a transport
            # failure (subprocess/CLI/API error mid-call, after the user
            # waited on a model call) would otherwise traceback (issue #198).
            # `FakeWorkerCrash` is a BaseException by design and is not caught.
            raise fail(f"the designer worker failed: {exc}") from exc
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


# ---- `design new`: the interactive wizard ----------------------------------------------------


def _ask_app_type() -> str:
    """Step 1: the app type, as a numbered list with the one-line descriptions."""
    console.print("[bold]What are you designing?[/bold]")
    keys = list(APP_TYPES)
    for i, key in enumerate(keys, 1):
        console.print(f"  {i}. [bold]{key}[/bold] — {APP_TYPES[key]}")
    choice = Prompt.ask(
        "App type",
        choices=[str(i) for i in range(1, len(keys) + 1)],
        default="1",
        console=console,
    )
    return keys[int(choice) - 1]


def _ask_stack(app_type: str) -> str:
    """Step 2: the stack. The first option is the recommended default; the stack
    only sets token defaults and export targets — it never scaffolds code."""
    stacks = STACKS[app_type]
    console.print(
        "[bold]Stack[/bold] (sets token defaults and export targets; no code is scaffolded)"
    )
    for i, stack in enumerate(stacks, 1):
        recommended = " [recommended]" if i == 1 else ""
        console.print(f"  {i}. {stack}{recommended}")
    choice = Prompt.ask(
        "Stack",
        choices=[str(i) for i in range(1, len(stacks) + 1)],
        default="1",
        console=console,
    )
    return stacks[int(choice) - 1]


def _ask_library_name() -> str:
    """Step 3: the system name, validated by the library (re-prompt until it passes)."""
    while True:
        raw = Prompt.ask(
            "System name (letters, digits, _ and -, starting with a letter or digit)",
            console=console,
        ).strip()
        try:
            return validate_library_name(raw)
        except ValueError as exc:
            console.print(f"[red]That name won't work:[/red] {escape(str(exc))}")


def _ask_non_empty(prompt: str) -> str:
    """Prompt until the answer is non-empty (used for the product name)."""
    while True:
        answer = Prompt.ask(prompt, console=console).strip()
        if answer:
            return answer
        console.print("[red]Please enter a value.[/red]")


@design_app.command("new")
def new() -> None:
    """Interactive wizard: answer six questions and save a complete design system
    (tokens, starter components, logo mark, favicons) into the design library."""
    if not sys.stdin.isatty():
        raise fail("`jev design new` is interactive: run it in a terminal (stdin is not a TTY).")
    app_type = _ask_app_type()
    stack = _ask_stack(app_type)
    name = _ask_library_name()
    product = _ask_non_empty("Product name")
    vibe = Prompt.ask("Vibe keywords (free text, optional)", default="", console=console).strip()
    theme = Prompt.ask("Theme", choices=["light", "dark", "both"], default="both", console=console)

    palette = palette_for_vibe(vibe)
    tokens = tokens_for_app_type(app_type, palette, theme)
    markdown = design_system_markdown(name, app_type, stack, palette, theme)
    seed = f"{name}:{product}"
    template = mark_template(seed)
    meta_extra: dict[str, object] = {
        "name": name,
        "app_type": app_type,
        "stack": stack,
        "source": "wizard",
        "product_name": product,
        "theme": theme,
        "logo_template": template,
    }
    version = save_library_system(name, markdown, tokens, meta_extra=meta_extra)

    mark = generate_mark(seed, palette)
    system_path = library_system_path(name)
    assets: dict[str, bytes] = {
        "logo.svg": mark_to_svg(mark, 512).encode("utf-8"),
        "favicon.svg": mark_to_svg(mark, 64).encode("utf-8"),
        "favicon-16.png": mark_to_png(mark, 16),
        "favicon-32.png": mark_to_png(mark, 32),
        "favicon-180.png": mark_to_png(mark, 180),
        "favicon-192.png": mark_to_png(mark, 192),
        "favicon-512.png": mark_to_png(mark, 512),
    }
    for filename, data in assets.items():
        (system_path / filename).write_bytes(data)

    created = ["design-system.md", "tokens.json", "meta.json", *assets]
    console.print()
    console.print(f'[bold]"{escape(name)}" saved to the design library[/bold] (v{version}):')
    console.print(f"  {system_path}", markup=False)
    for filename in created:
        console.print(f"  [green]✓[/green] {filename}")
    console.print(
        f"App type: {app_type} · stack: {escape(stack)} · theme: {theme} · mark: {template}"
    )


# ---- the user-level library (~/.jev/design-systems) ------------------------------------------


def _library_meta(name: str) -> dict[str, object]:
    """The library system's meta.json extras, or {} when it cannot be read.

    Corrupt library files are the library's own concern (`load_library_system`
    warns); `list` stays readable even when one system is broken.
    """
    try:
        raw = json.loads((library_system_path(name) / META_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _checked_name(name: str) -> str:
    """`validate_library_name`, raised as a CLI failure instead of a traceback."""
    try:
        return validate_library_name(name)
    except ValueError as exc:
        raise fail(str(exc)) from exc


def _require_system(name: str) -> DesignSystem:
    """Load a library system, or fail when it is missing/corrupt."""
    system = load_library_system(name)
    if system is None:
        raise fail(f'no design system named "{name}" in the library ({library_dir()}).')
    return system


def _meta_extras(meta: dict[str, object]) -> dict[str, object]:
    """Carry the known library meta extras (app_type, stack) into a new meta."""
    extras: dict[str, object] = {}
    for extra_key in ("app_type", "stack"):
        if isinstance(meta.get(extra_key), str):
            extras[extra_key] = meta[extra_key]
    return extras


@design_app.command("list")
def list_systems() -> None:
    """List the user-level design systems in the library."""
    table = Table(title="Design system library")
    table.add_column("name")
    table.add_column("app_type")
    table.add_column("stack")
    table.add_column("version", justify="right")
    table.add_column("updated")
    for name in list_library():
        meta = _library_meta(name)
        table.add_row(
            name,
            str(meta.get("app_type", "-")),
            str(meta.get("stack", "-")),
            str(meta.get("version", "-")),
            str(meta.get("updated_at", "-")),
        )
    console.print(table)


@design_app.command("use")
def use(name: str, repo: RepoOption = Path(".")) -> None:
    """Install a library system into the repo (records `source: library:<name>`)."""
    _checked_name(name)
    system = _require_system(name)
    with repo_context(repo) as ctx:
        meta_extra: dict[str, object] = {
            "source": f"library:{name}",
            **_meta_extras(_library_meta(name)),
        }
        version = save_design_system(
            ctx.root, system.markdown, dict(system.tokens), meta_extra=meta_extra
        )
    console.print(
        f'Installed "{name}" as the repo design system v{version} (source: library:{name}).'
    )


@design_app.command("clone")
def clone(name: str, new_name: str) -> None:
    """Copy a library system to a new name (fresh version history starting at 1)."""
    _checked_name(name)
    _checked_name(new_name)
    system = _require_system(name)
    if library_system_path(new_name).exists():
        raise fail(f'a design system named "{new_name}" already exists in the library.')
    meta_extra: dict[str, object] = {
        "name": new_name,
        "source": f"clone:{name}",
        **_meta_extras(_library_meta(name)),
    }
    version = save_library_system(
        new_name, system.markdown, dict(system.tokens), meta_extra=meta_extra
    )
    console.print(f'Cloned "{name}" to "{new_name}" (library v{version}).')


@design_app.command("delete")
def delete(
    name: str,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Delete without asking for confirmation."),
    ] = False,
) -> None:
    """Delete a library system (confirms on a TTY unless --yes).

    Deletion is never refused for a system a repo installed with `use`: the repo
    holds a full, independent copy in its own `.jev/design/`, so removing the
    library original cannot break any repo.
    """
    _checked_name(name)
    _require_system(name)
    if not yes and sys.stdin.isatty() and not typer.confirm(f'Delete library system "{name}"?'):
        raise typer.Abort()
    delete_library_system(name)
    console.print(f'Deleted "{name}" from the library.')


@design_app.command("export")
def export(
    name: str,
    format: Annotated[
        str,
        typer.Option("--format", help="Export format: w3c, tailwind or css."),
    ] = "w3c",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output file. Defaults to <name>.<format> in cwd."),
    ] = None,
) -> None:
    """Export a library system's tokens for other toolchains."""
    _checked_name(name)
    system = _require_system(name)
    fmt = format.lower()
    if fmt == "w3c":
        text = json.dumps(export_tokens_w3c(system.tokens), indent=2) + "\n"
        default = Path(f"{name}.tokens.json")
    elif fmt == "tailwind":
        text = export_tokens_tailwind(system.tokens)
        default = Path(f"{name}.tailwind.js")
    elif fmt == "css":
        text = export_tokens_css(system.tokens)
        default = Path(f"{name}.css")
    else:
        raise fail(f'unknown format "{format}": choose w3c, tailwind or css.')
    target = out if out is not None else default
    try:
        _write_atomic(target, text)
    except OSError as exc:
        raise fail(f"could not write {target}: {exc}") from exc
    console.print(f"Exported {fmt} tokens to {target}.")


# ---- reverse-engineering an existing product into the library -----------------------------
# The extraction itself lives in `app.design_reverse` (shared with the dashboard);
# the CLI keeps its own evidence gathering and converts `JevError` to `fail()`.


@design_app.command("reverse")
def reverse(
    target: Annotated[
        list[str],
        typer.Argument(help="A website URL (http/https) or one or more .png screenshots."),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Library system name. Defaults to the URL host or image name."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the evidence brief without calling the designer."),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Reverse-engineer a design system from a website or screenshots into the library.

    Evidence (page title, theme color, fonts, CSS colors — or PNG palettes) becomes
    a brief for the designer role, which extracts a `tokens.json` + system markdown.
    Saved as a named library system with `source: reverse:<url>`
    (or `source: reverse:screenshots`).
    """
    if not target:
        raise fail("give a website URL or at least one .png image path.")
    kinds = [is_url_target(item) for item in target]
    if any(kinds) and not all(kinds):
        raise fail("pass either one URL or only .png image paths, not both.")
    from_url = all(kinds)
    if from_url and len(target) > 1:
        raise fail("pass exactly one URL, or one or more .png image paths.")
    images: list[Path] = []
    if not from_url:
        for item in target:
            path = Path(item)
            if path.suffix.lower() != ".png":
                raise fail(f"{item}: only .png screenshots are supported.")
            if not path.is_file():
                raise fail(f"image not found: {item}")
            images.append(path)
    system_name = (
        _checked_name(name) if name is not None else _checked_name(suggest_library_name(target))
    )
    fetch_error: str | None = None
    evidence: dict[str, object]
    if from_url:
        url_evidence = fetch_url_evidence(target[0])
        if isinstance(url_evidence.get("error"), str):
            fetch_error = str(url_evidence["error"])
        evidence = {
            "target": target[0],
            "kind": "url",
            "url": target[0],
            "url_evidence": url_evidence,
            "screenshots": None,
        }
    else:
        shots: list[dict[str, object]] = []
        for path in images:
            try:
                palette = palette_from_png(path)
            except (OSError, ValueError) as exc:
                raise fail(f"could not read {path}: {exc}") from exc
            shots.append({"path": str(path), "palette": palette})
        evidence = {
            "target": ", ".join(target),
            "kind": "screenshots",
            "url": None,
            "url_evidence": None,
            "screenshots": shots,
        }
    brief = evidence_to_brief(evidence)
    if dry_run:
        console.print(brief, markup=False)
        if fetch_error is not None:
            raise fail(fetch_error)
        return
    if fetch_error is not None:
        raise fail(fetch_error)
    with cli_context(repo) as ctx:
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        try:
            tokens, markdown = extract_with_designer(ctx.pipeline.h, ctx.root, brief)
        except JevError as exc:
            raise fail(str(exc)) from exc
        source = f"reverse:{target[0]}" if from_url else "reverse:screenshots"
        version = save_library_system(
            system_name,
            markdown,
            dict(tokens),
            meta_extra={"name": system_name, "source": source, "app_type": "website"},
        )
    console.print(
        f'Reversed "{escape(system_name)}" into the library (v{version}, source: {escape(source)}).'
    )
