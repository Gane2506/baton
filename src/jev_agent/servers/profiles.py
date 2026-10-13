"""Workspace profiles: picking one for a run, the repo default, and the run's own copy.

- `[profiles.<name>]` in config defines them (`config/servers_schema.py`).
- `.jev/profile` holds the repo default (`jev profile use NAME`; `jev profile use none` clears).
- `.jev/runs/<run_id>/profile.json` is the profile a run started with, copied when it starts,
  so `jev resume` (and the agents' server tools) use the same servers, access and browser even
  if the config changes later. Servers removed from config since are skipped.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jev_agent.config.schema import JevConfig
from jev_agent.config.servers_schema import (
    NO_PROFILE,
    AccessLevel,
    WorkspaceProfileConfig,
)
from jev_agent.quality.levels import QualityLevel

DEFAULT_FILENAME = "profile"
RUN_PROFILE_FILENAME = "profile.json"


class ProfileError(ValueError):
    """An unknown or unreadable workspace profile. The message is user-facing."""


class RunProfile(BaseModel):
    """The copy of a workspace profile a run started with."""

    model_config = ConfigDict(extra="ignore")

    name: str
    servers: list[str] = Field(default_factory=list)
    access_overrides: dict[str, AccessLevel] = Field(default_factory=dict)
    browser: bool = False
    learning: str | bool | None = None
    quality: QualityLevel | None = None
    description: str = ""


def get_profile(config: JevConfig, name: str) -> WorkspaceProfileConfig:
    try:
        return config.profiles[name]
    except KeyError:
        known = ", ".join(config.profiles) or "none defined"
        raise ProfileError(
            f"no workspace profile named {name!r} (known: {known}); define it as a "
            f"`profiles.{name}` table in jev.toml"
        ) from None


def snapshot(config: JevConfig, name: str) -> RunProfile:
    profile = get_profile(config, name)
    return RunProfile(name=name, **profile.model_dump())


# ---- the repo default --------------------------------------------------------------------


def default_path(state_dir: Path) -> Path:
    return state_dir / DEFAULT_FILENAME


def repo_default(state_dir: Path) -> str | None:
    """The name in `.jev/profile`, or None."""
    try:
        text = default_path(state_dir).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return text or None


def set_repo_default(state_dir: Path, name: str | None) -> None:
    path = default_path(state_dir)
    if name is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n", encoding="utf-8")


def effective_name(config: JevConfig, flag: str | None, state_dir: Path) -> str | None:
    """The profile for a run: `--profile` (`none` means none), else the repo default if it
    still exists in config, else None. ProfileError for an unknown `--profile`."""
    if flag is not None:
        if flag == NO_PROFILE:
            return None
        get_profile(config, flag)
        return flag
    default = repo_default(state_dir)
    return default if default in config.profiles else None


def choose_interactively(
    config: JevConfig, default: str | None, ask: Callable[[str], str], show: Callable[[str], None]
) -> str | None:
    """Numbered menu: each profile, then "none / local only". Enter picks `default`."""
    names = list(config.profiles)
    show("Workspace profile for this run:")
    for number, name in enumerate(names, start=1):
        about = config.profiles[name].description
        marker = " (default)" if name == default else ""
        show(f"  {number}. {name}{marker}" + (f": {about}" if about else ""))
    none_number = len(names) + 1
    show(f"  {none_number}. none / local only" + (" (default)" if default is None else ""))
    while True:
        answer = ask("Choose a number").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= none_number:
            return None if int(answer) == none_number else names[int(answer) - 1]
        if answer in names:
            return answer
        if answer == NO_PROFILE:
            return None
        show(f"Type a number from 1 to {none_number}.")


# ---- the run's copy ----------------------------------------------------------------------


def run_profile_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "runs" / run_id / RUN_PROFILE_FILENAME


def write_run_profile(state_dir: Path, run_id: str, profile: RunProfile | None) -> None:
    """Record the run's profile; None removes it (the run uses no profile)."""
    path = run_profile_path(state_dir, run_id)
    if profile is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")


def read_run_profile(state_dir: Path, run_id: str) -> RunProfile | None:
    path = run_profile_path(state_dir, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunProfile.model_validate(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, ValidationError) as exc:
        raise ProfileError(f"{path}: unreadable workspace profile: {exc}") from exc
