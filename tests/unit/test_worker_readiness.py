"""Worker readiness (#19): runs and `jev doctor` drop the same unready workers, per role."""

import subprocess
from pathlib import Path

import pytest

from jev_agent.app import context
from jev_agent.app.context import FAKE_WORKERS_ENV, build_registry, worker_readiness
from jev_agent.cli import doctor_cmd
from jev_agent.config import JevConfig, load_config
from jev_agent.dispatch.requests import role_options
from jev_agent.workers.fake import FakeWorker
from jev_agent.workers.openrouter_models import ModelCatalog, ModelInfo

KEY = "OPENROUTER_API_KEY"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(context, "which", lambda cmd: f"/fake/bin/{cmd}")


def _config(data: dict[str, object] | None = None) -> JevConfig:
    return JevConfig.model_validate(data or {})


def test_api_without_key_is_dropped_with_a_warning() -> None:
    registry, warnings = build_registry(_config({"workers": {"api": {"default_model": "m/x"}}}))
    assert sorted(registry.names()) == ["claude_code", "codex"]
    assert [w for w in warnings if "worker api" in w] == [
        f"worker api is not used: {KEY} is not set"
    ]


def test_api_without_any_model_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, "sk-or-v1-fake")
    registry, warnings = build_registry(_config())
    assert "api" not in registry.names()
    assert len(warnings) == 1 and warnings[0].startswith("worker api is not used:")
    assert "default_model" in warnings[0]


def test_missing_cli_binary_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context, "which", lambda cmd: None if cmd == "codex" else f"/bin/{cmd}")
    registry, warnings = build_registry(_config({"workers": {"api": {"enabled": False}}}))
    assert registry.names() == ["claude_code"]
    assert warnings == [
        "worker codex is not used: `codex` not found on PATH; install or disable it"
    ]


def test_default_config_without_key_gives_code_reviewer_codex_first() -> None:
    config = _config()
    registry, _ = build_registry(config)
    options = role_options(config, "code_reviewer", registry.names("code_reviewer"))
    assert options.workers[0] == "codex"


def test_api_is_offered_only_to_roles_with_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, "sk-or-v1-fake")
    config = _config(
        {
            "roles": {
                "planner": {"models": ["anthropic/claude-sonnet-4"]},
                "learner": {"models": ["sonnet"]},  # a CLI model name: api cannot run it
            }
        }
    )
    registry, warnings = build_registry(config)
    assert "api" in registry.names()
    assert "api" in registry.names("planner")
    assert "api" not in registry.names("learner")
    assert "api" not in registry.names("reviewer")
    assert "api" not in registry.names("code_reviewer")
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "worker api is not offered to validator, reviewer, code_reviewer, learner: no model"
    )


def test_free_models_count_as_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, "sk-or-v1-fake")
    catalog = ModelCatalog(
        [ModelInfo("vendor/m:free", "m", 1000, 0.0, 0.0, ("structured_outputs",))], 0.0
    )
    config = _config(
        {"models": {"prefer_free": True}, "roles": {"coder": {"workers": ["codex", "api"]}}}
    )
    readiness = worker_readiness(config, catalog)
    assert readiness.ready_for("reviewer", "api")
    assert not readiness.ready_for("coder", "api")  # the api worker cannot edit files
    assert not worker_readiness(config, None).ready("api")


def test_fake_workers_skip_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setattr(context, "which", lambda cmd: None)
    registry, warnings = build_registry(_config())
    assert sorted(registry.names("reviewer")) == ["api", "claude_code", "codex"]
    assert all(isinstance(registry.get(n), FakeWorker) for n in registry.names())
    assert warnings == []


@pytest.mark.parametrize(
    ("key", "found", "toml"),
    [
        (False, {"claude", "codex"}, ""),
        (True, {"claude", "codex"}, ""),
        (True, {"codex"}, '[workers.api]\ndefault_model = "m/x"\n'),
        (True, set(), '[roles.planner]\nmodels = ["m/x"]\n'),
    ],
)
def test_doctor_and_wiring_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: bool,
    found: set[str],
    toml: str,
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "jev.toml").write_text(toml)
    if key:
        monkeypatch.setenv(KEY, "sk-or-v1-fake")

    def which(cmd: str) -> str | None:
        return f"/bin/{cmd}" if cmd in found else None

    monkeypatch.setattr(context, "which", which)
    monkeypatch.setattr(doctor_cmd, "which", which)
    config = load_config(repo)
    registry, _ = build_registry(config)
    checks = {c.name: c for c in doctor_cmd.run_checks(repo)}
    for role, rc in config.roles.items():
        offered = [w for w in rc.workers if w in registry.names(role)]
        check = checks[f"role {role}"]
        if offered:
            assert check.status == "ok" and check.detail.split(";")[0] == ", ".join(offered)
        else:
            assert "no usable worker" in check.detail
