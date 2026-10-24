"""Regression tests for the 2026-10-16 code audit (Worker E: config + CLI + app).

Each test pins the behavior an audit finding asked for, asserting the user-visible
outcome (exit codes, output text, error contracts) rather than the implementation.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from jev_agent.app import doctor as app_doctor
from jev_agent.app import format as app_format
from jev_agent.app import runs as app_runs
from jev_agent.cli import doctor_cmd
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import DEFAULT_CONFIG_TOML, JevConfig
from jev_agent.config.schema import (
    BEGINNER_FIELDS,
    ApiWorkerConfig,
    find_secret_keys,
)
from jev_agent.config.servers_schema import NO_PROFILE, ServerConfig
from jev_agent.core.chat_learning import ChatLearning
from jev_agent.core.chat_session import META_FILE, ChatMeta, chats_dir
from jev_agent.core.design_system import save_design_system
from jev_agent.core.learning import Learning
from jev_agent.core.models import Idea
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def _trust_this_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # These tests model the user's own repository: trust it, so the real defaults
    # (not the suite's rules-dispatcher user config) drive the dispatcher checks.
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text('[trust]\nrepos = ["*"]\n')


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def _jev(repo: Path, *args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)], input=input)
    return result.exit_code, result.output


# ---- config/schema.py: [cache.semantic] Literal constraints (audit major 17) ----


def test_semantic_embedder_typo_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        JevConfig.model_validate({"cache": {"semantic": {"embedder": "simhas"}}})
    assert "embedder" in str(exc_info.value)


def test_semantic_mode_typo_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        JevConfig.model_validate({"cache": {"semantic": {"mode": "servee"}}})
    assert "servee" in str(exc_info.value)  # the typo is named, not silently inert


@pytest.mark.parametrize("embedder", ["simhash", "openrouter"])
@pytest.mark.parametrize("mode", ["off", "shadow", "serve"])
def test_semantic_valid_values_accepted(embedder: str, mode: str) -> None:
    cfg = JevConfig.model_validate({"cache": {"semantic": {"embedder": embedder, "mode": mode}}})
    assert cfg.cache.semantic.embedder == embedder
    assert cfg.cache.semantic.mode == mode


# ---- config/schema.py: cache numeric bounds (audit config minor 1) ----


@pytest.mark.parametrize(
    "field", ["decision_threshold", "role_threshold", "chat_threshold", "min_agreement"]
)
@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_cache_similarity_bounds(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        JevConfig.model_validate({"cache": {"semantic": {field: bad}}})


@pytest.mark.parametrize(
    ("table", "field", "bad"),
    [("cache", "ttl_hours", 0), ("cache", "ttl_hours", -5), ("cache", "max_mb", -1)],
)
def test_cache_positive_bounds(table: str, field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        JevConfig.model_validate({table: {field: bad}})


# ---- config/schema.py: _SECRET_KEY names (audit config nit) ----


@pytest.mark.parametrize("name", ["passphrase", "authorization", "bearer", "auth"])
def test_secret_key_names_flagged(name: str) -> None:
    assert find_secret_keys({"extra": {name: "hunter2"}}) == [f"extra.{name}"]


# ---- config/schema.py: ApiWorkerConfig.base_url scheme (audit workers minor 3) ----


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1", "http://localhost:8080/v1"])
def test_api_worker_base_url_accepts_http_schemes(url: str) -> None:
    assert ApiWorkerConfig(base_url=url).base_url == url


@pytest.mark.parametrize("url", ["ftp://example.com/v1", "example.com/v1", ""])
def test_api_worker_base_url_rejects_other_schemes(url: str) -> None:
    with pytest.raises(ValidationError):
        ApiWorkerConfig(base_url=url)


# ---- config/servers_schema.py: workdir system-dir check (audit config minor 2) ----


@pytest.mark.parametrize("workdir", ["/etc", "/", "/root/.ssh", "/root/.ssh/"])
def test_server_workdir_rejects_system_dirs(workdir: str) -> None:
    with pytest.raises(ValidationError):
        ServerConfig(host="example.com", workdir=workdir)


def test_server_workdir_accepts_normal_dir() -> None:
    assert ServerConfig(host="example.com", workdir="/srv/app").workdir == "/srv/app"


# ---- config/defaults.py: template completeness (audit config minor 3) ----


def _schema_tables(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths of every nested config table (dynamic `dict` sub-tables skipped)."""
    tables: list[str] = []
    for name, field in model.model_fields.items():
        ann = field.annotation
        if get_origin(ann) is Union:
            ann = next(a for a in get_args(ann) if a is not type(None))
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            path = f"{prefix}.{name}" if prefix else name
            tables.append(path)
            tables.extend(_schema_tables(ann, path))
    return tables


def test_template_covers_every_schema_table() -> None:
    headers = re.findall(r"^#?\s*\[([^\]]+)\]", DEFAULT_CONFIG_TOML, re.MULTILINE)
    missing = [
        table
        for table in _schema_tables(JevConfig)
        if not any(h == table or h.startswith(table + ".") for h in headers)
    ]
    assert missing == []


# ---- BEGINNER_FIELDS pin (audit config minor 4) ----


def test_beginner_fields_resolve_to_schema() -> None:
    assert len(BEGINNER_FIELDS) == 11
    for dotted in BEGINNER_FIELDS:
        node: object = JevConfig
        for part in dotted.split("."):
            assert isinstance(node, type) and issubclass(node, BaseModel)
            assert part in node.model_fields, f"{dotted} does not resolve in the schema"
            ann = node.model_fields[part].annotation
            if get_origin(ann) is Union:
                ann = next(a for a in get_args(ann) if a is not type(None))
            node = ann


# ---- app/context.py: corrupt state.db error contract (audit cli-b major 1) ----


def test_corrupt_state_db_is_a_one_line_error(fixture_repo: Path) -> None:
    db = fixture_repo / ".jev" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(os.urandom(100))  # a file that is not a database
    result = runner.invoke(app, ["status", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "state database is corrupt" in result.stderr
    assert "Traceback" not in result.stderr


# ---- app/doctor.py: native-key-only dispatcher check (audit app major 1) ----


def _dispatcher_check(git_repo: Path) -> app_doctor.Check:
    return {c.name: c for c in doctor_cmd.run_checks(git_repo)}["dispatcher"]


def test_doctor_native_key_only_is_not_a_fail(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    check = _dispatcher_check(git_repo)
    assert check.status == "ok"
    assert "TYPESAFE_API_KEY is set" in check.detail
    assert "ts-test-key" not in check.detail


def test_doctor_neither_key_warns_under_jev(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The consensus dispatcher is gone (issue #193): with no Jev key at all, the
    # default jev dispatcher warns and every decision falls back to rules.
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    check = _dispatcher_check(git_repo)
    assert check.status == "warn"


# ---- app/doctor.py: bounded DNS lookup (audit cli-a minor 2 / app minor 8) ----


def test_resolve_host_bounded_when_dns_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(host: str, port: int, *args: object, **kwargs: object) -> None:
        threading.Event().wait(60)  # the resolver never answers

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    for resolve in (app_doctor._resolve_host, doctor_cmd.resolve_host):
        start = time.monotonic()
        assert resolve("example.com") is False
        assert time.monotonic() - start < 30  # bounded wait, not the 60s hang


# ---- app/format.py: short() width floor (audit app minor 5) ----


@pytest.mark.parametrize(
    ("width", "expected"), [(0, ""), (1, "h"), (2, "he"), (3, "hel"), (4, "h...")]
)
def test_short_never_exceeds_width(width: int, expected: str) -> None:
    assert app_format.short("hello world", width) == expected


def test_short_unchanged_for_fitting_text() -> None:
    assert app_format.short("hello world", 50) == "hello world"
    assert all(len(app_format.short("hello world", w)) <= w for w in range(0, 20))


# ---- app/runs.py: NO_PROFILE import is top-level (audit app minor 4) ----


def test_runs_exposes_no_profile_at_module_level() -> None:
    assert app_runs.NO_PROFILE is NO_PROFILE


# ---- cli/design_cmds.py: design show markup (audit cli-a major 1) ----


def test_design_show_keeps_bracketed_text(fixture_repo: Path) -> None:
    save_design_system(fixture_repo, "# System\n\nUse [primary] for the [Button] action.", {})
    code, output = _jev(fixture_repo, "design", "show")
    assert code == 0, output
    assert "[primary]" in output and "[Button]" in output


# ---- cli/demo_cmds.py: help text accuracy (audit cli-a minor 1) ----


def test_demo_help_does_not_promise_inspect() -> None:
    result = runner.invoke(app, ["demo", "--help"])
    assert result.exit_code == 0
    assert "remove it" in result.output
    assert "inspect it" not in result.output


# ---- cli/chat_cmds.py: chat --list title redaction (audit cli-a minor 3) ----


def test_chat_list_redacts_secret_in_title(fixture_repo: Path) -> None:
    meta = ChatMeta(
        chat_id="c-20261016-120000-ab12",
        created_at="2026-10-16T12:00:00+00:00",
        updated_at="2026-10-16T12:00:00+00:00",
        learning=ChatLearning(),
        profile=None,
        backend="api",
        model=None,
        brief="debug the sk-or-abcdefgh12345678 login failure",
    )
    chat_dir = chats_dir(fixture_repo) / meta.chat_id
    chat_dir.mkdir(parents=True)
    (chat_dir / META_FILE).write_text(json.dumps(meta.to_json()))
    code, output = _jev(fixture_repo, "chat", "--list")
    assert code == 0, output
    assert "sk-or-abcdefgh12345678" not in output
    assert "[REDACTED]" in output


# ---- cli/learning_cmds.py: show --limit, forget confirmation (audit cli-a minors 4-5) ----


def _add_lesson(repo: Path, profile: str, text: str) -> str:
    with Store.open(repo / ".jev" / "state.db") as store:
        learning = Learning.for_store(store)
        try:
            return learning.db_for(profile).add_lesson(profile, text, ["planner"], []).id
        finally:
            learning.close()


def test_learning_show_limit_caps_output(fixture_repo: Path) -> None:
    code, _ = _jev(fixture_repo, "learning", "create", "default")
    assert code == 0
    for n in range(5):
        _add_lesson(fixture_repo, "default", f"Lesson number {n}.")
    code, output = _jev(fixture_repo, "learning", "show", "--limit", "2")
    assert code == 0, output
    assert "Lesson number 3." in output and "Lesson number 4." in output
    assert "Lesson number 0." not in output and "Lesson number 2." not in output


def test_learning_show_rejects_nonpositive_limit(fixture_repo: Path) -> None:
    code, _ = _jev(fixture_repo, "learning", "show", "--limit", "0")
    assert code == 2  # typer usage error


def test_learning_forget_asks_before_retiring(fixture_repo: Path) -> None:
    code, _ = _jev(fixture_repo, "learning", "create", "default")
    assert code == 0
    lesson_id = _add_lesson(fixture_repo, "default", "A lesson to keep.")
    code, output = _jev(fixture_repo, "learning", "forget", lesson_id, input="n\n")
    assert code == 1  # declined at the prompt
    code, output = _jev(fixture_repo, "learning", "show", "--all")
    assert code == 0, output
    assert lesson_id in output  # still there, not retired


def test_learning_forget_yes_skips_the_prompt(fixture_repo: Path) -> None:
    code, _ = _jev(fixture_repo, "learning", "create", "default")
    assert code == 0
    lesson_id = _add_lesson(fixture_repo, "default", "A lesson to retire.")
    code, output = _jev(fixture_repo, "learning", "forget", lesson_id, "--yes")
    assert code == 0, output
    assert f"Retired {lesson_id}" in output


# ---- cli/cache_cmds.py: --older-than min (audit cli-a minor 6) ----


def test_cache_clear_rejects_negative_older_than(fixture_repo: Path) -> None:
    code, _ = _jev(fixture_repo, "cache", "clear", "--older-than", "-5")
    assert code == 2  # typer usage error, not a silent full clear


# ---- cli/dashboard_cmd.py: empty JEV_DASHBOARD_TOKEN (audit cli-a minor 7) ----


def test_dashboard_empty_token_env_generates_one(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.setenv("JEV_DASHBOARD_TOKEN", "")
    served: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: served.update(kw))
    result = runner.invoke(
        app, ["dashboard", "--host", "0.0.0.0", "--no-open", "--repo", str(fixture_repo)]
    )
    assert result.exit_code == 0, result.output
    assert "?token=" in result.output  # a token was generated, not rejected as too short
    assert served.get("host") == "0.0.0.0"


# ---- cli/init_cmd.py: --demo pre-check before writes (audit cli-a minor 8) ----


def test_init_demo_precheck_runs_before_any_write(fixture_repo: Path) -> None:
    state_db = fixture_repo / ".jev" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.write_bytes(b"existing state")
    code, output = _jev(fixture_repo, "init", "--demo")
    assert code == 1
    assert "already holds state" in output
    assert not (fixture_repo / "jev.toml").exists()  # no partial state left behind


def test_init_demo_failure_notes_the_residue(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_agent.cli import init_cmd

    def boom(root: Path, **kwargs: object) -> None:
        raise RuntimeError("seeder exploded")

    monkeypatch.setattr(init_cmd, "_demo_precheck", lambda root: None)
    monkeypatch.setattr("jev_agent.demo.seeder.seed", boom)
    result = runner.invoke(app, ["init", "--demo", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert "seeder exploded" in result.stderr
    assert "already updated" in result.stderr  # the residue is named, not silent


# ---- cli/run_cmds.py: run() catches PipelineError (audit cli-b minor 1) ----


def test_run_pipeline_error_is_a_one_line_error(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_agent.cli import run_cmds

    async def boom(*args: object, **kwargs: object) -> object:
        raise PipelineError("pipeline exploded mid-advance")

    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setattr(run_cmds, "start_run", boom)
    result = runner.invoke(app, ["run", "an idea", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback
    assert "error:" in result.stderr
    assert "pipeline exploded" in result.stderr


# ---- cli/run_cmds.py: approve() always prints warnings (audit cli-b minor 5) ----


def test_approve_prints_warnings_without_workers(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    code, _ = _jev(fixture_repo, "run", "--plan-only", "add search")
    assert code == 0
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run_id = store.list_runs()[0].id
    monkeypatch.delenv(FAKE_WORKERS_ENV)
    (fixture_repo / "jev.toml").write_text(
        '[dispatcher]\nprimary = "jev"\n'
        + "".join(f"[workers.{n}]\nenabled = false\n" for n in ("claude_code", "codex", "api"))
    )
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0, output
    # The dispatcher warning fires even though no worker is ready (it used to be gated).
    assert "TYPESAFE_API_KEY and OPENROUTER_API_KEY are not set" in output


# ---- cli/run_cmds.py: ship preview branch escaping (audit cli-b nit 1) ----


def test_print_ship_preview_escapes_branch(capsys: pytest.CaptureFixture[str]) -> None:
    from jev_agent.cli import run_cmds
    from jev_agent.core.ship import ShipPreview

    run_cmds.print_ship_preview(
        ShipPreview(title="ok", branch="jev/[evil]-branch", diff=None, vote=None)
    )
    out = capsys.readouterr().out
    assert "jev/[evil]-branch" in out  # printed literally, not swallowed as markup


# ---- cli/queue_cmds.py: --repeat validates --profile (audit cli-b minor 2) ----


def test_add_recurring_rejects_bogus_profile(fixture_repo: Path) -> None:
    code, output = _jev(
        fixture_repo, "queue", "add", "do things", "--repeat", "daily", "--profile", "bogus"
    )
    assert code == 1
    assert "no workspace profile named 'bogus'" in output


def test_add_recurring_accepts_known_profile(fixture_repo: Path) -> None:
    (fixture_repo / "jev.toml").write_text('[profiles.nightly]\ndescription = "n"\n')
    code, output = _jev(
        fixture_repo, "queue", "add", "do things", "--repeat", "daily", "--profile", "nightly"
    )
    assert code == 0, output


# ---- cli/queue_cmds.py: queue list on a corrupt schedule (audit cli-b minor 3) ----


def test_queue_list_renders_invalid_schedule(fixture_repo: Path) -> None:
    code, output = _jev(fixture_repo, "queue", "add", "do things", "--repeat", "daily")
    assert code == 0, output
    with sqlite3.connect(fixture_repo / ".jev" / "state.db") as conn:
        conn.execute("UPDATE recurring_tasks SET schedule_source = 'garbage'")
        conn.commit()
    code, output = _jev(fixture_repo, "queue", "list")
    assert code == 0, output
    assert "invalid schedule" in output


# ---- cli/replay_cmds.py: bisect --export OSError (audit cli-b minor 4) ----


def test_bisect_export_oserror_is_a_one_line_error(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="an idea", repo_path=fixture_repo))
    missing_dir = fixture_repo / "no-such-dir" / "timeline.html"
    result = runner.invoke(
        app, ["bisect", run.id, "--gate", "--export", str(missing_dir), "--repo", str(fixture_repo)]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback
    assert "error:" in result.stderr
    assert "cannot write" in result.stderr
