"""Config models for servers, the virtual browser and workspace profiles (docs/servers.md).

Kept apart from `schema.py` so the feature reads as one block; `schema.py` imports these and
adds `[servers.*]`, `[browser]` and `[profiles.*]` to `JevConfig`.

- `[servers.<name>]`: a machine reached with the system `ssh` (host or `~/.ssh/config` alias),
  an access level (`basic` read-only, `medium` limited writes, `unrestricted` anything — the
  last only with an explicit acknowledgment) and the paths writes may touch.
  Credentials never go here: keys come from ssh-agent or `identity_file` (a path only).
- `[browser]`: the Playwright MCP server for roles with `browser = true`.
- `[profiles.<name>]`: bundles of servers (with lower-only access overrides), browser, learning
  profile and quality level, picked per run with `--profile`.

`access = "unrestricted"` is opt-in only: it also needs `unrestricted_acknowledged = true`
on the same server, and its activation is written to the audit log (issue #3).
`access = "full"` is still rejected outright.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jev_agent.quality.levels import QualityLevel

AccessLevel = Literal["basic", "medium", "unrestricted"]
ACCESS_LEVELS: tuple[AccessLevel, ...] = ("basic", "medium", "unrestricted")  # lowest first
FULL_ACCESS_MESSAGE = (
    'access = "full" is not an access level; for unrestricted access use '
    'access = "unrestricted" with unrestricted_acknowledged = true (see issue #3)'
)

SERVER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"
WORKSPACE_PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"
NO_PROFILE = "none"  # `--profile none`: no workspace profile, even when a repo default is set
# An ssh destination: a host name, an IP address or a `~/.ssh/config` alias. Never an option.
_HOST = re.compile(r"^[A-Za-z0-9_\[][A-Za-z0-9_.:%\[\]-]{0,252}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# Top-level system directories that may never be an allowed path themselves (sub-paths may).
_SYSTEM_DIRS = frozenset(
    {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin"}
    | {"/sys", "/usr", "/var", "/home", "/opt", "/srv", "/tmp"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def lower_access(*levels: AccessLevel) -> AccessLevel:
    """The lowest of `levels`."""
    return min(levels, key=ACCESS_LEVELS.index)


def _reject_full(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "full":
        raise ValueError(FULL_ACCESS_MESSAGE)
    return value


def normalize_remote_path(value: str) -> str:
    """An absolute POSIX path, normalized (`..` resolved lexically). ValueError otherwise."""
    if not value.startswith("/") or "\n" in value or "\x00" in value:
        raise ValueError(f"{value!r} must be an absolute path on the server (start with `/`)")
    return posixpath.normpath(value) if value != "/" else "/"


class ServerConfig(_Strict):
    """`[servers.<name>]`: one machine agents may reach over SSH (docs/servers.md)."""

    host: str = Field(description="Host name, IP address or ~/.ssh/config alias of the server.")
    user: str | None = Field(default=None, description="SSH user; unset uses your local user name.")
    port: int | None = Field(default=None, ge=1, le=65535, description="SSH port; unset means 22.")
    # A path to a private key file (never the key itself); unset: ssh-agent / ~/.ssh/config.
    identity_file: str | None = Field(
        default=None,
        description=(
            "Path to a private key file (never the key itself); unset: ssh-agent or ~/.ssh/config."
        ),
    )
    access: AccessLevel = Field(
        default="basic",
        description=(
            'What agents may do: "basic" (read-only commands), "medium" (limited writes) '
            'or "unrestricted" (anything; needs unrestricted_acknowledged = true).'
        ),
    )
    unrestricted_acknowledged: bool = Field(
        default=False,
        description=(
            'Explicit acknowledgment that `access = "unrestricted"` is intended. The '
            "unrestricted tier is never granted without this flag set to true; it is "
            "inert for the other access levels."
        ),
    )
    workdir: str | None = Field(
        default=None, description="Directory commands run in, as an absolute path on the server."
    )
    allowed_paths: list[str] = Field(
        default_factory=list,
        description="Where medium-level writes may go; unset means [workdir].",
    )
    project_folder: str | None = Field(
        default=None,
        description=(
            "Folder that scopes medium access to one project: with this set, the agent "
            "may read, write and run commands only inside this folder on the server, "
            "and a symlink pointing out of it counts as a boundary violation. Unset: "
            "the medium scope is allowed_paths (default [workdir])."
        ),
    )
    description: str = Field(
        default="", description="A note to yourself about what this server is."
    )

    @field_validator("access", mode="before")
    @classmethod
    def _no_full(cls, value: Any) -> Any:
        return _reject_full(value)

    @model_validator(mode="after")
    def _check(self) -> ServerConfig:
        if not _HOST.match(self.host):
            raise ValueError(f"host: {self.host!r} is not a host name, address or ssh alias")
        if self.user is not None and not _USER.match(self.user):
            raise ValueError(f"user: {self.user!r} is not a user name")
        if self.identity_file is not None:
            text = self.identity_file
            if "\n" in text or "PRIVATE KEY" in text or "BEGIN" in text:
                raise ValueError(
                    "identity_file must be a path to a key file, never the key itself; "
                    "better still, load the key into ssh-agent"
                )
            if not text.strip() or text.startswith("-"):
                raise ValueError(f"identity_file: {text!r} is not a file path")
        if self.access == "unrestricted" and not self.unrestricted_acknowledged:
            raise ValueError(
                'access = "unrestricted" needs unrestricted_acknowledged = true on the '
                "same server: it lets agents run anything with the server account's "
                "rights, so it is never granted by default, by typo or by omission"
            )
        if self.workdir is not None:
            self.workdir = normalize_remote_path(self.workdir)
        paths = [normalize_remote_path(p) for p in self.allowed_paths]
        for value in paths:
            if value in _SYSTEM_DIRS:
                raise ValueError(
                    f"allowed_paths: {value!r} is too broad; name the directories agents may "
                    "write to (for example /srv/app)"
                )
            if "/.ssh" in value + "/":
                raise ValueError(f"allowed_paths: {value!r}: ~/.ssh is never writable")
        self.allowed_paths = paths
        if self.project_folder is not None:
            folder = normalize_remote_path(self.project_folder)
            if folder in _SYSTEM_DIRS:
                raise ValueError(
                    f"project_folder: {folder!r} is too broad; name the project folder "
                    "agents are scoped to (for example /srv/app/proj)"
                )
            if "/.ssh" in folder + "/":
                raise ValueError(f"project_folder: {folder!r}: ~/.ssh is never a project folder")
            self.project_folder = folder
        return self

    def write_roots(self) -> list[str]:
        """Where medium-level writes may go: `allowed_paths`, else `[workdir]`, else nowhere."""
        if self.allowed_paths:
            return list(self.allowed_paths)
        if self.workdir is not None and self.workdir not in _SYSTEM_DIRS:
            return [self.workdir]
        return []


class BrowserConfig(_Strict):
    """`[browser]`: a headless Chromium driven through the Playwright MCP server."""

    enabled: bool = Field(
        default=False,
        description=(
            "Turn on the virtual browser (headless Chromium via the Playwright MCP server)."
        ),
    )
    headless: bool = Field(default=True, description="Run Chromium without a visible window.")
    # Where `jev browser install` installs Chromium: "local" or a `[servers.<name>]` name.
    install_target: str = Field(
        default="local",
        min_length=1,
        description='Where `jev browser install` puts Chromium: "local" or a [servers.<name>].',
    )


class WorkspaceProfileConfig(_Strict):
    """`[profiles.<name>]`: what a run may reach, picked with `--profile NAME`."""

    servers: list[str] = Field(
        default_factory=list,
        description="Servers a run with this profile may reach, by name from [servers.<name>].",
    )
    access_overrides: dict[str, AccessLevel] = Field(
        default_factory=dict,
        description="Lower a server's access for this profile (never raise it).",
    )
    browser: bool = Field(
        default=False, description="Give runs with this profile the virtual browser."
    )
    learning: str | Literal[False] | None = Field(
        default=None, description="Learning profile to use, or false for no learning."
    )
    quality: QualityLevel | None = Field(
        default=None, description="Quality level for runs with this profile."
    )
    description: str = Field(
        default="", description="A note to yourself about what this profile is for."
    )

    @field_validator("access_overrides", mode="before")
    @classmethod
    def _no_full(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for item in value.values():
                _reject_full(item)
        return value

    @field_validator("learning", mode="before")
    @classmethod
    def _learning(cls, value: Any) -> Any:
        if value is True:
            raise ValueError("learning: name a learning profile, or set false to turn it off")
        return value


def check_servers_config(
    servers: dict[str, ServerConfig],
    browser: BrowserConfig,
    profiles: dict[str, WorkspaceProfileConfig],
    role_servers: dict[str, list[str]],
    learning_pattern: str,
) -> None:
    """Cross-checks between the tables; raises ValueError with a user-facing message."""
    known = ", ".join(servers) or "none defined"
    for name in servers:
        if not re.match(SERVER_NAME_PATTERN, name):
            raise ValueError(
                f"servers: {name!r} is not a valid name (letters, digits, `-` and `_`, "
                "starting with a letter or digit, at most 40 characters)"
            )
    for role, names in role_servers.items():
        for ref in names:
            if ref not in servers:
                raise ValueError(
                    f"roles.{role}.servers: unknown server {ref!r}: define it in "
                    f"[servers.{ref}] (known: {known})"
                )
    if browser.install_target != "local" and browser.install_target not in servers:
        raise ValueError(
            f'browser.install_target: {browser.install_target!r} is neither "local" nor a '
            f"server (known: {known})"
        )
    for name, profile in profiles.items():
        if not re.match(WORKSPACE_PROFILE_PATTERN, name) or name == NO_PROFILE:
            raise ValueError(
                f"profiles: {name!r} is not a valid profile name"
                + (" (`none` means no profile)" if name == NO_PROFILE else "")
            )
        for ref in [*profile.servers, *profile.access_overrides]:
            if ref not in servers:
                raise ValueError(
                    f"profiles.{name}: unknown server {ref!r}: define it in [servers.{ref}] "
                    f"(known: {known})"
                )
        for ref, level in profile.access_overrides.items():
            if ref not in profile.servers:
                raise ValueError(
                    f"profiles.{name}.access_overrides: {ref!r} is not in this profile's servers"
                )
            configured = servers[ref].access
            if ACCESS_LEVELS.index(level) > ACCESS_LEVELS.index(configured):
                raise ValueError(
                    f"profiles.{name}.access_overrides.{ref}: {level!r} would raise access above "
                    f"the server's {configured!r}; overrides may only lower it"
                )
        if isinstance(profile.learning, str) and not re.match(learning_pattern, profile.learning):
            raise ValueError(f"profiles.{name}.learning: {profile.learning!r} is not a name")
