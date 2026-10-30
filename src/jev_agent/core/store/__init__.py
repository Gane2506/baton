"""SQLite state store: runs, tasks, attempts and decisions (PLAN.md section 9).

The store only persists and loads state. It knows nothing about which state transitions are
legal or what to do next; that belongs to the pipeline. JSON columns hold pydantic models
written with `model_dump_json` and read back with `model_validate_json`, so round-trips are
exact. Every write runs in its own transaction.

The store is split into focused modules under this package; this module only composes
the `Store` class from the mixins and re-exports the public API, so existing imports
(`from jev_agent.core.store import Store, RunRecord, ...`) keep working unchanged.
"""

from __future__ import annotations

from jev_agent.core.migrations import SchemaVersionError

from .attempts import AttemptsMixin
from .base import _check_update_assignments as _check_update_assignments
from .base import new_run_id
from .cache import CacheMixin
from .decisions import DecisionsMixin
from .errors import NotFoundError, StoreError
from .events import EventsMixin
from .leases import LeasesMixin
from .quality import QualityMixin
from .queue import QueueMixin
from .records import (
    PR_STATES,
    QUEUE_PRIORITIES,
    QUEUE_STATUSES,
    AttemptRecord,
    DecisionRecord,
    LearningCounts,
    PrState,
    QualityReportRecord,
    QueueItem,
    QueuePriority,
    QueueStatus,
    RecurringTask,
    RunLease,
    RunRecord,
    ShippedPrRecord,
    StageRecord,
    TaskRecord,
)
from .runs import RunsMixin
from .shipped import ShippedMixin
from .stages import StagesMixin
from .tasks import TasksMixin

__all__ = [
    "PR_STATES",
    "QUEUE_PRIORITIES",
    "QUEUE_STATUSES",
    "AttemptRecord",
    "DecisionRecord",
    "LearningCounts",
    "NotFoundError",
    "PrState",
    "QualityReportRecord",
    "QueueItem",
    "QueuePriority",
    "QueueStatus",
    "RecurringTask",
    "RunLease",
    "RunRecord",
    "SchemaVersionError",
    "ShippedPrRecord",
    "StageRecord",
    "Store",
    "StoreError",
    "TaskRecord",
    "new_run_id",
]


class Store(
    RunsMixin,
    TasksMixin,
    AttemptsMixin,
    DecisionsMixin,
    QualityMixin,
    ShippedMixin,
    LeasesMixin,
    QueueMixin,
    CacheMixin,
    StagesMixin,
    EventsMixin,
):
    """Persistent state for runs. Open with `Store.open(path)`; use as a context manager."""
