"""`jev init --migrate`: move a repo set up with jao v0.1 to the jev names (docs/decisions.md).

What it does, all local (nothing is pushed):

- renames `jao.toml` → `jev.toml` and `.jao/` → `.jev/` (refuses when the new name exists);
- runs `git worktree repair` on the worktrees that lived under `.jao/`, so git follows them to
  their new place under `.jev/` (nothing is removed);
- rewrites `.jao/` in `.gitignore` to `.jev/` (adds `.jev/` if no such line existed);
- points the absolute log paths stored in `state.db` (attempts.log_path and the copy in
  result_json) at `.jev/` (idempotent), right after the move;
- renames local branches `jao/*` → `jev/*` and refs `refs/jao/*` → `refs/jev/*`, skipping
  branches checked out in a worktree, names that already exist and any rename git refuses
  (e.g. a branch named `jev` blocks every `jev/...` name); all are reported.

Only the pre-checks raise `MigrateError`, before anything changes. After that each step that
fails is reported and the rest still runs, so a rerun completes whatever is left.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.cli.repo import LEGACY_CONFIG_FILENAME, LEGACY_STATE_DIRNAME
from jev_agent.cli.wiring import STATE_DB_FILENAME
from jev_agent.config import CONFIG_FILENAME, STATE_DIRNAME


class MigrateError(Exception):
    """The migration cannot start (nothing was changed). Raised only by the pre-checks."""


@dataclass
class MigrateReport:
    moved: list[str] = field(default_factory=list)
    repaired_worktrees: list[Path] = field(default_factory=list)
    gitignore: str | None = None
    renamed_refs: list[tuple[str, str]] = field(default_factory=list)
    skipped_refs: list[tuple[str, str]] = field(default_factory=list)  # (ref, reason)
    db_rows: int = 0
    problems: list[str] = field(default_factory=list)  # steps that failed; a rerun retries


def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise MigrateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _worktrees(root: Path) -> list[tuple[Path, str | None]]:
    """(path, checked-out branch ref or None) for every worktree of the repo."""
    result: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*_git(root, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = Path(line[len("worktree ") :]), None
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
        elif not line and path is not None:
            result.append((path, branch))
            path = None
    return result


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def migrate(root: Path) -> MigrateReport:
    """Migrate the repo at `root` from the jao v0.1 layout. See the module docstring."""
    old_config, new_config = root / LEGACY_CONFIG_FILENAME, root / CONFIG_FILENAME
    old_state, new_state = root / LEGACY_STATE_DIRNAME, root / STATE_DIRNAME
    for old, new in ((old_config, new_config), (old_state, new_state)):
        if old.exists() and new.exists():
            raise MigrateError(f"both {old.name} and {new.name} exist; merge them by hand first")

    # Pre-checks: everything git has to tell us is read before anything changes.
    report = MigrateReport()
    worktrees = _worktrees(root)
    old_worktrees = [path for path, _ in worktrees if _is_under(path, old_state)]
    checked_out = {branch for _, branch in worktrees if branch}
    legacy_refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads/jao/", "refs/jao/")
    existing = set(_git(root, "for-each-ref", "--format=%(refname)").splitlines())

    if old_config.exists():
        old_config.rename(new_config)
        report.moved.append(f"{LEGACY_CONFIG_FILENAME} → {CONFIG_FILENAME}")
    if old_state.exists():
        old_state.rename(new_state)
        report.moved.append(f"{LEGACY_STATE_DIRNAME}/ → {STATE_DIRNAME}/")
    try:
        report.db_rows = _migrate_db(new_state / STATE_DB_FILENAME, old_state, new_state)
    except sqlite3.Error as exc:  # e.g. locked by a running `jev`
        report.problems.append(f"{STATE_DIRNAME}/{STATE_DB_FILENAME} not updated: {exc}")
    if old_worktrees:
        moved = [new_state / p.resolve().relative_to(old_state.resolve()) for p in old_worktrees]
        try:
            _git(root, "worktree", "repair", *[str(p) for p in moved])
            report.repaired_worktrees = moved
        except MigrateError as exc:
            report.problems.append(str(exc))

    report.gitignore = _migrate_gitignore(root / ".gitignore", add_if_missing=bool(report.moved))
    _migrate_refs(root, report, legacy_refs.splitlines(), existing, checked_out)
    return report


def _migrate_gitignore(gitignore: Path, *, add_if_missing: bool) -> str | None:
    bare = LEGACY_STATE_DIRNAME.strip("/")
    old_lines = {bare, f"{bare}/", f"/{bare}", f"/{bare}/"}
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        if line.strip() in old_lines:
            lines[i] = line.replace(LEGACY_STATE_DIRNAME, STATE_DIRNAME, 1)
            changed = True
    if changed:
        gitignore.write_text("".join(lines), encoding="utf-8")
        return f"replaced {LEGACY_STATE_DIRNAME}/ with {STATE_DIRNAME}/ in .gitignore"
    if not add_if_missing:
        return None
    from jev_agent.cli.init_cmd import add_to_gitignore  # avoid an import cycle

    if add_to_gitignore(gitignore, f"{STATE_DIRNAME}/"):
        return f"added {STATE_DIRNAME}/ to .gitignore"
    return None


def _migrate_refs(
    root: Path,
    report: MigrateReport,
    legacy_refs: list[str],
    existing: set[str],
    checked_out: set[str],
) -> None:
    for ref in legacy_refs:
        if ref.startswith("refs/heads/jao/"):
            new = "refs/heads/jev/" + ref[len("refs/heads/jao/") :]
        else:
            new = "refs/jev/" + ref[len("refs/jao/") :]
        if new in existing:
            report.skipped_refs.append((ref, f"{new} already exists"))
            continue
        if ref in checked_out:
            report.skipped_refs.append((ref, "checked out in a worktree"))
            continue
        try:
            if ref.startswith("refs/heads/"):
                # `git branch -m` keeps the reflog and branch config.
                _git(root, "branch", "-m", ref[len("refs/heads/") :], new[len("refs/heads/") :])
            else:
                sha = _git(root, "rev-parse", ref).strip()
                _git(root, "update-ref", new, sha, "")
                _git(root, "update-ref", "-d", ref, sha)
        except MigrateError as exc:
            report.skipped_refs.append((ref, str(exc)))
            continue
        report.renamed_refs.append((ref, new))


def _migrate_db(db_path: Path, old_state: Path, new_state: Path) -> int:
    """Point absolute paths under the old state dir at the new one. Safe to run twice."""
    if not db_path.exists():
        return 0
    old_prefix, new_prefix = f"{old_state}/", f"{new_state}/"
    conn = sqlite3.connect(db_path)
    try:
        has_attempts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attempts'"
        ).fetchone()
        if not has_attempts:
            return 0
        with conn:
            cur = conn.execute(
                "UPDATE attempts SET log_path = ? || substr(log_path, ?) "
                "WHERE substr(log_path, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            rows = cur.rowcount
            # The worker result keeps its own copy of the log path (`raw_log_path`).
            conn.execute(
                "UPDATE attempts SET result_json = replace(result_json, ?, ?) "
                "WHERE instr(result_json, ?) > 0",
                (old_prefix, new_prefix, old_prefix),
            )
        return rows
    finally:
        conn.close()
