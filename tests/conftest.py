import os
import subprocess
from importlib.util import find_spec
from pathlib import Path

import pytest

# The CLI's rich consoles are created at import time and size themselves from COLUMNS. Pin a
# wide terminal for the whole suite, whatever the caller's COLUMNS: rich tables cut cells in a
# narrow terminal, so most CLI tests could not pass there. Narrow output is checked on purpose
# in tests/unit/test_narrow_terminal.py (issue #53).
os.environ["COLUMNS"] = "200"

# Credentials and switches from the developer's shell that change what jev does. Tests that need
# one set it themselves (monkeypatch.setenv).
_UNSET_ENV = ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "JEV_FAKE_WORKERS")

# The dashboard tests need the `dashboard` extra (`uv sync --all-extras`, as CI does).
collect_ignore = [] if find_spec("starlette") else ["unit/test_dashboard.py"]

TEST_USER_CONFIG = '[dispatcher]\nprimary = "rules"\n'

GIT = [
    "git",
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "commit.gpgsign=false",
]


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit and `.jev/` ignored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / ".gitignore").write_text(".jev/\n")
    (repo / "README.md").write_text("fixture\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture(autouse=True)
def _isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Keep the developer's own config out of every test (issue #53).

    The user config (`$XDG_CONFIG_HOME/jev/config.toml`, here `tmp_path / "xdg"`, holding
    only `TEST_USER_CONFIG`), shared
    learning profiles and caches live under tmp_path; git reads no global or system config
    (a `commit.gpgsign = true` there would break every commit) and commits as a test identity;
    API keys and `JEV_FAKE_WORKERS` are unset. Live tests (tests/live) keep the keys and the
    real config home: they talk to real services and CLIs on purpose.
    """
    live = "live" in request.node.path.relative_to(Path(__file__).parent).parts[:1]
    if not live:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        # Tests never reach Jev, and without it the default consensus dispatcher stops every
        # run, so the user config picks the rules dispatcher; a repo `jev.toml` overrides it.
        user_config = tmp_path / "xdg" / "jev" / "config.toml"
        user_config.parent.mkdir(parents=True)
        user_config.write_text(TEST_USER_CONFIG)
        for name in _UNSET_ENV:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
