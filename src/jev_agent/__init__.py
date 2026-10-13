"""Baton Agent Orchestrator: an idea-to-code pipeline of AI agents dispatched by Jev."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("baton-agent-orchestrator")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
