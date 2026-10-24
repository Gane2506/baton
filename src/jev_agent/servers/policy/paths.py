"""Paths: resolution, the project-folder boundary, credential-file refusal."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

from .types import PolicyContext, _Denied

_KEY_NAMES = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?$|\.(pem|key|p12|pfx|jks|keystore)$")


_SECRET_FILES = frozenset(
    {".env", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "credentials"}
    | {"shadow", "gshadow", "shadow-", "gshadow-", "master.key", "secrets.yml", "environ"}
    | {"database.yml"}
)


# A file whose stem is one of these is a credential file whatever its extension
# (`credentials.txt`, `secrets.env`, `secret.yaml` — issue #224 fixed the same
# class in `core/workspace.py`; the policy denylist had the gap). A stem that
# merely *contains* the word (`my-credentials.txt`) is not matched: the file
# must be the secret.
_SECRET_STEMS = frozenset({"credentials", "secret", "secrets"})


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
    stem = name.split(".", 1)[0]
    if stem in _SECRET_STEMS:
        return f"{value}: files that usually hold keys or passwords are off limits"
    if name == "config" and _in_git_dir(value):
        # `.git/config` routinely holds credentials (`https://user:TOKEN@host`,
        # `extraHeader = Authorization: ...`); read it through `git config` instead.
        return f"{value}: git config may hold credentials"
    if len(parts) == 3 and parts[0] == "proc" and parts[2] == "cmdline":
        # Process command lines routinely embed secrets (`mysql -p…`, tokens in flags).
        return f"{value}: process command lines may hold secrets"
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
