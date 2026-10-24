"""GitHub remote URLs and PR titles for shipping."""

from __future__ import annotations

import re

TITLE_MAX = 72
FOOTER = "Opened by Baton Agent Orchestrator (`jev ship`)."
_PR_URL = re.compile(r"https?://\S+/pull/(\d+)")
_GITHUB_URL = re.compile(
    r"^(?:[a-z][a-z0-9+]*://)?(?:[^@/]+@)?(?P<host>[^/:@]+)(?::\d+)?[/:]"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def github_repo(url: str) -> str | None:
    """`owner/name` from a GitHub-style remote URL (https, ssh or scp form); None for local
    paths and anything else."""
    url = url.strip()
    if not url or url.startswith(("/", ".", "~", "file:")):
        return None
    match = _GITHUB_URL.match(url)
    if match is None or "." not in match.group("host"):
        return None
    return f"{match.group('owner')}/{match.group('name')}"


def pr_title(idea: str) -> str:
    """The idea's first non-empty line, at most `TITLE_MAX` characters."""
    line = next((ln.strip() for ln in idea.splitlines() if ln.strip()), "jev run")
    line = " ".join(line.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 3].rstrip() + "..."
