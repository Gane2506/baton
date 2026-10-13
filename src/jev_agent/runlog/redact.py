"""Redact secrets from text before it is written to a log (PLAN.md section 10).

Two kinds of secret are removed: values matching well-known key patterns (OpenAI/OpenRouter
style `sk-...` keys and HTTP `Bearer` tokens), and the current values (8+ chars) of environment
variables whose names say they hold a secret (containing KEY, TOKEN, SECRET, PASSWORD or
CREDENTIAL), plus the values of the variables listed by name in `JEV_REDACT_NAMES` (a
comma-separated list; workers put the env vars that feed MCP servers there, whatever they are
called).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

# (pattern, replacement) pairs.
_PATTERNS = [
    (re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}"), REDACTED),  # OpenRouter keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),  # OpenAI/Anthropic-style keys
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\g<1>" + REDACTED),  # headers
]
# Patterns of well-known credentials, without replacements: also used by the quality gate's
# built-in secret scan (`quality/checks.py`).
SECRET_PATTERNS: list[re.Pattern[str]] = [pattern for pattern, _ in _PATTERNS]
_SECRET_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
_MIN_SECRET_LEN = 8  # shorter env values are too likely to appear by accident
# Comma-separated env var names whose values are secrets regardless of their names.
REDACT_NAMES_ENV = "JEV_REDACT_NAMES"
_MIN_NAMED_SECRET_LEN = 4  # explicitly named secrets: only the tiniest values are kept


def secret_env_values(env: Mapping[str, str]) -> list[str]:
    """Values of environment variables whose names mark them as secrets, longest first."""
    values = {
        value
        for name, value in env.items()
        if _SECRET_ENV_NAME.search(name) and len(value) >= _MIN_SECRET_LEN
    }
    named = {n.strip() for n in env.get(REDACT_NAMES_ENV, "").split(",") if n.strip()}
    values |= {
        value
        for name, value in env.items()
        if name in named and len(value) >= _MIN_NAMED_SECRET_LEN
    }
    return sorted(values, key=len, reverse=True)


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Return `text` with secrets replaced by `[REDACTED]`. `env` defaults to `os.environ`."""
    for value in secret_env_values(os.environ if env is None else env):
        text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
