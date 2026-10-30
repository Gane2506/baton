"""Run use cases: create, start, approve and resume (the body of `jev run` and friends).

Shared by the CLI (`cli.run_cmds`) and the web UI (the dashboard, the chat): nothing here
prints or exits. Errors are `JevError` (context wiring) or domain exceptions
(`LearningError`, `ProfileError` as a `ValueError`) with user-facing messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jev_agent.app.context import AppContext, RepoContext
from jev_agent.app.errors import JevError
from jev_agent.app.profiles import (
    effective_name,
    get_profile,
    snapshot,
    write_run_profile,
)
from jev_agent.config import STATE_DIRNAME
from jev_agent.config.schema import JevConfig, ShipMode
from jev_agent.config.servers_schema import NO_PROFILE as NO_PROFILE
from jev_agent.core.learning import Learning, LearningError, check_profile_name, resolve_learning
from jev_agent.core.models import AutonomyMode, Idea
from jev_agent.core.store import RunRecord
from jev_agent.core.workflow_profiles import (
    ProfileStoreError,
    ProfileValidationError,
    build_default_snapshot,
    compute_assurance,
    default_via_profile_enabled,
    resolve_workflow_profile,
    workflows_enabled,
)
from jev_agent.quality.levels import QualityLevel, parse_level


@dataclass(frozen=True)
class RunOptions:
    """Flags for starting or resuming a run, validated once (issue #92).

    Raises `JevError` (user-facing) for `--learning` with `--no-learning`, or an unknown
    quality level. `ship_mode` is validated separately by `require_ship_enabled`, which
    needs the config.
    """

    plan_only: bool = False
    learning: str | None = None
    no_learning: bool = False
    quality: str | None = None
    profile: str | None = None
    ship_mode: ShipMode | None = None

    def __post_init__(self) -> None:
        if self.quality is not None:
            try:
                parse_level(self.quality)
            except ValueError as exc:
                raise JevError(str(exc)) from exc
        if self.learning is not None and self.no_learning:
            raise JevError("--learning and --no-learning cannot be used together")


def require_ship_enabled(
    config: JevConfig, ship_mode: ShipMode | None, *, what: str = "that choice"
) -> ShipMode | None:
    """The run's ship mode, or None; `JevError` when a mode is chosen but `[ship] enabled`
    is false. `what` names the choice the way the front end does (`--auto-ship`, ...)."""
    if ship_mode is None:
        return None
    if not config.ship.enabled:
        raise JevError(f"{what} needs shipping on: set `[ship] enabled = true` (see docs/ship.md)")
    return ship_mode


def _learning(ctx: RepoContext | AppContext) -> Learning:
    if ctx.learning is None:
        raise JevError("learning is not available: no learning registry was loaded for this repo")
    return ctx.learning


def _autonomy_for_new_run(ctx: RepoContext | AppContext) -> tuple[str, bool, str | None]:
    """Map the `[autonomy]` config section onto the run's autonomy fields.

    Choosing `mode = "autonomous"` in config is the explicit opt-in (it is never
    the default); `medium` needs `project_folder` set. Raises ValueError with a
    plain-language message when the combination is incomplete.
    """
    auto = ctx.config.autonomy
    mode = AutonomyMode(auto.mode)
    if mode is AutonomyMode.MEDIUM and not auto.project_folder:
        raise ValueError(
            'autonomy mode "medium" needs project_folder set: pick the folder '
            "the run is scoped to (Settings > Autonomy, or [autonomy] project_folder)"
        )
    return (
        mode.value,
        mode is AutonomyMode.AUTONOMOUS,
        str(auto.project_folder) if auto.project_folder else None,
    )


def create_run(
    ctx: RepoContext | AppContext,
    idea: str,
    *,
    plan_only: bool = False,
    learning: str | None = None,
    no_learning: bool = False,
    quality: str | None = None,
    profile: str | None = None,
    ship_mode: ShipMode | None = None,
    design_system: str | None = None,
    workflow_profile: str | None = None,
) -> RunRecord:
    """Create the run and record its flags, without advancing it (the front half of `jev run`;
    `Pipeline.advance` continues it from its first state). Same errors as `start_run`.

    Front ends that must show the run id at once (the dashboard, the chat) create a run this
    way and advance it in the background. `design_system` is the launching chat's
    design-system choice ("library:<name>", "repo", or None for the repo default).

    `workflow_profile` is the workflow-profile pick (issue #368): None or
    `"default"` keeps the legacy path (the resolver is skipped, no snapshot
    stored, execution behaviour unchanged). Any other value resolves through
    the one resolver function — a path that cannot resolve a profile refuses
    with the setting named (`JevError`), never silently falling back.
    """
    workspace = effective_name(ctx.config, profile, ctx.root / STATE_DIRNAME)
    # Keep the per-run flag and the workspace value apart: precedence (F1)
    # is flag > profile > workspace/config, so the resolver needs both.
    workspace_quality: str | None = None
    workspace_learning: str | None = None
    no_learning_eff = no_learning
    learning_in = learning
    if workspace is not None:
        settings = get_profile(ctx.config, workspace)
        if learning is None and not no_learning and settings.learning is not None:
            if settings.learning is False:
                no_learning_eff = True
            else:
                workspace_learning = settings.learning
                learning_in = settings.learning
        if quality is None and settings.quality is not None:
            workspace_quality = settings.quality
    legacy_level = (
        ctx.config.quality.level
        if quality is None and workspace_quality is None
        else parse_level(quality if quality is not None else workspace_quality)  # type: ignore[arg-type]
    )
    learning_profile = resolve_learning(
        ctx.store, _learning(ctx), ctx.config, learning_in, no_learning_eff
    )
    copy = None if workspace is None else snapshot(ctx.config, workspace)
    autonomy_mode, autonomous_opt_in, medium_folder = _autonomy_for_new_run(ctx)
    # One flags dict for both profile paths (stored profile and default
    # preset): tier-1 values plus tier-3 source markers, per section 9.2.
    resolver_flags = {
        "quality_level": parse_level(quality) if quality is not None else None,
        "quality_repo_value": (
            parse_level(workspace_quality)
            if workspace_quality is not None
            else ctx.config.quality.level
        ),
        "quality_source": "workspace" if workspace_quality is not None else "repo",
        "ship_mode": ship_mode,
        "learning_profile": learning_profile,
        "learning_profile_is_flag": learning is not None,
        "learning_source": ("workspace" if workspace_learning is not None else "repo"),
    }
    resolved = None
    if workflow_profile is not None and workflow_profile != "default":
        # Rollback switch (design section 17): disabled blocks profile
        # selection at creation — it never silently falls back to default.
        if not workflows_enabled(ctx.config):
            raise JevError(
                "workflow profiles are disabled: set `[workflows] enabled = true` to pick a profile"
            )
        learning_obj = ctx.learning
        try:
            resolved = resolve_workflow_profile(
                ref=workflow_profile,
                config=ctx.config,
                repo_root=ctx.root,
                user_root=Path.home() / ".jev",
                flags=resolver_flags,
                autonomy_mode=autonomy_mode,
                plan_approval_required=ctx.config.plan_approval_required,
                learning_profiles=(
                    {p.name for p in learning_obj.profiles()} if learning_obj else None
                ),
            )
        except (ProfileStoreError, ProfileValidationError) as exc:
            raise JevError(str(exc)) from exc
    elif workflows_enabled(ctx.config) and default_via_profile_enabled(ctx.config):
        # G1 migration trigger (design section 8.2): a no-profile run
        # executes via the profile path using the `default` preset's
        # parameters. Picker `default` still means no profile — only the
        # execution path changes, and only while the conformance suite is
        # green (the suite is the guard; the flag does not re-check it at
        # run time, it is the operator's attestation that it is green).
        try:
            resolved = build_default_snapshot(
                config=ctx.config,
                flags=resolver_flags,
                autonomy_mode=autonomy_mode,
                plan_approval_required=ctx.config.plan_approval_required,
            )
        except ProfileValidationError as exc:
            raise JevError(str(exc)) from exc
    # Phase 3: the snapshot drives execution (pipeline gates, caps, models,
    # ship decision); the run's existing quality/ship fields still carry the
    # resolved values for readers that use them. Learning enablement is
    # never changed by a profile (design 7.4): when learning is on, the
    # learning step runs under the snapshot's learning profile setting
    # (section 8.1), so the stored name is the snapshot's, not the
    # workspace/config value the profile beat. When learning is off
    # (`learning_profile` None) the stored name stays None.
    # Assurance is computed at creation for every run: from the snapshot for
    # a profiled run, from the run's effective settings for a legacy run
    # (design 10.3) — so a legacy run using a configuration bypass carries
    # the same reduced label a profiled run would.
    level = resolved.snapshot["quality_level"] if resolved else legacy_level
    run_ship_mode = resolved.snapshot["after_approval"] if resolved else ship_mode
    if resolved is not None and learning_profile is not None:
        learning_profile = resolved.snapshot["learning_profile"]
    if resolved is None:
        if autonomy_mode == "autonomous":
            legacy_plan_gate = False
        elif autonomy_mode == "ask_every_step":
            legacy_plan_gate = True
        else:
            legacy_plan_gate = ctx.config.plan_approval_required
        legacy_assurance, legacy_absent = compute_assurance(
            quality_level=legacy_level,
            score_skip_human=ctx.config.quality.score_skip_human,
            autonomy_mode=autonomy_mode,
            plan_gate_present=legacy_plan_gate,
        )
    else:
        legacy_assurance, legacy_absent = resolved.assurance, resolved.assurance_absent
    run = ctx.store.create_run(
        Idea(text=idea, repo_path=ctx.root),
        learning_profile,
        level,
        plan_only=plan_only,
        ship_mode=run_ship_mode,
        autonomy_mode=autonomy_mode,
        autonomous_opt_in=autonomous_opt_in,
        medium_folder=medium_folder,
        design_system=design_system,
        workflow_profile_id=resolved.profile_id if resolved else None,
        workflow_profile_version=resolved.version if resolved else None,
        workflow_profile_release=resolved.release if resolved else None,
        goal=resolved.goal if resolved else None,
        preset_goal=resolved.preset_goal if resolved else None,
        assurance=legacy_assurance,
        assurance_absent=legacy_absent,
        flow_snapshot=resolved.snapshot if resolved else None,
    )
    write_run_profile(ctx.root / STATE_DIRNAME, run.id, copy)
    return run


async def start_run(
    ctx: AppContext,
    idea: str,
    *,
    plan_only: bool = False,
    learning: str | None = None,
    no_learning: bool = False,
    quality: str | None = None,
    profile: str | None = None,
    ship_mode: ShipMode | None = None,
    design_system: str | None = None,
    workflow_profile: str | None = None,
) -> RunRecord:
    """Start a run for `idea` and advance it as far as it goes (the body of `jev run`).

    `learning` / `no_learning` are the run flags; `resolve_learning` applies the precedence
    (flag > `jev learning` setting > jev.toml > default). Raises `LearningError` for an
    unknown profile or both flags at once. `quality` is the quality gate level (None: the
    config's `[quality] level`); ValueError for an unknown level. Reusable by other front
    ends (the chat, the scheduler).

    `profile` is the workspace profile (`none`: none); without it the repo default applies
    (`jev profile use`). The profile's `learning` and `quality` apply when no flag says
    otherwise. ProfileError (a ValueError) for an unknown profile. `ship_mode` is what the
    run does after both votes approve at D6 (None: `[ship] after_approval`). `design_system`
    is the launching chat's design-system choice (None: the repo default).
    """
    run = create_run(
        ctx,
        idea,
        plan_only=plan_only,
        learning=learning,
        no_learning=no_learning,
        quality=quality,
        profile=profile,
        ship_mode=ship_mode,
        design_system=design_system,
        workflow_profile=workflow_profile,
    )
    return await ctx.pipeline.advance(run.id)


def set_run_profile(ctx: AppContext, run_id: str, profile: str | None) -> None:
    """`jev resume --profile NAME|none`: replace the run's workspace profile."""
    if profile is None:
        return
    ctx.store.get_run(run_id)  # NotFoundError for an unknown run
    copy = None if profile == NO_PROFILE else snapshot(ctx.config, profile)
    write_run_profile(ctx.root / STATE_DIRNAME, run_id, copy)


def set_run_learning(ctx: AppContext, run_id: str, learning: str | None, no_learning: bool) -> None:
    """Apply `--learning` / `--no-learning` to an existing run (from `jev resume`). Without
    either flag the run keeps the profile it started with."""
    if learning is not None and no_learning:
        raise LearningError("--learning and --no-learning cannot be used together")
    ctx.store.get_run(run_id)  # NotFoundError for an unknown run
    if no_learning:
        ctx.store.set_run_learning(run_id, None)
    elif learning is not None:
        if _learning(ctx).get(check_profile_name(learning)) is None:
            raise LearningError(
                f"no learning profile named {learning!r}; "
                f"create it with `jev learning create {learning}`"
            )
        ctx.store.set_run_learning(run_id, learning)


async def approve_run(ctx: AppContext, run_id: str) -> RunRecord:
    """Approve the run's plan and execute it when workers are available (the body of
    `jev approve`; the dashboard runs it in a background thread)."""
    return await ctx.pipeline.approve(run_id, execute=ctx.has_workers)


async def resume_run(
    ctx: AppContext,
    run_id: str,
    *,
    learning: str | None = None,
    no_learning: bool = False,
    quality: QualityLevel | None = None,
    profile: str | None = None,
) -> RunRecord:
    """Apply the resume flags, then continue the run (the body of `jev resume`)."""
    set_run_learning(ctx, run_id, learning, no_learning)
    set_run_profile(ctx, run_id, profile)
    if quality is not None:
        ctx.store.set_run_quality(run_id, quality)
    return await ctx.pipeline.resume(run_id)
