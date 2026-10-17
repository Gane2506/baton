"""Lint: every CLI command's help must render cleanly.

Typer renders help through Rich markup, so an unescaped config-table reference
like `[quality]` is silently swallowed and shows up as a double space
("overrides  level."). This walks the whole command tree, renders every option
help and command docstring through Rich exactly the way `--help` does, and
fails on double spaces or leftover `{...}` placeholders.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import cast

import typer
from click import Command, Group, Option
from rich.console import Console
from typer.testing import CliRunner

from jev_agent.cli.app import app

_UNREPLACED = re.compile(r"\{[^{}]*\}")


def _render(text: str) -> str:
    """What the user sees: the help string through Rich markup, like `--help`."""
    console = Console(width=300, color_system=None)
    with console.capture() as cap:
        console.print(text, markup=True, end="")
    return cap.get()


def _walk(
    command: Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[Command, tuple[str, ...]]]:
    yield command, path
    if isinstance(command, Group):
        for name, sub in command.commands.items():
            yield from _walk(sub, (*path, name))


def _help_texts(command: Command) -> Iterator[tuple[str, str]]:
    """`(help text, where it came from)` for a command and all its params."""
    if command.help:
        yield command.help, f"command `{command.name}`"
    for param in command.params:
        if isinstance(param, Option) and param.help:
            yield param.help, f"`{command.name}` option `{param.name}`"


def _commands() -> list[tuple[Command, tuple[str, ...]]]:
    return list(_walk(cast(Command, typer.main.get_command(app))))


def test_all_help_texts_render_without_double_spaces() -> None:
    problems: list[str] = []
    for command, _ in _commands():
        for text, where in _help_texts(command):
            rendered = _render(text)
            if "  " in rendered:
                problems.append(f"{where} renders {rendered!r}")
            if _UNREPLACED.search(text):
                problems.append(f"{where} has an unreplaced placeholder: {text!r}")
    assert not problems, "\n".join(problems)


def test_every_command_help_exits_zero() -> None:
    runner = CliRunner()
    failures: list[str] = []
    for _command, path in _commands():
        result = runner.invoke(app, [*path, "--help"])
        if result.exit_code != 0:
            failures.append(f"`jev {' '.join(path)}` --help exits {result.exit_code}")
    assert not failures, "\n".join(failures)
