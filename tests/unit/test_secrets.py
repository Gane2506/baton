"""Secrets file (`~/.jev/secrets.env`) and the dashboard key manager."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.core import secrets
from jev_agent.core.secrets import SecretsError
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture
def sfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated secrets file; the module's process-level `_loaded` set is reset."""
    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    yield file
    secrets._loaded.discard("JEV_TEST_KEY_A")
    secrets._loaded.discard("JEV_TEST_KEY_B")


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


# --- loader ---------------------------------------------------------------


def test_load_injects_missing_vars(sfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(sfile, "JEV_TEST_KEY_A=file-value\n")
    monkeypatch.delenv("JEV_TEST_KEY_A", raising=False)
    secrets.load_secrets()
    assert os.environ["JEV_TEST_KEY_A"] == "file-value"
    assert "JEV_TEST_KEY_A" in secrets.loaded_names()


def test_real_environment_wins(sfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(sfile, "JEV_TEST_KEY_A=file-value\n")
    monkeypatch.setenv("JEV_TEST_KEY_A", "real-value")
    secrets.load_secrets()
    assert os.environ["JEV_TEST_KEY_A"] == "real-value"
    assert "JEV_TEST_KEY_A" not in secrets.loaded_names()


def test_load_parses_comments_blanks_and_equals_in_value(
    sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        sfile,
        "# a comment\n\nJEV_TEST_KEY_A=a=b=c\nJEV_TEST_KEY_B = spaced\nbad line here\n",
    )
    monkeypatch.delenv("JEV_TEST_KEY_A", raising=False)
    monkeypatch.delenv("JEV_TEST_KEY_B", raising=False)
    secrets.load_secrets()
    assert os.environ["JEV_TEST_KEY_A"] == "a=b=c"
    assert os.environ["JEV_TEST_KEY_B"] == " spaced"


def test_load_skips_too_open_file(sfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(sfile, "JEV_TEST_KEY_A=file-value\n", mode=0o644)
    monkeypatch.delenv("JEV_TEST_KEY_A", raising=False)
    secrets.load_secrets()  # warns, never raises, never loads
    assert "JEV_TEST_KEY_A" not in os.environ


def test_load_missing_file_is_noop(sfile: Path) -> None:
    secrets.load_secrets()  # nothing there: fine


# --- write / perms ----------------------------------------------------------


def test_write_creates_private_file_and_dir(sfile: Path) -> None:
    secrets.write_secret("JEV_TEST_KEY_A", "v1")
    assert (sfile.stat().st_mode & 0o777) == 0o600
    assert (sfile.parent.stat().st_mode & 0o777) == 0o700
    assert sfile.read_text(encoding="utf-8") == "JEV_TEST_KEY_A=v1\n"


def test_write_refuses_too_open_file(sfile: Path) -> None:
    _write(sfile, "JEV_TEST_KEY_A=old\n", mode=0o644)
    with pytest.raises(SecretsError, match="too open"):
        secrets.write_secret("JEV_TEST_KEY_A", "new")
    assert "old" in sfile.read_text(encoding="utf-8")  # untouched


def test_write_replaces_existing_line(sfile: Path) -> None:
    secrets.write_secret("JEV_TEST_KEY_A", "v1")
    secrets.write_secret("JEV_TEST_KEY_B", "v2")
    secrets.write_secret("JEV_TEST_KEY_A", "v3")
    lines = sfile.read_text(encoding="utf-8").splitlines()
    assert lines == ["JEV_TEST_KEY_B=v2", "JEV_TEST_KEY_A=v3"]


def test_write_rejects_bad_names_and_empty_value(sfile: Path) -> None:
    for bad in ("no spaces", "9START", "", "semi;colon"):
        with pytest.raises(SecretsError, match="invalid key name"):
            secrets.write_secret(bad, "v")
    with pytest.raises(SecretsError, match="must not be empty"):
        secrets.write_secret("JEV_TEST_KEY_A", "")
    with pytest.raises(SecretsError, match="line breaks"):
        secrets.write_secret("JEV_TEST_KEY_A", "a\nb")


def test_delete_removes_line_and_pops_process_var(
    sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JEV_TEST_KEY_A", raising=False)
    secrets.write_secret("JEV_TEST_KEY_A", "v1")
    assert os.environ["JEV_TEST_KEY_A"] == "v1"
    secrets.delete_secret("JEV_TEST_KEY_A")
    assert "JEV_TEST_KEY_A" not in os.environ
    assert "JEV_TEST_KEY_A" not in sfile.read_text(encoding="utf-8")


def test_delete_never_pops_real_environment(sfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same name in the file AND the real environment: deleting the file line must
    # leave the real env var alone.
    _write(sfile, "JEV_TEST_KEY_B=file-value\n")
    monkeypatch.setenv("JEV_TEST_KEY_B", "real-value")
    secrets.delete_secret("JEV_TEST_KEY_B")
    assert os.environ["JEV_TEST_KEY_B"] == "real-value"


def test_delete_unknown_name_raises(sfile: Path) -> None:
    with pytest.raises(SecretsError, match="not in the secrets file"):
        secrets.delete_secret("JEV_NOPE")


# --- dashboard round-trip -----------------------------------------------------


@pytest.fixture
def dclient(
    fixture_repo: Path, sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("JEV_FAKE_WORKERS", "1")
    # A real-environment key the config names: must render "Set (environment)".
    monkeypatch.setenv("OPENROUTER_API_KEY", "<redacted>")
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=lambda root, run_id, action: "ok",
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response = client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False)
    return cast(httpx.Response, response)


def test_dashboard_add_and_remove_key(
    dclient: TestClient, sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-test-secret-value-123"
    response = _post(dclient, "/connections/keys/add", {"name": "JEV_TEST_KEY_A", "value": secret})
    assert response.status_code == 303
    # Status flips to Set without a restart; the value appears nowhere.
    page = dclient.get("/connections").text
    assert "JEV_TEST_KEY_A" in page
    assert secret not in page
    assert secret not in dclient.get("/connections/live").text
    assert sfile.read_text(encoding="utf-8") == "JEV_TEST_KEY_A=" + secret + "\n"
    assert (sfile.stat().st_mode & 0o777) == 0o600
    # Remove round-trips the same way: the row leaves the table (the flash message
    # confirming the removal still names it — that is the point of the message).
    response = _post(dclient, "/connections/keys/remove", {"name": "JEV_TEST_KEY_A"})
    assert response.status_code == 303
    page = dclient.get("/connections").text
    rows = re.findall(r"<tr>.*?</tr>", page, re.S)
    assert not [r for r in rows if "JEV_TEST_KEY_A" in r]
    assert secret not in page
    assert "JEV_TEST_KEY_A" not in os.environ
    assert sfile.read_text(encoding="utf-8") == ""


def test_dashboard_marks_real_environment_keys(dclient: TestClient) -> None:
    page = dclient.get("/connections").text
    # OPENROUTER_API_KEY came from the real environment: shown as such, and its
    # table row carries no Remove form (only secrets-file keys are removable).
    assert "Set (environment)" in page
    rows = re.findall(r"<tr>.*?</tr>", page, re.S)
    env_rows = [r for r in rows if "OPENROUTER_API_KEY" in r]
    assert env_rows
    assert all("/connections/keys/remove" not in r for r in env_rows)


def test_dashboard_add_key_never_in_get_or_logs(dclient: TestClient) -> None:
    secret = "sk-must-never-appear"
    response = dclient.post(
        "/connections/keys/add",
        data={"name": "JEV_TEST_KEY_B", "value": secret, "csrf_token": _csrf(dclient)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert secret not in response.text  # includes the flashed success message
    assert "JEV_TEST_KEY_B" in response.text
    dclient.post(
        "/connections/keys/remove",
        data={"name": "JEV_TEST_KEY_B", "csrf_token": _csrf(dclient)},
        follow_redirects=False,
    )


def test_dashboard_add_key_rejects_bad_name(dclient: TestClient) -> None:
    response = _post(dclient, "/connections/keys/add", {"name": "not a name", "value": "x"})
    assert response.status_code == 303  # flashed error, redirected back
    assert "not a name" not in os.environ


# --- doctor -------------------------------------------------------------------


def test_doctor_points_at_dashboard_when_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from jev_agent.cli import doctor_cmd

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # The suite's test user config picks the rules dispatcher; opt back into
    # consensus so the key check actually runs.
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "consensus"\n')
    monkeypatch.setattr(doctor_cmd, "which", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    check = {c.name: c for c in doctor_cmd.run_checks(repo)}["dispatcher"]
    assert check.status == "fail"
    assert "dashboard Connections page" in check.detail

    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    check = {c.name: c for c in doctor_cmd.run_checks(repo)}["dispatcher"]
    assert check.status == "warn"
    assert "dashboard Connections page" in check.detail


# --- _InjectionTracker (P9) ---------------------------------------------------


def test_loaded_names_round_trips_through_owner(sfile: Path) -> None:
    """Write -> tracked, delete -> untracked and env-popped: the injection owner
    wires end to end."""
    secrets.write_secret("JEV_TEST_KEY_A", "aaa", path=sfile)
    assert "JEV_TEST_KEY_A" in secrets.loaded_names()
    secrets.delete_secret("JEV_TEST_KEY_A", path=sfile)
    assert "JEV_TEST_KEY_A" not in secrets.loaded_names()
    assert "JEV_TEST_KEY_A" not in os.environ


def test_loaded_is_an_explicit_owner_not_a_bare_mutable() -> None:
    """P9 regression: the module-level run state has an explicit owner class,
    not a bare module-global mutable."""
    assert type(secrets._loaded).__name__ == "_InjectionTracker"
    assert not isinstance(secrets._loaded, (dict, list, set))
