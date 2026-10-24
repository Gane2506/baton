"""Basic access: read-only commands, each with its dangerous options refused."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from .paths import GREP_EXCLUDES, _boundary, _check_boundary_ctx, _sensitive_args, resolve, within
from .shape import _flag_set
from .types import PolicyContext, _Denied

FILTERS = frozenset({"grep", "egrep", "fgrep", "head", "tail", "wc", "sort"})


# A checker raises `_Denied`, or returns the arguments to run instead (None: run as written).
Checker = Callable[[list[str], PolicyContext], list[str] | None]


def _refuse(args: Sequence[str], bad: set[str], what: str) -> None:
    hit = sorted(_flag_set(args) & bad)
    if hit:
        raise _Denied(f"{what}: {', '.join(hit)} is not allowed")


def _plain(args: list[str], ctx: PolicyContext) -> None:
    _sensitive_args(args)


_GREP_VALUE_SHORT = "efmABCdD"  # short options whose value may be the next word


_GREP_VALUE_LONG = frozenset(
    {"--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context"}
    | {"--directories", "--devices", "--exclude", "--include", "--exclude-dir"}
    | {"--exclude-from", "--label", "--binary-files", "--color", "--colour"}
)


def _grep_words(args: list[str]) -> tuple[set[str], list[str], dict[str, str]]:
    """(flag letters/long options, operands, option values) of a grep command line."""
    flags: set[str] = set()
    operands: list[str] = []
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            operands += args[i + 1 :]
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            flags.add(name)
            if not eq and name in _GREP_VALUE_LONG and i + 1 < len(args):
                value, i = args[i + 1], i + 1
            values[name] = value
        elif arg.startswith("-") and arg != "-":
            for j, letter in enumerate(arg[1:], start=1):
                flags.add(f"-{letter}")
                if letter in _GREP_VALUE_SHORT:
                    value = arg[j + 1 :]
                    if not value and i + 1 < len(args):
                        value, i = args[i + 1], i + 1
                    values[f"-{letter}"] = value
                    break
        else:
            operands.append(arg)
        i += 1
    return flags, operands, values


def _grep(args: list[str], ctx: PolicyContext) -> list[str] | None:
    """A recursive grep reads every file under its folders, so the credential rule cannot rest
    on the names written: refuse `-R` (follows symlinks), allow recursion only inside the
    workdir/allowed_paths and add excludes for credential files (issue #44)."""
    _sensitive_args(args)
    flags, operands, values = _grep_words(args)
    if flags & {"-R", "--dereference-recursive"}:
        raise _Denied("grep: -R (follows symlinks) is not allowed; use -r")
    recurse = "recurse" in (values.get("-d"), values.get("--directories"))
    if not (recurse or flags & {"-r", "--recursive"}):
        return None
    pattern_given = bool(flags & {"-e", "-f", "--regexp", "--file"})
    paths = operands if pattern_given else operands[1:]
    roots: tuple[str, ...]
    if (boundary := _boundary(ctx)) is not None:
        _check_boundary_ctx(ctx, boundary)
        roots = (boundary,)
        scope = "the project folder"
    else:
        roots = tuple(r for r in (ctx.workdir, *ctx.write_roots) if r)
        scope = "the workdir/allowed_paths"
    for target in paths or ["."]:
        full = resolve(target, ctx.workdir)
        if full is None or not within(full, roots):
            where = ", ".join(roots) or "no workdir set"
            raise _Denied(f"grep: recursive grep only inside {scope} ({where})")
    return [*GREP_EXCLUDES, *args]


_PS_VALUE_OPTIONS = frozenset({"-o", "-O", "-p", "-u", "-U", "-g", "-G", "-t", "-C", "-q"})
_PS_VALUE_OPTIONS |= {"-s", "--format", "--pid", "--ppid", "--user", "--sort", "--cols"}


def _ps(args: list[str], ctx: PolicyContext) -> None:
    """`ps` without BSD-style `e` (it prints each process's environment)."""
    previous = ""
    for arg in args:
        if previous not in _PS_VALUE_OPTIONS and re.fullmatch(r"[A-Za-z]+", arg) and "e" in arg:
            raise _Denied("ps: the `e` option (process environments) is not allowed")
        previous = arg
    _sensitive_args(args)


def _tail(args: list[str], ctx: PolicyContext) -> None:
    _refuse(args, {"-f", "-F", "--follow", "--retry"}, "tail")
    _sensitive_args(args)


def _sort(args: list[str], ctx: PolicyContext) -> None:
    _refuse(args, {"-o", "--output", "--compress-program"}, "sort")
    _sensitive_args(args)


def _file(args: list[str], ctx: PolicyContext) -> None:
    _refuse(args, {"-C", "--compile"}, "file")
    _sensitive_args(args)


def _find(args: list[str], ctx: PolicyContext) -> None:
    actions = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0"}
    actions |= {"-fprintf", "-fls"}
    hit = sorted(set(args) & actions)
    if hit:
        raise _Denied(f"find: {', '.join(hit)} is not allowed (read-only)")
    _sensitive_args(args)


def _top(args: list[str], ctx: PolicyContext) -> None:
    if args not in (["-b", "-n1"], ["-b", "-n", "1"], ["-bn1"], ["-n1", "-b"]):
        raise _Denied("top: only `top -b -n1` (one batch snapshot)")


def _systemctl_read(args: list[str], ctx: PolicyContext) -> None:
    verbs = [a for a in args if not a.startswith("-")]
    allowed = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers"}
    allowed |= {"list-unit-files", "cat"}  # not `show`: it prints Environment= (issue #44)
    if not verbs or verbs[0] not in allowed:
        raise _Denied(f"systemctl: read-only verbs only ({', '.join(sorted(allowed))})")


def _journalctl(args: list[str], ctx: PolicyContext) -> None:
    bad = {"-f", "--follow", "--rotate", "--flush", "--sync", "--relinquish-var"}
    bad |= {"--smart-relinquish-var", "--setup-keys", "--update-catalog", "--vacuum-size"}
    bad |= {"--vacuum-time", "--vacuum-files"}
    _refuse(args, bad, "journalctl")


def _docker_read(args: list[str], ctx: PolicyContext) -> None:
    words = [a for a in args if not a.startswith("-")]
    if words[:1] in (["ps"], ["images"]):  # not `inspect`: it prints Config.Env (issue #44)
        return
    if words[:1] == ["logs"] or words[:2] in (["compose", "logs"], ["compose", "ps"]):
        _refuse(args, {"-f", "--follow"}, "docker logs")
        return
    raise _Denied("docker: only ps, logs, images, compose ps and compose logs")


_GIT_READ = frozenset({"status", "log", "diff", "show", "branch", "remote", "rev-parse"})


# Put in front of every git command: no hooks and no fsmonitor command run (issue #46).
GIT_SAFE_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def _git_args(args: list[str]) -> tuple[str | None, list[str], str | None]:
    """(subcommand, its args, `-C` directory) for `git [-C DIR] [--no-pager] SUB ...`."""
    directory: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-C" and i + 1 < len(args):
            directory, i = args[i + 1], i + 2
        elif arg == "--no-pager":
            i += 1
        elif arg.startswith("-"):
            raise _Denied(f"git: option {arg} before the subcommand is not allowed")
        else:
            return arg, args[i + 1 :], directory
    return None, [], directory


def _git_read(args: list[str], ctx: PolicyContext) -> list[str]:
    sub, rest, _ = _git_args(args)
    if sub not in _GIT_READ:
        raise _Denied(f"git: read-only subcommands only ({', '.join(sorted(_GIT_READ))})")
    if any(a.startswith("--output") or a == "--ext-diff" for a in rest):
        raise _Denied("git: --output and --ext-diff are not allowed")
    if "--no-index" in rest:
        raise _Denied("git diff: --no-index (comparing any two folders) is not allowed")
    if sub == "branch" and _flag_set(rest) & {"-d", "-D", "-m", "-M", "-c", "-C", "--delete"}:
        raise _Denied("git branch: listing only")
    if sub == "remote" and [a for a in rest if not a.startswith("-")]:
        raise _Denied("git remote: listing only (`git remote -v`)")
    _sensitive_args(rest)
    _sensitive_args([a.partition(":")[2] for a in rest])  # `git show REV:path`
    return [*GIT_SAFE_CONFIG, *args]


READ_ONLY_COMMANDS: dict[str, Checker] = {
    "ls": _plain,
    "cat": _plain,
    "head": _plain,
    "tail": _tail,
    "grep": _grep,
    "egrep": _grep,
    "fgrep": _grep,
    "find": _find,
    "df": _plain,
    "du": _plain,
    "free": _plain,
    "uptime": _plain,
    "ps": _ps,
    "top": _top,
    "systemctl": _systemctl_read,
    "journalctl": _journalctl,
    "docker": _docker_read,
    "git": _git_read,
    "uname": _plain,
    "whoami": _plain,
    "id": _plain,
    "wc": _plain,
    "sort": _sort,
    "stat": _plain,
    "file": _file,
    "pwd": _plain,
    "hostname": _plain,
}


_FILTER_CHECKS: dict[str, Checker] = {name: READ_ONLY_COMMANDS[name] for name in FILTERS}
