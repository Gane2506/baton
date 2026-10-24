"""SQLite row -> record converters (and the small time helpers they share)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jev_agent.core.cache import CacheEntry
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    DesignDoc,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.quality.models import GateResult

from .records import (
    AttemptRecord,
    DecisionRecord,
    LearningCounts,
    QualityReportRecord,
    QueueItem,
    RecurringTask,
    RunLease,
    RunRecord,
    ShippedPrRecord,
    TaskRecord,
)


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _aware_time(value: str) -> datetime:
    """ISO-8601 to a tz-aware datetime; a naive stored value is treated as UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _cache_entry(
    row: sqlite3.Row, *, hit_count: int | None = None, last_hit_at: str | None = None
) -> CacheEntry:
    return CacheEntry(
        key_hash=row["key_hash"],
        kind=row["kind"],
        key_json=row["key_json"],
        value_json=row["value_json"],
        cost_usd=float(row["cost_usd"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        hit_count=row["hit_count"] if hit_count is None else hit_count,
        last_hit_at=row["last_hit_at"] if last_hit_at is None else last_hit_at,
    )


def _shipped_pr(row: sqlite3.Row) -> ShippedPrRecord:
    raw = row["raw_json"]
    reverted = row["reverted"]
    return ShippedPrRecord(
        id=row["id"],
        run_id=row["run_id"],
        remote=row["remote"],
        repo=row["repo"],
        branch=row["branch"],
        base=row["base"],
        number=row["number"],
        url=row["url"],
        title=row["title"],
        draft=bool(row["draft"]),
        head_oid=row["head_oid"],
        pushed_commits=row["pushed_commits"],
        created_at=datetime.fromisoformat(row["created_at"]),
        state=row["state"],
        merged_at=_time(row["merged_at"]),
        closed_at=_time(row["closed_at"]),
        review_comments=row["review_comments"],
        reviews=row["reviews"],
        ci_status=row["ci_status"],
        commits_after_open=row["commits_after_open"],
        additions=row["additions"],
        deletions=row["deletions"],
        changed_files=row["changed_files"],
        reverted=None if reverted is None else bool(reverted),
        last_synced_at=_time(row["last_synced_at"]),
        raw=None if raw is None else json.loads(raw),
    )


def _run_lease(row: sqlite3.Row) -> RunLease:
    return RunLease(
        run_id=row["run_id"],
        token=row["token"],
        pid=int(row["pid"]),
        host=row["host"],
        acquired_at=datetime.fromisoformat(row["acquired_at"]),
        heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
    )


def _learning_counts(text: str | None) -> LearningCounts | None:
    if text is None:
        return None
    data = json.loads(text)
    return LearningCounts(int(data["added"]), int(data["updated"]), int(data["retired"]))


def _run_record(row: sqlite3.Row) -> RunRecord:
    plan_json = row["plan_json"]
    validation_json = row["validation_json"]
    design_json = row["design_json"]
    return RunRecord(
        id=row["id"],
        idea=Idea.model_validate_json(row["idea_json"]),
        state=RunState(row["state"]),
        plan=None if plan_json is None else Plan.model_validate_json(plan_json),
        plan_round=row["plan_round"],
        spend_usd=row["spend_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        reason=row["reason"],
        validation=None
        if validation_json is None
        else ValidationResult.model_validate_json(validation_json),
        resume_state=None if row["resume_state"] is None else RunState(row["resume_state"]),
        learning_profile=row["learning_profile"],
        learning_outcome=row["learning_outcome"],
        learning_counts=_learning_counts(row["learning_counts_json"]),
        quality_level=row["quality_level"] or "none",
        plan_only=bool(row["plan_only"]),
        ship_mode=row["ship_mode"],
        autonomy_mode=row["autonomy_mode"] or "standard",
        autonomous_opt_in=bool(row["autonomous_opt_in"]),
        medium_folder=row["medium_folder"],
        design=None if design_json is None else DesignDoc.model_validate_json(design_json),
        design_system=row["design_system"],
    )


def _task_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        run_id=row["run_id"],
        id=row["id"],
        card=TaskCard.model_validate_json(row["card_json"]),
        state=TaskState(row["state"]),
        attempts=row["attempts"],
        spend_usd=row["spend_usd"],
        worker=row["worker"],
        model=row["model"],
        effort=row["effort"],
        note=row["note"],
        lessons_scored=bool(row["lessons_scored"]),
    )


def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
    result_json = row["result_json"]
    log_path = row["log_path"]
    return AttemptRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        attempt_no=row["attempt_no"],
        role=row["role"],
        worker=row["worker"],
        model=row["model"],
        effort=row["effort"],
        log_path=None if log_path is None else Path(log_path),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_time(row["finished_at"]),
        result=None if result_json is None else WorkerResult.model_validate_json(result_json),
        lesson_ids=tuple(json.loads(row["lesson_ids"])) if row["lesson_ids"] else (),
    )


def _decision_record(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        request=DecisionRequest.model_validate_json(row["request_json"]),
        decision=Decision.model_validate_json(row["decision_json"]),
        fallback_reason=row["fallback_reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _quality_record(row: sqlite3.Row) -> QualityReportRecord:
    return QualityReportRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        result=GateResult.model_validate_json(row["result_json"]),
        report_path=None if row["report_path"] is None else Path(row["report_path"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _queue_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        idea=row["idea"],
        priority=row["priority"],
        position=row["position"],
        status=row["status"],
        run_id=row["run_id"],
        plan_only=bool(row["plan_only"]),
        learning_profile=row["learning_profile"],
        no_learning=bool(row["no_learning"]),
        profile=row["profile"],
        note=row["note"],
        quality=row["quality"],
        ship_mode=row["ship_mode"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _recurring_task(row: sqlite3.Row) -> RecurringTask:
    return RecurringTask(
        id=row["id"],
        brief=row["brief"],
        schedule=row["schedule"],
        schedule_source=row["schedule_source"],
        timezone=row["timezone"],
        next_due_at=_aware_time(row["next_due_at"]),
        last_run_at=None if row["last_run_at"] is None else _aware_time(row["last_run_at"]),
        enabled=bool(row["enabled"]),
        active_item_id=row["active_item_id"],
        priority=row["priority"],
        plan_only=bool(row["plan_only"]),
        learning_profile=row["learning_profile"],
        no_learning=bool(row["no_learning"]),
        profile=row["profile"],
        quality=row["quality"],
        ship_mode=row["ship_mode"],
        created_at=_aware_time(row["created_at"]),
    )
