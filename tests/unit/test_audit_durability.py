"""Regression tests for the 2026-10-16 durability audit — Worker B (P1 fixes).

- `app/settings.py`: atomic config writes, the validate/write TOCTOU, corrupt TOML
- `core/design_system.py`: atomic three-file writes
- `core/backfill.py`: all-or-nothing transactional backfill

Every test asserts observable behavior — file contents after a simulated crash,
error strings, what a retry does — never implementation details.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from tomlkit.exceptions import ParseError as TomlkitParseError

from jev_agent.app import settings
from jev_agent.config.loader import ConfigError, deep_merge
from jev_agent.config.schema import JevConfig


def _write_repo_toml(root: Path, text: str) -> Path:
    path = root / "jev.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ---- atomic config writes (audit app major 2) ---------------------------------------------


def test_config_write_crash_mid_write_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the temp write and the rename must not touch the live file."""
    original = "# the user's comment\n[budget]\nper_run_usd = 5.0\n"
    path = _write_repo_toml(tmp_path, original)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated crash between temp write and rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        settings.write_value(path, "budget.per_run_usd", 9.0)
    assert path.read_bytes() == original.encode("utf-8")
    assert list(path.parent.glob("jev.toml.*.tmp")) == []  # temp cleaned up


def test_config_write_round_trip_preserves_comments(tmp_path: Path) -> None:
    path = _write_repo_toml(tmp_path, "# keep me\n[budget]\nper_run_usd = 5.0\n")
    settings.write_value(path, "budget.per_run_usd", 9.0)
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text  # tomlkit formatting survives the atomic rewrite
    assert tomllib.loads(text)["budget"]["per_run_usd"] == 9.0
    settings.unset_value(path, "budget.per_run_usd")
    assert "per_run_usd" not in tomllib.loads(path.read_text(encoding="utf-8"))["budget"]


# ---- validate/write TOCTOU + corrupt TOML (audit app major 3) ------------------------------


def test_save_writes_validated_bytes_not_the_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent edit landing between validation and write must not leak in."""
    path = _write_repo_toml(tmp_path, "[budget]\nper_run_usd = 5.0\n")
    real_write = settings._write

    def concurrent_edit_then_write(doc_path: Path, doc: object) -> None:
        # Another settings tab saves after this save() validated its change.
        doc_path.write_text(
            "[budget]\nper_run_usd = 100.0\n# unvalidated concurrent edit\n",
            encoding="utf-8",
        )
        real_write(doc_path, doc)

    monkeypatch.setattr(settings, "_write", concurrent_edit_then_write)
    error = settings.save(
        tmp_path, "repo", lambda layer: settings.set_path(layer, "budget.per_run_usd", 9.0)
    )
    assert error is None
    text = path.read_text(encoding="utf-8")
    assert tomllib.loads(text)["budget"]["per_run_usd"] == 9.0
    assert "unvalidated concurrent edit" not in text


def test_save_corrupt_toml_returns_error_string_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    raw = b"[budget\nthis is not toml = = =\n"
    path = tmp_path / "jev.toml"
    path.write_bytes(raw)
    error = settings.save(
        tmp_path, "repo", lambda layer: settings.set_path(layer, "budget.per_run_usd", 9.0)
    )
    assert isinstance(error, str) and error
    assert "not valid TOML" in error
    assert "ParseError" not in error  # never the raw tomlkit exception
    assert path.read_bytes() == raw


def test_save_invalid_target_reports_an_error_string(tmp_path: Path) -> None:
    error = settings.save(tmp_path, "nope", lambda layer: None)
    assert error == "target must be 'repo' or 'user'"


def test_load_distinguishes_missing_from_corrupt(tmp_path: Path) -> None:
    assert settings._load(tmp_path / "nope.toml") == {}
    bad = tmp_path / "bad.toml"
    bad.write_text("[oops\n", encoding="utf-8")
    with pytest.raises(settings.CorruptConfigError, match="not valid TOML"):
        settings._load(bad)
    # A ConfigError so the dashboard renders its config-error page, not defaults.
    assert issubclass(settings.CorruptConfigError, ConfigError)


def test_write_value_on_corrupt_file_raises_clean_value_error(tmp_path: Path) -> None:
    """Direct writers get a ValueError with a message, never tomlkit.ParseError."""
    path = _write_repo_toml(tmp_path, "[oops\n")
    try:
        settings.write_value(path, "budget.per_run_usd", 9.0)
    except ValueError as exc:
        assert not isinstance(exc, TomlkitParseError)
        assert "not valid TOML" in str(exc)
    else:
        pytest.fail("expected ValueError")


def test_save_rejects_invalid_values_with_an_error_string(tmp_path: Path) -> None:
    _write_repo_toml(tmp_path, "")
    error = settings.save(
        tmp_path,
        "repo",
        lambda layer: settings.set_path(layer, "budget.per_run_usd", "lots"),
    )
    assert isinstance(error, str) and "per_run_usd" in error
    assert (tmp_path / "jev.toml").read_text(encoding="utf-8") == ""  # untouched


def test_save_guarded_key_on_untrusted_repo_is_refused(tmp_path: Path) -> None:
    """The trust gate still works now that `before` comes from the same snapshot."""
    _write_repo_toml(tmp_path, "")
    error = settings.save(
        tmp_path,
        "repo",
        lambda layer: settings.set_path(layer, "workers.claude_code.command", "evil"),
    )
    assert isinstance(error, str) and "trust" in error.lower()
    assert (tmp_path / "jev.toml").read_text(encoding="utf-8") == ""  # nothing written


def test_save_happy_path_writes_and_keeps_comments(tmp_path: Path) -> None:
    _write_repo_toml(tmp_path, "# keep me\n[budget]\nper_run_usd = 5.0\n")
    error = settings.save(
        tmp_path, "repo", lambda layer: settings.set_path(layer, "budget.per_run_usd", 9.0)
    )
    assert error is None
    text = (tmp_path / "jev.toml").read_text(encoding="utf-8")
    assert "# keep me" in text
    assert tomllib.loads(text)["budget"]["per_run_usd"] == 9.0


def test_save_entity_add_and_remove_round_trip(tmp_path: Path) -> None:
    """`mutate` applied to the validated document handles nested tables too."""
    # `servers` is a guarded key, so the entity round-trip goes to the user layer.
    from jev_agent.config.loader import default_user_config_path

    user_file = default_user_config_path()
    error = settings.save(
        tmp_path,
        "user",
        lambda layer: settings.set_path(layer, "servers.db", {"host": "db.local"}),
    )
    assert error is None
    assert (
        tomllib.loads(user_file.read_text(encoding="utf-8"))["servers"]["db"]["host"] == "db.local"
    )
    error = settings.save(tmp_path, "user", lambda layer: settings.remove_path(layer, "servers.db"))
    assert error is None
    # The emptied parent table is not rendered; the entity is gone either way.
    assert "db.local" not in user_file.read_text(encoding="utf-8")


# ---- entity source labels (audit app minor 1) ----------------------------------------------


def test_entity_source_distinguishes_user_repo_and_both() -> None:
    repo_raw = {"servers": {"repo_only": {"host": "r"}, "both": {"host": "rb"}}}
    user_raw = {"servers": {"user_only": {"host": "u"}, "both": {"host": "ub"}}}
    config = JevConfig.model_validate(deep_merge(user_raw, repo_raw))
    section = next(
        s for s in settings.entity_sections(config, repo_raw, user_raw) if s.path == "servers"
    )
    assert {e.name: e.source for e in section.entities} == {
        "repo_only": "repo",
        "user_only": "user",
        "both": "User + repo",
    }


# ---- parse_value bad inputs (audit app minor 2) --------------------------------------------


def test_parse_value_weights_non_numeric_is_a_clean_error() -> None:
    with pytest.raises(ValueError, match="correctness: 'abc' is not a number"):
        settings.parse_value("weights", "", {"w_correctness": "abc", "w_safety": "0.2"})


def test_parse_value_weights_empty_value_is_a_clean_error() -> None:
    with pytest.raises(ValueError, match="correctness: '' is not a number"):
        settings.parse_value("weights", "", {"w_correctness": ""})


def test_parse_value_weights_happy_path() -> None:
    assert settings.parse_value("weights", "", {"w_correctness": "0.3", "w_safety": "0.2"}) == {
        "correctness": 0.3,
        "safety": 0.2,
    }


def test_parse_value_bool_maps_unrecognized_text_to_false() -> None:
    """Checkbox-shaped on purpose: unchecked boxes submit nothing truthy, so anything
    that is not an affirmative reads as False instead of erroring."""
    assert settings.parse_value("bool", "on", {}) is True
    assert settings.parse_value("bool", "maybe", {}) is False
    assert settings.parse_value("bool", "", {}) is settings.UNSET


# ---- secret-kind heuristic (audit app minor 3) ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("api_key_env", "env"),
        ("token", "env"),
        ("api_token", "env"),
        ("envelope", "str"),  # substring matches must not count as secrets
        ("environment", "str"),
        ("tokenizer", "str"),
        ("host", "str"),
    ],
)
def test_secret_kind_heuristic_is_anchored(name: str, want: str) -> None:
    assert settings._kind_of(name, str)[0] == want


# ---- atomic design-system writes (audit core-a major 3) ------------------------------------


def test_design_system_crash_between_writes_keeps_old_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash while staging the new files leaves the old version served intact."""
    from jev_agent.core import design_system

    assert design_system.save_design_system(tmp_path, "# v1\n", {"colors": {"a": "#111"}}) == 1
    real_stage = design_system._stage_temp

    def flaky(path: Path, text: str) -> Path:
        if path.name == "tokens.json":
            raise RuntimeError("simulated crash between writes")
        return real_stage(path, text)

    monkeypatch.setattr(design_system, "_stage_temp", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        design_system.save_design_system(tmp_path, "# v2\n", {"colors": {"b": "#222"}})

    system = design_system.get_design_system(tmp_path)
    assert system is not None
    assert system.version == 1  # old version still served ...
    assert system.markdown == "# v1\n"  # ... intact ...
    assert system.tokens == {"colors": {"a": "#111"}}
    assert (
        json.loads((tmp_path / ".jev" / "design" / "meta.json").read_text())["version"] == 1
    )  # ... and consistent
    assert list((tmp_path / ".jev" / "design").glob("*.tmp")) == []  # temps cleaned


def test_design_system_save_commits_all_three_files_together(tmp_path: Path) -> None:
    from jev_agent.core import design_system

    assert design_system.save_design_system(tmp_path, "# v1\n", {"colors": {"a": "#111"}}) == 1
    assert design_system.save_design_system(tmp_path, "# v2\n", {"colors": {"b": "#222"}}) == 2
    d = tmp_path / ".jev" / "design"
    assert list(d.glob("*.tmp")) == []
    system = design_system.get_design_system(tmp_path)
    assert system is not None and system.version == 2
    assert system.markdown == "# v2\n"
    assert system.tokens == {"colors": {"b": "#222"}}
    assert (d / "design-system.v1.md").read_text(encoding="utf-8") == "# v1\n"


# ---- transactional backfill (audit core-a major 4) ------------------------------------------


def _pre_event_log_run(tmp_path: Path) -> tuple[Path, str]:
    """A run built with the log disabled, as if it predates the event log."""
    from jev_agent.core.models import (
        Decision,
        DecisionRequest,
        Idea,
        Plan,
        RunState,
        TaskCard,
        TaskState,
        ValidationResult,
        WorkerResult,
    )
    from jev_agent.core.store import Store
    from jev_agent.quality.models import GateResult

    db = tmp_path / "old.db"
    with Store.open(db, events_enabled=False) as store:
        run = store.create_run(Idea(text="add search", repo_path=tmp_path / "repo"))
        plan = Plan(
            summary="s",
            tasks=[
                TaskCard(id="T1", title="t1", description="d", test_command="pytest"),
            ],
        )
        store.set_run_plan(run.id, plan, 1)
        store.upsert_tasks(run.id, plan)
        store.set_task_state(run.id, "T1", TaskState.DONE)
        store.set_run_validation(
            run.id, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        attempt_id = store.start_attempt(run.id, "T1", 1, "coder", "w")
        store.finish_attempt(
            attempt_id,
            WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l"),
        )
        store.record_decision(
            run.id,
            "T1",
            DecisionRequest(point="D4", state={"task": "T1"}, questions={"effort": ["low"]}),
            Decision(point="D4", answers={"effort": "low"}, dispatcher="rules"),
        )
        store.add_quality_report(
            run.id, GateResult(level="basic", scope="task", task_id="T1", blocked=False)
        )
        store.set_run_state(run.id, RunState.EXECUTING)
        assert store.list_events(run.id) == []
        return db, run.id


def test_backfill_crash_midway_persists_nothing_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-or-nothing, as the docstring claims: a crash rolls back every event."""
    from jev_agent.core.backfill import backfill_run
    from jev_agent.core.store import Store

    db, run_id = _pre_event_log_run(tmp_path)
    with Store.open(db) as store:  # log enabled now
        real_emit = Store._emit
        calls = 0

        def flaky(
            self: Store,
            conn: object,
            rid: str,
            event_type: object,
            payload: object,
            **kwargs: object,
        ) -> object:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("simulated crash mid-backfill")
            return real_emit(self, conn, rid, event_type, payload, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Store, "_emit", flaky)
        with pytest.raises(RuntimeError, match="simulated crash"):
            backfill_run(store, run_id)
        # Zero synthetic events persisted: the partial log never happened.
        assert store.list_events(run_id) == []

        monkeypatch.undo()  # retry with the real emit
        count = backfill_run(store, run_id)
        assert count > 0
        events = store.list_events(run_id)
        assert len(events) == count
        assert all(e.synthetic for e in events)
