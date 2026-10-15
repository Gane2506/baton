"""Grader for T3 (input validation). Protocol: `--result DIR --run-json FILE --diff FILE`;
prints one JSON report on stdout; exits 0 (passed), 1 (graded failure) or 2
(harness error). Self-contained: stdlib + pytest only, no Baton internals.

Grading: hidden edge-case tests (valid configs unchanged, malformed input
never leaks KeyError/TypeError/AttributeError — ValueError or DEFAULTS-based
recovery only), and no changed file may import outside the stdlib.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from jev_agent.capbench.grading import (
        grade_main,
        paths_touched,
        stdlib_only_imports,
    )
except ImportError:  # grade.py run outside the installed package: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from jev_agent.capbench.grading import (
        grade_main,
        paths_touched,
        stdlib_only_imports,
    )

HIDDEN_TESTS = {
    "test_validation_hidden.py": '''\
"""Hidden edge-case tests for T3 (the agent never sees these)."""
import pytest

from config import DEFAULTS, parse_config  # result dir on sys.path via conftest


def _ok(data):
    """The call is acceptable iff it returns a dict or raises ValueError only."""
    try:
        result = parse_config(data)
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 — any other leak fails the test
        pytest.fail(f"parse_config({data!r}) leaked {type(exc).__name__}: {exc}")
    assert isinstance(result, dict), f"parse_config({data!r}) returned {result!r}"
    return result


def test_valid_config_unchanged():
    assert parse_config({"host": "db", "port": 5432, "debug": True}) == {
        "host": "db",
        "port": 5432,
        "debug": True,
    }


def test_valid_defaults():
    assert parse_config({"host": "h", "port": 1}) == {"host": "h", "port": 1, "debug": False}


def test_port_numeric_string_still_works():
    result = parse_config({"host": "h", "port": "8080", "debug": False})
    assert result["port"] == 8080


def test_none_input():
    _ok(None)


def test_empty_dict():
    _ok({})


def test_missing_host():
    _ok({"port": 1})


def test_missing_port():
    _ok({"host": "h"})


def test_wrong_types():
    _ok({"host": 123, "port": "abc", "debug": "yes"})


def test_list_input():
    _ok(["host", 1])


def test_string_input():
    _ok("not a dict")


def test_nested_garbage():
    _ok({"host": {"nested": ["deep"]}, "port": [80], "debug": {"x": 1}})


def test_extra_keys_ignored():
    result = _ok({"host": "h", "port": 9, "extra": "ignored", "debug": False})
    if result is not None:
        assert "extra" not in result


def test_unicode_host():
    result = _ok({"host": "héllo wörld", "port": 80})
    if result is not None:
        assert result["host"] == "héllo wörld"


def test_defaults_still_exported():
    assert DEFAULTS == {"host": "localhost", "port": 8080, "debug": False}
''',
}


def _no_new_dependencies(diff: str, result_dir: Path) -> list[str]:
    changed = [result_dir / path for path in paths_touched(diff) if path.endswith(".py")]
    bad = stdlib_only_imports([p for p in changed if p.is_file()])
    if bad:
        return [f"non-stdlib imports in changed files: {', '.join(sorted(bad))}"]
    return []


def main() -> int:
    return grade_main(
        "t3-validation",
        HIDDEN_TESTS,
        max_added=150,
        max_files=6,
        extra_checks=(_no_new_dependencies,),
    )


if __name__ == "__main__":
    sys.exit(main())
