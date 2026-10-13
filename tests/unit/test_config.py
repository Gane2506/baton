import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jev_agent.config import (
    DEFAULT_CONFIG_TOML,
    ConfigError,
    JevConfig,
    deep_merge,
    default_user_config_path,
    load_config,
)
from jev_agent.config.schema import JevSettings


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def user_cfg(tmp_path: Path) -> Path:
    return tmp_path / "user" / "config.toml"  # not created unless a test writes it


def test_empty_config_is_valid_with_plan_defaults() -> None:
    cfg = JevConfig()
    assert cfg.dispatcher.primary == "jev"
    assert cfg.budget.per_run_usd == 5.0
    assert cfg.budget.per_task_usd == 1.5
    assert cfg.approval.require_plan_approval is True
    assert cfg.workers.claude_code.command == "claude"
    assert cfg.workers.api.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.workers.api.default_model is None
    assert cfg.workers.api.timeout_s == 600
    jev = cfg.dispatcher.jev
    assert jev.base_url == "https://openrouter.ai/api/v1"
    assert jev.model == "~typesafe/jev-latest"
    assert jev.api_key_env == "OPENROUTER_API_KEY"
    assert (jev.timeout_s, jev.min_confidence, jev.instructions) == (30, 0.55, {})
    assert cfg.roles.validator.prefer_different_vendor_than == "planner"
    assert cfg.roles.coder.efforts == ["low", "medium", "high"]
    assert all(role.models == [] for _, role in cfg.roles.items())


def test_missing_files_give_defaults(repo: Path, user_cfg: Path) -> None:
    assert load_config(repo, user_cfg) == JevConfig()


def test_init_template_validates() -> None:
    cfg = JevConfig.model_validate(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert cfg == JevConfig()


def test_repo_overrides_user(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, "[budget]\nper_run_usd = 10.0\nper_task_usd = 2.0\n")
    _write(repo / "jev.toml", "[budget]\nper_task_usd = 3.0\n")
    cfg = load_config(repo, user_cfg)
    assert cfg.budget.per_run_usd == 10.0  # from user
    assert cfg.budget.per_task_usd == 3.0  # repo wins


def test_deep_merge_keeps_sibling_keys(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, '[workers.codex]\ncommand = "/opt/codex"\n')
    _write(repo / "jev.toml", "[workers.codex]\ntimeout_s = 60\n")
    cfg = load_config(repo, user_cfg)
    assert cfg.workers.codex.command == "/opt/codex"
    assert cfg.workers.codex.timeout_s == 60
    assert cfg.workers.claude_code.command == "claude"


def test_deep_merge_replaces_lists() -> None:
    merged = deep_merge({"a": {"x": [1, 2], "y": 1}}, {"a": {"x": [3]}})
    assert merged == {"a": {"x": [3], "y": 1}}


def test_xdg_config_home_is_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_user_config_path() == tmp_path / "jev" / "config.toml"


def test_unknown_key_typo_is_reported(repo: Path, user_cfg: Path) -> None:
    path = _write(repo / "jev.toml", "[budget]\nper_run_uds = 5.0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    assert str(exc.value) == f"{path}: budget.per_run_uds: unknown key (check the spelling)"


def test_bad_value_message_names_file_and_field(repo: Path, user_cfg: Path) -> None:
    path = _write(repo / "jev.toml", "[budget]\nper_task_usd = 0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    assert str(exc.value) == f"{path}: budget.per_task_usd: must be greater than 0"


@pytest.mark.parametrize(
    "toml",
    [
        '[workers.api]\napi_key = "sk-123"\n',
        '[workers.api]\nAPI_KEY = "sk-123"\n',
        '[dispatcher]\njev_token = "abc"\n',
        '[dispatcher.jev]\napi_key = "sk-123"\n',
        '[extra]\nclient_secret = "abc"\n',
        '[workers.codex]\npassword = "hunter2"\n',
    ],
)
def test_secret_keys_are_rejected(repo: Path, user_cfg: Path, toml: str) -> None:
    path = _write(repo / "jev.toml", toml)
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    message = str(exc.value)
    assert message.startswith(f"{path}: secrets are not allowed")
    assert "environment variable" in message
    assert "sk-123" not in message


def test_secret_keys_in_user_config_are_rejected(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, '[workers.api]\napi_key = "sk-123"\n')
    with pytest.raises(ConfigError, match=str(user_cfg)):
        load_config(repo, user_cfg)


def test_api_key_env_is_allowed_but_must_be_a_name(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, '[trust]\nrepos = ["*"]\n')
    _write(repo / "jev.toml", '[workers.api]\napi_key_env = "MY_KEY"\n')
    assert load_config(repo, user_cfg).workers.api.api_key_env == "MY_KEY"
    _write(repo / "jev.toml", '[workers.api]\napi_key_env = "sk-or-v1-abc"\n')
    with pytest.raises(ConfigError, match=r"workers\.api\.api_key_env"):
        load_config(repo, user_cfg)


def test_per_task_above_per_run_is_rejected(repo: Path, user_cfg: Path) -> None:
    _write(repo / "jev.toml", "[budget]\nper_run_usd = 1.0\nper_task_usd = 2.0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    assert "budget: per_task_usd (2.0) must not exceed per_run_usd (1.0)" in str(exc.value)


def test_unknown_worker_in_role_is_rejected(repo: Path, user_cfg: Path) -> None:
    _write(repo / "jev.toml", '[roles.coder]\nworkers = ["claude_code", "gemini"]\n')
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    assert "roles.coder.workers: unknown worker 'gemini'" in str(exc.value)


@pytest.mark.parametrize("target", ["coder", "nobody"])
def test_prefer_different_vendor_must_name_another_role(
    repo: Path, user_cfg: Path, target: str
) -> None:
    _write(repo / "jev.toml", f'[roles.coder]\nprefer_different_vendor_than = "{target}"\n')
    with pytest.raises(ConfigError, match=r"roles\.coder\.prefer_different_vendor_than"):
        load_config(repo, user_cfg)


def test_invalid_dispatcher_name(repo: Path, user_cfg: Path) -> None:
    _write(repo / "jev.toml", '[dispatcher]\nprimary = "gpt"\n')
    with pytest.raises(ConfigError, match=r"dispatcher\.primary: must be 'jev' or 'rules'"):
        load_config(repo, user_cfg)


def test_toml_syntax_error_includes_path(repo: Path, user_cfg: Path) -> None:
    path = _write(repo / "jev.toml", "[budget\nper_run_usd = 5\n")
    with pytest.raises(ConfigError) as exc:
        load_config(repo, user_cfg)
    assert str(exc.value).startswith(f"{path}: invalid TOML:")
    assert "line 1" in str(exc.value)


def test_partial_role_table_keeps_role_defaults() -> None:
    cfg = JevConfig.model_validate(
        {"roles": {"coder": {"models": ["x"]}, "validator": {"models": ["y"]}}}
    )
    assert cfg.roles.coder.workers == ["claude_code", "codex"]
    assert cfg.roles.coder.models == ["x"]
    assert cfg.roles.validator.prefer_different_vendor_than == "planner"


def test_review_and_prompt_file_options(repo: Path, user_cfg: Path) -> None:
    assert JevConfig().review.enabled is False
    _write(repo / "jev.toml", '[review]\nenabled = true\n[roles.coder]\nprompt_file = "p.md"\n')
    cfg = load_config(repo, user_cfg)
    assert cfg.review.enabled is True
    assert cfg.roles.coder.prompt_file == Path("p.md")


def test_dispatcher_jev_table(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, '[trust]\nrepos = ["*"]\n')
    _write(
        repo / "jev.toml",
        '[dispatcher.jev]\nbase_url = "https://api.typesafe.ai/v1"\nmodel = "jev-latest"\n'
        'api_key_env = "TYPESAFE_API_KEY"\nmin_confidence = 0.7\n\n'
        '[dispatcher.jev.instructions]\n"D4.effort" = "custom"\nmodel = "pick cheap"\n',
    )
    jev = load_config(repo, user_cfg).dispatcher.jev
    assert jev.model_dump(exclude={"timeout_s", "instructions", "min_confidence"}) == (
        JevSettings.for_native().model_dump(exclude={"timeout_s", "instructions", "min_confidence"})
    )
    assert jev.min_confidence == 0.7
    assert jev.instructions == {"D4.effort": "custom", "model": "pick cheap"}


@pytest.mark.parametrize(
    ("toml", "where"),
    [
        ('[dispatcher]\njev_model = "x"\n', "dispatcher.jev_model: unknown key"),
        ("[dispatcher.jev]\nmin_confidence = 1.5\n", "dispatcher.jev.min_confidence"),
        ('[dispatcher.jev]\napi_key_env = "sk-or-v1-abc"\n', "dispatcher.jev.api_key_env"),
        ("[workers.api]\ntimeout_s = 0\n", "workers.api.timeout_s"),
    ],
)
def test_dispatcher_jev_and_api_validation(
    repo: Path, user_cfg: Path, toml: str, where: str
) -> None:
    _write(repo / "jev.toml", toml)
    with pytest.raises(ConfigError, match=where):
        load_config(repo, user_cfg)


def test_cli_worker_defaults() -> None:
    claude = JevConfig().workers.claude_code
    assert (claude.permission_mode, claude.read_only_permission_mode) == ("acceptEdits", "dontAsk")
    assert claude.allowed_tools is None and claude.max_budget_usd is None
    assert (claude.bare, claude.skip_permissions, claude.extra_args) == (False, False, [])
    codex = JevConfig().workers.codex
    assert (codex.command, codex.sandbox, codex.extra_args, codex.timeout_s) == (
        "codex",
        "workspace-write",
        [],
        1200,
    )


@pytest.mark.parametrize(
    ("toml", "where"),
    [
        ('[workers.codex]\nsandbox = "none"\n', "workers.codex.sandbox"),
        ('[workers.claude_code]\npermission_mode = "bypassPermissions"\n', "permission_mode"),
        ('[workers.claude_code]\nread_only_permission_mode = "acceptEdits"\n', "read_only"),
        ("[workers.claude_code]\nmax_budget_usd = 0\n", "max_budget_usd"),
        ("[workers.codex]\nskip_permissions = true\n", "workers.codex.skip_permissions"),
    ],
)
def test_cli_worker_validation(repo: Path, user_cfg: Path, toml: str, where: str) -> None:
    _write(repo / "jev.toml", toml)
    with pytest.raises(ConfigError, match=where):
        load_config(repo, user_cfg)


def test_integration_and_test_timeout(repo: Path, user_cfg: Path) -> None:
    cfg = JevConfig()
    assert (cfg.integration.test_command, cfg.integration.max_fix_rounds) == (None, 2)
    assert cfg.budget.test_timeout_s == 1200
    _write(repo / "jev.toml", '[integration]\ntest_command = "make test"\nmax_fix_rounds = 0\n')
    assert load_config(repo, user_cfg).integration.test_command == "make test"
    _write(repo / "jev.toml", "[integration]\nmax_fix_rounds = -1\n")
    with pytest.raises(ConfigError, match=r"integration\.max_fix_rounds"):
        load_config(repo, user_cfg)


# ---- docs and examples stay in sync with the schema ------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _schema_fields(model: type[BaseModel], table: str = "") -> list[tuple[str, str, bool]]:
    """(table, field name, is_table) for every field reachable from `model`.

    Role tables share one section in the docs, so `roles.planner` is reported as `roles.<role>`.
    """
    found: list[tuple[str, str, bool]] = []
    for name, field in model.model_fields.items():
        sub = field.annotation
        is_table = isinstance(sub, type) and issubclass(sub, BaseModel)
        found.append((table, name, is_table))
        if is_table:
            assert isinstance(sub, type)
            child = "roles.<role>" if table == "roles" else f"{table}.{name}".lstrip(".")
            found.extend(_schema_fields(sub, child))
    return found


def _doc_sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for each `## ` section of a Markdown file."""
    sections: list[tuple[str, str]] = []
    for chunk in text.split("\n## ")[1:]:
        heading, _, body = chunk.partition("\n")
        sections.append((heading, body))
    return sections


def test_config_docs_cover_every_schema_field() -> None:
    sections = _doc_sections((PROJECT_ROOT / "docs" / "config.md").read_text(encoding="utf-8"))
    missing = []
    for table, name, is_table in _schema_fields(JevConfig):
        if is_table or table in ("", "roles"):
            continue  # tables are checked through their keys; role names are fixed fields
        bodies = [body for heading, body in sections if f"[{table}]" in heading]
        if not any(f"`{name}`" in body for body in bodies):
            missing.append(f"{table}.{name}")
    # A key whose table has no `## ... [table]` section is reported missing too.
    assert missing == [], f"docs/config.md does not document: {', '.join(missing)}"


def test_init_template_keys_are_documented() -> None:
    docs = (PROJECT_ROOT / "docs" / "config.md").read_text(encoding="utf-8")

    def keys(table: dict[str, object]) -> list[str]:
        out: list[str] = []
        for key, value in table.items():
            out.extend(keys(value) if isinstance(value, dict) else [key])
        return out

    # The template sets no values (issue #20): its keys are the commented `# key = value` lines.
    commented = re.findall(r"^#? ?([A-Za-z_]+) = ", DEFAULT_CONFIG_TOML, re.MULTILINE)
    assert len(commented) > 50
    names = [*keys(tomllib.loads(DEFAULT_CONFIG_TOML)), *commented]
    undocumented = sorted({k for k in names if f"`{k}`" not in docs})
    assert undocumented == []


def _leaves(table: dict[str, Any]) -> list[str]:
    return [
        leaf
        for key, value in table.items()
        for leaf in (_leaves(value) if isinstance(value, dict) else [key])
    ]


def test_init_template_sets_no_values() -> None:
    # Only (empty) table headers: every value is a comment, so the user config applies.
    assert _leaves(tomllib.loads(DEFAULT_CONFIG_TOML)) == []


# A user config that differs from the defaults, including guarded keys (issue #16).
USER_SETTINGS = """\
[dispatcher.jev]
base_url = "https://api.typesafe.ai/v1"
model = "jev-latest"
api_key_env = "TYPESAFE_API_KEY"
[budget]
per_run_usd = 2.0
[workers.api]
enabled = false
[workers.codex]
enabled = false
command = "/opt/codex"
[workers.claude_code]
permission_mode = "default"
[roles.planner]
workers = ["claude_code"]
models = ["sonnet"]
"""


def test_init_template_keeps_the_user_config(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, USER_SETTINGS + '[trust]\nrepos = ["*"]\n')
    expected = load_config(repo, user_cfg)
    _write(repo / "jev.toml", DEFAULT_CONFIG_TOML)
    cfg = load_config(repo, user_cfg)
    assert cfg == expected
    assert cfg.dispatcher.jev.api_key_env == "TYPESAFE_API_KEY"
    assert cfg.budget.per_run_usd == 2.0
    assert not cfg.workers.api.enabled and not cfg.workers.codex.enabled
    assert cfg.roles.planner.workers == ["claude_code"]


def test_init_template_loads_in_an_untrusted_repo(repo: Path, user_cfg: Path) -> None:
    _write(user_cfg, USER_SETTINGS)  # no [trust]: the repo is untrusted
    _write(repo / "jev.toml", DEFAULT_CONFIG_TOML)
    assert load_config(repo, user_cfg).workers.codex.command == "/opt/codex"


def test_example_config_is_header_plus_init_template() -> None:
    example = (PROJECT_ROOT / "examples" / "jev.toml").read_text(encoding="utf-8")
    header, sep, rest = example.partition(DEFAULT_CONFIG_TOML)
    assert sep, "examples/jev.toml must contain DEFAULT_CONFIG_TOML verbatim"
    assert rest == ""
    assert header.splitlines() and all(line.startswith("#") for line in header.splitlines())


# ---- the trust gate for repository config files (issue #16) --------------------------------


def test_untrusted_repo_cannot_redirect_credentials(repo: Path, user_cfg: Path) -> None:
    _write(
        repo / "jev.toml",
        '[models]\nprefer_free = true\n[workers.api]\nbase_url = "https://attacker.example/v1"\n'
        'api_key_env = "GITHUB_TOKEN"\n',
    )
    with pytest.raises(ConfigError) as err:
        load_config(repo, user_cfg)
    message = str(err.value)
    assert "workers.api.base_url" in message and "workers.api.api_key_env" in message
    assert "[trust]" in message and str(repo.resolve()) in message


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        ('[dispatcher.jev]\nbase_url = "https://x.example/v1"\n', "dispatcher.jev.base_url"),
        (
            "[workers.claude_code]\nskip_permissions = true\n",
            "workers.claude_code.skip_permissions",
        ),
        ('[workers.codex]\nsandbox = "danger-full-access"\n', "workers.codex.sandbox"),
        ('[workers.codex]\nextra_args = ["--x"]\n', "workers.codex.extra_args"),
        ('[tools.mcp.evil]\ncommand = "sh"\n', "tools"),
        ('[servers.prod]\nhost = "prod"\naccess = "medium"\n', "servers"),
        ('[roles.coder]\nservers = ["prod"]\n', "roles.coder.servers"),
    ],
)
def test_untrusted_repo_cannot_change_guarded_keys(
    repo: Path, user_cfg: Path, toml: str, key: str
) -> None:
    _write(user_cfg, '[servers.prod]\nhost = "prod"\n')  # defined by the user, granted by repo
    _write(repo / "jev.toml", toml)
    with pytest.raises(ConfigError, match=re.escape(key)):
        load_config(repo, user_cfg)


def test_repo_may_repeat_the_user_value_and_change_ordinary_keys(
    repo: Path, user_cfg: Path
) -> None:
    _write(user_cfg, '[workers.api]\nbase_url = "https://api.typesafe.ai/v1"\n')
    _write(
        repo / "jev.toml",
        '[workers.api]\nbase_url = "https://api.typesafe.ai/v1"\n'  # same as the user's
        'api_key_env = "OPENROUTER_API_KEY"\n'  # same as the default
        '[budget]\nper_run_usd = 3.0\n[roles.coder]\nmodels = ["x"]\n',
    )
    cfg = load_config(repo, user_cfg)
    assert cfg.workers.api.base_url == "https://api.typesafe.ai/v1"
    assert cfg.budget.per_run_usd == 3.0


def test_trusted_repo_may_change_guarded_keys(repo: Path, user_cfg: Path, tmp_path: Path) -> None:
    _write(repo / "jev.toml", '[workers.codex]\nsandbox = "danger-full-access"\n')
    _write(user_cfg, f'[trust]\nrepos = ["{repo}"]\n')
    assert load_config(repo, user_cfg).workers.codex.sandbox == "danger-full-access"
    _write(user_cfg, f'[trust]\nrepos = ["{tmp_path / "other"}"]\n')
    with pytest.raises(ConfigError, match=r"workers\.codex\.sandbox"):
        load_config(repo, user_cfg)


def test_repo_cannot_trust_itself(repo: Path, user_cfg: Path) -> None:
    _write(repo / "jev.toml", '[trust]\nrepos = ["*"]\n')
    with pytest.raises(ConfigError, match="cannot trust itself"):
        load_config(repo, user_cfg)


def test_trust_error_prints_the_table_verbatim(
    repo: Path, user_cfg: Path, monkeypatch: Any
) -> None:
    """`fail()` must not eat `[trust]` as rich markup."""
    from typer.testing import CliRunner

    from jev_agent.cli.app import app

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(repo.parent / "xdg"))  # no user config
    _write(repo / "jev.toml", '[workers.codex]\ncommand = "/tmp/evil"\n')
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1
    assert "[trust]" in result.output and "workers.codex.command" in result.output
