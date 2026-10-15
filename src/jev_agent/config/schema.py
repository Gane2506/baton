"""Configuration schema (PLAN.md section 8). See docs/config.md for a reference."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jev_agent.config.servers_schema import (
    BrowserConfig,
    ServerConfig,
    WorkspaceProfileConfig,
    check_servers_config,
)
from jev_agent.quality.levels import QualityLevel
from jev_agent.runlog.redact import SECRET_PATTERNS

DispatcherName = Literal["consensus", "jev", "rules"]
Selection = Literal["dispatcher", "fixed"]

# The questions asked at each decision point, in request order (dispatch/requests.py builds
# the requests; a test checks they match). `[dispatcher.jev.instructions]` keys are checked
# against this table.
DECISION_QUESTIONS: dict[str, tuple[str, ...]] = {
    "D1": ("clear", "worker", "model", "effort"),
    "D2": ("worker", "model", "effort"),
    "D3": ("verdict",),
    "D4": ("complexity", "worker", "model", "effort"),
    "D5": ("action",),
    "D6": ("action",),
    "D7": ("learn", "worker", "model", "effort"),
    "D8": ("worker", "model", "effort"),
}

# Effort levels each worker passes on (docs/verification.md); any other level is ignored by
# that worker. `[roles.<role>] efforts` must be accepted by at least one of the role's workers.
WORKER_EFFORTS: dict[str, frozenset[str]] = {
    "claude_code": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "codex": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
    "api": frozenset({"low", "medium", "high"}),
}
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")


def _effort_list(levels: frozenset[str]) -> str:
    return ", ".join(level for level in _EFFORT_ORDER if level in levels)


# Key names that look like they hold a secret. `api_key_env` and `bearer_token_env` are
# allowed: they name an env var.
_SECRET_KEY = re.compile(r"(api_)?key$|token|secret|password", re.IGNORECASE)
_ALLOWED_SECRET_LOOKALIKES = frozenset({"api_key_env", "bearer_token_env"})
# Tables whose keys are names chosen by the user (MCP server, skill and plugin names, HTTP
# header names), not settings: a server called "secrets" or a header "X-Api-Key" is fine.
_NAME_TABLES = frozenset({"tools.mcp", "tools.skills", "tools.plugins", "servers", "profiles"})


def _keys_are_names(prefix: str) -> bool:
    return (
        prefix in _NAME_TABLES
        or prefix.endswith(".headers_from_env")
        or (prefix.startswith("profiles.") and prefix.endswith(".access_overrides"))
    )


def find_secret_keys(data: Any, prefix: str = "") -> list[str]:
    """Return the dotted path of every key in `data` whose name looks like it holds a secret."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if (
                _SECRET_KEY.search(str(key))
                and key not in _ALLOWED_SECRET_LOOKALIKES
                and not _keys_are_names(prefix)
            ):
                found.append(path)
            found.extend(find_secret_keys(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(find_secret_keys(item, f"{prefix}.{index}"))
    return found


def secret_keys_message(paths: list[str]) -> str:
    return (
        f"secrets are not allowed in config files (found: {', '.join(paths)}); "
        "put the secret in an environment variable and name it with `api_key_env`"
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NATIVE_JEV_BASE_URL = "https://api.typesafe.ai/v1"
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


class JevSettings(_Strict):
    """`[dispatcher.jev]`: connection and gating settings for the Jev dispatcher.

    Defaults call Jev through OpenRouter; `for_native()` gives the native TypeSafe values.
    """

    base_url: str = Field(
        default=OPENROUTER_BASE_URL,
        min_length=1,
        description=(
            "Where jev sends its decision requests: OpenRouter by default, or TypeSafe directly."
        ),
    )
    model: str = Field(
        default="~typesafe/jev-latest",
        min_length=1,
        description="Which Jev model answers the decision questions.",
    )
    api_key_env: str = Field(
        default="OPENROUTER_API_KEY",
        pattern=_ENV_NAME,
        description="Name of the environment variable holding the API key (never the key itself).",
    )
    timeout_s: float = Field(
        default=30.0, gt=0, description="Seconds to wait for one Jev answer before giving up."
    )
    min_confidence: float = Field(
        default=0.55,
        ge=0,
        le=1,
        description="Below this confidence on any answer, the plain rules decide instead of Jev.",
    )
    # Instruction overrides, keyed "D4.effort" (one point) or "effort" (every point).
    instructions: dict[str, str] = Field(
        default_factory=dict,
        description="Replace jev's instruction for a decision question, keyed like 'D4.effort'.",
    )

    @field_validator("instructions")
    @classmethod
    def _check_instruction_keys(cls, value: dict[str, str]) -> dict[str, str]:
        questions = sorted({q for qs in DECISION_QUESTIONS.values() for q in qs})
        for key in value:
            point, dot, question = key.rpartition(".")
            if not dot and question in questions:
                continue
            if dot and question in DECISION_QUESTIONS.get(point, ()):
                continue
            hint = (
                f"; {point} asks: {', '.join(DECISION_QUESTIONS[point])}"
                if point in DECISION_QUESTIONS
                else f"; points are {', '.join(DECISION_QUESTIONS)}"
                if dot
                else ""
            )
            raise ValueError(
                f'unknown key {key!r}: use "<point>.<question>" (e.g. "D4.effort") or a '
                f"question name ({', '.join(questions)}){hint}"
            )
        return value

    @classmethod
    def for_native(cls, **overrides: Any) -> JevSettings:
        """Settings for calling TypeSafe directly instead of through OpenRouter."""
        values: dict[str, Any] = {
            "base_url": NATIVE_JEV_BASE_URL,
            "model": "jev-latest",
            "api_key_env": "TYPESAFE_API_KEY",
        }
        values.update(overrides)
        return cls(**values)


# The Laya CDA decision matrix (issue #66): the criteria Laya scores every allowed answer on,
# and their default weights. `[dispatcher.laya] weights` may change the weights, not the names.
LAYA_CRITERIA: dict[str, str] = {
    "correctness": "Correctness: will it solve the problem?",
    "safety": "Safety: does it avoid harmful changes?",
    "simplicity": "Simplicity: is it the least complex solution?",
    "maintainability": "Maintainability: is it readable and sound long-term?",
    "tool_fit": "Tool/resource fit: does it use the available tools and budget well?",
    "user_preference": "User preference: does it align with the stated intent?",
}
DEFAULT_LAYA_WEIGHTS: dict[str, float] = {
    "correctness": 0.30,
    "safety": 0.20,
    "simplicity": 0.15,
    "maintainability": 0.15,
    "tool_fit": 0.10,
    "user_preference": 0.10,
}


class LayaSettings(_Strict):
    """`[dispatcher.laya]`: Laya's vote in the consensus dispatcher (Jev scoring every allowed
    answer on the Laya CDA matrix). Laya reaches Jev with the `[dispatcher.jev]` settings."""

    weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_LAYA_WEIGHTS),
        description="How much each of the six criteria counts in Laya's vote; must add up to 1.",
    )
    # The winner's weighted total (1-5 scale) must lead the runner-up by this much, also after
    # every low-confidence score is moved one level against it.
    min_margin: float = Field(
        default=0.2,
        ge=0,
        le=4,
        description="How far ahead the winner must be for Laya to count it as agreement.",
    )
    # Extra rounds after a first round without agreement, before the run needs a human.
    follow_up_rounds: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Extra voting rounds after a disagreement before a human is asked.",
    )

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = sorted(set(value) - set(LAYA_CRITERIA))
        missing = sorted(set(LAYA_CRITERIA) - set(value))
        if unknown or missing:
            raise ValueError(
                f"weights must name exactly the criteria {', '.join(LAYA_CRITERIA)}"
                + (f"; unknown: {', '.join(unknown)}" if unknown else "")
                + (f"; missing: {', '.join(missing)}" if missing else "")
            )
        if any(w < 0 for w in value.values()):
            raise ValueError("weights must not be negative")
        if abs(sum(value.values()) - 1) > 1e-6:
            raise ValueError(f"weights must add up to 1 (they add up to {sum(value.values()):g})")
        return value


class CostSettings(_Strict):
    """`[dispatcher.cost]`: cost-aware leg ordering and pre-run cost estimates.

    Model prices live here, not in code: providers reprice, and both the leg
    ordering and the pricing-fallback estimate must use the user's numbers.
    """

    prefer_cheaper_leg: bool = Field(
        default=True,
        description=(
            "Try the cheaper Jev leg first (native TypeSafe vs OpenRouter) when both are "
            "reachable. The other leg still serves on failure, and the rules dispatcher "
            "still backs up both: this changes the order, never the availability."
        ),
    )
    native_price_per_million: float = Field(
        default=0.042,
        ge=0,
        description=(
            "USD per 1M input tokens for one Jev decision call on the native TypeSafe "
            "leg (output tokens are free)."
        ),
    )
    openrouter_price_per_million: float = Field(
        default=0.042,
        ge=0,
        description="USD per 1M input tokens for one Jev decision call through OpenRouter.",
    )
    min_history_runs: int = Field(
        default=3,
        ge=1,
        description=(
            "Ended runs needed before the pre-run estimate trusts history; below this "
            "it falls back to model pricing times expected tokens."
        ),
    )
    history_runs: int = Field(
        default=20,
        ge=1,
        description="How many of the newest ended runs feed the pre-run estimate.",
    )
    tokens_per_decision: int = Field(
        default=8000,
        gt=0,
        description="Expected input tokens of one Jev decision call (pricing fallback).",
    )
    tokens_per_task: int = Field(
        default=100_000,
        gt=0,
        description="Expected input tokens of one task's worker calls (pricing fallback).",
    )
    worker_price_per_million: float = Field(
        default=3.0,
        ge=0,
        description=(
            "USD per 1M input tokens for worker calls (pricing fallback). Only a guess: "
            "set it to your own blended rate once you have run history."
        ),
    )
    warn_fraction: float = Field(
        default=0.8,
        gt=0,
        description=(
            "The estimate warns when its high end reaches this fraction of `[budget] per_run_usd`."
        ),
    )
    refuse_fraction: float = Field(
        default=1.0,
        gt=0,
        description=(
            "The estimate refuses approval when its high end reaches this fraction of "
            "`[budget] per_run_usd` (advisory: `--force` still approves)."
        ),
    )

    @model_validator(mode="after")
    def _check_fractions(self) -> CostSettings:
        if self.warn_fraction > self.refuse_fraction:
            raise ValueError(
                f"warn_fraction ({self.warn_fraction:g}) must not exceed "
                f"refuse_fraction ({self.refuse_fraction:g})"
            )
        if self.history_runs < self.min_history_runs:
            raise ValueError(
                f"history_runs ({self.history_runs}) must not be below "
                f"min_history_runs ({self.min_history_runs})"
            )
        return self


class DispatcherConfig(_Strict):
    primary: DispatcherName = Field(
        default="consensus",
        description=(
            "Who makes the decisions: Jev and Laya voting together, Jev alone, or fixed rules."
        ),
    )
    jev: JevSettings = Field(
        default_factory=JevSettings, description="Connection and gating for the Jev dispatcher."
    )
    laya: LayaSettings = Field(
        default_factory=LayaSettings, description="Laya's vote in the consensus dispatcher."
    )
    cost: CostSettings = Field(
        default_factory=CostSettings,
        description="Cost-aware leg ordering and pre-run cost estimates.",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_fallback(cls, data: Any) -> Any:
        """`fallback` was removed (issue #30): the rules dispatcher always backs up Jev. Old
        files may still say `fallback = "rules"`; any other value would be silently ignored."""
        if not isinstance(data, dict) or "fallback" not in data:
            return data
        if data["fallback"] != "rules":
            raise ValueError(
                f'fallback = {data["fallback"]!r} is not supported: only "rules" is used '
                "(the rules dispatcher always backs up Jev); remove the key"
            )
        return {key: value for key, value in data.items() if key != "fallback"}


class BudgetConfig(_Strict):
    per_run_usd: float = Field(
        default=5.0,
        gt=0,
        description="The most one run may spend on AI calls, in dollars; the run stops past this.",
    )
    per_task_usd: float = Field(
        default=1.5,
        gt=0,
        description="The most one task may spend, in dollars; cannot exceed per_run_usd.",
    )
    max_plan_rounds: int = Field(
        default=3,
        gt=0,
        description="How many times the planner may revise its plan before a human is asked.",
    )
    max_task_attempts: int = Field(
        default=3,
        gt=0,
        description="How many tries a task gets before it escalates or stops.",
    )
    test_timeout_s: int = Field(
        default=1200, gt=0, description="Seconds one test command may run before it is stopped."
    )

    @model_validator(mode="after")
    def _task_within_run(self) -> BudgetConfig:
        if self.per_task_usd > self.per_run_usd:
            raise ValueError(
                f"per_task_usd ({self.per_task_usd}) must not exceed "
                f"per_run_usd ({self.per_run_usd})"
            )
        return self


class IntegrationConfig(_Strict):
    """`[integration]`: the step that merges task branches and tests the result."""

    # Full test suite run on the result branch after each task's own test command.
    test_command: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Full test suite run on the merged result, e.g. 'uv run pytest -q'; unset: no full "
            "suite."
        ),
    )
    # Fix tasks D6 may add after failing integration tests before a human is asked.
    max_fix_rounds: int = Field(
        default=2,
        ge=0,
        description=(
            "How many fix tasks the decider may add for failing integration tests before a human "
            "is asked."
        ),
    )


class WorkspaceConfig(_Strict):
    """`[workspace]`: the git worktrees jev commits and merges task work in."""

    # Run the repository's git hooks (pre-commit, commit-msg, ...) on jev's own commits and
    # merges. Off: jev passes `--no-verify`. Hooks are a quality signal, but a hook that hangs
    # stops the run (git calls time out, issue #24).
    run_hooks: bool = Field(
        default=True,
        description="Run the repo's git hooks (pre-commit, ...) on jev's own commits and merges.",
    )


class ApprovalConfig(_Strict):
    # Stop for a human after the plan is validated. Unset: off under the consensus dispatcher,
    # whose D3 approve vote (Jev and Laya agreeing) is the gate, and on otherwise (issue #66).
    require_plan_approval: bool | None = Field(
        default=None,
        description=(
            "Stop and ask a human after the plan is checked, before building. Unset: off with "
            "consensus, on otherwise."
        ),
    )


# How independently a run may act before it stops for a human (Phase 2, workstream C).
# Values match core.models.AutonomyMode (workstream C1); the config-to-run bridge maps
# them onto a run.
AutonomyMode = Literal["standard", "autonomous", "ask_every_step", "manual", "medium"]
# What agents are allowed to touch (Phase 2, workstream C).
AccessTier = Literal["basic", "medium", "unrestricted"]


class AutonomyConfig(_Strict):
    """`[autonomy]`: how much jev asks you, and what its agents may touch."""

    mode: AutonomyMode = Field(
        default="standard",
        description=(
            'How much jev acts on its own. "standard": you approve the plan once, then '
            'it builds alone (today\'s behavior). "medium": like "standard" but scoped '
            'to project_folder. "ask_every_step": it asks before every step. '
            '"autonomous": it never stops for approval. "manual": it only acts when '
            "you tell it to."
        ),
    )
    access_tier: AccessTier = Field(
        default="basic",
        description=(
            'What agents may touch. "basic" (safest): work inside the repo, read-only '
            'outside it. "medium": also writes inside project_folder. "unrestricted": '
            "anything, with your privileges. Needs the acknowledgment below; never pick "
            "it casually."
        ),
    )
    project_folder: Path | None = Field(
        default=None,
        description="The folder the medium access tier may change. Unset: the repository root.",
    )
    unrestricted_acknowledged: bool = Field(
        default=False,
        description=(
            'Tick this only if you understand that "unrestricted" lets agents run any '
            "command with your privileges. The config is rejected while the tier is "
            '"unrestricted" and this is unticked; lower the tier before unticking it.'
        ),
    )

    @field_validator("project_folder", mode="before")
    @classmethod
    def _empty_folder_is_unset(cls, value: object) -> object:
        # The dashboard posts "" for an empty text box; that means "unset", not ".".
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_unrestricted_acknowledged(self) -> AutonomyConfig:
        if self.access_tier == "unrestricted" and not self.unrestricted_acknowledged:
            raise ValueError(
                'access_tier = "unrestricted" needs unrestricted_acknowledged = true: '
                "tick the acknowledgment in Settings > Advanced first"
            )
        return self


# Learning profile names: letters, digits, `-` and `_`, starting with a letter or digit.
PROFILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"


class LearningConfig(_Strict):
    """`[learning]`: recursive learning (docs/learning.md). Off by default: when on, each run
    ends with one extra model call (the learner)."""

    enabled: bool = Field(
        default=False,
        description=(
            "Learn from past runs: add distilled lessons to future prompts (costs one extra model "
            "call per run)."
        ),
    )
    profile: str = Field(
        default="default",
        pattern=PROFILE_NAME_PATTERN,
        description="Which lesson store to read and write; `jev learning list` shows them.",
    )
    max_lessons: int = Field(default=8, ge=0, description="Most lessons added to a single prompt.")
    max_chars: int = Field(
        default=2000, ge=0, description="Most characters of lesson text added to a single prompt."
    )


FREE_MODELS_TOKEN = "free"  # in a role's `models`: the cached free models (docs/config.md)


class ModelsConfig(_Strict):
    """`[models]`: free OpenRouter models (docs/config.md "Free models"). They only apply to
    roles that can use the `api` worker; Claude Code and Codex never get a free model."""

    prefer_free: bool = Field(
        default=False,
        description="Offer free OpenRouter models first for roles that can use the API worker.",
    )
    free_models_limit: int = Field(
        default=3, ge=0, description='How many cached free models the "free" entry expands to.'
    )
    cache_ttl_hours: float = Field(
        default=24.0,
        gt=0,
        description=("Refetch the free-model list when the cache is older than this many hours."),
    )


class ReviewConfig(_Strict):
    # Review each task's diff after its tests pass. Unset: on under the consensus dispatcher,
    # so a task is done only when both votes accept a reviewed attempt, and off otherwise
    # (issue #67).
    enabled: bool | None = Field(
        default=None,
        description=(
            "Have the reviewer check each finished task's diff after its tests pass. Unset: on "
            "with consensus, off otherwise."
        ),
    )


_TOOL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class QualityConfig(_Strict):
    """`[quality]`: the optional quality and security gate (docs/quality.md). Off by default
    ("none"): each level adds checks and, from `basic`, one AI code review per gate."""

    level: QualityLevel = Field(
        default="none",
        description=(
            "How strict the quality and security gate is: none, basic, medium, production or "
            "scalable (each level adds checks)."
        ),
    )
    # Full test suite for the gate; unset: `[integration] test_command`, else auto-detected.
    test_command: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Full test suite the gate runs. Unset: [integration] test_command, else auto-detected."
        ),
    )
    # Minimum line coverage in percent; unset: 60 at medium, 80 at production and scalable.
    min_coverage: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Minimum line coverage in percent the gate demands. Unset: 60 at medium, 80 at "
            "production and scalable."
        ),
    )
    # Tools that must be installed: a missing one fails its check instead of skipping it.
    required_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tools that must be installed (e.g. ruff, gitleaks); a missing one fails its check "
            "instead of skipping it."
        ),
    )
    timeout_s: int = Field(default=600, gt=0, description="Seconds each gate tool command may run.")
    # The score the gate's code review must award (0-100) for the work to pass. A whole
    # number so the dashboard can show it as a slider; below it the result goes back
    # for rework instead of shipping.
    score_threshold: int = Field(
        default=85,
        ge=0,
        le=100,
        description=(
            "Minimum score out of 100 the quality gate's review must give the work to pass; below "
            "it the result goes back for rework instead of shipping."
        ),
    )
    # Rework rounds the ship score gate allows before the run stops for a human.
    score_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "How many times a result below the score threshold goes back for rework before the "
            "run stops for a human; the run never ships below the threshold."
        ),
    )
    # Skip the ship score gate for human-authored code. The user's call: scoring is
    # optional for human contributions, but AI-generated code is always scored.
    score_skip_human: bool = Field(
        default=False,
        description=(
            "Skip the ship score gate for human-authored code; the score is still recorded. "
            "AI-generated code is always gated."
        ),
    )

    @model_validator(mode="after")
    def _check_tools(self) -> QualityConfig:
        for tool in self.required_tools:
            if not re.match(_TOOL_NAME_PATTERN, tool):
                raise ValueError(f"required_tools: {tool!r} is not a tool name")
        return self


_BRANCH_PREFIX = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
_REMOTE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


# What a run does once both votes approve at D6 (`[ship] after_approval`, issue #68).
ShipMode = Literal["merge", "ask"]
MergeMethod = Literal["squash", "merge", "rebase"]


class ShipConfig(_Strict):
    """`[ship]`: push a run's result branch and open a GitHub pull request with `gh`
    (docs/ship.md). Off by default; it never force-pushes. With shipping on, a D6 "ship" vote
    ships the run: `after_approval = "merge"` opens the PR ready, waits for its checks and
    merges it; "ask" waits for a person to approve, modify or stop first."""

    enabled: bool = Field(
        default=False,
        description=(
            "Push the finished run's branch and open a GitHub pull request with your gh login. "
            "Never force-pushes."
        ),
    )
    remote: str = Field(
        default="origin",
        pattern=_REMOTE_NAME,
        description="Which git remote the result branch is pushed to.",
    )
    # Base branch of the PR; empty: the repo's default branch (from the remote's HEAD or
    # `gh repo view`), else the branch checked out in the repo.
    base: str = Field(
        default="",
        description="Base branch of the pull request; empty means the repo's default branch.",
    )
    branch_prefix: str = Field(
        default="jev/",
        pattern=_BRANCH_PREFIX,
        description="Pushed branch name is <branch_prefix><run_id>.",
    )
    draft: bool = Field(default=True, description="Open pull requests as drafts.")
    labels: list[str] = Field(
        default_factory=list,
        description="Labels added to the pull request (they must already exist in the repo).",
    )
    require_quality_pass: bool = Field(
        default=True,
        description="Refuse to ship runs whose latest quality gate blocked.",
    )
    # After both votes approve at D6; unset: "merge" under `primary = "consensus"`, else "ask".
    after_approval: ShipMode | None = Field(
        default=None,
        description=(
            'What happens when both votes approve: "merge" opens the PR ready and merges after '
            'checks, "ask" waits for you first. Unset: merge with consensus, ask otherwise.'
        ),
    )
    merge_method: MergeMethod = Field(
        default="squash", description="How the pull request is merged: squash, merge or rebase."
    )
    # A PR with no checks after `checks_grace_s`: stop for a person (true) or merge (false).
    require_checks: bool = Field(
        default=True,
        description="A pull request with no checks stops for a human instead of merging.",
    )
    checks_grace_s: int = Field(
        default=120, ge=0, description="Seconds to wait for checks to appear on the pull request."
    )
    checks_timeout_s: int = Field(
        default=3600, gt=0, description="Seconds to wait for the pull request's checks to finish."
    )
    checks_poll_s: int = Field(default=30, gt=0, description="Seconds between checks polls.")

    @model_validator(mode="after")
    def _check(self) -> ShipConfig:
        if self.base and not re.match(_BRANCH_PREFIX, self.base):
            raise ValueError(f"base: {self.base!r} is not a branch name")
        if ".." in self.branch_prefix or ".." in self.base:
            raise ValueError("branch names cannot contain `..`")
        for label in self.labels:
            if not label.strip() or label.startswith("-"):
                raise ValueError(f"labels: {label!r} is not a label name")
        return self


# Claude Code `--permission-mode` values (docs/verification.md section 2), minus
# `bypassPermissions`: that one is only reachable through the explicit `skip_permissions`.
ClaudePermissionMode = Literal["acceptEdits", "auto", "default", "dontAsk", "plan"]
ClaudeReadOnlyPermissionMode = Literal["dontAsk", "plan", "default"]
# Codex `--sandbox` values (docs/verification.md section 3).
CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]


class WorkerConfig(_Strict):
    """Settings shared by the workers that run a local CLI (Claude Code, Codex)."""

    enabled: bool = Field(default=True, description="Whether this worker may be given work.")
    command: str = Field(
        min_length=1, description="Executable to run (a name on PATH or a full path)."
    )
    timeout_s: int = Field(default=1200, gt=0, description="Seconds one worker call may run.")
    # Appended to every invocation, e.g. ["--max-turns", "40"].
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            'Extra flags appended to every call of this worker, e.g. ["--max-turns", "40"].'
        ),
    )


class ClaudeCodeConfig(WorkerConfig):
    command: str = Field(
        default="claude",
        min_length=1,
        description="Executable to run (a name on PATH or a full path).",
    )
    permission_mode: ClaudePermissionMode = Field(
        default="acceptEdits",
        description="Claude Code permission mode for the coder, working in its own worktree.",
    )
    read_only_permission_mode: ClaudeReadOnlyPermissionMode = Field(
        default="dontAsk",
        description="Permission mode for read-only roles (planner, validator, reviewer).",
    )
    # Coder tools; unset keeps the built-in list (read/edit tools, `git status`/`git diff`).
    allowed_tools: list[str] | None = Field(
        default=None,
        description=(
            "Tools the coder may use; unset keeps the built-in list (read/edit tools, git "
            "status/diff)."
        ),
    )
    # Claude Code's own per-attempt cap (`--max-budget-usd`); unset means budget.per_task_usd.
    max_budget_usd: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Claude Code's own spending cap per attempt, in dollars; unset means "
            "budget.per_task_usd."
        ),
    )
    bare: bool = Field(
        default=False, description="Run Claude Code in --bare mode (needs ANTHROPIC_API_KEY)."
    )
    # DANGEROUS: the coder runs with `--dangerously-skip-permissions` (any command, your
    # privileges). Only in a throwaway sandbox without secrets or network.
    skip_permissions: bool = Field(
        default=False,
        description=(
            "DANGEROUS: the coder runs with --dangerously-skip-permissions (any command, your "
            "privileges). Only in a throwaway sandbox."
        ),
    )


class CodexConfig(WorkerConfig):
    command: str = Field(
        default="codex",
        min_length=1,
        description="Executable to run (a name on PATH or a full path).",
    )
    sandbox: CodexSandbox = Field(
        default="workspace-write",
        description=(
            "How much of the machine the coder may touch: read-only, workspace-write, or "
            "danger-full-access."
        ),
    )


class ApiWorkerConfig(_Strict):
    """A worker that calls an OpenAI-compatible HTTP API. The key comes from `api_key_env`."""

    enabled: bool = Field(default=True, description="Whether this worker may be given work.")
    base_url: str = Field(
        default=OPENROUTER_BASE_URL,
        min_length=1,
        description="OpenAI-compatible API endpoint the worker calls.",
    )
    api_key_env: str = Field(
        default="OPENROUTER_API_KEY",
        pattern=_ENV_NAME,
        description="Name of the environment variable holding the API key (never the key itself).",
    )
    # Model used when the role's `models` pool is empty; unset means such roles fail on api.
    default_model: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Model used when a role lists no models; unset means such roles cannot use this worker."
        ),
    )
    timeout_s: int = Field(default=600, gt=0, description="Seconds one API call may take.")


class WorkersConfig(_Strict):
    claude_code: ClaudeCodeConfig = Field(
        default_factory=ClaudeCodeConfig, description="The Claude Code CLI worker."
    )
    codex: CodexConfig = Field(default_factory=CodexConfig, description="The Codex CLI worker.")
    api: ApiWorkerConfig = Field(
        default_factory=ApiWorkerConfig, description="The OpenRouter API worker."
    )

    def names(self) -> list[str]:
        return list(type(self).model_fields)

    def is_enabled(self, name: str) -> bool:
        worker: WorkerConfig | ApiWorkerConfig = getattr(self, name)
        return worker.enabled


# --- agent tools: MCP servers, skills, plugins (docs/config.md "Agent tools") ---------------

_TOOL_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
# Credentials in a URL's query string; the other literal credentials that must never sit in a
# config file (args, URLs) are the shapes every log redacts (`runlog.redact`).
_SECRET_QUERY = re.compile(r"[?&](?:api_?key|key|token|access_token|secret|password)=", re.I)


def _literal_secret(value: str) -> bool:
    return bool(_SECRET_QUERY.search(value)) or any(p.search(value) for p in SECRET_PATTERNS)


McpTransport = Literal["http", "sse"]


class McpServerConfig(_Strict):
    """`[tools.mcp.<name>]`: one MCP server, either local (`command`) or remote (`url`).

    Secrets never go here: `env_from` and `headers_from_env` name environment variables, and
    jev reads their values when it starts the agent.
    """

    command: str | None = Field(
        default=None,
        min_length=1,
        description="Local server: the executable to start (set exactly one of command or url).",
    )
    args: list[str] = Field(
        default_factory=list, description="Arguments for the local server command."
    )
    env_from: list[str] = Field(
        default_factory=list,
        description=(
            "Names of environment variables passed to the local server (names only, never values)."
        ),
    )
    url: str | None = Field(
        default=None,
        pattern=r"^https?://",
        description="Remote server: its https URL (set exactly one of command or url).",
    )
    transport: McpTransport = Field(default="http", description="Remote servers only: http or sse.")
    bearer_token_env: str | None = Field(
        default=None,
        pattern=_ENV_NAME,
        description="Remote servers only: name of the env var holding the bearer token.",
    )
    headers_from_env: dict[str, str] = Field(
        default_factory=dict,
        description="Remote servers only: HTTP header -> env var name holding its value.",
    )

    @model_validator(mode="before")
    @classmethod
    def _no_literal_values(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, use in (("env", "env_from"), ("headers", "headers_from_env")):
                if key in data:
                    raise ValueError(
                        f"`{key}` is not allowed: values in config files could leak secrets; "
                        f"name environment variables with `{use}` instead"
                    )
        return data

    @model_validator(mode="after")
    def _check(self) -> McpServerConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of `command` (local server) or `url` (remote)")
        if self.command is not None and (self.bearer_token_env or self.headers_from_env):
            raise ValueError("`bearer_token_env` and `headers_from_env` need a remote `url`")
        if self.url is None and "transport" in self.model_fields_set:
            raise ValueError("`transport` only applies to a remote `url`")
        if self.url is not None and (self.args or self.env_from):
            raise ValueError("`args` and `env_from` only apply to a local `command`")
        for name in [*self.env_from, *self.headers_from_env.values()]:
            if not re.match(_ENV_NAME, name):
                raise ValueError(f"{name!r} is not an environment variable name")
        for value in [*(self.args), self.url or "", self.command or ""]:
            if _literal_secret(value):
                raise ValueError(
                    "looks like a literal credential; pass it through an environment variable "
                    "(`env_from`, `bearer_token_env` or `headers_from_env`)"
                )
        return self

    def env_names(self) -> list[str]:
        """Every environment variable this server reads its secrets from."""
        names = [*self.env_from, *self.headers_from_env.values()]
        if self.bearer_token_env:
            names.append(self.bearer_token_env)
        return list(dict.fromkeys(names))


class PluginConfig(_Strict):
    """`[tools.plugins.<name>]`: a Claude Code plugin directory loaded with `--plugin-dir`."""

    path: Path = Field(description="Folder holding the Claude Code plugin.")
    # Permission rules the plugin's tools need in headless runs, e.g. "mcp__plugin_x_db__*".
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Permission patterns the plugin's tools need in headless runs, e.g. "
            '"mcp__plugin_x_db__*".'
        ),
    )


class ToolsConfig(_Strict):
    """`[tools]`: tools defined once and given to roles by name (`[roles.<role>] mcp = [...]`)."""

    mcp: dict[str, McpServerConfig] = Field(
        default_factory=dict, description="MCP servers, by name; roles pick them by name."
    )
    skills: dict[str, Path] = Field(
        default_factory=dict, description="Skills, by name; each names a folder with a SKILL.md."
    )
    plugins: dict[str, PluginConfig] = Field(
        default_factory=dict, description="Claude Code plugins, by name."
    )

    @model_validator(mode="after")
    def _check_names(self) -> ToolsConfig:
        for kind, names in (("mcp", self.mcp), ("skills", self.skills), ("plugins", self.plugins)):
            for name in names:
                if not re.match(_TOOL_NAME, name):
                    raise ValueError(
                        f"tools.{kind}: {name!r} is not a valid name "
                        "(letters, digits, `-` and `_`, starting with a letter or digit)"
                    )
        return self


# Control characters forbidden in `RoleConfig.prompt_addendum`: everything below
# U+0020 and U+007F except the whitespace a multi-line instruction block needs.
_ADDENDUM_FORBIDDEN = frozenset(chr(code) for code in (*range(0x00, 0x20), 0x7F)) - frozenset(
    "\t\n\r"
)


class RoleConfig(_Strict):
    workers: list[str] = Field(
        default_factory=list,
        description="Which workers may do this job (claude_code, codex, api).",
    )
    models: list[str] = Field(
        default_factory=list,
        description=(
            'Which models the dispatcher may pick; empty means "let the worker use its default".'
        ),
    )
    selection: Selection = Field(
        default="dispatcher",
        description='"dispatcher" lets jev choose worker/model/effort; "fixed" skips that choice.',
    )
    # Overrides `[models] prefer_free` for this role; unset follows it.
    prefer_free: bool | None = Field(
        default=None,
        description="Offer free models first for this role; unset follows [models] prefer_free.",
    )
    efforts: list[str] = Field(
        default_factory=lambda: ["low", "medium", "high"],
        description="Effort levels the dispatcher may pick for this role.",
    )
    prefer_different_vendor_than: str | None = Field(
        default=None,
        description="Prefer a different vendor than the named role's, for cross-checking.",
    )
    # Your own Jinja2 prompt template; relative paths are resolved against the repo root.
    prompt_file: Path | None = Field(
        default=None,
        description="Your own Jinja2 prompt template for this role, replacing the built-in one.",
    )
    # Extra instructions appended to the rendered prompt, after the template (built-in or
    # prompt_file). Blank means none; kept in plain text so the dashboard can edit it.
    prompt_addendum: str = Field(
        default="",
        max_length=4000,
        description=(
            "Extra instructions appended to this role's prompt, after the prompt template "
            "is rendered. Blank means none."
        ),
    )

    @field_validator("prompt_addendum")
    @classmethod
    def _check_addendum_text(cls, value: str) -> str:
        # TOML and terminals have no use for control characters; tabs and newlines are
        # the legitimate formatting of a multi-line instruction block.
        bad = sorted({char for char in value if char in _ADDENDUM_FORBIDDEN}, key=ord)
        if bad:
            shown = ", ".join(f"U+{ord(char):04X}" for char in bad)
            raise ValueError(f"prompt_addendum must not contain control characters ({shown})")
        return value

    # Agent tools for this role, by name from `[tools]`.
    mcp: list[str] = Field(
        default_factory=list, description="MCP servers this role may use, by name from [tools.mcp]."
    )
    skills: list[str] = Field(
        default_factory=list, description="Skills this role gets, by name from [tools.skills]."
    )
    plugins: list[str] = Field(
        default_factory=list,
        description="Claude Code plugins this role gets, by name from [tools.plugins].",
    )
    # Servers and the browser for this role (docs/servers.md); read-only roles get basic access.
    servers: list[str] = Field(
        default_factory=list,
        description=(
            "Servers this role may reach, by name from [servers.<name>]; read-only roles are "
            "capped at basic access."
        ),
    )
    browser: bool = Field(
        default=False,
        description="Give this role the virtual browser (needs [browser] enabled).",
    )


# Per-role defaults. A partial `[roles.<role>]` table is layered on top of these, so setting
# only `models` keeps the role's default `workers`.
_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "planner": {"workers": ["claude_code", "api"]},
    "validator": {"workers": ["codex", "api"], "prefer_different_vendor_than": "planner"},
    "coder": {"workers": ["claude_code", "codex"]},
    "reviewer": {"workers": ["api", "codex"]},
    "code_reviewer": {"workers": ["api", "codex", "claude_code"]},
    "learner": {"workers": ["claude_code", "api", "codex"]},
    "designer": {"workers": ["api", "claude_code"]},
}


class RolesConfig(_Strict):
    planner: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["planner"]),
        description="Turns the idea into a plan.",
    )
    validator: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["validator"]),
        description="Checks the plan before it is approved.",
    )
    coder: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["coder"]),
        description="Writes the code for each task.",
    )
    reviewer: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["reviewer"]),
        description="Reviews each task's diff after its tests pass.",
    )
    code_reviewer: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["code_reviewer"]),
        description="The AI code review of the quality gate.",
    )
    learner: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["learner"]),
        description="Distills lessons at the end of a run, when learning is on.",
    )
    designer: RoleConfig = Field(
        default_factory=lambda: RoleConfig(**_ROLE_DEFAULTS["designer"]),
        description="Designs the UI before it is approved.",
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_role_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            name: {**_ROLE_DEFAULTS[name], **value}
            if name in _ROLE_DEFAULTS and isinstance(value, dict)
            else value
            for name, value in data.items()
        }

    def items(self) -> list[tuple[str, RoleConfig]]:
        return [(name, getattr(self, name)) for name in type(self).model_fields]


def role_prefers_free(config: JevConfig, role: str) -> bool:
    """`[roles.<role>] prefer_free` if set, else `[models] prefer_free`."""
    override: bool | None = getattr(config.roles, role).prefer_free
    return config.models.prefer_free if override is None else override


def role_uses_free(config: JevConfig, role: str) -> bool:
    """The role asks for free models: it prefers them or lists the `"free"` token."""
    return (
        role_prefers_free(config, role) or FREE_MODELS_TOKEN in getattr(config.roles, role).models
    )


def role_can_use_free(config: JevConfig, role: str) -> bool:
    """Free models run only on the api worker (OpenRouter); the role must allow it."""
    return "api" in getattr(config.roles, role).workers and config.workers.api.enabled


class TrustConfig(_Strict):
    """`[trust]` (user config only): repositories whose jev.toml may change the guarded keys
    (`config.loader.GUARDED_KEYS`): absolute paths, or `"*"` for every repository."""

    repos: list[str] = Field(
        default_factory=list,
        description=(
            'Repository paths whose jev.toml may change guarded keys; "*" trusts every repository.'
        ),
    )


# The eleven settings the dashboard shows first: plain-language, everyday knobs a beginner
# can understand and safely change. Every other field stays available under the "Advanced"
# toggle. Dotted paths into `JevConfig`; the settings page renders these in its beginner view.
BEGINNER_FIELDS: tuple[str, ...] = (
    "budget.per_run_usd",
    "budget.per_task_usd",
    "budget.max_task_attempts",
    "quality.level",
    "quality.score_threshold",
    "approval.require_plan_approval",
    "review.enabled",
    "learning.enabled",
    "ship.enabled",
    "models.prefer_free",
    "autonomy.mode",
)


class JevConfig(_Strict):
    dispatcher: DispatcherConfig = Field(
        default_factory=DispatcherConfig, description="Who makes the decisions."
    )
    budget: BudgetConfig = Field(
        default_factory=BudgetConfig, description="Spending caps and attempt limits."
    )
    approval: ApprovalConfig = Field(
        default_factory=ApprovalConfig, description="Whether a human gates the plan."
    )
    autonomy: AutonomyConfig = Field(
        default_factory=AutonomyConfig,
        description="How much jev asks you, and what its agents may touch.",
    )
    integration: IntegrationConfig = Field(
        default_factory=IntegrationConfig,
        description=("Merging task branches and testing the result."),
    )
    workspace: WorkspaceConfig = Field(
        default_factory=WorkspaceConfig, description="The git worktrees task work happens in."
    )
    review: ReviewConfig = Field(
        default_factory=ReviewConfig, description="Whether the reviewer checks each task's diff."
    )
    quality: QualityConfig = Field(
        default_factory=QualityConfig, description="The optional quality and security gate."
    )
    learning: LearningConfig = Field(
        default_factory=LearningConfig, description="Learning lessons from past runs."
    )
    ship: ShipConfig = Field(
        default_factory=ShipConfig, description="Opening a GitHub pull request for a finished run."
    )
    models: ModelsConfig = Field(
        default_factory=ModelsConfig, description="Free OpenRouter models."
    )
    workers: WorkersConfig = Field(
        default_factory=WorkersConfig, description="The workers that run the agents."
    )
    roles: RolesConfig = Field(
        default_factory=RolesConfig, description="Who does each job: planner, coder, reviewer, ..."
    )
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig, description="MCP servers, skills and plugins given to roles."
    )
    # Servers, browser and workspace profiles (docs/servers.md).
    servers: dict[str, ServerConfig] = Field(
        default_factory=dict, description="SSH servers agents may reach, by name."
    )
    browser: BrowserConfig = Field(
        default_factory=BrowserConfig, description="The virtual browser for roles."
    )
    profiles: dict[str, WorkspaceProfileConfig] = Field(
        default_factory=dict, description="Workspace profiles picked per run."
    )
    trust: TrustConfig = Field(
        default_factory=TrustConfig, description="Repos allowed to change guarded keys."
    )

    @property
    def plan_approval_required(self) -> bool:
        """Whether a validated plan waits for a human (`[approval] require_plan_approval`)."""
        required = self.approval.require_plan_approval
        return self.dispatcher.primary != "consensus" if required is None else required

    @property
    def review_enabled(self) -> bool:
        """Whether the reviewer checks each task's diff (`[review] enabled`)."""
        enabled = self.review.enabled
        return self.dispatcher.primary == "consensus" if enabled is None else enabled

    @model_validator(mode="before")
    @classmethod
    def _reject_secrets(cls, data: Any) -> Any:
        secrets = find_secret_keys(data)
        if secrets:
            raise ValueError(secret_keys_message(secrets))
        return data

    @model_validator(mode="after")
    def _check_role_references(self) -> JevConfig:
        known_workers = self.workers.names()
        role_names = [name for name, _ in self.roles.items()]
        for name, role in self.roles.items():
            for worker in role.workers:
                if worker not in known_workers:
                    raise ValueError(
                        f"roles.{name}.workers: unknown worker {worker!r} "
                        f"(known: {', '.join(known_workers)})"
                    )
            for kind in ("mcp", "skills", "plugins"):
                defined: dict[str, Any] = getattr(self.tools, kind)
                for ref in getattr(role, kind):
                    if ref not in defined:
                        known = ", ".join(defined) or "none defined"
                        raise ValueError(
                            f"roles.{name}.{kind}: unknown {kind} entry {ref!r}: define it in "
                            f"[tools.{kind}] (known: {known})"
                        )
            accepted = frozenset[str]().union(
                *(WORKER_EFFORTS.get(w, frozenset()) for w in role.workers)
            )
            # A role without workers cannot run (jev doctor reports it); nothing to check.
            for effort in role.efforts if role.workers else []:
                if effort not in accepted:
                    levels = "; ".join(
                        f"{w} accepts: {_effort_list(WORKER_EFFORTS[w])}"
                        for w in role.workers
                        if w in WORKER_EFFORTS
                    )
                    raise ValueError(
                        f"roles.{name}.efforts: {effort!r} is not accepted by any of the role's "
                        f"workers ({', '.join(role.workers) or 'none'}): {levels or 'none'}"
                    )
            other = role.prefer_different_vendor_than
            if other is not None and (other == name or other not in role_names):
                raise ValueError(
                    f"roles.{name}.prefer_different_vendor_than: must name another role "
                    f"(one of: {', '.join(r for r in role_names if r != name)})"
                )
        return self

    @model_validator(mode="after")
    def _check_servers(self) -> JevConfig:
        reserved = {"jev_servers", "jev_browser"} & set(self.tools.mcp)
        if reserved:
            raise ValueError(f"tools.mcp: {', '.join(sorted(reserved))} is reserved for jev")
        check_servers_config(
            self.servers,
            self.browser,
            self.profiles,
            {name: role.servers for name, role in self.roles.items()},
            PROFILE_NAME_PATTERN,
        )
        return self
