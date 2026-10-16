"""Workspace profile use cases shared by the CLI and the web UI (issue #92).

Listing, describing and setting the repo default (`jev profile list | show | use`): typed
inputs, typed results, no printing. `cli.server_cmds` renders them. The profile storage
itself stays in `jev_agent.servers.profiles`; the names the app layer needs are
re-exported here so `app/` has one profile surface.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from jev_agent.config.defaults import CONFIG_FILENAME
from jev_agent.config.loader import default_user_config_path
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
    "AccessOverride",
    "ProfileDetail",
    "ProfileError",
    "ProfileSummary",
    "RoleGrants",
    "RunProfile",
    "describe_profile",
    "effective_name",
    "get_profile",
    "list_profiles",
    "profile_sources",
    "read_run_profile",
    "repo_default",
    "servers_text",
    "set_default_profile",
    "set_repo_default",
    "snapshot",
    "write_run_profile",
]


@dataclass(frozen=True)
class AccessOverride:
    server: str
    level: str  # the lowered access level for this profile


@dataclass(frozen=True)
class ProfileSummary:
    name: str
    servers: str  # "db (medium), web (basic)", or "none"
    browser: bool
    learning: str  # "off", a profile name, or "-" (the repo setting)
    quality: str  # the level, or "-" (the config `[quality] level`)
    description: str
    is_default: bool
    source: str = ""  # "User config", "Repo jev.toml", "User + repo", or "" when unknown


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
    source: str = ""
    access_overrides: tuple[AccessOverride, ...] = ()


def _profile_names_in(path: Path) -> set[str]:
    """Profile names in a raw TOML file's `[profiles]` table (names only, no validation)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    profiles = data.get("profiles")
    return set(profiles) if isinstance(profiles, dict) else set()


def profile_sources(repo_root: Path, user_config_path: Path | None = None) -> dict[str, str]:
    """Where each workspace profile is defined: user config, repo jev.toml, or both.

    Reads the raw TOML layers (names only, never validated) so the UI can label each
    profile's source. Missing or unreadable files count as defining nothing.
    """
    user_names = _profile_names_in(user_config_path or default_user_config_path())
    repo_names = _profile_names_in(repo_root / CONFIG_FILENAME)
    sources: dict[str, str] = {}
    for name in sorted(user_names | repo_names):
        if name in user_names and name in repo_names:
            sources[name] = "User + repo"
        elif name in user_names:
            sources[name] = "User config"
        else:
            sources[name] = "Repo jev.toml"
    return sources


def servers_text(config: JevConfig, name: str) -> str:
    """`name`'s servers with their effective access levels, or "none"."""
    profile = config.profiles[name]
    parts = []
    for server in profile.servers:
        level = profile.access_overrides.get(server, config.servers[server].access)
        parts.append(f"{server} ({level})")
    return ", ".join(parts) or "none"


def list_profiles(
    config: JevConfig, state_dir: Path, sources: dict[str, str] | None = None
) -> list[ProfileSummary]:
    """Every workspace profile, with the repo default marked."""
    default = repo_default(state_dir)
    known = sources or {}
    return [
        ProfileSummary(
            name=name,
            servers=servers_text(config, name),
            browser=profile.browser,
            learning="off" if profile.learning is False else (profile.learning or "-"),
            quality=profile.quality or "-",
            description=profile.description,
            is_default=name == default,
            source=known.get(name, ""),
        )
        for name, profile in config.profiles.items()
    ]


def describe_profile(
    config: JevConfig,
    state_dir: Path,
    name: str,
    sources: dict[str, str] | None = None,
) -> ProfileDetail:
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
        source=(sources or {}).get(name, ""),
        access_overrides=tuple(
            AccessOverride(server=server, level=str(level))
            for server, level in profile.access_overrides.items()
        ),
    )


def set_default_profile(config: JevConfig, state_dir: Path, name: str | None) -> None:
    """Set the repo's default workspace profile (None clears it). `ProfileError` for an
    unknown name."""
    if name is not None:
        get_profile(config, name)  # ProfileError for an unknown profile
    set_repo_default(state_dir, name)
