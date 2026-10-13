"""Agent tools for one job: the MCP servers, skills and plugins a role was given in config.

`[tools.*]` defines each tool once; `[roles.<role>] mcp/skills/plugins` names the ones a role
gets. `tools_for_role` resolves those names into an `AgentTools`, which travels to the worker on
`WorkerJob.tools`. Paths are made absolute against the repo root here, so workers never guess.

Only environment variable NAMES are held here. Workers read the values from their own
environment when they start the agent, and add the names to `REDACT_NAMES_ENV` so the values
are scrubbed from every log line (`jev_agent.runlog.redact`).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jev_agent.config.schema import JevConfig, McpServerConfig
from jev_agent.core.models import Role
from jev_agent.runlog.redact import REDACT_NAMES_ENV


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: Path  # absolute folder holding SKILL.md


class Plugin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: Path  # absolute plugin folder (or .zip)
    allowed_tools: tuple[str, ...] = ()


class AgentTools(BaseModel):
    """The tools one job may use. Empty by default, which changes nothing in a worker's argv."""

    model_config = ConfigDict(frozen=True)

    mcp: dict[str, McpServerConfig] = Field(default_factory=dict)
    skills: tuple[Skill, ...] = ()
    plugins: tuple[Plugin, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.mcp or self.skills or self.plugins)

    def secret_env_names(self) -> list[str]:
        """Names of every environment variable whose value an MCP server receives."""
        names = [name for server in self.mcp.values() for name in server.env_names()]
        return list(dict.fromkeys(names))

    def describe(self) -> str:
        """One line for logs and `jev doctor`, e.g. `mcp: github, docs; skills: lint`."""
        parts = [
            f"{kind}: {', '.join(names)}"
            for kind, names in (
                ("mcp", list(self.mcp)),
                ("skills", [s.name for s in self.skills]),
                ("plugins", [p.name for p in self.plugins]),
            )
            if names
        ]
        return "; ".join(parts) or "none"


def resolve_path(path: Path, repo: Path) -> Path:
    return path if path.is_absolute() else repo / path


def tools_for_role(config: JevConfig, role: Role, repo: Path) -> AgentTools:
    """The tools `[roles.<role>]` names, resolved against `[tools]` (validated at load time)."""
    role_config = getattr(config.roles, role)
    tools = config.tools
    return AgentTools(
        mcp={name: tools.mcp[name] for name in role_config.mcp},
        skills=tuple(
            Skill(name=name, path=resolve_path(tools.skills[name], repo))
            for name in role_config.skills
        ),
        plugins=tuple(
            Plugin(
                name=name,
                path=resolve_path(tools.plugins[name].path, repo),
                allowed_tools=tuple(tools.plugins[name].allowed_tools),
            )
            for name in role_config.plugins
        ),
    )


def with_redact_names(env: dict[str, str], tools: AgentTools) -> dict[str, str]:
    """`env` plus `REDACT_NAMES_ENV`, so log redaction also covers the MCP secret values."""
    names = tools.secret_env_names()
    if not names:
        return env
    existing = [n for n in env.get(REDACT_NAMES_ENV, "").split(",") if n]
    return {**env, REDACT_NAMES_ENV: ",".join(dict.fromkeys([*existing, *names]))}
