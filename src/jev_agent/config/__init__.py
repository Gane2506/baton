"""Configuration: schema, layered loading and the `jev init` template."""

from jev_agent.config.defaults import CONFIG_FILENAME, DEFAULT_CONFIG_TOML, STATE_DIRNAME
from jev_agent.config.loader import ConfigError, deep_merge, default_user_config_path, load_config
from jev_agent.config.schema import JevConfig

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_CONFIG_TOML",
    "STATE_DIRNAME",
    "ConfigError",
    "JevConfig",
    "deep_merge",
    "default_user_config_path",
    "load_config",
]
