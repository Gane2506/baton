"""File store for workflow profiles (issue #368, design sections 6.2-6.3).

Repo scope: `<repo>/.jev/workflow-profiles/<slug>/v<n>.json` plus a
`<slug>/current.json` pointer. User scope: the same layout under
`~/.jev/workflow-profiles/`. Repo profiles do NOT travel via git (`.jev/`
is gitignored); sharing is dashboard export/import only.

Version creation is atomic per step: the writer writes `v<n>.json` to a
temporary file and renames it into place, then writes `current.json` the
same way. A crash between the two steps leaves `current.json` pointing at
the previous version, so the new version simply is not current yet. If
`v<n>.json` already exists when a writer claims version n (two editors
racing), the writer retries as n+1 instead of overwriting.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .schema import MAX_PROFILE_BYTES, parse_profile_id, profile_id_for
from .validator import (
    ProfileValidationError,
    ValidationContext,
    normalize_profile,
    validate_profile,
)

PROFILE_DIRNAME = "workflow-profiles"


class ProfileStoreError(ValueError):
    """A profile store failure, with a user-facing message."""


@dataclass
class RecheckFailure:
    """One stored version that fails the full catalogue on re-check (design
    section 15, phase 3): the profile id and version plus every error, each
    naming its setting. `archived` marks versions of archived profiles —
    archived versions are re-checked too, because their snapshots still run."""

    profile_id: str
    version: int
    archived: bool
    messages: list[str]


class ProfileCorruptError(ProfileStoreError):
    """A stored profile file failed validation on read: reported, never
    executed, never repaired without the user saving a new version."""


class ProfileRefusal(ProfileStoreError):
    """A creation/save refusal naming the setting (design section 15:
    refusals name the setting and never silently fall back)."""


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temporary file in the same directory plus
    rename, so a reader never sees a half-written version file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ProfileStore:
    """Repo-scope and user-scope profile files for one machine."""

    def __init__(self, repo_root: Path | None, user_root: Path | None = None) -> None:
        self.repo_root = repo_root
        self.user_root = user_root

    # ---- layout ---------------------------------------------------------------

    def scope_dir(self, scope: str) -> Path:
        if scope == "repo":
            if self.repo_root is None:
                raise ProfileStoreError("repo-scope profiles need a repo checkout")
            return self.repo_root / ".jev" / PROFILE_DIRNAME
        if scope == "user":
            if self.user_root is None:
                raise ProfileStoreError("user-scope profiles need a user directory")
            return self.user_root / PROFILE_DIRNAME
        raise ProfileStoreError(f"unknown profile scope {scope!r}")

    def profile_dir(self, scope: str, slug: str) -> Path:
        return self.scope_dir(scope) / slug

    # ---- writes -----------------------------------------------------------------

    def save(
        self, data: dict[str, Any], *, context: ValidationContext | None = None
    ) -> dict[str, Any]:
        """Save a profile dict as a new version (creating version 1, or
        current+1 for an edit). The stored file is the normalized form
        (defaults filled); the returned dict is what was stored, including
        its assigned version. Validates before writing anything."""
        normalized = normalize_profile(data)
        errors = validate_profile(normalized, context=context)
        if errors:
            raise ProfileValidationError(errors)
        parsed = parse_profile_id(str(normalized["id"]))
        if parsed is None:
            raise ProfileValidationError([f"Unknown profile id {normalized['id']!r}."])
        scope, slug = parsed
        directory = self.profile_dir(scope, slug)
        text = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ProfileRefusal(
                f"Refused: profile {normalized['id']!r} is "
                f"{len(text.encode('utf-8'))} bytes, over the {MAX_PROFILE_BYTES}-byte "
                "cap for one profile version."
            )
        version = self._next_version(directory)
        while True:
            target = directory / f"v{version}.json"
            if target.exists():
                # Two editors racing for the same version: retry as n+1,
                # never overwrite.
                version += 1
                continue
            stored = dict(normalized, version=version)
            stored.pop("release", None)
            text = json.dumps(stored, indent=2, sort_keys=True) + "\n"
            _atomic_write(target, text)
            break
        _atomic_write(directory / "current.json", json.dumps({"version": version}) + "\n")
        return stored

    def _next_version(self, directory: Path) -> int:
        current = directory / "current.json"
        if current.exists():
            try:
                return int(json.loads(current.read_text(encoding="utf-8"))["version"]) + 1
            except (ValueError, KeyError, TypeError):
                pass
        existing = [int(p.name[1:-5]) for p in directory.glob("v*.json") if p.name[1:-5].isdigit()]
        return (max(existing) + 1) if existing else 1

    # ---- reads ------------------------------------------------------------------

    def get(self, profile_id: str, version: int | None = None) -> dict[str, Any]:
        """A stored version (current when `version` is None). Validates on
        read: a corrupt file is reported naming id and version, never
        executed and never partially applied."""
        parsed = parse_profile_id(profile_id)
        if parsed is None:
            raise ProfileStoreError(f"unknown workflow profile {profile_id!r}")
        scope, slug = parsed
        directory = self.profile_dir(scope, slug)
        if version is None:
            current = directory / "current.json"
            if not current.exists():
                raise ProfileStoreError(
                    f"workflow profile {profile_id!r} no longer resolves: pick a current profile"
                )
            try:
                version = int(json.loads(current.read_text(encoding="utf-8"))["version"])
            except (ValueError, KeyError, TypeError):
                raise ProfileCorruptError(
                    f"workflow profile {profile_id!r} cannot be read: its current-version "
                    "pointer is corrupt; save a new version"
                ) from None
        target = directory / f"v{version}.json"
        if not target.exists():
            raise ProfileStoreError(
                f"workflow profile {profile_id!r} version {version} no longer resolves: "
                "pick a current profile"
            )
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            raise ProfileCorruptError(
                f"workflow profile {profile_id!r} version {version} cannot be read: "
                "its file is not valid JSON; save a new version"
            ) from None
        errors = validate_profile(data)
        if errors:
            raise ProfileCorruptError(
                f"workflow profile {profile_id!r} version {version} cannot be read: "
                + "; ".join(errors)
            )
        return cast(dict[str, Any], data)

    def versions(self, profile_id: str) -> list[int]:
        parsed = parse_profile_id(profile_id)
        if parsed is None:
            return []
        directory = self.profile_dir(*parsed)
        if not directory.exists():
            return []
        return sorted(
            int(p.name[1:-5]) for p in directory.glob("v*.json") if p.name[1:-5].isdigit()
        )

    def exists(self, profile_id: str) -> bool:
        return bool(self.versions(profile_id))

    def is_archived(self, profile_id: str) -> bool:
        parsed = parse_profile_id(profile_id)
        if parsed is None:
            return False
        return (self.profile_dir(*parsed) / "archived.json").exists()

    def list_profiles(
        self, *, scope: str | None = None, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Current versions, one per profile, with scope badges from the id.
        Archived profiles are hidden from creation lists (their snapshots
        still run); plain delete never removes files, it archives."""
        out: list[dict[str, Any]] = []
        scopes = [scope] if scope else ["repo", "user"]
        for name in scopes:
            try:
                base = self.scope_dir(name)
            except ProfileStoreError:
                continue
            if not base.exists():
                continue
            for directory in sorted(base.iterdir()):
                if not directory.is_dir():
                    continue
                profile_id = profile_id_for(name, directory.name)
                if not include_archived and self.is_archived(profile_id):
                    continue
                try:
                    out.append(self.get(profile_id))
                except ProfileStoreError:
                    continue  # a corrupt profile is reported on direct read, not listed
        return out

    def recheck_all(self, *, context: ValidationContext | None = None) -> list[RecheckFailure]:
        """Phase-3 re-check (design sections 7.5, 15): every stored version in
        both scopes, archived profiles included, against the full catalogue.
        Stored files are read raw — not through `get`, which would raise on
        the first failure — so one report covers the whole store, with each
        failure naming its setting. An empty list means every artefact stored
        under phase 2 still passes. This is the store-wide audit; the
        creation-time re-check for the picked version lives in the resolver,
        which validates the chosen version before resolving its snapshot."""
        failures: list[RecheckFailure] = []
        for scope in ("repo", "user"):
            try:
                base = self.scope_dir(scope)
            except ProfileStoreError:
                continue
            if not base.exists():
                continue
            for directory in sorted(base.iterdir()):
                if not directory.is_dir():
                    continue
                profile_id = profile_id_for(scope, directory.name)
                archived = (directory / "archived.json").exists()
                for path in sorted(directory.glob("v*.json")):
                    stem = path.name[1:-5]
                    if not stem.isdigit():
                        continue
                    version = int(stem)
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except ValueError:
                        failures.append(
                            RecheckFailure(
                                profile_id,
                                version,
                                archived,
                                [
                                    f"workflow profile {profile_id!r} version {version} "
                                    "cannot be read: its file is not valid JSON; "
                                    "save a new version"
                                ],
                            )
                        )
                        continue
                    messages = validate_profile(data, context=context)
                    if messages:
                        failures.append(RecheckFailure(profile_id, version, archived, messages))
        return failures

    # ---- archive / delete ---------------------------------------------------------

    def archive(self, profile_id: str) -> None:
        """Plain delete: the definition files stay, the profile disappears
        from creation lists, and existing snapshots keep running."""
        parsed = parse_profile_id(profile_id)
        if parsed is None or not self.exists(profile_id):
            raise ProfileStoreError(f"unknown workflow profile {profile_id!r}")
        _atomic_write(
            self.profile_dir(*parsed) / "archived.json",
            json.dumps({"archived": True}) + "\n",
        )

    def delete_permanent(self, profile_id: str, *, referencing_runs: int) -> None:
        """Permanent deletion, refused while any run references any version
        (the refusal names the referencing run count). Referenced versions
        are never auto-deleted."""
        if referencing_runs > 0:
            raise ProfileRefusal(
                f"Refused: workflow profile {profile_id!r} cannot be permanently deleted "
                f"while {referencing_runs} run(s) reference it; archive it instead, or "
                "wait until no run references any version."
            )
        parsed = parse_profile_id(profile_id)
        if parsed is None or not self.exists(profile_id):
            raise ProfileStoreError(f"unknown workflow profile {profile_id!r}")
        directory = self.profile_dir(*parsed)
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
