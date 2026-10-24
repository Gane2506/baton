"""Load and layer configuration: built-in defaults < user config < repo `jev.toml`."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from jev_agent.config.defaults import CONFIG_FILENAME
from jev_agent.config.schema import JevConfig, find_secret_keys, secret_keys_message


class ConfigError(Exception):
    """A config file could not be read, parsed or validated. The message is user-facing."""


def default_user_config_path() -> Path:
    """`$XDG_CONFIG_HOME/jev/config.toml`, falling back to `~/.config/jev/config.toml`."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jev" / "config.toml"


# Keys a repository's jev.toml may change only when the user config trusts that repository
# (`[trust] repos`, docs/security.md): where jev sends API keys and which credential variables
# it reads, which commands its workers run and with what permissions, which tools, servers
# and browser agents reach, and the safety gates a run operates under (the autonomy mode and
# ship settings decide whether a run stops for approval and whether it may merge a PR on its
# own). A committed jev.toml from someone else could otherwise send a token to their host,
# give an agent production access, or silently turn a user-started run into an autonomous,
# auto-merging pipeline (issue #16). `*` matches any table key.
GUARDED_KEYS: tuple[str, ...] = (
    "dispatcher.jev.base_url",
    "dispatcher.jev.api_key_env",
    "workers.api.base_url",
    "workers.api.api_key_env",
    "workers.claude_code.command",
    "workers.claude_code.extra_args",
    "workers.claude_code.permission_mode",
    "workers.claude_code.read_only_permission_mode",
    "workers.claude_code.allowed_tools",
    "workers.claude_code.skip_permissions",
    "workers.claude_code.bare",
    "workers.codex.command",
    "workers.codex.extra_args",
    "workers.codex.sandbox",
    "tools",
    "servers",
    "browser",
    "profiles",
    "roles.*.servers",
    "roles.*.mcp",
    "roles.*.skills",
    "roles.*.plugins",
    "roles.*.prompt_file",
    "roles.*.prompt_addendum",
    "roles.*.browser",
    "autonomy.mode",
    "autonomy.access_tier",
    "autonomy.project_folder",
    "autonomy.unrestricted_acknowledged",
    "ship",
)


def load_config(repo_path: Path, user_config_path: Path | None = None) -> JevConfig:
    """Load the effective config for `repo_path`: defaults < user config < the repo's jev.toml.
    Missing files are skipped. Each file is validated after it is merged, so an error names
    the file that caused it. A repo file may change `GUARDED_KEYS` only in a trusted repo."""
    user_path = user_config_path or default_user_config_path()
    return config_from_layers(
        repo_path, user_path, _read_toml(user_path), _read_toml(repo_path / CONFIG_FILENAME)
    )


def config_from_layers(
    repo_path: Path,
    user_path: Path,
    user: dict[str, Any] | None,
    repo: dict[str, Any] | None,
) -> JevConfig:
    """`load_config` on already parsed layers (None: the file is missing). The dashboard checks
    a candidate jev.toml with it before writing the file."""
    repo_file = repo_path / CONFIG_FILENAME
    merged: dict[str, Any] = {}
    config = JevConfig()
    if user is not None:
        _check_secrets(user_path, user)
        merged = user
        config = _validate(user_path, merged)
    if repo is None:
        return config
    _check_secrets(repo_file, repo)
    if "trust" in repo:
        raise ConfigError(
            f"{repo_file}: [trust] belongs in your user config ({user_path}); a repository "
            "cannot trust itself"
        )
    merged = deep_merge(merged, repo)
    final = _validate(repo_file, merged)
    if not trusts(config, repo_path):
        changed = changed_guarded_keys(config, final)
        if changed:
            raise ConfigError(_untrusted_message(repo_file, repo_path, user_path, changed))
    return final


def trusts(config: JevConfig, repo_path: Path) -> bool:
    """Whether the user config's `[trust] repos` lists `repo_path` (or is `["*"]`)."""
    root = repo_path.resolve()
    return any(
        entry == "*" or Path(entry).expanduser().resolve() == root for entry in config.trust.repos
    )


def changed_guarded_keys(before: JevConfig, after: JevConfig) -> list[str]:
    """The `GUARDED_KEYS` paths whose value differs between the two configs."""
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return [key for pattern in GUARDED_KEYS for key in _changed(pattern.split("."), old, new, "")]


def _changed(parts: list[str], old: Any, new: Any, prefix: str) -> list[str]:
    if not parts:
        return [prefix] if old != new else []
    old_table = old if isinstance(old, dict) else {}
    new_table = new if isinstance(new, dict) else {}
    head, rest = parts[0], parts[1:]
    keys = sorted(set(old_table) | set(new_table)) if head == "*" else [head]
    return [
        changed
        for key in keys
        for changed in _changed(
            rest, old_table.get(key), new_table.get(key), f"{prefix}.{key}" if prefix else key
        )
    ]


def _untrusted_message(repo_file: Path, repo_path: Path, user_path: Path, keys: list[str]) -> str:
    return (
        f"{repo_file} changes settings that a repository's jev.toml may only change when you "
        f"trust it: {', '.join(keys)}. They decide where jev sends API keys, which credentials "
        "it reads, and which commands, permissions, tools and servers agents get. If you trust "
        f"this repository, add it to your user config ({user_path}):\n\n"
        f'[trust]\nrepos = ["{repo_path.resolve()}"]\n\n'
        "Otherwise remove those keys from its jev.toml (see docs/security.md)."
    )


def _check_secrets(path: Path, data: dict[str, Any]) -> None:
    secrets = find_secret_keys(data)
    if secrets:
        raise ConfigError(f"{path}: {secret_keys_message(secrets)}")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` into a copy of `base`. Tables merge recursively; other values replace."""
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    # `tomllib` decodes the bytes before parsing, so a non-UTF8 file raises
    # `UnicodeDecodeError` (not a `TOMLDecodeError` subclass): keep the ConfigError
    # contract for it too (audit 2026-10-16).
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc.strerror or exc}") from exc


def _validate(path: Path, data: dict[str, Any]) -> JevConfig:
    try:
        return JevConfig.model_validate(data)
    except ValidationError as exc:
        lines = [_format_error(path, err) for err in exc.errors()]
        raise ConfigError("\n".join(lines)) from exc


def _format_error(path: Path, err: ErrorDetails) -> str:
    where = ".".join(str(part) for part in err["loc"])
    if err["type"] == "extra_forbidden":
        message = "unknown key (check the spelling)"
    else:
        message = err["msg"].removeprefix("Value error, ")
        if message.startswith("Input should be "):
            message = "must be " + message.removeprefix("Input should be ")
    return f"{path}: {where}: {message}" if where else f"{path}: {message}"
