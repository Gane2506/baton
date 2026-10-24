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
    if (boundary := _boundary(ctx)) is not None:
        # A project folder is set: it replaces the write roots as the write scope.
        _check_boundary_ctx(ctx, boundary)
        full = resolve(value, ctx.workdir)
        if full is None:
            raise _Denied(f"{value}: use an absolute path (or one relative to the workdir)")
        if problem := sensitive(full):
            raise _Denied(problem)
        if _in_git_dir(full):
            raise _Denied(f"{full}: nothing is written under .git (hooks and config run code)")
        if not within(full, (boundary,), strictly=strictly):
            raise _Denied(f"{full} is outside the project folder ({boundary})")
        return full
    _need_roots(ctx)
    full = resolve(value, ctx.workdir)
    if full is None:
        raise _Denied(f"{value}: use an absolute path (or one relative to the workdir)")
    if problem := sensitive(full):
        raise _Denied(problem)
    if _in_git_dir(full):
        raise _Denied(f"{full}: nothing is written under .git (hooks and config run code)")
    if not within(full, ctx.write_roots, strictly=strictly):
        raise _Denied(f"{full} is outside allowed_paths ({', '.join(ctx.write_roots)})")
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


def _cp(args: list[str], ctx: PolicyContext) -> None:
    _only_flags(args, {"-r", "-R", "-a", "-p", "-v", "-f", "-n", "-u", "--recursive"}, "cp")
    operands = _operands(args)
    if len(operands) < 2:
        raise _Denied("cp: SOURCE... DEST")
    _sensitive_args(operands[:-1])
    boundary = _boundary(ctx)
    if boundary is not None:
        _check_boundary_ctx(ctx, boundary)
    for source in operands[:-1]:
        full = resolve(source, ctx.workdir)
        if full is not None and _in_git_dir(full):
            raise _Denied(f"{full}: nothing is copied from or into .git")
        if _flag_set(args) & {"-r", "-R", "-a", "--recursive"}:
            roots = (boundary,) if boundary is not None else ctx.write_roots
            scope = (
                f"the project folder ({boundary})"
                if boundary is not None
                else f"allowed_paths ({', '.join(ctx.write_roots)})"
            )
            if full is None or not within(full, roots, strictly=True):
                raise _Denied(
                    f"cp: recursive cp only from inside {scope}; {source} may hold credential files"
                )
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


def _apt(args: list[str], ctx: PolicyContext) -> None:
    words = _operands(args)
    if words == ["update"]:
        _only_flags(args, {"-q", "-y", "--yes", "--quiet"}, "apt-get update")
        return
    if not words or words[0] != "install":
        raise _Denied("apt-get: only `apt-get update` and `apt-get install -y PACKAGE...`")
    _only_flags(args, {"-y", "-q", "--yes", "--quiet", "--no-install-recommends"}, "apt-get")
    if not _flag_set(args) & {"-y", "--yes"}:
        raise _Denied("apt-get install: add -y (nobody can answer a prompt)")
    packages = words[1:]
    if not packages or not all(_APT_PACKAGE.match(p) for p in packages):
        raise _Denied("apt-get install: package names only (no paths or URLs)")


def _pip_install(args: list[str], ctx: PolicyContext) -> None:
    allowed = {"-U", "-q", "--upgrade", "--user", "--quiet", "--break-system-packages"}
    allowed |= {"--no-cache-dir", "-r", "--requirement", "-e", "--editable"}
    _only_flags(args, allowed, "pip install")
    i, packages = 0, 0
    while i < len(args):
        arg = args[i]
        if arg in {"-r", "--requirement", "-e", "--editable"}:
            if i + 1 >= len(args):
                raise _Denied(f"pip install: {arg} needs a path")
            target = args[i + 1]
            _sensitive_args([target])
            if (boundary := _boundary(ctx)) is not None:
                _require_within_boundary(target, ctx, boundary)
            else:
                # A `-r` file's own options are unchecked (documented residual risk),
                # but its *location* gets the same scope check as `-e`: both run
                # with the installer's privileges.
                _writable(target, ctx)
            i, packages = i + 2, packages + 1
            continue
        if not arg.startswith("-"):
            if not _PIP_PACKAGE.match(arg) or "/" in arg or ":" in arg:
                raise _Denied(f"pip install: {arg!r} is not a package name (no paths or URLs)")
            packages += 1
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


def _systemctl(args: list[str], ctx: PolicyContext) -> None:
    verbs = [a for a in args if not a.startswith("-")]
    if verbs and verbs[0] in {"restart", "reload", "start", "stop", "try-restart"}:
        _only_flags(args, {"--no-block", "-q", "--quiet"}, "systemctl")
        units = verbs[1:]
        if not units or not all(_UNIT.match(u) and not u.startswith("-") for u in units):
            raise _Denied("systemctl: name the unit(s)")
        if _SSH_UNITS & set(units):
            raise _Denied("systemctl: ssh itself is never restarted or stopped (lock-out risk)")
        return
    _systemctl_read(args, ctx)


def _docker(args: list[str], ctx: PolicyContext) -> None:
    if args[:1] == ["compose"]:
        _compose(args[1:], ctx)
        return
    _docker_read(args, ctx)


def _compose(args: list[str], ctx: PolicyContext) -> None:
    i = 0
    while i < len(args) and args[i] in {"-f", "--file", "-p", "--project-name"}:
        if i + 1 >= len(args):
            raise _Denied(f"docker compose: {args[i]} needs a value")
        if args[i] in {"-p", "--project-name"}:
            # Disallowed, not charset-restricted: `-p` selects which project's
            # containers `down`/`restart` act on, so *any* value lets the agent hit
            # another project's containers, and no charset could prevent that.
            # The default (directory-derived) project name covers the agent's own.
            raise _Denied("docker compose: -p/--project-name is not allowed")
        _sensitive_args([args[i + 1]])
        if (boundary := _boundary(ctx)) is not None:
            _require_within_boundary(args[i + 1], ctx, boundary)
        else:
            # The compose file decides which images, volumes and env the agent
            # brings up: it must live inside the agent's write scope, like `-e`.
            # Lexical only — the service re-checks the server-resolved path
            # (`command_scope_paths`) before running, so a symlink out is refused.
            _writable(args[i + 1], ctx)
        i += 2
    rest = args[i:]
    if not rest:
        raise _Denied("docker compose: name a subcommand")
    sub, sub_args = rest[0], rest[1:]
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
        return bool(words) and words[0] in {"restart", "reload", "start", "stop", "try-restart"}
    if cmd == "docker":
        return words[:1] == ["compose"] and not set(words[1:2]) & {"ps", "logs"}
    return False
