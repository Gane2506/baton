"""Grader for T1 (CLI flag). Protocol: `--result DIR --run-json FILE --diff FILE`;
prints one JSON report on stdout; exits 0 (passed), 1 (graded failure) or 2
(harness error). Self-contained: stdlib + pytest only, no Baton internals.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from jev_agent.capbench.grading import grade_main
except ImportError:  # grade.py run outside the installed package: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from jev_agent.capbench.grading import grade_main

HIDDEN_TESTS = {
    "test_format_hidden.py": '''\
"""Hidden tests for T1: the `--format` contract (the agent never sees these)."""
import json
import subprocess
import sys
from pathlib import Path

import demo_cli  # the result dir is on sys.path via the harness conftest

CLI = Path(demo_cli.__file__)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60
    )


def test_text_is_default():
    proc = _run("--name", "Ada")
    assert proc.returncode == 0
    assert proc.stdout == "Hello, Ada!\\n"


def test_text_explicit():
    proc = _run("--name", "Ada", "--format", "text")
    assert proc.returncode == 0
    assert proc.stdout == "Hello, Ada!\\n"


def test_json_shape():
    proc = _run("--name", "Ada", "--format", "json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"greeting": "Hello, Ada!", "name": "Ada"}


def test_json_default_name():
    proc = _run("--format", "json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload == {"greeting": "Hello, world!", "name": "world"}


def test_bad_format_fails():
    proc = _run("--format", "yaml")
    assert proc.returncode != 0


def test_name_still_works():
    proc = _run("--name", "Grace Hopper")
    assert proc.returncode == 0
    assert proc.stdout == "Hello, Grace Hopper!\\n"
''',
}


def main() -> int:
    return grade_main("t1-cli-flag", HIDDEN_TESTS, max_added=150, max_files=6)


if __name__ == "__main__":
    sys.exit(main())
