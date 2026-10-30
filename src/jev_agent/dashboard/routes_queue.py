"""QueueRoutes: Queue and scheduler actions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app.errors import JevError
from jev_agent.app.queue import QueueOptions
from jev_agent.app.runs import require_ship_enabled
from jev_agent.config import ConfigError
from jev_agent.config.schema import ShipMode
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.queue import Move, QueueError, add_idea, move_item, queue_counts, remove_item
from jev_agent.core.recurrence import (
    describe,
    local_timezone_name,
    next_due,
    parse_schedule,
)
from jev_agent.core.scheduler import SchedulerLocked, spent_today
from jev_agent.core.store import QUEUE_PRIORITIES
from jev_agent.core.workflow_profiles import ProfileStore, pin_workflow_profile
from jev_agent.core.workflow_profiles.presets import (
    PRESET_NAMES,
    is_preset_ref,
    parse_preset_ref,
)
from jev_agent.core.workflow_profiles.schema import parse_profile_ref
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _int,
    _text,
)
from jev_agent.dashboard.security import (
    checked_form,
)
from jev_agent.quality.levels import LEVELS


def _validate_recurring_profile(ref: str, repo_root: Path) -> str:
    """A recurring task's workflow-profile reference, checked at add time
    but deliberately NOT pinned: pinning happens at each enqueue (see
    `Scheduler._enqueue_recurring`), so the stored reference stays the
    unpinned form and each occurrence resolves the version current then.
    A preset passes through unchanged (pinned by release); a stored
    profile must exist now, or the task refuses with the reference named."""
    if is_preset_ref(ref):
        name = parse_preset_ref(ref)
        if name not in PRESET_NAMES:
            raise JevError(
                f"unknown workflow preset {ref!r}: pick one of: "
                + ", ".join(f"preset:{n}" for n in PRESET_NAMES)
            )
        return ref
    parsed = parse_profile_ref(ref)
    if parsed is None:
        raise JevError(f"workflow profile {ref!r} no longer resolves: pick a current profile")
    profile_id = f"{parsed[0]}:{parsed[1]}"
    store = ProfileStore(repo_root, Path.home() / ".jev")
    if not store.exists(profile_id):
        raise JevError(
            f"workflow profile {profile_id!r} no longer resolves: pick a current profile"
        )
    return ref


class QueueRoutes(DashboardCore):
    def queue_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        query = request.query_params.get("q", "")
        return self.page(
            request,
            "queue.html",
            lambda repo: {
                "q": views.queue_view(repo, show_all, query),
                "query": query.strip(),
                "sched": self.scheduler_runner.status(),
                "show_all": show_all,
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
            },
        )

    async def queue_add(self, request: Request) -> Response:
        def add(repo: views.Repo, form: FormData) -> str:
            learning_choice = _text(form, "learning")
            options = QueueOptions(
                priority=_text(form, "priority") or "normal",
                plan_only=form.get("plan_only") == "on",
                learning=learning_choice if learning_choice not in (None, "", "none") else None,
                no_learning=learning_choice == "none",
                profile=_text(form, "profile") or None,
                quality=_text(form, "quality") or None,
                ship_mode=require_ship_enabled(
                    repo.config, cast(ShipMode | None, _text(form, "ship_mode") or None)
                ),
            )
            # Queue-time snapshot (issue #368): a queued run executes the
            # version it was queued with, even if the profile is edited
            # before execution — so the reference is pinned here, at queue
            # time, and run creation resolves the pinned version. An
            # unresolvable reference refuses (ValueError flashes) instead
            # of queueing a run that cannot start. Picker `default` means
            # no profile (G1); a preset reference passes through pinning
            # unchanged, because presets are pinned by release, not version.
            workflow_profile = _text(form, "workflow_profile") or None
            if workflow_profile == "default":
                workflow_profile = None
            if workflow_profile is not None:
                workflow_profile = pin_workflow_profile(
                    ref=workflow_profile, repo_root=repo.root, user_root=Path.home() / ".jev"
                )
            item = add_idea(
                repo.store,
                repo.learning,
                _text(form, "idea") or "",
                options.priority,
                plan_only=options.plan_only,
                learning_profile=options.learning,
                no_learning=options.no_learning,
                profile=options.profile,
                quality=options.quality,
                ship_mode=options.ship_mode,
                workflow_profile=workflow_profile,
            )
            return f"queued item {item.id} ({item.priority})"

        return await self.act(request, "/queue", add)

    async def queue_move(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def move(repo: views.Repo, form: FormData) -> str:
            moves: dict[str | None, Move] = {"up": "up", "down": "down", "top": "top"}
            direction = moves.get(_text(form, "direction"))
            if direction is None:
                raise QueueError("move needs a direction: up, down or top")
            before = repo.store.get_queue_item(item_id)
            after = move_item(repo.store, item_id, direction)
            note = (
                f" (priority {before.priority} -> {after.priority})"
                if after.priority != before.priority
                else ""
            )
            return f"moved item {item_id} {direction}{note}"

        return await self.act(request, "/queue", move)

    async def queue_remove(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def remove(repo: views.Repo, form: FormData) -> str:
            remove_item(repo.store, item_id)
            return f"removed item {item_id}"

        return await self.act(request, "/queue", remove)

    async def queue_recurring_add(self, request: Request) -> Response:
        """Create a recurring task from the browser (issue #251): the same
        `parse_schedule` DSL as `jev queue add --repeat`, so the browser and
        the CLI accept exactly the same schedules. A bad schedule flashes the
        parser's message (it already lists the valid formats) instead of a 500.

        Learning/profile/quality/ship options stay CLI-only for recurring
        tasks; this form covers brief, schedule, priority, plan-only, and
        (phase 4, issue #368) the workflow profile. The profile reference
        is stored unpinned: the scheduler pins it at each enqueue (the
        existing queue pin-at-queue-time timing), so every occurrence runs
        the version current at that enqueue, not the version current when
        the task was made. A reference that does not resolve now (an
        unknown preset, a missing stored profile) refuses at add time
        instead of queueing a task that can never run.
        """

        def add(repo: views.Repo, form: FormData) -> str:
            brief = _text(form, "brief") or ""
            if not brief:
                raise JevError("the brief is empty")
            priority = _text(form, "priority") or "normal"
            if priority not in QUEUE_PRIORITIES:
                raise JevError(f"unknown priority {priority!r}")
            schedule = parse_schedule(_text(form, "schedule") or "")
            workflow_profile = _text(form, "workflow_profile") or None
            if workflow_profile == "default":
                workflow_profile = None
            if workflow_profile is not None:
                workflow_profile = _validate_recurring_profile(workflow_profile, repo.root)
            tz_name = local_timezone_name()
            tz = ZoneInfo(tz_name)
            task = repo.store.add_recurring_task(
                brief,
                schedule,
                tz_name,
                next_due(schedule, datetime.now(tz), tz),
                priority=priority,
                plan_only=form.get("plan_only") == "on",
                workflow_profile=workflow_profile,
            )
            return f"recurring task {task.id}: {describe(schedule)}"

        return await self.act(request, "/queue", add)

    async def queue_recurring_pause(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def pause(repo: views.Repo, form: FormData) -> str:
            repo.store.set_recurring_enabled(task_id, False)
            return f"paused recurring task {task_id}"

        return await self.act(request, "/queue", pause)

    async def queue_recurring_resume(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def resume(repo: views.Repo, form: FormData) -> str:
            repo.store.set_recurring_enabled(task_id, True)
            return f"resumed recurring task {task_id}"

        return await self.act(request, "/queue", resume)

    async def queue_recurring_remove(self, request: Request) -> Response:
        task_id = _int(request.path_params["id"])

        def remove(repo: views.Repo, form: FormData) -> str:
            repo.store.remove_recurring_task(task_id)
            return f"removed recurring task {task_id}"

        return await self.act(request, "/queue", remove)

    async def scheduler_start(self, request: Request) -> Response:
        """Two steps (Tier 2: a scheduler spends real money): the first POST names the
        spend implication on a confirmation page; only `confirm=yes` starts the
        dashboard's own scheduler, which dies with the dashboard.

        A GET (refreshing the confirmation page, issue #316) goes back to the
        queue: the confirmation only follows its form.
        """
        if request.method == "GET":
            self.flash(
                request, "error", "start again from the queue — this page only follows its form"
            )
            return self.back("/queue")
        form = await checked_form(request)
        to = "/queue"
        if form.get("confirm") != "yes":

            def preview() -> dict[str, Any]:
                with views.open_repo(self.settings.root) as repo:
                    return {
                        "queued": queue_counts(repo.store).get("queued", 0),
                        "spent_today": spent_today(repo.store, datetime.now(UTC)),
                    }

            try:
                info = await run_in_threadpool(preview)
            except (JevError, ConfigError) as exc:
                self.flash(request, "error", _friendly_error(exc))
                return self.back(to)
            if self.scheduler_runner.status().owner is not None:
                self.flash(request, "error", "a scheduler is already running")
                return self.back(to)
            return self.render(request, "confirm_scheduler.html", **info)

        try:
            await run_in_threadpool(self.scheduler_runner.start)
        except (SchedulerLocked, PipelineError, JevError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        else:
            self.flash(request, "ok", "scheduler started; it stops when the dashboard stops")
        return self.back(to)

    async def scheduler_stop(self, request: Request) -> Response:
        """Stop the dashboard's scheduler after the current item (Tier 1: graceful, and
        only ever our own scheduler — a terminal's keeps running)."""
        await checked_form(request)
        if self.scheduler_runner.stop():
            self.flash(
                request, "ok", "scheduler stopping: it finishes the current item, then stops"
            )
        else:
            self.flash(request, "error", "the dashboard's scheduler is not running")
        return self.back("/queue")

    async def queue_clear(self, request: Request) -> Response:
        """Two steps (Tier 2), like `learning_flush`: the first POST shows how many queued
        ideas would go; only `confirm=yes` deletes them (items with a run are kept).

        A GET (refreshing the confirmation page, issue #316) goes back to the
        queue: the confirmation only follows its form.
        """
        if request.method == "GET":
            self.flash(
                request, "error", "start again from the queue — this page only follows its form"
            )
            return self.back("/queue")
        form = await checked_form(request)
        to = "/queue"
        if form.get("confirm") != "yes":

            def count() -> int:
                with views.open_repo(self.settings.root) as repo:
                    return queue_counts(repo.store).get("queued", 0)

            try:
                queued = await run_in_threadpool(count)
            except (JevError, ConfigError) as exc:
                self.flash(request, "error", _friendly_error(exc))
                return self.back(to)
            if queued == 0:
                self.flash(request, "ok", "the queue is already empty")
                return self.back(to)
            return self.render(request, "confirm_queue_clear.html", queued=queued)

        def clear() -> int:
            with views.open_repo(self.settings.root) as repo:
                return repo.store.clear_queued()

        try:
            deleted = await run_in_threadpool(clear)
        except (JevError, ConfigError) as exc:
            self.flash(request, "error", _friendly_error(exc))
        else:
            self.flash(request, "ok", f"removed {deleted} queued item(s)")
        return self.back(to)
