"""Medium access: limited writes, package installs, service and compose control."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .paths import (
    _boundary,
    _check_boundary_ctx,
    _in_git_dir,
    _require_within_boundary,
    _sensitive_args,
    resolve,
    sensitive,
    within,
)
from .read import (
    _GIT_READ,
    GIT_SAFE_CONFIG,
    READ_ONLY_COMMANDS,
    Checker,
    _docker_read,
    _git_args,
    _git_read,
    _refuse,
    _systemctl_read,
)
from .shape import _flag_set, _name, _operands
from .types import PolicyContext, _Denied

# The Playwright install a medium server may run (servers/browser.py pins the version).
BROWSER_INSTALL_PREFIX = ("npx", "-y")


def _need_roots(ctx: PolicyContext) -> None:
    if not ctx.write_roots:
        raise _Denied("this server has no allowed_paths (or workdir) to write to")


def _writable(value: str, ctx: PolicyContext, *, strictly: bool = False) -> str:
    """`value` resolved and inside the write scope, or `_Denied`.

    The scope is the project folder when one is set, else `allowed_paths`;
    every other check (resolvable, not a credential file, not under `.git`,
    inside the scope) is the same either way, so it runs once.
    """
    if (boundary := _boundary(ctx)) is not None:
        # A project folder is set: it replaces the write roots as the write scope.
        _check_boundary_ctx(ctx, boundary)
        roots: Sequence[str] = (boundary,)
        scope = f"the project folder ({boundary})"
    else:
        _need_roots(ctx)
        roots = ctx.write_roots
        scope = f"allowed_paths ({', '.join(ctx.write_roots)})"
    full = resolve(value, ctx.workdir)
    if full is None:
        raise _Denied(f"{value}: use an absolute path (or one relative to the workdir)")
    if problem := sensitive(full):
        raise _Denied(problem)
    if _in_git_dir(full):
        raise _Denied(f"{full}: nothing is written under .git (hooks and config run code)")
    if not within(full, roots, strictly=strictly):
        raise _Denied(f"{full} is outside {scope}")
    return full


def _only_flags(args: Sequence[str], allowed: set[str], what: str) -> None:
    extra = sorted(_flag_set(args) - allowed)
    if extra:
        raise _Denied(f"{what}: option {', '.join(extra)} is not allowed")


def _mkdir(args: list[str], ctx: PolicyContext) -> None:
    _only_flags(args, {"-p", "-v", "--parents", "--verbose"}, "mkdir")
    targets = _operands(args)
    if not targets:
        raise _Denied("mkdir: name a directory")
    for target in targets:
        _writable(target, ctx)


def _touch(args: list[str], ctx: PolicyContext) -> None:
    _only_flags(args, {"-c", "--no-create"}, "touch")
    for target in _operands(args) or [""]:
        _writable(target, ctx)


_CP_RECURSIVE = frozenset({"-r", "-R", "-a", "--recursive"})


def _cp_scope(ctx: PolicyContext, boundary: str | None) -> tuple[Sequence[str], str]:
    """The roots a recursive `cp` may read from, and how the denial names them."""
    if boundary is not None:
        return (boundary,), f"the project folder ({boundary})"
    return ctx.write_roots, f"allowed_paths ({', '.join(ctx.write_roots)})"


def _check_cp_source(
    source: str,
    ctx: PolicyContext,
    *,
    recursive_roots: Sequence[str] | None,
    scope: str,
) -> None:
    """One `cp` source: never from `.git`, and — for a recursive copy, which can
    sweep up credential files — strictly inside the scope (`None`: no scope check)."""
    full = resolve(source, ctx.workdir)
    if full is not None and _in_git_dir(full):
        raise _Denied(f"{full}: nothing is copied from or into .git")
    if recursive_roots is not None and (
        full is None or not within(full, recursive_roots, strictly=True)
    ):
        raise _Denied(
            f"cp: recursive cp only from inside {scope}; {source} may hold credential files"
        )


def _cp(args: list[str], ctx: PolicyContext) -> None:
    _only_flags(args, {"-r", "-R", "-a", "-p", "-v", "-f", "-n", "-u", "--recursive"}, "cp")
    operands = _operands(args)
    if len(operands) < 2:
        raise _Denied("cp: SOURCE... DEST")
    _sensitive_args(operands[:-1])
    boundary = _boundary(ctx)
    if boundary is not None:
        _check_boundary_ctx(ctx, boundary)
    if _flag_set(args) & _CP_RECURSIVE:
        roots, scope = _cp_scope(ctx, boundary)
    else:
        roots, scope = None, ""
    for source in operands[:-1]:
        _check_cp_source(source, ctx, recursive_roots=roots, scope=scope)
    _writable(operands[-1], ctx)


def _mv(args: list[str], ctx: PolicyContext) -> None:
    _only_flags(args, {"-f", "-n", "-v", "-u"}, "mv")
    operands = _operands(args)
    if len(operands) < 2:
        raise _Denied("mv: SOURCE... DEST")
    for target in operands:
        _writable(target, ctx, strictly=True)


def _rm(args: list[str], ctx: PolicyContext) -> None:
    flags = {"-r", "-R", "-f", "-v", "-d", "--recursive", "--force", "--dir", "--verbose"}
    _only_flags(args, flags, "rm")
    targets = _operands(args)
    if not targets:
        raise _Denied("rm: name what to remove")
    for target in targets:
        _writable(target, ctx, strictly=True)


_GIT_WRITE = frozenset({"add", "commit", "pull", "fetch", "checkout", "switch"})


def _git_write(args: list[str], ctx: PolicyContext) -> list[str]:
    sub, rest, directory = _git_args(args)
    if sub in _GIT_READ:
        return _git_read(args, ctx)
    if sub not in _GIT_WRITE:
        allowed = ", ".join(sorted(_GIT_READ | _GIT_WRITE))
        raise _Denied(f"git: {sub or 'no subcommand'} is not allowed (allowed: {allowed})")
    where = directory if directory is not None else ctx.workdir
    if where is None:
        raise _Denied("git: set the server's workdir or pass -C DIR")
    _writable(where, ctx)
    if sub in {"pull", "fetch"} and any(a.startswith("--upload-pack") for a in rest):
        raise _Denied("git: --upload-pack is not allowed")
    _sensitive_args(rest)
    return [*GIT_SAFE_CONFIG, *args]


_APT_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.:~=-]*$")


_PIP_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~*]*$")


_NPM_PACKAGE = re.compile(
    r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[A-Za-z0-9._^~<>=*-]+)?$"
)


def _apt_install(args: list[str], words: list[str]) -> None:
    """`apt-get install -y PACKAGE...`: package names only, never interactive."""
    _only_flags(args, {"-y", "-q", "--yes", "--quiet", "--no-install-recommends"}, "apt-get")
    if not _flag_set(args) & {"-y", "--yes"}:
        raise _Denied("apt-get install: add -y (nobody can answer a prompt)")
    packages = words[1:]
    if not packages or not all(_APT_PACKAGE.match(p) for p in packages):
        raise _Denied("apt-get install: package names only (no paths or URLs)")


def _apt(args: list[str], ctx: PolicyContext) -> None:
    words = _operands(args)
    if words == ["update"]:
        _only_flags(args, {"-q", "-y", "--yes", "--quiet"}, "apt-get update")
        return
    if not words or words[0] != "install":
        raise _Denied("apt-get: only `apt-get update` and `apt-get install -y PACKAGE...`")
    _apt_install(args, words)


def _pip_target(target: str, ctx: PolicyContext) -> None:
    """One `-r`/`-e` operand of `pip install`: a credential file is never read,
    and the file itself must live inside the write scope (both run with the
    installer's privileges).

    Residual risk, documented not fixed: the *contents* of a `-r` file are
    unchecked — a requirements file may carry its own options (`--index-url`,
    `--extra-index-url`, `-e git+...`, nested `-r`), so a scoped file can
    still redirect installs. It cannot be checked here: this checker runs on
    the dashboard host, while the file lives on the server (the command is
    only shipped there afterwards), so its bytes are unobservable at check
    time. The file's *location* is checked twice instead — lexically here,
    and again against its server-resolved path (`service.run_command`,
    issue #204 I-3). A service-layer read (the transport has `read`) could
    vet the contents before running, but it would still be TOCTOU: the file
    can change between that read and pip's read, so it would be an advisory
    check, not a guarantee.
    """
    _sensitive_args([target])
    if (boundary := _boundary(ctx)) is not None:
        _require_within_boundary(target, ctx, boundary)
    else:
        _writable(target, ctx)


_PIP_TARGETS = frozenset({"-r", "--requirement", "-e", "--editable"})


def _pip_package(arg: str) -> bool:
    """True when `arg` is a bare package-name operand (options return False);
    anything else that is not an option is denied — never a path or URL."""
    if arg.startswith("-"):
        return False
    if not _PIP_PACKAGE.match(arg) or "/" in arg or ":" in arg:
        raise _Denied(f"pip install: {arg!r} is not a package name (no paths or URLs)")
    return True


def _pip_install(args: list[str], ctx: PolicyContext) -> None:
    allowed = {"-U", "-q", "--upgrade", "--user", "--quiet", "--break-system-packages"}
    allowed |= {"--no-cache-dir", "-r", "--requirement", "-e", "--editable"}
    _only_flags(args, allowed, "pip install")
    i, packages = 0, 0
    while i < len(args):
        arg = args[i]
        if arg in _PIP_TARGETS:
            if i + 1 >= len(args):
                raise _Denied(f"pip install: {arg} needs a path")
            _pip_target(args[i + 1], ctx)
            i, packages = i + 2, packages + 1
            continue
        packages += _pip_package(arg)
        i += 1
    if not packages:
        raise _Denied("pip install: name a package or -r requirements file")


def _pip(args: list[str], ctx: PolicyContext) -> None:
    if not args or args[0] != "install":
        raise _Denied("pip: only `pip install`")
    _pip_install(args[1:], ctx)


def _python(args: list[str], ctx: PolicyContext) -> None:
    if args[:2] != ["-m", "pip"]:
        raise _Denied("python: only `python3 -m pip install ...`")
    _pip(args[2:], ctx)


def _npm(args: list[str], ctx: PolicyContext) -> None:
    if not args or args[0] not in {"install", "i", "ci"}:
        raise _Denied("npm: only `npm install` and `npm ci`")
    allowed = {"--omit", "--production", "--no-audit", "--no-fund", "--ignore-scripts"}
    allowed |= {"--save-dev", "-D", "--save", "-S", "--save-exact", "-E", "--legacy-peer-deps"}
    _only_flags(args[1:], allowed, "npm")
    if ctx.workdir is None:
        raise _Denied("npm: set the server's workdir (npm installs into it)")
    _writable(ctx.workdir, ctx)
    for name in _operands(args[1:]):
        if not _NPM_PACKAGE.match(name):
            raise _Denied(f"npm: {name!r} is not a package name (no paths, URLs or git)")


_UNIT = re.compile(r"^[A-Za-z0-9@_.:\\-]+$")


_SSH_UNITS = frozenset({"ssh", "sshd", "ssh.service", "sshd.service", "ssh.socket", "sshd.socket"})


# The verbs that change service state (everything else goes to the read-only checker).
_SYSTEMCTL_WRITE_VERBS = frozenset({"restart", "reload", "start", "stop", "try-restart"})


def _systemctl_write(args: list[str], verbs: list[str]) -> None:
    """`systemctl restart|reload|start|stop|try-restart UNIT...`: named units only,
    never ssh itself (restarting or stopping it would lock the agent out)."""
    _only_flags(args, {"--no-block", "-q", "--quiet"}, "systemctl")
    units = verbs[1:]
    if not units or not all(_UNIT.match(u) and not u.startswith("-") for u in units):
        raise _Denied("systemctl: name the unit(s)")
    if _SSH_UNITS & set(units):
        raise _Denied("systemctl: ssh itself is never restarted or stopped (lock-out risk)")


def _systemctl(args: list[str], ctx: PolicyContext) -> None:
    verbs = [a for a in args if not a.startswith("-")]
    if verbs and verbs[0] in _SYSTEMCTL_WRITE_VERBS:
        _systemctl_write(args, verbs)
        return
    _systemctl_read(args, ctx)


def _docker(args: list[str], ctx: PolicyContext) -> None:
    if args[:1] == ["compose"]:
        _compose(args[1:], ctx)
        return
    _docker_read(args, ctx)


def _compose_options(args: list[str], ctx: PolicyContext) -> int:
    """Consume the leading `-f FILE` / `-p NAME` options; returns the index of the
    subcommand. `-p` is refused outright: it selects which project's containers
    `down`/`restart` act on, so *any* value lets the agent hit another project's
    containers. A compose file must live inside the write scope: it decides
    which images, volumes and env the agent brings up (lexical only — the
    service re-checks the server-resolved path before running, so a symlink
    out is refused)."""
    i = 0
    while i < len(args) and args[i] in {"-f", "--file", "-p", "--project-name"}:
        if i + 1 >= len(args):
            raise _Denied(f"docker compose: {args[i]} needs a value")
        if args[i] in {"-p", "--project-name"}:
            # Disallowed, not charset-restricted: no charset could prevent
            # selecting another project. The default (directory-derived)
            # project name covers the agent's own.
            raise _Denied("docker compose: -p/--project-name is not allowed")
        _sensitive_args([args[i + 1]])
        if (boundary := _boundary(ctx)) is not None:
            _require_within_boundary(args[i + 1], ctx, boundary)
        else:
            _writable(args[i + 1], ctx)
        i += 2
    return i


def _compose_subcommand(sub: str, sub_args: list[str]) -> None:
    """The write subcommands of `docker compose` (ps/logs are read-only, above)."""
    if sub in {"ps", "logs"}:
        _refuse(sub_args, {"-f", "--follow"}, "docker compose logs")
        return
    flags = _flag_set(sub_args)
    if sub == "up":
        if not flags & {"-d", "--detach"}:
            raise _Denied("docker compose up: add -d (detached)")
        allowed = {"-d", "--detach", "--build", "--remove-orphans", "--force-recreate"}
        _only_flags(sub_args, allowed | {"--no-deps", "--pull", "--wait"}, "docker compose up")
    elif sub == "down":
        if flags & {"-v", "--volumes", "--rmi"}:
            raise _Denied("docker compose down: removing volumes or images is not allowed")
        _only_flags(sub_args, {"--remove-orphans", "-t", "--timeout"}, "docker compose down")
    elif sub in {"restart", "pull"}:
        _only_flags(sub_args, {"-q", "--quiet", "-t", "--timeout"}, f"docker compose {sub}")
    else:
        raise _Denied("docker compose: up -d, down, restart, pull, ps and logs only")


def _compose(args: list[str], ctx: PolicyContext) -> None:
    rest = args[_compose_options(args, ctx) :]
    if not rest:
        raise _Denied("docker compose: name a subcommand")
    _compose_subcommand(rest[0], rest[1:])


def _npx(args: list[str], ctx: PolicyContext) -> None:
    if not ctx.browser_install or ["npx", *args] != list(ctx.browser_install):
        raise _Denied("npx: only the pinned browser install (`jev browser install --server`)")


MEDIUM_COMMANDS: dict[str, Checker] = {
    **READ_ONLY_COMMANDS,
    "mkdir": _mkdir,
    "touch": _touch,
    "cp": _cp,
    "mv": _mv,
    "rm": _rm,
    "git": _git_write,
    "apt-get": _apt,
    "apt": _apt,
    "pip": _pip,
    "pip3": _pip,
    "python3": _python,
    "python": _python,
    "npm": _npm,
    "systemctl": _systemctl,
    "docker": _docker,
    "docker-compose": _compose,
    "npx": _npx,
}


# Commands `sudo -n` may prefix (medium only).
SUDO_COMMANDS = frozenset({"apt-get", "apt", "pip", "pip3", "python3", "npm", "systemctl", "npx"})


_WRITE_COMMANDS = frozenset(MEDIUM_COMMANDS) - frozenset(READ_ONLY_COMMANDS)


def _writes(argv: Sequence[str]) -> bool:
    cmd = _name(argv[0])
    if cmd in _WRITE_COMMANDS:
        return True
    words = [a for a in argv[1:] if not a.startswith("-")]
    if cmd == "git":
        return bool(set(words) & _GIT_WRITE)
    if cmd == "systemctl":
        return bool(words) and words[0] in _SYSTEMCTL_WRITE_VERBS
    if cmd == "docker":
        return words[:1] == ["compose"] and not set(words[1:2]) & {"ps", "logs"}
    return False
