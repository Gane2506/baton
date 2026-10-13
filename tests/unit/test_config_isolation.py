"""The test suite's own isolation from the developer's environment (tests/conftest.py, #53)."""

import os
import subprocess
from pathlib import Path

from jev_agent.config import JevConfig, default_user_config_path, load_config


def test_user_config_and_data_live_under_tmp_path(tmp_path: Path) -> None:
    assert default_user_config_path() == tmp_path / "xdg" / "jev" / "config.toml"
    assert os.environ["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
    repo = tmp_path / "repo"
    repo.mkdir()
    # Only the suite's own user config (tests/conftest.py), no developer config (prefer_free...).
    expected = JevConfig.model_validate({"dispatcher": {"primary": "rules"}})
    assert load_config(repo) == expected


def test_credentials_and_fake_workers_are_unset() -> None:
    for name in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "JEV_FAKE_WORKERS"):
        assert name not in os.environ


def test_git_ignores_global_and_system_config(tmp_path: Path) -> None:
    listed = subprocess.run(
        ["git", "config", "--global", "--list"], capture_output=True, text=True, check=False
    )
    assert listed.stdout == ""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )
    author = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "-1", "--format=%an <%ae>"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert author.stdout.strip() == "test <test@example.com>"
