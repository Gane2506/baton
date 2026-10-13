import os
import subprocess
from importlib.util import find_spec
from pathlib import Path

import pytest

# The CLI's rich consoles are created at import time and size themselves from COLUMNS. Pin a
# wide terminal so output does not wrap differently on narrow CI runners.
os.environ["COLUMNS"] = "200"

# The dashboard tests need the `dashboard` extra (`uv sync --all-extras`, as CI does).
collect_ignore = [] if find_spec("starlette") else ["unit/test_dashboard.py"]

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
def _isolated_shared_learning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Shared learning profiles live under XDG_DATA_HOME; never touch the real one in tests."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
