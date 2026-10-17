"""Packaging regression tests.

Every third-party module imported at the top level of ``src/jev_agent`` must be
a *declared* direct dependency in ``pyproject.toml``. Depending on a package
that only arrives transitively is fragile: it works in the dev venv but breaks
the isolated wheel install in CI.

History: ``src/jev_agent/cli/events_cmds.py`` did ``from click import Command,
Context`` while click was only a transitive dependency via ``typer>=0.12``.
When typer 0.20+ dropped its click dependency, ``uv run --isolated
--no-project --with dist/*.whl jev --version`` failed with
``ModuleNotFoundError: No module named 'click'``. The same scan later caught
``pydantic_core`` imported directly by ``config/loader.py``.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "jev_agent"

# Import name -> distribution name where they differ (compared PEP 503 normalized).
MODULE_TO_DIST = {
    "markdown_it": "markdown-it-py",
}


def _dist_name(requirement: str) -> str:
    """Extract and normalize the distribution name from a requirement string."""
    name = re.split(r"[<>=!~;\s\[]", requirement, maxsplit=1)[0]
    return name.lower().replace("_", "-").replace(".", "-")


def _declared_distributions() -> set[str]:
    """Distribution names from [project] dependencies and optional-dependencies."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        project = tomllib.load(f)["project"]
    names: set[str] = set()
    for req in project.get("dependencies", []):
        names.add(_dist_name(req))
    for extra_reqs in project.get("optional-dependencies", {}).values():
        for req in extra_reqs:
            names.add(_dist_name(req))
    return names


def _imported_top_level_modules() -> set[str]:
    """Top-level module names from absolute imports anywhere under src/jev_agent."""
    modules: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def test_every_third_party_import_is_a_declared_dependency() -> None:
    declared = _declared_distributions()
    stdlib = sys.stdlib_module_names
    undeclared: list[str] = []
    for module in sorted(_imported_top_level_modules()):
        if module in stdlib or module == "jev_agent":
            continue
        dist = MODULE_TO_DIST.get(module, module).lower().replace("_", "-")
        if dist not in declared:
            undeclared.append(f"{module} (distribution '{dist}')")
    assert not undeclared, (
        "These third-party modules are imported by src/jev_agent but are not "
        "declared as direct dependencies in pyproject.toml. Transitive-only "
        "imports break the isolated wheel install in CI:\n  " + "\n  ".join(undeclared)
    )
