"""`WorkerRegistry`: worker name (as used in config) -> `Worker`.

The registry holds whatever implementations it is given: the CLI builds the real adapters from
config (`jev_agent.cli.wiring.build_registry`), tests inject `FakeWorker`s.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from jev_agent.config.schema import JevConfig
from jev_agent.workers.base import Worker


class WorkerNotAvailable(LookupError):
    """No worker is registered under this name."""


class WorkerRegistry:
    """`not_for` maps a role to registered workers that cannot run for it (the api worker for a
    role without a model, `cli.wiring.worker_readiness`); `names(role)` leaves them out."""

    def __init__(
        self,
        workers: Mapping[str, Worker] | None = None,
        *,
        not_for: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._workers: dict[str, Worker] = dict(workers or {})
        self._not_for: dict[str, frozenset[str]] = {
            role: frozenset(names) for role, names in (not_for or {}).items()
        }

    @classmethod
    def from_config(
        cls,
        config: JevConfig,
        injected: Mapping[str, Worker] | None = None,
        *,
        not_for: Mapping[str, Collection[str]] | None = None,
    ) -> WorkerRegistry:
        """Register the given implementation of each enabled worker in `config`.

        Disabled workers are dropped; enabled workers without an implementation are left out.
        """
        injected = injected or {}
        names = [n for n in config.workers.names() if config.workers.is_enabled(n)]
        return cls({name: injected[name] for name in names if name in injected}, not_for=not_for)

    def register(self, name: str, worker: Worker) -> None:
        self._workers[name] = worker

    def get(self, name: str) -> Worker:
        try:
            return self._workers[name]
        except KeyError:
            raise WorkerNotAvailable(f"no worker registered as {name!r}") from None

    def names(self, role: str | None = None) -> list[str]:
        """Registered workers; with `role`, only those that can run for it."""
        excluded = self._not_for.get(role, frozenset()) if role is not None else frozenset()
        return [name for name in self._workers if name not in excluded]

    def __bool__(self) -> bool:
        return bool(self._workers)
