"""The Settings page's editor for the repository's `jev.toml` (issue #9, docs/dashboard.md).

A curated set of common settings can be changed from the browser. None of them is guarded
(`config.loader.GUARDED_KEYS`) or under `[trust]`, and only the repo's `jev.toml` is ever
written, never the user config. The flow keeps decisions apart from side effects:

1. `plan_edit` (reads the files, writes nothing) turns the posted fields into the candidate
   `jev.toml` text with tomlkit, so comments and layout survive, and checks it exactly as
   `load_config` would (schema, secrets, `[trust]`, guarded keys in an untrusted repo).
2. The page shows the unified diff. The confirm form carries the same field values and the
   hash of the file the diff was made from.
3. `write_config` replaces the file atomically, and only if its hash still matches.

The browser never sends TOML: every step rebuilds the candidate on the server from the
whitelisted `FIELDS`, so no request can write any other key.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import os
import stat
import tempfile
import threading
import tomllib
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, get_args

import tomlkit
from pydantic import ValidationError

from jev_agent.config import CONFIG_FILENAME, ConfigError, JevConfig, deep_merge
from jev_agent.config.loader import GUARDED_KEYS, config_from_layers, default_user_config_path
from jev_agent.config.schema import DispatcherName, RolesConfig, ShipMode, WorkersConfig
from jev_agent.core.learning import LearningStatus
from jev_agent.quality.levels import LEVELS

FieldKind = Literal["toggle", "choice", "number", "integer", "text", "names"]
Source = Literal["default", "user config", "jev.toml"]
RESET_FIELD = "reset"  # form field: keys to remove from jev.toml (back to user config/default)
NO_FILE_HASH = "no-file"


@dataclass(frozen=True)
class FieldSpec:
    """One editable setting: its dotted key and how the form shows and parses it."""

    key: str
    label: str
    kind: FieldKind
    help: str
    choices: tuple[str, ...] = ()
    unit: str = ""
    # Non-empty: the key may be unset; the form offers "auto: <this rule>", which removes it.
    auto: str = ""

    @property
    def parts(self) -> list[str]:
        return self.key.split(".")

    @property
    def options(self) -> list[tuple[str, str]]:
        """(value, text) for a select: on/off for a toggle, else the choices."""
        if self.kind == "toggle":
            return [("true", "on"), ("false", "off")]
        return [(choice, choice) for choice in self.choices]


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    fields: tuple[FieldSpec, ...]


WORKER_NAMES: tuple[str, ...] = tuple(WorkersConfig.model_fields)
ROLE_NAMES: tuple[str, ...] = tuple(RolesConfig.model_fields)


def _role_fields(role: str) -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            f"roles.{role}.workers",
            f"{role}: workers",
            "names",
            f"Comma-separated, in order of preference ({', '.join(WORKER_NAMES)}).",
        ),
        FieldSpec(
            f"roles.{role}.models",
            f"{role}: models",
            "names",
            'Comma-separated, weakest to strongest; empty: the worker\'s default model; "free": '
            "cached free models.",
        ),
    )


SECTIONS: tuple[Section, ...] = (
    Section(
        "dispatcher",
        "Dispatcher",
        (
            FieldSpec(
                "dispatcher.primary",
                "Primary dispatcher",
                "choice",
                "Who decides at each decision point; the rules dispatcher always backs up Jev.",
                choices=get_args(DispatcherName),
            ),
        ),
    ),
    Section(
        "budget",
        "Budgets",
        (
            FieldSpec(
                "budget.per_run_usd", "Per run", "number", "Spend cap for one run.", unit="USD"
            ),
            FieldSpec(
                "budget.per_task_usd",
                "Per task",
                "number",
                "Spend cap for one task; at most the per-run cap.",
                unit="USD",
            ),
            FieldSpec(
                "budget.max_task_attempts",
                "Attempts per task",
                "integer",
                "Attempts before a task needs a human.",
                unit="attempts",
            ),
        ),
    ),
    Section(
        "approval",
        "Approval and review",
        (
            FieldSpec(
                "approval.require_plan_approval",
                "Wait for plan approval",
                "toggle",
                "Stop for a human after the plan is validated.",
                auto="off under consensus, else on",
            ),
            FieldSpec(
                "review.enabled",
                "Review each task",
                "toggle",
                "Review each task's diff after its tests pass.",
                auto="on under consensus, else off",
            ),
        ),
    ),
    Section(
        "quality",
        "Quality gate",
        (
            FieldSpec(
                "quality.level",
                "Level",
                "choice",
                "Each level adds checks; from basic on, one AI code review per gate.",
                choices=LEVELS,
            ),
        ),
    ),
    Section(
        "ship",
        "Ship",
        (
            FieldSpec(
                "ship.enabled",
                "Shipping",
                "toggle",
                "Push a run's result branch and open a GitHub pull request with gh.",
            ),
            FieldSpec(
                "ship.after_approval",
                "After both votes approve",
                "choice",
                '"merge" opens the PR ready and merges it once checks pass; "ask" waits for a '
                "person. Needs shipping on.",
                choices=get_args(ShipMode),
                auto="merge under consensus, else ask",
            ),
        ),
    ),
    Section(
        "learning",
        "Learning",
        (
            FieldSpec(
                "learning.enabled",
                "Learning",
                "toggle",
                "Each run ends with one extra model call that distills lessons.",
            ),
            FieldSpec(
                "learning.profile",
                "Profile",
                "text",
                "Lesson profile runs use: letters, digits, - and _.",
            ),
        ),
    ),
    Section(
        "models",
        "Free models",
        (
            FieldSpec(
                "models.prefer_free",
                "Prefer free models",
                "toggle",
                "Put free OpenRouter models first for roles that can use the api worker.",
            ),
        ),
    ),
    Section("roles", "Roles", tuple(f for role in ROLE_NAMES for f in _role_fields(role))),
)

FIELDS: dict[str, FieldSpec] = {spec.key: spec for s in SECTIONS for spec in s.fields}


def is_protected(key: str) -> bool:
    """A key the browser must never write: `[trust]`, a guarded key or anything under one."""
    parts = key.split(".")
    if parts[0] == "trust":
        return True
    for pattern in GUARDED_KEYS:
        guarded = pattern.split(".")
        if len(parts) >= len(guarded) and all(
            g in ("*", p) for g, p in zip(guarded, parts, strict=False)
        ):
            return True
    return False


def editable(key: str) -> bool:
    return key in FIELDS and not is_protected(key)


# ---- the files on disk -----------------------------------------------------------------------


@dataclass(frozen=True)
class Layers:
    """The user config (parsed) and the repo's jev.toml (as text), as they are on disk now."""

    root: Path
    user_path: Path
    user: dict[str, Any] | None
    repo_text: str | None  # None: no jev.toml

    @property
    def repo_file(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def repo_hash(self) -> str:
        return file_hash(self.repo_text)

    @cached_property
    def repo_data(self) -> dict[str, Any] | None:
        return None if self.repo_text is None else _parse(self.repo_file, self.repo_text)

    def config(self) -> JevConfig:
        """The effective config, exactly as `load_config` builds it."""
        return config_from_layers(self.root, self.user_path, self.user, self.repo_data)


def file_hash(text: str | None) -> str:
    return NO_FILE_HASH if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc


def _parse(path: Path, text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def read_layers(root: Path) -> Layers:
    user_path = default_user_config_path()
    user_text = _read_text(user_path)
    user = None if user_text is None else _parse(user_path, user_text)
    return Layers(root, user_path, user, _read_text(root / CONFIG_FILENAME))


def _lookup(data: dict[str, Any] | None, parts: list[str]) -> tuple[bool, Any]:
    node: Any = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def source_of(layers: Layers, key: str) -> Source:
    """Which layer the effective value of `key` comes from."""
    parts = key.split(".")
    if _lookup(layers.repo_data, parts)[0]:
        return "jev.toml"
    if _lookup(layers.user, parts)[0]:
        return "user config"
    return "default"


# ---- values: config <-> form text ------------------------------------------------------------


class FieldError(ValueError):
    """A typed value that cannot be parsed for its field. The message is user-facing."""


def current_value(config: JevConfig, spec: FieldSpec) -> Any:
    value: Any = config
    for part in spec.parts:
        value = getattr(value, part)
    return value


def format_value(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return ""
    if spec.kind == "toggle":
        return "true" if value else "false"
    if spec.kind == "number":
        return f"{value:g}"
    if spec.kind == "names":
        return ", ".join(value)
    return str(value)


def parse_value(spec: FieldSpec, text: str) -> Any:
    """The form text as a TOML value; None means "auto" (remove the key)."""
    text = text.strip()
    if spec.auto and text == "":
        return None
    if spec.kind == "toggle":
        if text not in ("true", "false"):
            raise FieldError("choose on or off")
        return text == "true"
    if spec.kind == "choice":
        if text not in spec.choices:
            raise FieldError(f"choose one of: {', '.join(spec.choices)}")
        return text
    if spec.kind == "number":
        try:
            number = float(text)
        except ValueError:
            raise FieldError("must be a number") from None
        if not math.isfinite(number):
            raise FieldError("must be a finite number")
        return number
    if spec.kind == "integer":
        try:
            return int(text)
        except ValueError:
            raise FieldError("must be a whole number") from None
    if spec.kind == "names":
        return [name.strip() for name in text.split(",") if name.strip()]
    return text


# ---- the edit plan (no side effects) ---------------------------------------------------------


@dataclass(frozen=True)
class Submission:
    """What the form posted: text per known field (absent: unchanged) and keys to reset.
    Names outside `FIELDS` are dropped here, whatever the request contains."""

    values: dict[str, str] = field(default_factory=dict)
    resets: frozenset[str] = frozenset()

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, Any]]) -> Submission:
        values: dict[str, str] = {}
        resets: set[str] = set()
        for name, value in pairs:
            if not isinstance(value, str):
                continue
            if name == RESET_FIELD and editable(value):
                resets.add(value)
            elif editable(name):
                # A toggle posts a hidden "false" and then, when ticked, "true": the last wins.
                values[name] = value
        return cls(values, frozenset(resets))


@dataclass(frozen=True)
class EditPlan:
    """The candidate jev.toml for a submission, and whether it may be written."""

    submission: Submission
    base_hash: str  # hash of the jev.toml the candidate was built from
    old_text: str
    new_text: str
    set_keys: tuple[str, ...]  # keys given a new value
    removed_keys: tuple[str, ...]  # keys removed from jev.toml
    field_errors: dict[str, str]
    errors: tuple[str, ...]  # errors about the whole file (from the loader)

    @property
    def ok(self) -> bool:
        return not self.field_errors and not self.errors

    @property
    def has_changes(self) -> bool:
        return self.new_text != self.old_text

    def diff(self) -> str:
        return "\n".join(
            difflib.unified_diff(
                self.old_text.splitlines(),
                self.new_text.splitlines(),
                fromfile=f"{CONFIG_FILENAME} (now)",
                tofile=f"{CONFIG_FILENAME} (after saving)",
                lineterm="",
            )
        )

    def diff_lines(self) -> list[tuple[str, str]]:
        """The diff as (kind, line) rows, kind one of "file", "hunk", "add", "del", "same"."""
        rows: list[tuple[str, str]] = []
        for line in self.diff().splitlines():
            if line.startswith(("+++", "---")):
                kind = "file"
            elif line.startswith("@@"):
                kind = "hunk"
            else:
                kind = {"+": "add", "-": "del"}.get(line[:1], "same")
            rows.append((kind, line))
        return rows

    def carried(self) -> list[tuple[str, str]]:
        """Hidden form fields that make the confirm POST rebuild exactly this candidate."""
        values = self.submission.values
        return [(key, values[key]) for key in self.set_keys] + [
            (RESET_FIELD, key) for key in self.removed_keys
        ]

    def summary(self) -> str:
        parts = [f"set {', '.join(self.set_keys)}"] if self.set_keys else []
        if self.removed_keys:
            parts.append(f"reset {', '.join(self.removed_keys)}")
        return "; ".join(parts)


def plan_edit(layers: Layers, submission: Submission) -> EditPlan:
    """Build and check the candidate jev.toml for `submission`. Writes nothing.

    A field is written only when its value differs from the effective one, so saving the page
    unchanged changes nothing. Raises `ConfigError` when the files on disk do not load."""
    current = layers.config()
    old_text = layers.repo_text or ""
    old_data = layers.repo_data or {}
    doc = tomlkit.parse(old_text)
    set_keys: list[str] = []
    removed: list[str] = []
    field_errors: dict[str, str] = {}
    for spec in FIELDS.values():
        in_repo = _lookup(old_data, spec.parts)[0]
        if spec.key in submission.resets:
            if in_repo:
                _remove(doc, spec.parts)
                removed.append(spec.key)
            continue
        text = submission.values.get(spec.key)
        if text is None:
            continue
        try:
            value = parse_value(spec, text)
        except FieldError as exc:
            field_errors[spec.key] = str(exc)
            continue
        if value == current_value(current, spec):
            continue
        if value is not None:
            _set(doc, spec.parts, value)
            set_keys.append(spec.key)
        elif in_repo:
            _remove(doc, spec.parts)
            removed.append(spec.key)
        else:
            field_errors[spec.key] = (
                f"set in your user config ({layers.user_path}); jev.toml cannot unset it, "
                "choose a value instead"
            )
    new_text = doc.as_string()
    errors: tuple[str, ...] = ()
    if not field_errors:
        candidate = _parse(layers.repo_file, new_text)
        stray = [k for k in _changed_paths(old_data, candidate) if not editable(k)]
        if stray:  # a bug in this module, never a user error: refuse rather than write
            errors = (f"refusing to change keys the dashboard does not edit: {', '.join(stray)}",)
        else:
            try:
                config_from_layers(layers.root, layers.user_path, layers.user, candidate)
            except ConfigError as exc:
                errors = (str(exc),)
                field_errors = _attribute(layers.user, candidate)
    return EditPlan(
        submission=submission,
        base_hash=layers.repo_hash,
        old_text=old_text,
        new_text=new_text,
        set_keys=tuple(set_keys),
        removed_keys=tuple(removed),
        field_errors=field_errors,
        errors=errors,
    )


def _container(node: Any) -> MutableMapping[str, Any] | None:
    return node if isinstance(node, MutableMapping) else None


def _set(doc: tomlkit.TOMLDocument, parts: list[str], value: Any) -> None:
    node: MutableMapping[str, Any] = doc
    for depth, part in enumerate(parts[:-1]):
        if part not in node:
            # `[roles.coder]` rather than an empty `[roles]` header above it.
            node[part] = tomlkit.table(is_super_table=depth < len(parts) - 2)
        child = _container(node[part])
        if child is None:
            raise ConfigError(f"{'.'.join(parts[: depth + 1])} in jev.toml is not a table")
        node = child
    node[parts[-1]] = value


def _remove(doc: tomlkit.TOMLDocument, parts: list[str]) -> None:
    node: MutableMapping[str, Any] | None = doc
    for part in parts[:-1]:
        node = _container(node[part]) if node is not None and part in node else None
    if node is not None and parts[-1] in node:
        del node[parts[-1]]


def _changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted paths of the leaves that differ between two parsed TOML documents."""
    if isinstance(old, dict) or isinstance(new, dict):
        if (old is not None and not isinstance(old, dict)) or (
            new is not None and not isinstance(new, dict)
        ):
            return [prefix]
        old_table, new_table = old or {}, new or {}
        return [
            path
            for key in sorted(set(old_table) | set(new_table))
            for path in _changed_paths(
                old_table.get(key), new_table.get(key), f"{prefix}.{key}" if prefix else key
            )
        ]
    return [] if old == new else [prefix]


def _attribute(user: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, str]:
    """Schema errors of the merged candidate, keyed by the editable field they are about.
    Errors about other keys stay in the whole-file message only."""
    try:
        JevConfig.model_validate(deep_merge(user or {}, candidate))
    except ValidationError as exc:
        found: dict[str, str] = {}
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"])
            message = err["msg"].removeprefix("Value error, ")
            if message.startswith("Input should be "):
                message = "must be " + message.removeprefix("Input should be ")
            key, message = _field_for(where, message)
            if key is not None:
                found.setdefault(key, message)
        return found
    return {}


def _field_for(where: str, message: str) -> tuple[str | None, str]:
    if where in FIELDS:
        return where, message
    for key in FIELDS:  # model-level errors name the key: "roles.coder.workers: unknown ..."
        if message.startswith(f"{key}: "):
            return key, message.removeprefix(f"{key}: ")
    for key in FIELDS:  # table-level errors name the leaf: "per_task_usd (2) must not ..."
        section, _, leaf = key.rpartition(".")
        if section == where and message.startswith(leaf):
            return key, message
    return None, message


# ---- writing (the only side effect) ----------------------------------------------------------

_WRITE_LOCK = threading.Lock()


def write_config(path: Path, text: str, expected_hash: str) -> bool:
    """Replace `path` with `text` atomically (temp file in the same folder, then rename), only
    if the file still has `expected_hash`. False: it changed since; nothing was written."""
    with _WRITE_LOCK:
        if file_hash(_read_text(path)) != expected_hash:
            return False
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, mode)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    return True


# ---- what the page shows ---------------------------------------------------------------------


@dataclass(frozen=True)
class FieldView:
    spec: FieldSpec
    value: str  # the effective value, or what the person typed when the save failed
    source: Source
    in_repo: bool  # jev.toml sets it, so it can be reset
    reset: bool  # the reset box was ticked in the failed save
    error: str | None
    note: str  # e.g. a `jev learning` setting that overrides the config value


@dataclass(frozen=True)
class SectionView:
    id: str
    title: str
    fields: list[FieldView]


def editor(
    layers: Layers,
    config: JevConfig,
    learning: LearningStatus,
    plan: EditPlan | None = None,
) -> list[SectionView]:
    """The editor's sections; with a failed `plan`, its typed values and errors."""
    typed = plan.submission.values if plan else {}
    resets = plan.submission.resets if plan else frozenset()
    errors = plan.field_errors if plan else {}
    notes = _learning_notes(learning)
    return [
        SectionView(
            section.id,
            section.title,
            [
                FieldView(
                    spec=spec,
                    value=typed.get(spec.key, format_value(spec, current_value(config, spec))),
                    source=(source := source_of(layers, spec.key)),
                    in_repo=source == "jev.toml",
                    reset=spec.key in resets,
                    error=errors.get(spec.key),
                    note=notes.get(spec.key, ""),
                )
                for spec in section.fields
            ],
        )
        for section in SECTIONS
    ]


def _learning_notes(status: LearningStatus) -> dict[str, str]:
    notes: dict[str, str] = {}
    if status.enabled_source == "cli setting":
        notes["learning.enabled"] = (
            f"Overridden by `jev learning enable/disable` for this repo: currently "
            f"{'on' if status.enabled else 'off'}."
        )
    if status.profile_source == "cli setting":
        notes["learning.profile"] = (
            f"Overridden by `jev learning use` for this repo: currently {status.profile}."
        )
    return notes
