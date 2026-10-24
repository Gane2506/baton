"""Entry points: check a command, a read path, or a write path against a context."""

from __future__ import annotations

import shlex

from .deny import always_denied
from .paths import (
    _boundary,
    _check_boundary_ctx,
    _require_within_boundary,
    resolve,
    sensitive,
    within,
)
from .read import _FILTER_CHECKS, READ_ONLY_COMMANDS
from .shape import _name, _operands, _split, split_pipeline
from .types import Decision, PolicyContext, _Denied
from .write import MEDIUM_COMMANDS, SUDO_COMMANDS, _writable, _writes

SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox", "env", "xargs"}
    | {"python", "python3", "perl", "ruby", "node", "php", "lua", "sudo", "su", "doas"}
)


def check_command(text: str, ctx: PolicyContext) -> Decision:
    """Whether `text` may run on a server with `ctx`, and the exact command to send.

    `unrestricted` (explicit opt-in in config) skips every check: the command is passed
    through as written. A medium server with a project boundary additionally requires the
    workdir inside the folder and every operand of every pipeline segment inside it.
    """
    if ctx.access == "unrestricted":
        return Decision(True, "unrestricted access: no policy checks", text.strip(), writes=True)
    try:
        boundary = _boundary(ctx)
        if boundary is not None:
            _check_boundary_ctx(ctx, boundary)
        segments = [_split(s) for s in split_pipeline(text.strip())]
        for argv in segments:
            inner = argv
            while inner and _name(inner[0]) == "sudo":
                inner = [a for a in inner[1:] if a != "-n"] if len(inner) > 1 else []
            reason = always_denied(inner, ctx) if inner else None
            if reason:
                return Decision.deny(f"always blocked: {reason}")
        if len(segments) == 2:
            # A leading `sudo` is not a shell: strip it before this check so the
            # filter table below (not this branch) refuses `df | sudo grep x`,
            # with the accurate "a pipe may only feed ..." reason.
            second = segments[1]
            inner = second[1:] if _name(second[0]) == "sudo" else second
            if inner and _name(inner[0]) in SHELLS:
                return Decision.deny(
                    "always blocked: piping into a shell or interpreter (curl | sh)"
                )
        first = segments[0]
        sudo = _name(first[0]) == "sudo"
        if sudo:
            first = _strip_sudo(first, ctx)
        table = READ_ONLY_COMMANDS if ctx.access == "basic" else MEDIUM_COMMANDS
        cmd = _name(first[0])
        if first[0] != cmd:
            raise _Denied(f"{first[0]}: call commands by name, not by path")
        checker = table.get(cmd)
        if checker is None:
            raise _Denied(f"{cmd!r} is not allowed at {ctx.access} access")
        if (replaced := checker(list(first[1:]), ctx)) is not None:
            first = [first[0], *replaced]
        parts = [shlex.join((["sudo", "-n"] if sudo else []) + first)]
        if len(segments) == 2:
            pipe = segments[1]
            filt = _FILTER_CHECKS.get(pipe[0])
            if filt is None:
                raise _Denied("a pipe may only feed grep, head, tail, wc or sort")
            if (replaced := filt(list(pipe[1:]), ctx)) is not None:
                pipe = [pipe[0], *replaced]
            parts.append(shlex.join(pipe))
        if boundary is not None:
            # Every operand must stay inside the project folder: this covers the read
            # commands (whose operands are paths) as well as the write commands (already
            # checked by their checkers through `_writable`). `..` escapes and absolute
            # paths outside are refused; unresolvable paths fail closed.
            swept = [first, pipe] if len(segments) == 2 else [first]
            for argv in swept:
                for operand in _operands(argv[1:]):
                    _require_within_boundary(operand, ctx, boundary)
    except _Denied as exc:
        return Decision.deny(str(exc))
    writes = _writes(first)
    return Decision(True, "allowed", " | ".join(parts), writes=writes)


def _strip_sudo(argv: list[str], ctx: PolicyContext) -> list[str]:
    if ctx.access == "basic":
        raise _Denied("sudo is not allowed at basic access")
    rest = argv[1:]
    if rest[:1] == ["-n"]:
        rest = rest[1:]
    if not rest or rest[0].startswith("-"):
        raise _Denied("sudo: no options (jev always runs `sudo -n COMMAND`)")
    if _name(rest[0]) not in SUDO_COMMANDS:
        raise _Denied("sudo is only allowed for package installs and systemctl")
    return rest


def check_read_path(value: str, ctx: PolicyContext) -> Decision:
    """Reading a file (any level): anywhere except credential files.

    `unrestricted` skips every check. A medium server with a project boundary only allows
    reads inside the folder; the caller re-checks the server-resolved path with
    `within_boundary`, so a symlink pointing out is a boundary violation.
    """
    if ctx.access == "unrestricted":
        full = resolve(value, ctx.workdir)
        if full is None:
            return Decision.deny(f"{value}: use an absolute path (or one relative to the workdir)")
        return Decision(True, "unrestricted access: no policy checks", path=full)
    try:
        full = resolve(value, ctx.workdir)
        if full is None:
            raise _Denied(f"{value}: use an absolute path (or one relative to the workdir)")
        if problem := sensitive(full):
            raise _Denied(problem)
        if (boundary := _boundary(ctx)) is not None:
            _check_boundary_ctx(ctx, boundary)
            if not within(full, (boundary,)):
                raise _Denied(f"{full} is outside the project folder ({boundary})")
    except _Denied as exc:
        return Decision.deny(str(exc))
    return Decision(True, "allowed", path=full)


def check_write_path(value: str, ctx: PolicyContext) -> Decision:
    """Writing or uploading a file: medium only, inside the write roots, never credentials.

    `unrestricted` skips every check. A medium server with a project boundary only allows
    writes inside the folder (the boundary replaces the write roots as the write scope);
    the caller re-checks the server-resolved path with `within_boundary`, so a symlink
    pointing out is a boundary violation.
    """
    if ctx.access == "unrestricted":
        full = resolve(value, ctx.workdir)
        if full is None:
            return Decision.deny(f"{value}: use an absolute path (or one relative to the workdir)")
        return Decision(True, "unrestricted access: no policy checks", path=full, writes=True)
    if ctx.access != "medium":
        return Decision.deny("writing files needs medium access")
    if any("authorized_keys" in p for p in (value,)):
        return Decision.deny("always blocked: editing ~/.ssh/authorized_keys")
    try:
        full = _writable(value, ctx, strictly=True)
    except _Denied as exc:
        return Decision.deny(str(exc))
    return Decision(True, "allowed", path=full, writes=True)
