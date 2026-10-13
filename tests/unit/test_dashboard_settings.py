"""The Settings page's jev.toml editor (issue #9): typed fields, preview with a diff, confirmed
atomic write, reset to default, and the server-side refusal of guarded keys, `[trust]` and the
user config. Starlette's in-process test client; no network, no real workers."""

from __future__ import annotations

import html
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.config import load_config
from jev_agent.config.loader import GUARDED_KEYS, default_user_config_path
from jev_agent.core.learning import SETTING_ENABLED
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.dashboard.settings_edit import (
    FIELDS,
    NO_FILE_HASH,
    Submission,
    editable,
    file_hash,
    is_protected,
    plan_edit,
    read_layers,
    write_config,
)

REPO_TOML = """\
# my settings, keep this comment
[dispatcher]
primary = "rules"  # tests never reach Jev

[budget]
per_run_usd = 9.0  # agreed with the team
"""


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(REPO_TOML)
    return fixture_repo


@pytest.fixture
def client(repo: Path) -> Iterator[TestClient]:
    app = create_app(DashboardSettings(root=repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as test_client:
        yield test_client


def _token(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/settings").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, pairs: list[tuple[str, str]]) -> httpx.Response:
    """POST the fields in order (a toggle posts its name twice), with the CSRF token."""
    body = urlencode([("csrf_token", _token(client)), *pairs])
    response: httpx.Response = client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    return response


def _hidden(page: str) -> list[tuple[str, str]]:
    """The confirm form's hidden fields (without the CSRF token)."""
    form = page[page.index('action="/settings/apply"') :]
    form = form[: form.index("</form>")]
    pairs = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    return [(name, html.unescape(value)) for name, value in pairs if name != "csrf_token"]


def _text(page: httpx.Response) -> str:
    return html.unescape(page.text)


# ---- the field list --------------------------------------------------------------------------


def test_no_editable_field_is_guarded_or_trust() -> None:
    assert FIELDS and not [key for key in FIELDS if is_protected(key)]
    for key in (
        "trust.repos",
        "workers.claude_code.command",
        "workers.api.base_url",
        "dispatcher.jev.api_key_env",
        "roles.coder.mcp",
        "roles.planner.servers",
        "tools.mcp.x.command",
        "servers.prod.host",
    ):
        assert is_protected(key) and not editable(key), key
    for pattern in GUARDED_KEYS:
        assert not editable(pattern.replace("*", "coder"))
    assert {"roles.coder.workers", "roles.coder.models", "budget.per_run_usd"} <= set(FIELDS)


def test_submission_drops_unknown_names_and_last_toggle_value_wins() -> None:
    submission = Submission.from_pairs(
        [
            ("ship.enabled", "false"),
            ("ship.enabled", "true"),
            ("workers.claude_code.command", "evil"),
            ("trust.repos", "*"),
            ("reset", "trust.repos"),
            ("reset", "budget.per_run_usd"),
            ("csrf_token", "x"),
        ]
    )
    assert submission.values == {"ship.enabled": "true"}
    assert submission.resets == frozenset({"budget.per_run_usd"})


# ---- plan_edit (pure) ------------------------------------------------------------------------


def test_plan_without_a_file_creates_nested_tables(fixture_repo: Path) -> None:
    plan = plan_edit(
        read_layers(fixture_repo),
        Submission({"roles.coder.workers": "codex", "budget.per_run_usd": "8"}),
    )
    assert plan.ok and plan.base_hash == NO_FILE_HASH
    assert plan.set_keys == ("budget.per_run_usd", "roles.coder.workers")
    assert "[roles.coder]" in plan.new_text and "[roles]\n" not in plan.new_text
    assert 'workers = ["codex"]' in plan.new_text and "per_run_usd = 8.0" in plan.new_text
    assert not (fixture_repo / "jev.toml").exists()  # planning writes nothing


def test_unchanged_values_change_nothing(repo: Path) -> None:
    config = load_config(repo)
    values = {
        "budget.per_run_usd": "9",
        "budget.per_task_usd": f"{config.budget.per_task_usd:g}",
        "dispatcher.primary": "rules",
        "ship.enabled": "false",
        "approval.require_plan_approval": "",
        "roles.coder.workers": ", ".join(config.roles.coder.workers),
    }
    plan = plan_edit(read_layers(repo), Submission(values))
    assert plan.ok and not plan.has_changes and plan.new_text == REPO_TOML


def test_auto_cannot_unset_a_user_config_value(repo: Path) -> None:
    user = default_user_config_path()
    user.write_text(user.read_text() + "[review]\nenabled = true\n")
    plan = plan_edit(read_layers(repo), Submission({"review.enabled": ""}))
    assert "user config" in plan.field_errors["review.enabled"] and not plan.has_changes


# ---- the page --------------------------------------------------------------------------------


def test_page_shows_sections_controls_and_sources(client: TestClient) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    text = page.text
    for anchor in ("#s-budget", "#s-roles", "#effective", "#env", "#doctor", "#dashboard"):
        assert f'href="{anchor}"' in text
    assert 'action="/settings/preview"' in text
    assert 'type="number" id="f-budget-per_run_usd" name="budget.per_run_usd" value="9"' in text
    assert 'name="roles.coder.workers" value="claude_code, codex"' in text
    # jev.toml sets per_run_usd (resettable); per_task_usd is the default.
    assert 'name="reset" value="budget.per_run_usd"' in text
    assert 'name="reset" value="budget.per_task_usd"' not in text
    head = text[text.index('for="f-budget-per_task_usd"') :]
    assert "s-src" in head[:300] and ">default<" in head[:300]
    # The read-only sections stay.
    assert "Effective config" in text and "Environment variables" in text and "Doctor" in text


def test_learning_cli_setting_is_noted(client: TestClient, repo: Path) -> None:
    with Store.open(repo / ".jev" / "state.db") as store:
        store.set_setting(SETTING_ENABLED, "true")
    assert "Overridden by `jev learning enable/disable`" in _text(client.get("/settings"))


# ---- preview, confirm, write -----------------------------------------------------------------


def test_preview_then_apply_writes_and_keeps_comments(client: TestClient, repo: Path) -> None:
    user_before = default_user_config_path().read_text()
    preview = _post(
        client,
        "/settings/preview",
        [
            ("budget.per_task_usd", "2"),
            ("ship.enabled", "false"),
            ("ship.enabled", "true"),
            ("roles.reviewer.models", "anthropic/claude-sonnet-4.5, free"),
        ],
    )
    assert preview.status_code == 200
    text = _text(preview)
    assert "+per_task_usd = 2.0" in text and "+enabled = true" in text
    assert (repo / "jev.toml").read_text() == REPO_TOML  # nothing written yet
    hidden = _hidden(preview.text)
    assert ("base_hash", file_hash(REPO_TOML)) in hidden

    done = _post(client, "/settings/apply", hidden)
    assert done.status_code == 303 and done.headers["location"] == "/settings"
    written = (repo / "jev.toml").read_text()
    assert written.startswith(REPO_TOML.rstrip("\n").rsplit("\n", 1)[0])
    assert "# my settings, keep this comment" in written and "# agreed with the team" in written
    config = load_config(repo)
    assert config.budget.per_task_usd == 2.0 and config.ship.enabled
    assert config.roles.reviewer.models == ["anthropic/claude-sonnet-4.5", "free"]
    assert default_user_config_path().read_text() == user_before
    assert "saved jev.toml" in _text(client.get("/settings"))


def test_reset_removes_the_key(client: TestClient, repo: Path) -> None:
    preview = _post(client, "/settings/preview", [("reset", "budget.per_run_usd")])
    assert "-per_run_usd = 9.0" in _text(preview)
    assert _post(client, "/settings/apply", _hidden(preview.text)).status_code == 303
    written = (repo / "jev.toml").read_text()
    assert "per_run_usd" not in written and "# my settings, keep this comment" in written
    assert load_config(repo).budget.per_run_usd == 5.0  # the default again


def test_saving_unchanged_page_writes_nothing(client: TestClient, repo: Path) -> None:
    response = _post(client, "/settings/preview", [("budget.per_run_usd", "9")])
    assert response.status_code == 303
    assert "nothing to save" in _text(client.get("/settings"))
    assert (repo / "jev.toml").read_text() == REPO_TOML


def test_invalid_values_rerender_with_field_errors(client: TestClient, repo: Path) -> None:
    response = _post(
        client,
        "/settings/preview",
        [("budget.per_run_usd", "abc"), ("learning.profile", "my profile")],
    )
    assert response.status_code == 400
    text = _text(response)
    assert "must be a number" in text and "nothing was written" in text
    assert 'name="budget.per_run_usd" value="abc"' in text  # what was typed stays
    assert 'name="learning.profile" value="my profile"' in text
    assert (repo / "jev.toml").read_text() == REPO_TOML


def test_schema_errors_are_shown_on_their_field(client: TestClient, repo: Path) -> None:
    cases = [
        ("budget.per_task_usd", "20", "must not exceed per_run_usd"),
        ("roles.coder.workers", "claude_code, gpt", "unknown worker 'gpt'"),
    ]
    for key, value, message in cases:
        response = _post(client, "/settings/preview", [(key, value)])
        assert response.status_code == 400
        text = _text(response)
        field = text[text.index(f'for="f-{key.replace(".", "-")}"') :]
        assert message in field[: field.index('class="setting-head"')], key
        assert message in text[: text.index("<form")]  # and in the whole-file error
    assert (repo / "jev.toml").read_text() == REPO_TOML


def test_changed_file_is_not_overwritten(client: TestClient, repo: Path) -> None:
    preview = _post(client, "/settings/preview", [("quality.level", "basic")])
    hidden = _hidden(preview.text)
    edited = REPO_TOML + '\n[integration]\ntest_command = "make test"\n'
    (repo / "jev.toml").write_text(edited)
    stale = _post(client, "/settings/apply", hidden)
    assert stale.status_code == 200
    text = _text(stale)
    assert "changed after the diff was shown" in text and '+level = "basic"' in text
    assert (repo / "jev.toml").read_text() == edited
    # Confirming the new diff writes on top of the edited file.
    assert _post(client, "/settings/apply", _hidden(stale.text)).status_code == 303
    config = load_config(repo)
    assert config.quality.level == "basic" and config.integration.test_command == "make test"


def test_guarded_keys_trust_and_user_config_are_never_written(
    client: TestClient, repo: Path
) -> None:
    user_before = default_user_config_path().read_text()
    forged = [
        ("workers.claude_code.command", "evil"),
        ("workers.claude_code.skip_permissions", "true"),
        ("roles.coder.mcp", "evil"),
        ("trust.repos", "*"),
        ("reset", "trust.repos"),
        ("reset", "dispatcher.jev.base_url"),
    ]
    for url in ("/settings/preview", "/settings/apply"):
        response = _post(client, url, [*forged, ("base_hash", file_hash(REPO_TOML))])
        assert response.status_code == 303  # nothing editable changed: nothing to save
    assert (repo / "jev.toml").read_text() == REPO_TOML
    # Mixed with a real change, only the real change is written.
    response = _post(
        client,
        "/settings/apply",
        [*forged, ("models.prefer_free", "true"), ("base_hash", file_hash(REPO_TOML))],
    )
    assert response.status_code == 303
    written = (repo / "jev.toml").read_text()
    assert "prefer_free = true" in written
    assert "evil" not in written and "trust" not in written and "command" not in written
    assert default_user_config_path().read_text() == user_before


def test_settings_posts_need_csrf_and_same_origin(client: TestClient, repo: Path) -> None:
    data = {"budget.per_run_usd": "7"}
    assert client.post("/settings/preview", data=data).status_code == 403
    assert client.post("/settings/apply", data=data).status_code == 403
    cross = client.post(
        "/settings/apply",
        data={**data, "csrf_token": _token(client), "base_hash": file_hash(REPO_TOML)},
        headers={"Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert (repo / "jev.toml").read_text() == REPO_TOML


# ---- write_config ----------------------------------------------------------------------------


def test_write_config_is_atomic_and_checks_the_hash(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    path = folder / "jev.toml"
    assert write_config(path, "a = 1\n", NO_FILE_HASH)
    assert path.read_text() == "a = 1\n" and stat.S_IMODE(path.stat().st_mode) == 0o644
    os.chmod(path, 0o600)
    assert not write_config(path, "a = 2\n", NO_FILE_HASH)  # it exists now: refused
    assert write_config(path, "a = 2\n", file_hash("a = 1\n"))
    assert path.read_text() == "a = 2\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in folder.iterdir()] == ["jev.toml"]  # no temp file left
