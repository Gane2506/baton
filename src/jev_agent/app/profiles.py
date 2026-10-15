"""Workspace profile use cases shared by the CLI and the web UI (issue #92).

Listing, describing and setting the repo default (`jev profile list | show | use`): typed
inputs, typed results, no printing. `cli.server_cmds` renders them. The profile storage
itself stays in `jev_agent.servers.profiles`; the names the app layer needs are
re-exported here so `app/` has one profile surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.servers.access import (
    READ_ONLY_ROLES,
    WRITE_ROLE,
    browser_for,
    server_grants,
)
from jev_agent.servers.profiles import (
    ProfileError,
    RunProfile,
    effective_name,
    get_profile,
    read_run_profile,
    repo_default,
    set_repo_default,
    snapshot,
    write_run_profile,
)

__all__ = [
    "ProfileDetail",
    "ProfileError",
    "ProfileSummary",
    "RoleGrants",
    "RunProfile",
    "describe_profile",
    "effective_name",
    "get_profile",
    "list_profiles",
    "read_run_profile",
    "repo_default",
    "servers_text",
    "set_default_profile",
    "set_repo_default",
    "snapshot",
    "write_run_profile",
]


@dataclass(frozen=True)
class ProfileSummary:
    name: str
    servers: str  # "db (medium), web (basic)", or "none"
    browser: bool
    learning: str  # "off", a profile name, or "-" (the repo setting)
    quality: str  # the level, or "-" (the config `[quality] level`)
    description: str
    is_default: bool


@dataclass(frozen=True)
class RoleGrants:
    role: str
    text: str  # "db: medium, web: basic; browser", or "no servers"


@dataclass(frozen=True)
class ProfileDetail:
    name: str
    description: str
    servers: str
    browser: bool
    learning: str
    quality: str
    roles: list[RoleGrants]
    is_default: bool


def servers_text(config: JevConfig, name: str) -> str:
    """`name`'s servers with their effective access levels, or "none"."""
    profile = config.profiles[name]
    parts = []
    for server in profile.servers:
        level = profile.access_overrides.get(server, config.servers[server].access)
        parts.append(f"{server} ({level})")
    return ", ".join(parts) or "none"


def list_profiles(config: JevConfig, state_dir: Path) -> list[ProfileSummary]:
    """Every workspace profile, with the repo default marked."""
    default = repo_default(state_dir)
    return [
        ProfileSummary(
            name=name,
            servers=servers_text(config, name),
            browser=profile.browser,
            learning="off" if profile.learning is False else (profile.learning or "-"),
            quality=profile.quality or "-",
            description=profile.description,
            is_default=name == default,
        )
        for name, profile in config.profiles.items()
    ]


def describe_profile(config: JevConfig, state_dir: Path, name: str) -> ProfileDetail:
    """One profile and what each role gets from it. `ProfileError` for an unknown name."""
    profile = snapshot(config, name)  # ProfileError for an unknown profile
    learning = "off" if profile.learning is False else (profile.learning or "repo setting")
    roles = []
    for role in (WRITE_ROLE, *sorted(READ_ONLY_ROLES - {"chat"})):
        text = ", ".join(f"{s}: {lvl}" for s, lvl in server_grants(config, role, profile).items())
        text = text or "no servers"
        if browser_for(config, role, profile):
            text += "; browser"
        roles.append(RoleGrants(role, text))
    return ProfileDetail(
        name=name,
        description=profile.description,
        servers=servers_text(config, name),
        browser=profile.browser,
        learning=str(learning),
        quality=profile.quality or "config [quality] level",
        roles=roles,
        is_default=name == repo_default(state_dir),
    )


def set_default_profile(config: JevConfig, state_dir: Path, name: str | None) -> None:
    """Set the repo's default workspace profile (None clears it). `ProfileError` for an
    unknown name."""
    if name is not None:
        get_profile(config, name)  # ProfileError for an unknown profile
    set_repo_default(state_dir, name)
