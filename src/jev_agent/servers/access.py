"""Which servers (at which access level) and whether the browser each role gets for a run.

Sources, merged per role:
- `[roles.<role>] servers = [...]` and `browser = true` (every run);
- the run's workspace profile (`.jev/runs/<id>/profile.json`): its servers go to every role,
  its `browser = true` gives the coder the browser.

Levels: a server's configured `access`, lowered by the profile's `access_overrides`, and
capped at `basic` for every role except the coder (read-only roles: planner, validator,
reviewer, code_reviewer, learner, chat). Nothing here can raise a level.

`tools_for_run` adds the result to the role's configured tools as two synthesized MCP servers:
`jev_servers` (`python -m jev_agent mcp servers ...`, which enforces the policy and writes the
audit log) and `jev_browser` (the pinned Playwright MCP server).
"""

from __future__ import annotations

import sys
from pathlib import Path

from jev_agent.config.schema import JevConfig, McpServerConfig
from jev_agent.config.servers_schema import (
    ACCESS_LEVELS,
    FULL_ACCESS_MESSAGE,
    AccessLevel,
    lower_access,
)
from jev_agent.core.models import Role
from jev_agent.servers.browser import BROWSER_DIRNAME, BROWSER_MCP_NAME, browser_mcp_config
from jev_agent.servers.profiles import RunProfile, read_run_profile
from jev_agent.workers.tools import AgentTools, tools_for_role

SERVERS_MCP_NAME = "jev_servers"
WRITE_ROLE = "coder"
READ_ONLY_ROLES = frozenset(
    {"planner", "validator", "reviewer", "code_reviewer", "learner", "chat"}
)
# Forwarded to the servers MCP process so ssh can use the agent's keys (MCP clients start
# stdio servers with a reduced environment).
SSH_ENV = ("SSH_AUTH_SOCK",)


def role_cap(role: str) -> AccessLevel:
    """The highest level a role may get: medium for the coder, basic for everyone else."""
    return "medium" if role == WRITE_ROLE else "basic"


def server_grants(
    config: JevConfig, role: str, profile: RunProfile | None
) -> dict[str, AccessLevel]:
    """Server name -> effective access level for `role` in a run with `profile`."""
    names: list[str] = []
    role_config = getattr(config.roles, role, None)
    if role_config is not None:
        names += role_config.servers
    if profile is not None:
        names += profile.servers
    grants: dict[str, AccessLevel] = {}
    for name in dict.fromkeys(names):
        server = config.servers.get(name)
        if server is None:  # removed from config since the run started
            continue
        level = lower_access(server.access, role_cap(role))
        if profile is not None and name in profile.access_overrides:
            level = lower_access(level, profile.access_overrides[name])
        grants[name] = level
    return grants


def browser_for(config: JevConfig, role: str, profile: RunProfile | None) -> bool:
    """`[roles.<role>] browser = true` (with `[browser] enabled`), or the coder in a run whose
    profile turns the browser on."""
    role_config = getattr(config.roles, role, None)
    if role_config is not None and role_config.browser and config.browser.enabled:
        return True
    return profile is not None and profile.browser and role == WRITE_ROLE


def parse_allow(value: str) -> dict[str, AccessLevel | None]:
    """`a,b:basic` -> {"a": None, "b": "basic"} (None: the highest the role may have)."""
    out: dict[str, AccessLevel | None] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, level = item.partition(":")
        if level == "full":
            raise ValueError(FULL_ACCESS_MESSAGE)
        if level and level not in ACCESS_LEVELS:
            raise ValueError(f"--allow {item!r}: level must be basic or medium")
        out[name] = next((lvl for lvl in ACCESS_LEVELS if lvl == level), None)
    return out


def allowed_grants(
    config: JevConfig, role: str | None, allow: dict[str, AccessLevel | None]
) -> dict[str, AccessLevel]:
    """The levels `jev mcp servers --allow ...` serves: config access, capped by the role (no
    role: as configured) and by any level given in `--allow`. ValueError for unknown servers."""
    grants: dict[str, AccessLevel] = {}
    for name, asked in allow.items():
        server = config.servers.get(name)
        if server is None:
            known = ", ".join(config.servers) or "none defined"
            raise ValueError(f"unknown server {name!r} (known: {known})")
        level = server.access if role is None else lower_access(server.access, role_cap(role))
        grants[name] = level if asked is None else lower_access(level, asked)
    return grants


def servers_mcp_config(
    root: Path, grants: dict[str, AccessLevel], *, role: str, run_id: str | None
) -> McpServerConfig:
    allow = ",".join(f"{name}:{level}" for name, level in grants.items())
    args = ["-m", "jev_agent", "mcp", "servers", "--repo", str(root), "--allow", allow]
    args += ["--role", role]
    if run_id is not None:
        args += ["--run-id", run_id]
    return McpServerConfig(command=sys.executable, args=args, env_from=list(SSH_ENV))


def extra_mcp(
    config: JevConfig,
    role: str,
    root: Path,
    state_dir: Path,
    run_id: str | None,
    profile: RunProfile | None,
) -> dict[str, McpServerConfig]:
    """The synthesized `jev_servers` / `jev_browser` entries for one job (maybe none)."""
    extra: dict[str, McpServerConfig] = {}
    grants = server_grants(config, role, profile)
    if grants:
        extra[SERVERS_MCP_NAME] = servers_mcp_config(root, grants, role=role, run_id=run_id)
    if browser_for(config, role, profile):
        output = state_dir / BROWSER_DIRNAME / (run_id or "adhoc")
        extra[BROWSER_MCP_NAME] = browser_mcp_config(
            headless=config.browser.headless, output_dir=output
        )
    return extra


def tools_for_run(
    config: JevConfig, role: Role, repo: Path, state_dir: Path, run_id: str
) -> AgentTools:
    """`tools_for_role` plus the servers and browser this role gets in this run."""
    tools = tools_for_role(config, role, repo)
    profile = read_run_profile(state_dir, run_id)
    extra = extra_mcp(config, role, repo, state_dir, run_id, profile)
    if not extra:
        return tools
    return tools.model_copy(update={"mcp": {**tools.mcp, **extra}})
