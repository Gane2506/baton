"""`jev dashboard`: a small local web UI over the same store and functions as the CLI.

Optional: it needs the `dashboard` extra (Starlette, uvicorn, python-multipart, markdown-it-py
and tomlkit). `missing_dependencies` tells the CLI what is missing, so `jev dashboard` can
print an install hint instead of a traceback. See docs/dashboard.md.
"""

from __future__ import annotations

from importlib.util import find_spec

INSTALL_HINT = (
    "The dashboard needs the `dashboard` extra: "
    "`uv tool install 'baton-agent-orchestrator[dashboard]'` "
    "(or `pip install 'baton-agent-orchestrator[dashboard]'`)."
)

# Import name -> distribution name.
REQUIRED_MODULES: dict[str, str] = {
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "python_multipart": "python-multipart",
    "markdown_it": "markdown-it-py",
    "tomlkit": "tomlkit",
}


def missing_dependencies() -> list[str]:
    """Distributions of the `dashboard` extra that are not installed."""
    return [dist for module, dist in REQUIRED_MODULES.items() if find_spec(module) is None]
