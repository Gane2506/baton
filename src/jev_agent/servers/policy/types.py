"""Policy types: the server context, the allow/deny decision, the deny signal."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from jev_agent.config.servers_schema import AccessLevel


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


def _normalize_boundary(value: str) -> str:
    """`value` as a normalized absolute POSIX path; ValueError when it is not one."""
    if not value.startswith("/") or "\n" in value or "\x00" in value:
        raise ValueError(f"project boundary {value!r} must be an absolute path")
    return posixpath.normpath(value) if value != "/" else "/"
