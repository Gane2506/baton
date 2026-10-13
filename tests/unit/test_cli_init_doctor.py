import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import doctor_cmd
from jev_agent.cli.app import app
from jev_agent.config import DEFAULT_CONFIG_TOML

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # These tests model the user's own repository: trust it, so its jev.toml may set the
    # guarded keys (servers, tools, permissions; docs/security.md).
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text('[trust]\nrepos = ["*"]\n')


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


@pytest.fixture
def all_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-super-secret-value")


def _init(repo: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["init", "--repo", str(repo), *extra])
    return result.exit_code, result.output


def test_init_creates_files(git_repo: Path) -> None:
    code, output = _init(git_repo)
    assert code == 0, output
    assert (git_repo / "jev.toml").read_text() == DEFAULT_CONFIG_TOML
    assert (git_repo / ".jev").is_dir()
    assert (git_repo / ".gitignore").read_text() == ".jev/\n"


def test_init_from_subdirectory_uses_repo_root(git_repo: Path) -> None:
    sub = git_repo / "src"
    sub.mkdir()
    code, output = _init(sub)
    assert code == 0, output
    assert (git_repo / "jev.toml").exists()
    assert not (sub / "jev.toml").exists()


def test_init_appends_to_existing_gitignore_once(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("*.pyc")  # no trailing newline
    assert _init(git_repo)[0] == 0
    assert _init(git_repo, "--force")[0] == 0
    assert (git_repo / ".gitignore").read_text() == "*.pyc\n.jev/\n"


def test_init_recognises_equivalent_gitignore_entry(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("/.jev\n")
    assert _init(git_repo)[0] == 0
    assert (git_repo / ".gitignore").read_text() == "/.jev\n"


def test_init_refuses_to_overwrite(git_repo: Path) -> None:
    (git_repo / "jev.toml").write_text("# mine\n")
    code, output = _init(git_repo)
    assert code == 1
    assert "--force" in output
    assert (git_repo / "jev.toml").read_text() == "# mine\n"


def test_init_force_overwrites(git_repo: Path) -> None:
    (git_repo / "jev.toml").write_text("# mine\n")
    code, output = _init(git_repo, "--force")
    assert code == 0, output
    assert (git_repo / "jev.toml").read_text() == DEFAULT_CONFIG_TOML


def test_init_fails_outside_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    code, output = _init(plain)
    assert code == 1
    assert "not inside a git repository" in output
    assert not (plain / "jev.toml").exists()


def test_doctor_passes_after_init(git_repo: Path, all_tools: None) -> None:
    assert _init(git_repo)[0] == 0
    result = runner.invoke(app, ["doctor", "--repo", str(git_repo)])
    assert result.exit_code == 0, result.output
    assert "fail" not in result.output
    assert "OPENROUTER_API_KEY is set" in result.output
    assert "sk-super-secret-value" not in result.output
    assert "[budget] caps" in " ".join(result.output.split())  # not eaten as Rich markup


def test_doctor_fails_on_broken_config(git_repo: Path, all_tools: None) -> None:
    assert _init(git_repo)[0] == 0
    (git_repo / "jev.toml").write_text("[budget]\nper_task_usd = -1\n")
    result = runner.invoke(app, ["doctor", "--repo", str(git_repo)])
    assert result.exit_code == 1
    assert "per_task_usd" in result.output


def test_doctor_fails_outside_git_repo(tmp_path: Path, all_tools: None) -> None:
    result = runner.invoke(app, ["doctor", "--repo", str(tmp_path)])
    assert result.exit_code == 1


def test_doctor_warns_on_missing_tools_and_fails_unusable_roles(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(git_repo)[0] == 0
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    checks = {c.name: c for c in doctor_cmd.run_checks(git_repo)}
    assert checks["worker claude_code"].status == "warn"
    assert checks["worker api"].status == "warn"
    assert checks["role coder"].status == "fail"


def test_doctor_only_codex_and_api_is_enough_for_all_roles(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(git_repo)[0] == 0
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: "/bin/codex" if cmd == "codex" else None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    checks = doctor_cmd.run_checks(git_repo)
    assert [c.name for c in checks if c.status == "fail"] == []


def test_doctor_reports_jev_dispatcher_key(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _init(git_repo)[0] == 0
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-super-secret-value")
    check = {c.name: c for c in doctor_cmd.run_checks(git_repo)}["dispatcher"]
    assert check.status == "ok"
    assert "~typesafe/jev-latest" in check.detail and "OPENROUTER_API_KEY is set" in check.detail
    assert "sk-super-secret-value" not in check.detail

    monkeypatch.delenv("OPENROUTER_API_KEY")
    check = {c.name: c for c in doctor_cmd.run_checks(git_repo)}["dispatcher"]
    assert check.status == "warn" and "fall back to the rules dispatcher" in check.detail


def test_doctor_dispatcher_rules_and_native_jev(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    (git_repo / "jev.toml").write_text('[dispatcher]\nprimary = "rules"\n')
    check = {c.name: c for c in doctor_cmd.run_checks(git_repo)}["dispatcher"]
    assert (check.status, check.detail) == ("ok", "rules")

    (git_repo / "jev.toml").write_text(
        '[dispatcher.jev]\nbase_url = "https://api.typesafe.ai/v1"\nmodel = "jev-latest"\n'
        'api_key_env = "TYPESAFE_API_KEY"\n'
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-value-123")
    check = {c.name: c for c in doctor_cmd.run_checks(git_repo)}["dispatcher"]
    assert check.status == "ok" and "TYPESAFE_API_KEY is set" in check.detail
    assert "ts-secret-value-123" not in check.detail


def test_doctor_warns_on_dangerous_permissions(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    names = {c.name for c in doctor_cmd.run_checks(git_repo)}
    assert not names & {"claude_code permissions", "codex sandbox"}

    (git_repo / "jev.toml").write_text(
        "[workers.claude_code]\nskip_permissions = true\n\n"
        '[workers.codex]\nsandbox = "danger-full-access"\n'
        'extra_args = ["--dangerously-bypass-approvals-and-sandbox"]\n'
    )
    checks = {c.name: c for c in doctor_cmd.run_checks(git_repo)}
    assert checks["claude_code permissions"].status == "warn"
    assert "dangerously-skip-permissions" in checks["claude_code permissions"].detail
    assert checks["codex sandbox"].status == "warn"
    assert checks["codex extra_args"].status == "warn"


def test_doctor_checks_prompt_files(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    names = {c.name for c in doctor_cmd.run_checks(git_repo)}
    assert not any(name.endswith(" prompt") for name in names)

    (git_repo / "jev.toml").write_text(
        '[roles.planner]\nprompt_file = "prompts/planner.md"\n'
        '[roles.coder]\nprompt_file = "prompts/coder.md"\n'
        '[roles.reviewer]\nprompt_file = "prompts/reviewer.md"\n'
    )
    (git_repo / "prompts").mkdir()
    (git_repo / "prompts" / "planner.md").write_text("Plan {{ idea }}\n")
    (git_repo / "prompts" / "coder.md").write_text("{% if task %}\nunclosed\n")
    checks = {c.name: c for c in doctor_cmd.run_checks(git_repo)}
    assert checks["role planner prompt"].status == "ok"
    assert checks["role coder prompt"].status == "fail"
    assert "syntax error on line" in checks["role coder prompt"].detail
    assert checks["role reviewer prompt"].status == "fail"
    assert "file not found" in checks["role reviewer prompt"].detail
    assert "reviewer.md" in checks["role reviewer prompt"].detail

    result = runner.invoke(app, ["doctor", "--repo", str(git_repo)])
    assert result.exit_code == 1


def test_doctor_notes_codex_has_no_cost(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    check = {c.name: c for c in doctor_cmd.run_checks(git_repo)}["codex cost"]
    assert check.status == "note"
    assert "max_task_attempts" in check.detail and "timeout_s" in check.detail

    (git_repo / "jev.toml").write_text("[workers.codex]\nenabled = false\n")
    assert "codex cost" not in {c.name for c in doctor_cmd.run_checks(git_repo)}
