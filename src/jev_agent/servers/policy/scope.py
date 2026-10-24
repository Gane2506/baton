"""Scope re-checks: operands whose server-resolved path must stay in scope."""

from __future__ import annotations

from dataclasses import dataclass

from .paths import _boundary, resolve, within
from .shape import _flag_set, _name, _operands, _split, split_pipeline
from .types import PolicyContext, _Denied


@dataclass(frozen=True)
class ScopedPath:
    """A scope-sensitive operand of an already-checked command: the file whose *contents*
    the command will act on, so a symlink on the server could redirect it outside the
    scope the lexical check approved. `path` is the operand resolved against the workdir
    ("" when unresolvable — fail closed); `strictly` mirrors the lexical check (a
    recursive `cp` source must be strictly inside, a compose/`pip -r` file may be a
    root itself)."""

    path: str
    strictly: bool
    kind: str


def scope_violation(real: str, ctx: PolicyContext, *, strictly: bool) -> str | None:
    """Why server-resolved `real` is outside the command's scope, or None.

    The same scope the lexical checks use: the project boundary when one is set (it
    replaces the write roots), else the write roots."""
    if (boundary := _boundary(ctx)) is not None:
        if within(real, (boundary,), strictly=strictly):
            return None
        return f"{real} is outside the project folder ({boundary})"
    if not ctx.write_roots:
        return f"{real} is outside allowed_paths (none set)"
    if within(real, ctx.write_roots, strictly=strictly):
        return None
    return f"{real} is outside allowed_paths ({', '.join(ctx.write_roots)})"


def command_scope_paths(command: str, ctx: PolicyContext) -> list[ScopedPath]:
    """Scope-sensitive operands of `command` (an already-checked, re-quoted command).

    The caller's service layer resolves each path on the server and re-checks it with
    `scope_violation` (plus `sensitive`) before running, because the lexical checks in
    `check_command` cannot see server symlinks. Covers the operands whose contents the
    command executes or copies: `docker compose -f` files (they choose images, volumes
    and env), recursive `cp` sources (they may hold credential files), and `pip -r`/`-e`
    files (they run with the installer's privileges). Anything else — including a
    compose run with no `-f`, whose file is the workdir's own — needs no re-check.
    Unparseable input yields no paths: `check_command` has already refused it, so the
    service never runs it.
    """
    try:
        segments = [_split(s) for s in split_pipeline(command.strip())]
    except _Denied:
        return []
    if not segments or not segments[0]:
        return []
    argv = segments[0]
    if _name(argv[0]) == "sudo":
        argv = [a for a in argv[1:] if a != "-n"]
    if not argv:
        return []
    cmd, args = _name(argv[0]), list(argv[1:])
    paths: list[ScopedPath] = []

    def add(value: str, strictly: bool, kind: str) -> None:
        paths.append(ScopedPath(resolve(value, ctx.workdir) or "", strictly, kind))

    if cmd == "docker" and args[:1] == ["compose"]:
        i = 1  # skip the `compose` subcommand word itself
        while i < len(args) - 1 and args[i] in {"-f", "--file"}:
            add(args[i + 1], False, "compose file")
            i += 2
    elif cmd == "docker-compose":
        i = 0
        while i < len(args) - 1 and args[i] in {"-f", "--file"}:
            add(args[i + 1], False, "compose file")
            i += 2
    elif cmd == "cp" and _flag_set(args) & {"-r", "-R", "-a", "--recursive"}:
        for source in _operands(args)[:-1]:
            add(source, True, "recursive cp source")
    elif cmd in {"pip", "pip3"} or (cmd in {"python", "python3"} and args[:2] == ["-m", "pip"]):
        rest = args[2:] if cmd in {"python", "python3"} else args
        if rest[:1] == ["install"]:
            i = 1
            while i < len(rest):
                arg = rest[i]
                name, eq, value = arg.partition("=")
                if name in {"-r", "--requirement", "-e", "--editable"}:
                    if eq:
                        add(value, False, "pip requirement file")
                    elif i + 1 < len(rest):
                        add(rest[i + 1], False, "pip requirement file")
                        i += 1
                i += 1
    return paths
