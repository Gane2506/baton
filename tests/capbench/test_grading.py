"""Deterministic grading helpers: no LLM judge, no model spend."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jev_agent.capbench.grading import (
    _parse_pytest_summary,
    diff_within_bands,
    load_run_json,
    paths_touched,
    read_ship_score,
    run_hidden_pytest,
    stdlib_only_imports,
)
from jev_agent.capbench.grading import tests_dir_untouched as _tests_dir_untouched

DIFF_SMALL = """\
diff --git a/demo_cli.py b/demo_cli.py
index 1111111..2222222 100644
--- a/demo_cli.py
+++ b/demo_cli.py
@@ -1,2 +1,3 @@
 line1
+line2
 line3
"""

DIFF_BIG = """\
diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
 line1
""" + "".join(f"+added{i}\n" for i in range(200))


def test_diff_within_bands() -> None:
    assert diff_within_bands(DIFF_SMALL, 150, 6) is True
    assert diff_within_bands(DIFF_BIG, 150, 6) is False  # 200 added lines
    assert diff_within_bands("", 150, 6) is True


def test_paths_touched() -> None:
    assert paths_touched(DIFF_SMALL) == ["demo_cli.py"]
    assert paths_touched("") == []


def test_tests_dir_untouched() -> None:
    assert _tests_dir_untouched(DIFF_SMALL) is True
    diff = DIFF_SMALL + "diff --git a/tests/test_x.py b/tests/test_x.py\n"
    assert _tests_dir_untouched(diff) is False


def test_stdlib_only_imports_clean(tmp_path: Path) -> None:
    module = tmp_path / "ok.py"
    module.write_text("import os\nimport sys\nfrom pathlib import Path\n", encoding="utf-8")
    assert stdlib_only_imports([module]) == []


def test_stdlib_only_imports_flags_third_party(tmp_path: Path) -> None:
    module = tmp_path / "bad.py"
    module.write_text(
        "import os\nimport requests\nfrom pydantic import BaseModel\n", encoding="utf-8"
    )
    assert stdlib_only_imports([module]) == ["requests", "pydantic"]


def _tiny_module(tmp_path: Path) -> Path:
    (tmp_path / "tiny.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_run_hidden_pytest_pass(tmp_path: Path) -> None:
    result_dir = _tiny_module(tmp_path)
    hidden = {"test_hidden.py": "from tiny import f\n\ndef test_f():\n    assert f() == 1\n"}
    assert run_hidden_pytest(hidden, result_dir) == (1, 1)


def test_run_hidden_pytest_fail_counts(tmp_path: Path) -> None:
    result_dir = _tiny_module(tmp_path)
    hidden = {
        "test_hidden.py": (
            "from tiny import f\n\n"
            "def test_ok():\n    assert f() == 1\n\n"
            "def test_bad():\n    assert f() == 2\n"
        )
    }
    assert run_hidden_pytest(hidden, result_dir) == (1, 2)


def test_run_hidden_pytest_keeps_result_dir_clean(tmp_path: Path) -> None:
    result_dir = _tiny_module(tmp_path)
    run_hidden_pytest({"test_h.py": "def test_x():\n    assert True\n"}, result_dir)
    leftovers = [p for p in result_dir.iterdir() if p.name.startswith("test_")]
    assert leftovers == []


def test_parse_pytest_summary() -> None:
    assert _parse_pytest_summary("3 passed in 0.12s") == (3, 3)
    assert _parse_pytest_summary("2 failed, 1 passed in 0.5s") == (1, 3)
    assert _parse_pytest_summary("1 passed, 2 skipped in 0.1s") == (1, 3)
    assert _parse_pytest_summary("no tests ran") == (0, 0)


def test_read_ship_score() -> None:
    assert read_ship_score({"ship_score": 82}) == 82
    assert read_ship_score({"ship_score": None}) is None
    assert read_ship_score({}) is None


def test_load_run_json_rejects_bad_input(tmp_path: Path) -> None:
    bad = tmp_path / "run.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_run_json(bad)
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        load_run_json(missing)


def test_git_available() -> None:
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0  # the task tests below need git for diffs
