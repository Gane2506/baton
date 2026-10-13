"""Redact secrets from text before it is written to a log (PLAN.md section 10).

Two kinds of secret are removed: values matching well-known credential shapes
(`CREDENTIAL_PATTERNS`: API keys, GitHub/Slack/AWS/Google tokens, private keys, JWTs, `Bearer`
and `Authorization:` headers, `user:password@` in URLs), and the current values (8+ chars)
of environment variables whose names say they hold a secret (containing KEY, TOKEN, SECRET,
PASSWORD or CREDENTIAL), plus the values of the variables listed by name in `JEV_REDACT_NAMES` (a
comma-separated list; workers put the env vars that feed MCP servers there, whatever they are
called).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

# (kind, pattern, replacement): well-known credential shapes. The single list of them: the
# quality gate's secret scan (`quality/checks_medium.py`) and the config check for literal
# secrets (`config/schema.py`) use it too. More specific shapes come first.
CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private key",  # the whole block; to the end of the text when the END line is cut off
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"(?:.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|.*\Z)",
            re.DOTALL,
        ),
        REDACTED,
    ),
    ("URL credentials", re.compile(r"(?<=://)[^/@\s:]+:[^/@\s]+(?=@)"), REDACTED),
    ("OpenRouter key", re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}"), REDACTED),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}"),
        REDACTED,
    ),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        REDACTED,
    ),
    (
        "authorization header",  # `Authorization: token ...` / `Basic ...` (gh, git, curl)
        re.compile(r"(?i)\b(Authorization:\s*(?:token|Basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\g<1>" + REDACTED,
    ),
    ("bearer token", re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\g<1>" + REDACTED),
]
# The patterns alone (no replacements).
SECRET_PATTERNS: list[re.Pattern[str]] = [pattern for _, pattern, _ in CREDENTIAL_PATTERNS]
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
    for _, pattern, replacement in CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
