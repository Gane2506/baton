"""Command shape: one line, no shell syntax, split and re-quoted words."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Sequence

from .types import _Denied

MAX_COMMAND_CHARS = 4000


def split_pipeline(text: str) -> list[str]:
    """Split `text` at its unquoted pipes after checking it holds no other shell syntax."""
    if len(text) > MAX_COMMAND_CHARS:
        raise _Denied(f"command longer than {MAX_COMMAND_CHARS} characters")
    if "\x00" in text:
        raise _Denied("NUL characters are not allowed")
    quote: str | None = None
    pipes: list[int] = []
    i = 0
    while i < len(text):
        char = text[i]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\":
                i += 1
            elif char == '"':
                quote = None
            elif char in "`$":
                raise _Denied("no variables or command substitution (`$`, backticks)")
        elif char == "\\":
            i += 1
        elif char in "'\"":
            quote = char
        elif char in "\n\r":
            raise _Denied("one command per call (no newlines)")
        elif char in "`$":
            raise _Denied("no variables or command substitution (`$`, backticks)")
        elif char in ";&":
            raise _Denied("one command per call (no `;`, `&`, `&&`)")
        elif char in "<>":
            raise _Denied("no redirects (`<`, `>`); use the write_file tool to write files")
        elif char in "()":
            raise _Denied("no subshells (`(`, `)`)")
        elif char == "|":
            if text[i + 1 : i + 2] == "|":
                raise _Denied("one command per call (no `||`)")
            pipes.append(i)
        i += 1
    if quote is not None:
        raise _Denied("unbalanced quotes")
    if len(pipes) > 1:
        raise _Denied("at most one pipe, into grep, head, tail, wc or sort")
    if not pipes:
        return [text]
    return [text[: pipes[0]], text[pipes[0] + 1 :]]


def _split(segment: str) -> list[str]:
    try:
        argv = shlex.split(segment)
    except ValueError as exc:
        raise _Denied(f"cannot parse the command: {exc}") from exc
    if not argv:
        raise _Denied("empty command")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        raise _Denied("no environment assignments (`NAME=value cmd`)")
    return argv


def _name(word: str) -> str:
    return posixpath.basename(word)


def _options(args: Sequence[str]) -> list[str]:
    # `"--"` is the end-of-options separator, not an option: excluding it lets the
    # safe `rm -- -dashfile` idiom through (`_operands` already handles it).
    return [a for a in args if a.startswith("-") and a not in ("-", "--")]


def _operands(args: Sequence[str]) -> list[str]:
    out: list[str] = []
    after_dashes = False
    for arg in args:
        if arg == "--" and not after_dashes:
            after_dashes = True
        elif after_dashes or not arg.startswith("-") or arg == "-":
            out.append(arg)
    return out


def _flag_set(args: Sequence[str]) -> set[str]:
    """Short options split into letters (`-rf` -> {-r, -f}) plus long options as written."""
    flags: set[str] = set()
    for opt in _options(args):
        if opt.startswith("--"):
            flags.add(opt.split("=", 1)[0])
        else:
            flags.update(f"-{c}" for c in opt[1:])
    return flags
