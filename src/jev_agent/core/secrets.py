"""`~/.jev/secrets.env`: API keys the dashboard and CLI load at startup.

`jev` reads keys only from the process environment. The dashboard Connections page
lets the user save a key once; this module keeps it in `~/.jev/secrets.env` (mode
0600, directory 0700) and injects it into `os.environ` at startup — real environment
variables always win over the file.

The file format is deliberately trivial: one `NAME=value` per line, split on the
first `=`. Blank lines and lines starting with `#` are ignored. No new dependencies.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from jev_agent.app.errors import JevError
from jev_agent.runlog.redact import REDACT_NAMES_ENV

__all__ = [
    "SecretsError",
    "default_path",
    "delete_secret",
    "load_secrets",
    "loaded_names",
    "read_secrets",
    "write_secret",
]

FILE_ENV = "JEV_SECRETS_FILE"  # test/CI override for the secrets file location

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _InjectionTracker:
    """Explicit owner of the names this process injected from the secrets file.

    P9: run state needs an explicit owner, not a module-global mutable. The
    singleton `_loaded` below is the only reader/writer of this set.
    `delete_secret` only pops a name from `os.environ` when it is tracked here,
    so a real environment variable is never removed by deleting a secrets-file
    line with the same name.
    """

    def __init__(self) -> None:
        self._names: set[str] = set()

    def add(self, name: str) -> None:
        self._names.add(name)

    def discard(self, name: str) -> None:
        self._names.discard(name)

    def clear(self) -> None:
        self._names.clear()

    def __contains__(self, name: object) -> bool:
        return name in self._names

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


_loaded = _InjectionTracker()
"""Names this process injected from the secrets file (see `_InjectionTracker`)."""


class SecretsError(JevError):
    """The secrets file is unusable or a key name/value is invalid."""


def default_path() -> Path:
    """Where the secrets file lives. Overridable via `JEV_SECRETS_FILE` (tests)."""
    override = os.environ.get(FILE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jev" / "secrets.env"


def _warn(message: str) -> None:
    print(f"jev: {message}", file=sys.stderr)


def _read_lines(path: Path) -> dict[str, str]:
    """Parse the file, keeping first occurrence order. Never raises on content."""
    secrets: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.match(name):
            _warn(f"ignoring malformed line in {path}: {name!r}")
            continue
        secrets.setdefault(name, value)
    return secrets


def _check_permissions(path: Path) -> bool:
    """True when the file is private (0600-ish): no group/other read or write."""
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        _warn(
            f"refusing to use {path}: permissions {mode:03o} are too open "
            "(run `chmod 600` on it, or delete it)"
        )
        return False
    return True


def _register_redact_names(names: list[str]) -> None:
    """Append `names` to `JEV_REDACT_NAMES` in the process environment, so `redact()`
    covers secrets-file values regardless of what they are called. `with_redact_names`
    preserves the existing list, so the names propagate to every worker's log redaction."""
    names = [name for name in names if name]
    if not names:
        return
    existing = [n for n in os.environ.get(REDACT_NAMES_ENV, "").split(",") if n]
    os.environ[REDACT_NAMES_ENV] = ",".join(dict.fromkeys([*existing, *names]))


def load_secrets(path: Path | None = None) -> None:
    """Inject the secrets file into `os.environ`. Real environment wins.

    Never raises: a missing file is the normal case, and a malformed or too-open
    file only warns, so a bad secrets file can never break a CLI invocation.
    Every injected name is registered for log redaction (see `_register_redact_names`).
    """
    file = path or default_path()
    if not file.is_file():
        return
    try:
        if not _check_permissions(file):
            return
        secrets = _read_lines(file)
    except OSError as exc:
        _warn(f"could not read {file}: {exc}")
        return
    injected = []
    for name, value in secrets.items():
        if name not in os.environ:
            os.environ[name] = value
            _loaded.add(name)
            injected.append(name)
    _register_redact_names(injected)


def loaded_names() -> frozenset[str]:
    """Names this process injected from the secrets file (not real environment)."""
    return frozenset(_loaded)


def read_secrets(path: Path | None = None) -> dict[str, str]:
    """The secrets file's names, in file order. Values are returned; callers must
    never render or log them — only the names are safe to display."""
    file = path or default_path()
    if not file.is_file():
        return {}
    if not _check_permissions(file):
        return {}
    return _read_lines(file)


def _write_all(path: Path, secrets: dict[str, str]) -> None:
    """Atomic rewrite: temp file in the same directory, chmod 0600, then rename."""
    existed = path.parent.is_dir()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(path.parent, 0o700)
    elif path.parent.stat().st_mode & 0o777 & 0o077:
        _warn(f"{path.parent} is group/other-readable; consider `chmod 700 {path.parent}`")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for name, value in secrets.items():
                handle.write(f"{name}={value}\n")
            # Flush to the file before the rename: a crash between write and rename must
            # not leave a truncated secrets file behind.
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _check_name(name: str) -> None:
    if not _NAME.match(name):
        raise SecretsError(f"invalid key name: {name!r} (use A-Z, 0-9 and _)")


def write_secret(name: str, value: str, path: Path | None = None) -> None:
    """Save (or replace) a secret, apply it to this process, record its origin.

    Refuses when the existing file has looser-than-0600 permissions: fixing that
    is a deliberate user action, not something jev should do silently.
    """
    _check_name(name)
    if not value:
        raise SecretsError("value must not be empty")
    if "\n" in value or "\r" in value:
        raise SecretsError("value must not contain line breaks")
    file = path or default_path()
    if file.is_file() and not _check_permissions(file):
        raise SecretsError(f"refusing to write to {file}: permissions are too open")
    secrets = _read_lines(file) if file.is_file() else {}
    if name in secrets:
        del secrets[name]  # re-append, so the file reads in edit order
    secrets[name] = value
    _write_all(file, secrets)
    os.environ[name] = value
    _loaded.add(name)
    _register_redact_names([name])


def delete_secret(name: str, path: Path | None = None) -> None:
    """Remove a secrets-file line. Pops the process variable only when this
    process set it from the file — never a real environment variable."""
    _check_name(name)
    file = path or default_path()
    if file.is_file() and not _check_permissions(file):
        raise SecretsError(f"refusing to write to {file}: permissions are too open")
    secrets = _read_lines(file) if file.is_file() else {}
    if name not in secrets:
        raise SecretsError(f"{name} is not in the secrets file")
    del secrets[name]
    _write_all(file, secrets)
    if name in _loaded:
        os.environ.pop(name, None)
        _loaded.discard(name)
