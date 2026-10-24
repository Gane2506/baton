"""Which profile a run uses: the repo switch, the active profile, and the
precedence between run flags, repo settings, `jev.toml`, and defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jev_agent.config.schema import JevConfig
from jev_agent.core.learning.models import SETTING_ENABLED, SETTING_PROFILE, LearningError
from jev_agent.core.learning.profiles import check_profile_name
from jev_agent.core.learning.registry import Learning
from jev_agent.core.store import Store

# Where a status value came from: the repo switch (`jev learning ...`), jev.toml, or the
# built-in default. A Literal, not a free string, so a typo'd source is a type error.
StatusSource = Literal["the switch", "jev.toml", "default"]


@dataclass(frozen=True)
class LearningStatus:
    """The repo's learning switch and active profile, with where each value came from."""

    enabled: bool
    enabled_source: StatusSource
    profile: str
    profile_source: StatusSource


def _setting_bool(value: str | None) -> bool | None:
    return None if value is None else value == "true"


def learning_status(store: Store | None, config: JevConfig) -> LearningStatus:
    """The repo switch and active profile: CLI setting (state.db) > jev.toml > default."""
    lc, set_in_config = config.learning, config.learning.model_fields_set
    enabled = None if store is None else _setting_bool(store.get_setting(SETTING_ENABLED))
    profile = None if store is None else store.get_setting(SETTING_PROFILE)
    return LearningStatus(
        enabled=lc.enabled if enabled is None else enabled,
        enabled_source="the switch"
        if enabled is not None
        else ("jev.toml" if "enabled" in set_in_config else "default"),
        profile=profile or lc.profile,
        profile_source="the switch"
        if profile
        else ("jev.toml" if "profile" in set_in_config else "default"),
    )


def resolve_learning(
    store: Store,
    learning: Learning,
    config: JevConfig,
    flag_profile: str | None = None,
    no_learning: bool = False,
) -> str | None:
    """The profile a new run uses, or None for no learning.

    Precedence: `--no-learning` / `--learning NAME` (NAME must exist; it is used even if
    disabled) > the repo setting from `jev learning enable|disable|use` > `[learning]` in
    jev.toml > defaults (off, "default"). The active profile is created in repo scope if it
    does not exist; a disabled active profile means no learning.
    """
    if flag_profile is not None and no_learning:
        raise LearningError("--learning and --no-learning cannot be used together")
    if no_learning:
        return None
    if flag_profile is not None:
        profile = learning.get(check_profile_name(flag_profile))
        if profile is None:
            raise LearningError(
                f"no learning profile named {flag_profile!r}; "
                f"create it with `jev learning create {flag_profile}`"
            )
        return profile.name
    status = learning_status(store, config)
    if not status.enabled:
        return None
    profile = learning.ensure(status.profile)
    return profile.name if profile.enabled else None


def set_repo_learning(store: Store, enabled: bool) -> None:
    store.set_setting(SETTING_ENABLED, "true" if enabled else "false")


def set_active_profile(store: Store, name: str | None) -> None:
    store.set_setting(SETTING_PROFILE, name)


def use_profile(store: Store, learning: Learning, name: str) -> None:
    """Make an existing profile the repo's active one (`jev learning use`)."""
    if learning.get(check_profile_name(name)) is None:
        raise LearningError(f"no learning profile named {name!r}; create it first")
    set_active_profile(store, name)


def enable_learning(
    store: Store, config: JevConfig, learning: Learning, name: str | None = None
) -> str:
    """`jev learning enable [NAME]`: without a name, turn learning on for the repo (creating
    and enabling the active profile); with one, re-enable that profile. Returns the profile."""
    if name is not None:
        learning.set_enabled(name, True)
        return name
    active = learning_status(store, config).profile
    learning.ensure(active)
    learning.set_enabled(active, True)
    set_repo_learning(store, True)
    return active


def disable_learning(store: Store, learning: Learning, name: str | None = None) -> None:
    """`jev learning disable [NAME]`: learning off for the repo, or one profile disabled.
    Lessons are kept either way."""
    if name is None:
        set_repo_learning(store, False)
    else:
        learning.set_enabled(name, False)


def delete_profile(store: Store, learning: Learning, name: str) -> None:
    """Delete a profile and its lessons; the repo falls back to the configured profile when it
    was the active one."""
    learning.delete(name)
    if store.get_setting(SETTING_PROFILE) == name:
        set_active_profile(store, None)


__all__ = [
    "LearningStatus",
    "delete_profile",
    "disable_learning",
    "enable_learning",
    "learning_status",
    "resolve_learning",
    "set_active_profile",
    "set_repo_learning",
    "use_profile",
]
