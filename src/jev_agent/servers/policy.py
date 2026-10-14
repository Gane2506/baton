"""Access policy for server commands and file operations (docs/servers.md "Access levels").

Every command an agent sends is checked here before anything reaches `ssh`:

1. **Shape.** One line; no `;`, `&`, `&&`, `||`, redirects (`<`, `>`), subshells (`(`, `)`),
   backticks, `$` (variables and `$(...)`), or more than one pipe. Quotes must balance. The
   text is then split with `shlex` and every word is re-quoted (`shlex.join`) for the remote
   shell, so nothing the agent wrote is ever interpreted by a shell there (no globbing either).
2. **Always denied**, at every level: formatting disks, `dd of=/dev/*`, shutdown/reboot,
   user/group/password/sudoers changes, firewall changes, recursive chmod/chown on `/`,
   `rm` on `/` or recursive/forced `rm` outside `allowed_paths`, `crontab -r`, `kill -9 1`,
   anything touching `authorized_keys`, `git push --force`, and piping into a shell or
   interpreter (`curl ... | sh`).
3. **basic**: an allowlist of read-only commands (`READ_ONLY_COMMANDS`), each with its
   dangerous options refused (`find -exec`, `sort -o`, `tail -f`, ...), optionally piped into
   one read-only filter (`grep`, `head`, `tail`, `wc`, `sort`). No `sudo`. Reads of credential
   files (`~/.ssh`, private keys, `.env`, `/etc/shadow`, ...) are refused: by name, and for
   recursive `grep` (only inside workdir/`allowed_paths`, never `-R`) by credential-name
   excludes jev adds; nothing that prints environments (`docker inspect`, `systemctl show`,
   `ps e`). Every `git` runs with hooks and fsmonitor turned off.
4. **medium**: basic, plus `mkdir`/`cp`/`mv`/`rm`/`touch` inside the write roots
   (`allowed_paths`, default `[workdir]`), `git add|commit|pull|fetch|checkout|switch` in a
   working copy inside them, package installs (`apt-get install -y`, `pip install`,
   `npm install|ci`, the pinned Playwright browser install), `systemctl
   restart|reload|start|stop UNIT` (not ssh) and `docker compose up -d|down|restart|pull`.
   `sudo` is accepted only in front of package installs and `systemctl`, and always runs as
   `sudo -n` (never prompts). Nothing is ever written under a `.git` directory (hooks and
   config run code); recursive `cp` only copies from inside the write roots. Medium still
   means code execution as the server account: package installs run the packages' own code
   and a `pip install -r` file's own options are not checked.
5. **unrestricted**: explicit opt-in only (`unrestricted_acknowledged = true` in the server
   config, so it is never granted by default, by typo or by omission). No policy checks at
   all: any command is passed through as written (`writes=True`), any path is allowed. The
   controls are the acknowledgment plus the prominent audit-log line the service layer
   writes when the tier is granted.

A medium server may also set `project_folder` (`PolicyContext.project_boundary`): the agent
may then read, write and run commands only inside that folder, nothing outside it. The
boundary is enforced lexically here — `..` escapes and absolute paths outside are refused,
and an unset or out-of-boundary workdir fails closed — and the file tools re-check the
server-resolved path against it (`within_boundary`), so a symlink pointing out of the
folder counts as a boundary violation.

`check_command`, `check_read_path` and `check_write_path` return a `Decision`; nothing here
runs anything.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from jev_agent.config.servers_schema import AccessLevel

MAX_COMMAND_CHARS = 4000
SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox", "env", "xargs"}
    | {"python", "python3", "perl", "ruby", "node", "php", "lua", "sudo", "su", "doas"}
)
FILTERS = frozenset({"grep", "egrep", "fgrep", "head", "tail", "wc", "sort"})
# The Playwright install a medium server may run (servers/browser.py pins the version).
BROWSER_INSTALL_PREFIX = ("npx", "-y")


@dataclass(frozen=True)
class PolicyContext:
    """What the policy needs to know about one server."""

    access: AccessLevel
    workdir: str | None = None
    write_roots: tuple[str, ...] = ()
    # The exact `npx -y playwright@V install --with-deps chromium` argv allowed in medium.
    browser_install: tuple[str, ...] = ()
    # Folder-scoping for medium (from `servers.<name>.project_folder`): reads, writes and
    # command operands must stay inside it; it replaces `write_roots` as the write scope.
    # None: no boundary (the default).
    project_boundary: str | None = None

    def __post_init__(self) -> None:
        if self.project_boundary is not None:
            object.__setattr__(self, "project_boundary", _normalize_boundary(self.project_boundary))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    command: str = ""  # the re-quoted command to run remotely (empty when denied)
    writes: bool = False  # it may change the server (medium)
    path: str = ""  # for file operations: the resolved absolute path

    @classmethod
    def deny(cls, reason: str) -> Decision:
        return cls(False, reason)


class _Denied(Exception):
    pass


# ---- paths -------------------------------------------------------------------------------

_KEY_NAMES = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?$|\.(pem|key|p12|pfx|jks|keystore)$")
_SECRET_FILES = frozenset(
    {".env", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "credentials"}
    | {"shadow", "gshadow", "shadow-", "gshadow-", "master.key", "secrets.yml", "environ"}
)
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker", "sudoers.d"})


def resolve(value: str, workdir: str | None) -> str | None:
    """`value` as a normalized absolute path (relative to `workdir`); None if unknowable
    (a `~` path, or a relative path without a workdir)."""
    if not value or "\x00" in value or "\n" in value:
        return None
    if value.startswith("/"):
        return posixpath.normpath(value) if value != "/" else "/"
    if value.startswith("~") or workdir is None:
        return None
    return posixpath.normpath(posixpath.join(workdir, value))


def within(value: str, roots: Sequence[str], *, strictly: bool = False) -> bool:
    """`value` is one of `roots` (unless `strictly`) or inside one."""
    for root in roots:
        if value == root and not strictly:
            return True
        if value.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _normalize_boundary(value: str) -> str:
    """`value` as a normalized absolute POSIX path; ValueError when it is not one."""
    if not value.startswith("/") or "\n" in value or "\x00" in value:
        raise ValueError(f"project boundary {value!r} must be an absolute path")
    return posixpath.normpath(value) if value != "/" else "/"


def _boundary(ctx: PolicyContext) -> str | None:
    """The project-folder boundary constraining this server (medium only), if any."""
    if ctx.access != "medium" or not ctx.project_boundary:
        return None
    return ctx.project_boundary


def _check_boundary_ctx(ctx: PolicyContext, boundary: str) -> None:
    """Fail closed when the workdir is unset or outside the boundary: relative paths and
    operand-less commands would otherwise run outside the folder."""
    if ctx.workdir is None or not within(ctx.workdir, (boundary,)):
        raise _Denied(f"the project folder is {boundary}: set the server's workdir inside it")


def _require_within_boundary(value: str, ctx: PolicyContext, boundary: str) -> str:
    """`value` resolved against the workdir; _Denied when unresolvable or outside."""
    full = resolve(value, ctx.workdir)
    if full is None:
        raise _Denied(f"{value}: use an absolute path (or one relative to the workdir)")
    if not within(full, (boundary,)):
        raise _Denied(f"{full} is outside the project folder ({boundary})")
    return full


def within_boundary(value: str, ctx: PolicyContext) -> bool:
    """A server-resolved absolute path is inside the project boundary.

    For the caller to re-check a path after resolving symlinks on the server
    (`realpath -m`): a symlink pointing out of the folder is a boundary violation.
    True when no boundary applies (unset, or the access is not medium).
    """
    boundary = _boundary(ctx)
    return boundary is None or within(value, (boundary,))


def sensitive(value: str) -> str | None:
    """Why reading or writing `value` is refused (credentials), or None."""
    parts = [p for p in value.split("/") if p]
    if not parts:
        return None
    name = parts[-1]
    if any(p in _SECRET_DIRS for p in parts) or "authorized_keys" in value:
        return f"{value}: credential directories (~/.ssh, ~/.aws, ...) are off limits"
    if "sudoers" in value or value.startswith("/etc/shadow") or value.startswith("/etc/gshadow"):
        return f"{value}: system credential files are off limits"
    if _KEY_NAMES.search(name) or name in _SECRET_FILES or name.startswith(".env."):
        return f"{value}: files that usually hold keys or passwords are off limits"
    return None


def _sensitive_args(args: Sequence[str]) -> None:
    for arg in args:
        for piece in (arg, arg.partition("=")[2]):
            if piece and (problem := sensitive(piece)):
                raise _Denied(problem)


def _in_git_dir(value: str) -> bool:
    """`value` is a `.git` directory or file, or inside one (any letter case)."""
    return any(p.lower() == ".git" for p in value.split("/"))


# What jev adds to a recursive grep so it skips credential files (base names) and folders.
_KEY_GLOBS = ("*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore")
_KEY_FILES = tuple(
    f"id_{kind}{suffix}" for kind in ("rsa", "dsa", "ecdsa", "ed25519") for suffix in ("", "_sk")
)
GREP_EXCLUDES: tuple[str, ...] = (
    *(f"--exclude={name}" for name in (*sorted(_SECRET_FILES), ".env.*", "authorized_keys")),
    *(f"--exclude={name}" for name in (*_KEY_GLOBS, *_KEY_FILES)),
    *(f"--exclude-dir={name}" for name in sorted(_SECRET_DIRS)),
)


# ---- shape -------------------------------------------------------------------------------


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
    return [a for a in args if a.startswith("-") and a != "-"]


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


# ---- always denied -----------------------------------------------------------------------

_SHUTDOWN = frozenset({"shutdown", "reboot", "halt", "poweroff"})
_USER_ADMIN = frozenset(
    {"useradd", "userdel", "usermod", "adduser", "deluser", "addgroup", "delgroup"}
    | {"groupadd", "groupdel", "groupmod", "passwd", "chpasswd", "chage", "gpasswd"}
    | {"newusers", "visudo", "vipw", "vigr", "chsh", "chfn"}
)
_FIREWALL = frozenset(
    {"iptables", "ip6tables", "iptables-restore", "ip6tables-restore", "iptables-legacy"}
    | {"ufw", "nft", "firewall-cmd", "ebtables", "arptables"}
)
_DISK = frozenset({"mke2fs", "wipefs", "fdisk", "sfdisk", "parted", "mkswap", "shred"})
_SYSTEMCTL_POWER = frozenset(
    {"poweroff", "reboot", "halt", "kexec", "emergency", "rescue", "suspend", "hibernate"}
    | {"soft-reboot", "default", "isolate"}
)


def always_denied(argv: Sequence[str], ctx: PolicyContext) -> str | None:
    """Why `argv` is blocked at every access level, or None."""
    cmd, args = _name(argv[0]), list(argv[1:])
    flags = _flag_set(args)
    if any("authorized_keys" in a for a in argv):
        return "editing ~/.ssh/authorized_keys"
    if any("/etc/sudoers" in a for a in argv):
        return "changing sudoers"
    if cmd.startswith("mkfs") or cmd in _DISK:
        return "formatting or wiping disks"
    if cmd == "dd" and any(a.startswith("of=/dev/") for a in args):
        return "dd onto a device"
    if cmd in _SHUTDOWN or (cmd in {"init", "telinit"} and any(a in {"0", "6"} for a in args)):
        return "shutting down or rebooting the server"
    if cmd == "systemctl" and _SYSTEMCTL_POWER & set(args):
        return "shutting down or rebooting the server"
    if cmd in _USER_ADMIN:
        return "changing users, groups, passwords or sudoers"
    if cmd in _FIREWALL:
        return "changing the firewall"
    if cmd in {"chmod", "chown", "chgrp"} and flags & {"-R", "--recursive"}:
        targets = [resolve(a, ctx.workdir) for a in _operands(args)]
        if "/" in targets or any(a in {"/", "/*"} for a in args):
            return "recursive chmod/chown on /"
    if cmd == "rm":
        if "--no-preserve-root" in args or any(
            resolve(a, ctx.workdir) == "/" or a in {"/*", "/."} for a in _operands(args)
        ):
            return "rm on /"
        if flags & {"-r", "-R", "-f", "--recursive", "--force"}:
            for target in _operands(args):
                full = resolve(target, ctx.workdir)
                if full is None or not within(full, ctx.write_roots, strictly=True):
                    return "rm -rf outside allowed_paths"
    if cmd == "crontab" and "-r" in flags:
        return "crontab -r"
    if cmd in {"kill", "pkill", "killall"} and "1" in _operands(args):
        return "killing init (pid 1)"
    if cmd == "git" and "push" in args:
        forced = {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror"}
        if forced & flags or any(a.startswith("+") for a in _operands(args)):
            return "git push --force"
    return None


# ---- basic: read-only commands -----------------------------------------------------------

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


# ---- medium: limited writes --------------------------------------------------------------


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
            if arg in {"-e", "--editable"}:
                _writable(target, ctx)
            else:
                _sensitive_args([target])
                if (boundary := _boundary(ctx)) is not None:
                    _require_within_boundary(target, ctx, boundary)
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
_SSH_UNITS = frozenset({"ssh", "sshd", "ssh.service", "sshd.service", "ssh.socket"})


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
        _sensitive_args([args[i + 1]])
        if args[i] in {"-f", "--file"} and (boundary := _boundary(ctx)) is not None:
            _require_within_boundary(args[i + 1], ctx, boundary)
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


# ---- entry points ------------------------------------------------------------------------


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
        if len(segments) == 2 and _name(segments[1][0]) in SHELLS:
            return Decision.deny("always blocked: piping into a shell or interpreter (curl | sh)")
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
