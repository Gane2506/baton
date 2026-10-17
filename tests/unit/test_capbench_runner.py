"""The benchmark sandbox's git calls enforce timeouts (#24 remainder)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from jev_agent.capbench import runner as runner_mod


def test_git_calls_time_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung git is killed instead of hanging the benchmark run."""
    bin_dir = tmp_path / "hang-bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\nsleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(runner_mod, "GIT_QUICK_TIMEOUT_S", 0.5)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runner_mod._git(tmp_path, "rev-parse", "HEAD")
    assert time.monotonic() - started < 20
