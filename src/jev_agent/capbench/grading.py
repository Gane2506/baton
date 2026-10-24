"""Deterministic grading helpers shared by the task `grade.py` files and the runner.

No LLM judge anywhere here: hidden pytest executed by the harness, diff-size
bands (the `_SIMPLICITY_BANDS` heuristic from `quality/scoring.py`), and the
gate's ship score read from the run record. Phase 1 is deterministic-only;
T8-style semi-deterministic graders are out of scope.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from jev_agent.capbench.models import GradeReport
from jev_agent.quality.scoring import diff_stats

# A `grade.py` exits 0 (passed), 1 (graded failure), 2 (harness error: bad args,
# missing pytest, unreadable inputs). Anything else is also a harness error.
GRADE_PASS = 0
GRADE_FAIL = 1
GRADE_ERROR = 2

_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error|skipped)")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def run_hidden_pytest(
    hidden_tests: Mapping[str, str],
    result_dir: Path,
    extra_collect: Sequence[Path] = (),
    *,
    timeout_s: int = 300,
) -> tuple[int, int]:
    """Run hidden pytest files against `result_dir`; return (passed, total).

    The hidden tests are written to a temp dir (never into the result dir, so
    they cannot pollute the diff), with a conftest that puts `result_dir` on
    `sys.path` so the tests import the result's modules. `extra_collect` adds
    paths to collect as well (e.g. the fixture's own `tests/` for T2).
    """
    with tempfile.TemporaryDirectory(prefix="capbench-hidden-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "conftest.py").write_text(
            f"import sys\nsys.path.insert(0, {str(result_dir)!r})\n", encoding="utf-8"
        )
        for name, source in hidden_tests.items():
            (tmpdir / name).write_text(source, encoding="utf-8")
        targets = [str(tmpdir), *(str(p) for p in extra_collect)]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *targets,
                "-p",
                "no:cacheprovider",
                "-q",
                "--tb=no",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=tmp,
        )
    return _parse_pytest_summary(proc.stdout)


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """`(passed, total)` from pytest's `-q` summary line; (0, 0) when unparseable."""
    for line in reversed(output.splitlines()):
        counts = {word: int(num) for num, word in _SUMMARY_RE.findall(line.strip())}
        if counts:
            passed = counts.get("passed", 0)
            total = passed + sum(counts.get(name, 0) for name in ("failed", "error", "skipped"))
            return passed, total
    return 0, 0


def paths_touched(diff: str) -> list[str]:
    """Repo-relative paths changed by a unified diff (the `b/` side)."""
    paths: list[str] = []
    for line in diff.splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match:
            paths.append(match.group(2))
    return paths


def diff_within_bands(diff: str, max_added: int, max_files: int) -> bool:
    """The diff's size inside the task's band (added lines and files changed)."""
    stats = diff_stats(diff)
    return stats.added <= max_added and stats.files_changed <= max_files


def tests_dir_untouched(diff: str, protected: Sequence[str] = ("tests/",)) -> bool:
    """No changed path under a protected dir (T2: the agent must not touch `tests/`)."""
    return not any(path.startswith(prefix) for path in paths_touched(diff) for prefix in protected)


def stdlib_only_imports(files: Sequence[Path]) -> list[str]:
    """Non-stdlib top-level modules imported by `files` (T3: no new dependencies)."""
    stdlib = sys.stdlib_module_names
    bad: list[str] = []
    for file in files:
        if file.suffix != ".py":
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import (`from .utils import x`, `from . import x`) is
                # a module of the result itself, not a new dependency: `level`
                # is 0 only for absolute imports, and only those can be non-stdlib.
                if node.level:
                    continue
                names = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            bad.extend(name for name in names if name not in stdlib and name not in bad)
    return bad


def read_ship_score(run_json: Mapping[str, object]) -> int | None:
    """The gate's ship score from the run JSON the runner hands to `grade.py`.

    Clamped to 0-100: a run record claiming 150 must not reach the
    scoreboard. Booleans are not scores (`True` is an `int` in Python).
    """
    score = run_json.get("ship_score")
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    return max(0, min(100, score))


def load_run_json(path: Path) -> dict[str, object]:
    """Parse the run JSON file; raises ValueError with a plain message on bad input."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"run JSON {path} is not an object")
    return data


def run_grade_script(
    grade_py: Path,
    result_dir: Path,
    run_json: Path,
    diff: Path,
    *,
    timeout_s: int = 600,
) -> GradeReport:
    """Run a task's `grade.py` (`--result/--run-json/--diff`) and parse its JSON report.

    Raises RuntimeError on harness errors (exit 2, unparseable stdout, or the
    exit code disagreeing with the JSON verdict) - those are bugs in the
    grader, not failures of the graded code.
    """
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(grade_py),
                "--result",
                str(result_dir),
                "--run-json",
                str(run_json),
                "--diff",
                str(diff),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung grader is a harness error like any other, not a crash of the
        # runner: the caller records it and moves on to the next task.
        raise RuntimeError(f"grade.py timed out after {timeout_s}s") from exc
    if proc.returncode == GRADE_ERROR:
        raise RuntimeError(f"grade.py harness error: {proc.stderr.strip() or proc.stdout.strip()}")
    if proc.returncode not in (GRADE_PASS, GRADE_FAIL):
        raise RuntimeError(f"grade.py exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        report = GradeReport.model_validate_json(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"grade.py printed no JSON report: {exc}") from exc
    if report.passed != (proc.returncode == GRADE_PASS):
        raise RuntimeError(
            f"grade.py exit {proc.returncode} disagrees with its JSON verdict "
            f"(passed={report.passed})"
        )
    return report


# --- the grade.py protocol -------------------------------------------------------------
# Every `benchmarks/tasks/<id>/grade.py` speaks the same argv:
#   python grade.py --result DIR --run-json FILE --diff FILE
# prints one GradeReport JSON object on stdout and exits 0 (passed), 1 (graded
# failure) or 2 (harness error). `grade_main` implements the shared skeleton so
# each grade.py only declares its hidden tests and its extra checks.

CheckFn = Callable[[str, Path], list[str]]
"""An extra grader check: (diff text, result dir) -> failure reasons (empty: ok)."""


def grade_main(
    task_id: str,
    hidden_tests: Mapping[str, str],
    *,
    max_added: int,
    max_files: int,
    extra_collect: Sequence[str] = (),
    extra_checks: Sequence[CheckFn] = (),
) -> int:
    """The body of a task's `grade.py`; returns the process exit code."""
    import argparse

    parser = argparse.ArgumentParser(prog="grade.py", description=f"Grader for {task_id}.")
    parser.add_argument("--result", required=True, help="Checkout of the result to grade.")
    parser.add_argument("--run-json", required=True, help="Run record JSON (ship score).")
    parser.add_argument("--diff", required=True, help="Unified diff of the result.")
    args = parser.parse_args()
    result_dir = Path(args.result)
    try:
        run_json = load_run_json(Path(args.run_json))
        diff_text = Path(args.diff).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return GRADE_ERROR

    reasons: list[str] = []
    details: dict[str, str] = {}
    hidden_timeout_s = 300
    try:
        tests_passed, tests_total = run_hidden_pytest(
            hidden_tests,
            result_dir,
            extra_collect=[result_dir / rel for rel in extra_collect],
            timeout_s=hidden_timeout_s,
        )
    except subprocess.TimeoutExpired:
        # Exit 2 is the harness-error protocol: the grading infrastructure
        # failed, not the code under test.
        print(json.dumps({"error": f"hidden tests timed out after {hidden_timeout_s}s"}))
        return GRADE_ERROR
    details["hidden_tests"] = f"{tests_passed}/{tests_total} passed"
    if tests_passed < tests_total:
        reasons.append(f"hidden tests: {tests_passed}/{tests_total} passed")

    stats = diff_stats(diff_text)
    details["diff"] = f"+{stats.added}/-{stats.removed} in {stats.files_changed} file(s)"
    # Reuse the stats computed above instead of recomputing them via
    # diff_within_bands: same predicate, one pass over the diff.
    diff_ok = stats.added <= max_added and stats.files_changed <= max_files
    if not diff_ok:
        reasons.append(
            f"diff too big: +{stats.added} lines in {stats.files_changed} files "
            f"(band: {max_added} lines, {max_files} files)"
        )
    for check in extra_checks:
        reasons.extend(check(diff_text, result_dir))

    ship_score = read_ship_score(run_json)
    details["ship_score"] = str(ship_score) if ship_score is not None else "no gate ran"
    passed = not reasons
    report = GradeReport(
        task_id=task_id,
        passed=passed,
        tests_passed=tests_passed,
        tests_total=tests_total,
        diff_added=stats.added,
        diff_files=stats.files_changed,
        diff_within_band=diff_ok,
        ship_score=ship_score,
        reasons=reasons,
        details=details,
    )
    print(report.model_dump_json())
    return GRADE_PASS if passed else GRADE_FAIL
