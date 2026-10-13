"""Agent tools (MCP servers, skills, plugins): config, resolution, worker argv/files, doctor.

No real CLI or network: workers get a fake `CommandRunner` that records the call and, for
Claude Code, reads the MCP config file while it exists.
"""

from __future__ import annotations

import json
import stat
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jev_agent.cli import doctor_cmd
from jev_agent.config import ConfigError, JevConfig, load_config
from jev_agent.core.models import ValidationResult
from jev_agent.runlog.redact import REDACT_NAMES_ENV, REDACTED, redact
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.claude_code import ClaudeCodeWorker, mcp_config, tool_permissions
from jev_agent.workers.codex import CodexWorker, mcp_overrides, toml_value
from jev_agent.workers.subprocess_runner import CommandResult, log_command
from jev_agent.workers.tools import AgentTools, Plugin, Skill, tools_for_role, with_redact_names

GH_SECRET = "gh-value-0123456789abcdef"
DOCS_SECRET = "docs-value-abcdef0123456789"
# A deliberately plain variable name: only JEV_REDACT_NAMES marks it as secret.
PLAIN_VAR = "DOCS_AUTH"

TOOLS_TOML = """
[tools.mcp.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env_from = ["GITHUB_PERSONAL_ACCESS_TOKEN"]

[tools.mcp.docs]
url = "https://docs.example.com/mcp"
bearer_token_env = "DOCS_MCP_TOKEN"
headers_from_env = { "X-Api-Key" = "DOCS_AUTH" }

[tools.mcp.legacy]
url = "https://legacy.example.com/sse"
transport = "sse"

[tools.skills]
house-style = "skills/house-style"

[tools.plugins.deploy-tools]
path = "plugins/deploy-tools"
allowed_tools = ["mcp__plugin_deploy-tools_api__*"]

[roles.planner]
mcp = ["github", "docs"]

[roles.coder]
mcp = ["github"]
skills = ["house-style"]
plugins = ["deploy-tools"]
"""


def config_from(text: str) -> JevConfig:
    return JevConfig.model_validate(tomllib.loads(text))


# --- config --------------------------------------------------------------------------------


def test_tools_config_parses_and_roles_reference_it() -> None:
    config = config_from(TOOLS_TOML)
    assert config.roles.planner.mcp == ["github", "docs"]
    assert config.roles.coder.skills == ["house-style"]
    assert config.roles.planner.workers == ["claude_code", "api"]  # role defaults kept
    docs = config.tools.mcp["docs"]
    assert docs.env_names() == ["DOCS_AUTH", "DOCS_MCP_TOKEN"]
    assert config.tools.plugins["deploy-tools"].allowed_tools == ["mcp__plugin_deploy-tools_api__*"]


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[roles.planner]\nmcp = ["nope"]', "roles.planner.mcp: unknown mcp entry 'nope'"),
        ('[roles.coder]\nskills = ["x"]', "roles.coder.skills: unknown skills entry 'x'"),
        ('[roles.coder]\nplugins = ["p"]', "define it in [tools.plugins]"),
        ('[tools.mcp.a]\ncommand = "x"\nurl = "https://x"', "exactly one of `command`"),
        ("[tools.mcp.a]\nargs = []", "exactly one of `command`"),
        ('[tools.mcp.a]\ncommand = "x"\nenv = { A = "b" }', "`env` is not allowed"),
        ('[tools.mcp.a]\nurl = "https://x"\nheaders = { A = "b" }', "`headers` is not allowed"),
        ('[tools.mcp.a]\ncommand = "x"\nenv_from = ["not a name"]', "not an environment"),
        ('[tools.mcp.a]\ncommand = "x"\nbearer_token_env = "T"', "need a remote `url`"),
        ('[tools.mcp.a]\nurl = "https://x"\nenv_from = ["A"]', "only apply to a local"),
        ('[tools.mcp.a]\ncommand = "x"\ntransport = "sse"', "only applies to a remote"),
        ('[tools.mcp.a]\nurl = "ftp://x"', "should match pattern"),
        ('[tools.mcp."bad name"]\ncommand = "x"', "is not a valid name"),
        (
            '[tools.mcp.a]\ncommand = "x"\nargs = ["--auth", "ghp_abcdefghijklmnop"]',
            "literal credential",
        ),
        ('[tools.mcp.a]\nurl = "https://u:p4ss@x.example"', "literal credential"),
        ('[tools.mcp.a]\nurl = "https://x.example/mcp?token=abc"', "literal credential"),
    ],
)
def test_tools_config_errors(toml: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message.replace("[", r"\[").replace("?", r"\?")):
        config_from(toml)


def test_secret_key_names_still_rejected_but_user_names_are_not() -> None:
    with pytest.raises(ValidationError, match=r"secrets are not allowed.*tools\.mcp\.a\.token"):
        config_from('[tools.mcp.a]\ncommand = "x"\ntoken = "abc"')
    # Server, skill and header names that merely look secret-ish are fine.
    config = config_from(
        '[tools.mcp.secrets-manager]\nurl = "https://x.example"\n'
        'headers_from_env = { "X-Api-Key" = "K" }\nbearer_token_env = "T"\n'
        '[tools.skills]\ntoken-budget = "s"\n'
    )
    assert "secrets-manager" in config.tools.mcp


def test_unknown_reference_is_reported_against_the_file(tmp_path: Path) -> None:
    (tmp_path / "jev.toml").write_text('[roles.planner]\nmcp = ["github"]\n')
    with pytest.raises(ConfigError, match=r"jev\.toml: roles\.planner\.mcp: unknown mcp entry"):
        load_config(tmp_path, user_config_path=tmp_path / "none.toml")


def test_tools_for_role_resolves_paths_against_repo(tmp_path: Path) -> None:
    config = config_from(TOOLS_TOML)
    coder = tools_for_role(config, "coder", tmp_path)
    assert list(coder.mcp) == ["github"]
    assert coder.skills == (Skill(name="house-style", path=tmp_path / "skills/house-style"),)
    assert coder.plugins[0].path == tmp_path / "plugins/deploy-tools"
    assert coder.describe() == "mcp: github; skills: house-style; plugins: deploy-tools"
    assert not tools_for_role(config, "reviewer", tmp_path)
    assert tools_for_role(config, "reviewer", tmp_path).describe() == "none"
    assert tools_for_role(config, "planner", tmp_path).secret_env_names() == [
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "DOCS_AUTH",
        "DOCS_MCP_TOKEN",
    ]


# --- redaction -----------------------------------------------------------------------------


def test_redact_by_listed_names() -> None:
    env = {PLAIN_VAR: DOCS_SECRET, "OTHER": "plain-value-123456", REDACT_NAMES_ENV: PLAIN_VAR}
    out = redact(f"a {DOCS_SECRET} b plain-value-123456", env)
    assert DOCS_SECRET not in out and REDACTED in out
    assert "plain-value-123456" in out  # not listed, not secret-named


def test_with_redact_names_merges() -> None:
    tools = tools_for_role(config_from(TOOLS_TOML), "planner", Path("/r"))
    env = with_redact_names({REDACT_NAMES_ENV: "MINE"}, tools)
    assert env[REDACT_NAMES_ENV].split(",") == [
        "MINE",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "DOCS_AUTH",
        "DOCS_MCP_TOKEN",
    ]
    assert with_redact_names({"A": "b"}, AgentTools()) == {"A": "b"}


# --- workers -------------------------------------------------------------------------------


@dataclass
class Recorder:
    """Fake runner: records argv/env and snapshots the MCP config file during the call."""

    stdout: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    mcp_file: Path | None = None
    mcp_text: str | None = None
    mcp_mode: int | None = None
    skills_seen: list[str] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin_text: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> CommandResult:
        result = CommandResult(0, "", "") if argv[0] == "git" else CommandResult(0, self.stdout, "")
        if argv[0] != "git":
            self.argv, self.env = list(argv), dict(env)
            if "--mcp-config" in argv:
                self.mcp_file = Path(argv[argv.index("--mcp-config") + 1])
                self.mcp_text = self.mcp_file.read_text()
                self.mcp_mode = stat.S_IMODE(self.mcp_file.stat().st_mode)
            if "--add-dir" in argv:
                root = Path(argv[argv.index("--add-dir") + 1]) / ".claude" / "skills"
                self.skills_seen = sorted(p.name for p in root.iterdir())
        if log_path is not None:
            log_command(log_path, argv, cwd, timeout_s, stdin_text, result, env)
        return result


CLAUDE_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "structured_output": {"verdict": "approve", "issues": []},
    }
)
CODEX_OK = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
)


def make_job(tmp_path: Path, role: str, tools: AgentTools, **kw: Any) -> WorkerJob:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return WorkerJob(
        role=role,  # type: ignore[arg-type]
        prompt="PROMPT",
        workdir=workdir,
        log_path=tmp_path / "logs" / "a.log",
        tools=tools,
        **kw,
    )


@pytest.fixture
def repo_tools(tmp_path: Path) -> JevConfig:
    skill = tmp_path / "repo" / "skills" / "house-style"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: house-style\ndescription: d\n---\nUse tabs.\n")
    (tmp_path / "repo" / "plugins" / "deploy-tools").mkdir(parents=True)
    return config_from(TOOLS_TOML)


def secret_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "GITHUB_PERSONAL_ACCESS_TOKEN": GH_SECRET,
        "DOCS_MCP_TOKEN": "docs-bearer-0123456789",
        PLAIN_VAR: DOCS_SECRET,
    }


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_claude_argv_without_tools_is_unchanged(tmp_path: Path) -> None:
    argv = ClaudeCodeWorker().build_argv(make_job(tmp_path, "planner", AgentTools()))
    assert not {"--mcp-config", "--strict-mcp-config", "--add-dir", "--plugin-dir"} & set(argv)
    assert flag(argv, "--allowedTools") == "Read Glob Grep"


def test_claude_argv_with_tools(tmp_path: Path, repo_tools: JevConfig) -> None:
    tools = tools_for_role(repo_tools, "coder", tmp_path / "repo")
    job = make_job(tmp_path, "coder", tools)
    with pytest.raises(ValueError, match="tools_dir"):
        ClaudeCodeWorker().build_argv(job)
    argv = ClaudeCodeWorker().build_argv(job, tmp_path / "t")
    allowed = flag(argv, "--allowedTools").split()
    assert allowed[-3:] == ["mcp__github", "Skill(house-style)", "mcp__plugin_deploy-tools_api__*"]
    assert "Edit" in allowed  # the coder's own tools stay
    assert flag(argv, "--mcp-config") == str(tmp_path / "t" / "mcp.json")
    assert "--strict-mcp-config" in argv
    assert flag(argv, "--add-dir") == str(tmp_path / "t" / "skills")
    assert flag(argv, "--plugin-dir") == str(tmp_path / "repo" / "plugins" / "deploy-tools")


def test_claude_read_only_role_gets_mcp_tools_but_no_write_tools(
    tmp_path: Path, repo_tools: JevConfig
) -> None:
    tools = tools_for_role(repo_tools, "planner", tmp_path / "repo")
    argv = ClaudeCodeWorker().build_argv(
        make_job(tmp_path, "planner", tools, output_schema=ValidationResult), tmp_path / "t"
    )
    assert flag(argv, "--allowedTools") == "Read Glob Grep mcp__github mcp__docs"
    assert flag(argv, "--disallowedTools") == "Edit Write NotebookEdit"
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert "--add-dir" not in argv and "--plugin-dir" not in argv


def test_tool_permissions_dedupes() -> None:
    tools = AgentTools(
        skills=(Skill(name="a", path=Path("/a")),),
        plugins=(Plugin(name="p", path=Path("/p"), allowed_tools=("Skill(a)", "X")),),
    )
    assert tool_permissions(tools) == ["Skill(a)", "X"]


def test_mcp_config_fills_values_and_notes_unset_vars(repo_tools: JevConfig) -> None:
    tools = tools_for_role(repo_tools, "planner", Path("/r"))
    config, notes = mcp_config(tools, {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_SECRET})
    assert config == {
        "mcpServers": {
            "github": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_SECRET},
            },
            "docs": {"type": "http", "url": "https://docs.example.com/mcp", "headers": {}},
        }
    }
    assert notes == [
        "mcp server docs: DOCS_AUTH is not set; left out",
        "mcp server docs: DOCS_MCP_TOKEN is not set; left out",
    ]
    full, _ = mcp_config(tools, secret_env())
    assert full["mcpServers"]["docs"]["headers"] == {
        "X-Api-Key": DOCS_SECRET,
        "Authorization": "Bearer docs-bearer-0123456789",
    }
    legacy = AgentTools(mcp={"legacy": repo_tools.tools.mcp["legacy"]})
    assert mcp_config(legacy, {})[0]["mcpServers"]["legacy"]["type"] == "sse"


async def test_claude_run_writes_private_files_and_never_logs_secrets(
    tmp_path: Path, repo_tools: JevConfig
) -> None:
    tools = tools_for_role(repo_tools, "planner", tmp_path / "repo")
    tools = tools.model_copy(
        update={"skills": tools_for_role(repo_tools, "coder", tmp_path / "repo").skills}
    )
    runner = Recorder(CLAUDE_OK)
    worker = ClaudeCodeWorker(runner=runner, env=secret_env())
    job = make_job(tmp_path, "planner", tools, output_schema=ValidationResult)
    out = await worker.run(job)
    assert out.result.status == "success"
    # The config file existed during the call, owner-only, outside the workdir; it is gone now.
    assert runner.mcp_file is not None and runner.mcp_mode == 0o600
    assert not runner.mcp_file.is_relative_to(job.workdir)
    assert not runner.mcp_file.exists()
    servers = json.loads(runner.mcp_text or "")["mcpServers"]
    assert servers["github"]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_SECRET}
    assert runner.skills_seen == ["house-style"]
    # No secret in argv; the log lists the tools and redacts even the plainly named variable.
    for secret in (GH_SECRET, DOCS_SECRET):
        assert all(secret not in arg for arg in runner.argv)
    assert PLAIN_VAR in runner.env[REDACT_NAMES_ENV].split(",")
    log = job.log_path.read_text()
    assert "mcp: github, docs; skills: house-style" in log
    assert GH_SECRET not in log and DOCS_SECRET not in log
    assert list((tmp_path / "work").iterdir()) == []  # nothing written into the workdir


async def test_claude_run_drops_missing_skills_and_plugins(tmp_path: Path) -> None:
    tools = AgentTools(
        skills=(Skill(name="gone", path=tmp_path / "nope"),),
        plugins=(Plugin(name="gone-plugin", path=tmp_path / "nope2"),),
    )
    runner = Recorder(CLAUDE_OK)
    job = make_job(tmp_path, "planner", tools, output_schema=ValidationResult)
    await ClaudeCodeWorker(runner=runner, env={"PATH": "/bin"}).run(job)
    assert not {"--add-dir", "--plugin-dir", "--mcp-config"} & set(runner.argv)
    assert "Skill(gone)" not in flag(runner.argv, "--allowedTools")
    log = job.log_path.read_text()
    assert "skill gone: no SKILL.md" in log and "plugin gone-plugin:" in log


def test_toml_value_round_trips() -> None:
    for value in ['np"x\\y', ["-y", "ünï", "a b"], {"X-Api-Key": "DOCS_AUTH", "é": "B"}]:
        assert tomllib.loads(f"v = {toml_value(value)}")["v"] == value
    with pytest.raises(TypeError):
        toml_value(3)


def test_codex_mcp_overrides_pass_names_only(repo_tools: JevConfig) -> None:
    tools = AgentTools(mcp=dict(repo_tools.tools.mcp))
    argv = mcp_overrides(tools)
    pairs = [argv[i + 1] for i in range(0, len(argv), 2)]
    assert all(argv[i] == "-c" for i in range(0, len(argv), 2))
    assert pairs == [
        'mcp_servers.github.command="npx"',
        'mcp_servers.github.args=["-y", "@modelcontextprotocol/server-github"]',
        'mcp_servers.github.env_vars=["GITHUB_PERSONAL_ACCESS_TOKEN"]',
        'mcp_servers.docs.url="https://docs.example.com/mcp"',
        'mcp_servers.docs.bearer_token_env_var="DOCS_MCP_TOKEN"',
        'mcp_servers.docs.env_http_headers={ "X-Api-Key" = "DOCS_AUTH" }',
    ]  # the SSE server is left out
    # Each override parses as the TOML Codex expects.
    for pair in pairs:
        value = pair.partition("=")[2]
        tomllib.loads(f"v = {value}")


async def test_codex_run_with_tools(tmp_path: Path, repo_tools: JevConfig) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path / "work")], check=True)
    tools = tools_for_role(repo_tools, "coder", tmp_path / "repo")
    runner = Recorder(CODEX_OK + "\n")
    job = make_job(tmp_path, "planner", tools)
    out = await CodexWorker(runner=runner, env=secret_env()).run(job)
    assert out.result.status == "success"
    assert 'mcp_servers.github.env_vars=["GITHUB_PERSONAL_ACCESS_TOKEN"]' in runner.argv
    assert runner.argv[-1] == "-"
    assert all(GH_SECRET not in arg for arg in runner.argv)
    log = job.log_path.read_text()
    assert "skill house-style: Codex cannot load skills per run" in log
    assert "plugin deploy-tools: Codex cannot load plugins per run" in log
    assert GH_SECRET not in log


def test_codex_argv_without_tools_has_no_overrides(tmp_path: Path) -> None:
    argv = CodexWorker().build_argv(make_job(tmp_path, "coder", AgentTools()), tmp_path)
    assert not any(arg.startswith("mcp_servers.") for arg in argv)


# --- doctor --------------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # These tests model the user's own repository: trust it, so its jev.toml may set the
    # guarded keys (servers, tools, permissions; docs/security.md).
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text('[trust]\nrepos = ["*"]\n')
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def rows(repo: Path) -> list[tuple[str, str, str]]:
    return [(c.name, c.status, c.detail) for c in doctor_cmd.run_checks(repo)]


def test_doctor_tool_rows(
    git_repo: Path, repo_tools: JevConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: None if cmd == "npx" else f"/b/{cmd}")
    for name, value in secret_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DOCS_MCP_TOKEN")
    (git_repo / "jev.toml").write_text(TOOLS_TOML)
    found = rows(git_repo)
    text = "\n".join(" | ".join(row) for row in found)
    assert ("role planner tools", "ok", "mcp: github, docs") in found
    assert (
        "role coder tools",
        "ok",
        "mcp: github; skills: house-style; plugins: deploy-tools",
    ) in found
    assert ("tools.mcp.github", "warn", "`npx` not found on PATH") in found
    assert ("tools.mcp.docs", "warn", "DOCS_MCP_TOKEN not set") in found
    assert "codex cannot use skill house-style, plugin deploy-tools" in text
    assert "the api worker gets no tools" in text  # planner may use api
    assert "read-only role with MCP servers (github, docs)" in text
    assert GH_SECRET not in text and DOCS_SECRET not in text  # never values
    assert not any(status == "fail" for _, status, _ in found)


def test_doctor_fails_missing_skill_and_plugin_paths(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/b/{cmd}")
    (git_repo / "jev.toml").write_text(
        '[tools.skills]\ns = "missing"\n[tools.plugins.p]\npath = "nope"\n'
        '[roles.coder]\nskills = ["s"]\nplugins = ["p"]\n'
    )
    fails = {name for name, status, _ in rows(git_repo) if status == "fail"}
    assert {"tools.skills.s", "tools.plugins.p"} <= fails


def test_doctor_fails_unknown_reference(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/b/{cmd}")
    (git_repo / "jev.toml").write_text('[roles.planner]\nmcp = ["github"]\n')
    config_row = next(row for row in rows(git_repo) if row[0] == "config")
    assert config_row[1] == "fail" and "unknown mcp entry 'github'" in config_row[2]
