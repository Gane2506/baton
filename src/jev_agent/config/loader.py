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


def load_config(repo_path: Path, user_config_path: Path | None = None) -> JevConfig:
    """Load the effective config for `repo_path`. Missing files are skipped."""
    layers = [
        user_config_path or default_user_config_path(),
        repo_path / CONFIG_FILENAME,
    ]
    merged: dict[str, Any] = {}
    config = JevConfig()
    for path in layers:
        data = _read_toml(path)
        if data is None:
            continue
        secrets = find_secret_keys(data)
        if secrets:
            raise ConfigError(f"{path}: {secret_keys_message(secrets)}")
        merged = deep_merge(merged, data)
        # Validate after each layer so an error is reported against the file that caused it.
        config = _validate(path, merged)
    return config


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
    except tomllib.TOMLDecodeError as exc:
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
