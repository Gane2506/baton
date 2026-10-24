"""The server tools agents call: policy check, an audit line, SSH, then the result line.

`ServerService` serves only the servers it was granted (`access.allowed_grants`), each at its
granted level; the MCP server (`servers/mcp.py`) maps tool calls onto these methods. Results
are plain text, clipped and redacted. Nothing here raises for a refused or failed operation:
it returns a `ToolResult` with `is_error`, which the agent sees. Every allowed operation is
audited twice (issue #48): a "started" line before ssh runs (if it cannot be written, nothing
runs) and a "finished" line after; an audit write that fails becomes an error result.

Content read from a server is untrusted (it may contain instructions aimed at the agent); the
tool descriptions say so, and docs/servers.md warns about it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.config.servers_schema import AccessLevel
from jev_agent.runlog.redact import redact
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.browser import install_argv
from jev_agent.servers.policy import (
    Decision,
    PolicyContext,
    check_command,
    check_read_path,
    check_write_path,
    command_scope_paths,
    scope_violation,
    sensitive,
    within,
    within_boundary,
)
from jev_agent.servers.ssh import SshResult, SshRunner, SshTransport, clip, control_dir, run_process

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_SKIP_UPLOAD_DIRS = frozenset({".git", ".jev"})


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False


class ServerService:
    def __init__(
        self,
        config: JevConfig,
        state_dir: Path,
        repo: Path,
        grants: dict[str, AccessLevel],
        *,
        run_id: str | None = None,
        role: str | None = None,
        runner: SshRunner = run_process,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.repo = repo.resolve()
        self.grants = dict(grants)
        self.run_id = run_id
        self.role = role
        self.runner = runner
        self.audit = AuditLog(audit_path(state_dir))
        for granted_name, granted_level in self.grants.items():
            if granted_level == "unrestricted":
                server = config.servers[granted_name]
                self._log(
                    granted_name,
                    "unrestricted",
                    "unrestricted_access_granted",
                    f"host={server.host} user={server.user}",
                    True,
                    "explicit unrestricted_acknowledged=true in config",
                )

    # ---- helpers ---------------------------------------------------------------------------

    def has_medium(self) -> bool:
        return "medium" in self.grants.values() or "unrestricted" in self.grants.values()

    def context(self, name: str) -> PolicyContext:
        server = self.config.servers[name]
        return PolicyContext(
            access=self.grants[name],
            workdir=server.workdir,
            write_roots=tuple(server.write_roots()),
            browser_install=tuple(install_argv()),
            project_boundary=server.project_folder,
        )

    def transport(self, name: str) -> SshTransport:
        sockets = control_dir(self.state_dir)
        return SshTransport(self.config.servers[name], sockets=sockets, runner=self.runner)

    def _unknown(self, name: str, action: str, command: str) -> ToolResult | None:
        if name in self.grants:
            return None
        problem = self._log(name, "-", action, command, False, "server not granted to this agent")
        allowed = ", ".join(self.grants) or "none"
        text = f"server {name!r} is not available here (available: {allowed})"
        return ToolResult(text + _audit_note(problem), True)

    def _log(
        self,
        server: str,
        access: str,
        action: str,
        command: str,
        allowed: bool,
        reason: str = "",
        result: SshResult | None = None,
        phase: str = "",
    ) -> str | None:
        """Append an audit line; why it could not be written, or None."""
        try:
            self.audit.write(
                AuditEntry(
                    run_id=self.run_id,
                    role=self.role,
                    server=server,
                    access=access,
                    action=action,
                    command=command,
                    allowed=allowed,
                    reason=reason,
                    exit_code=None if result is None else result.exit_code,
                    duration_s=None if result is None else result.duration_s,
                    phase=phase,
                )
            )
        except OSError as exc:
            return redact(f"{type(exc).__name__}: {exc}")
        return None

    def _started(self, name: str, action: str, command: str) -> ToolResult | None:
        """The "started" line before ssh runs; an error result (run nothing) if it failed."""
        problem = self._log(name, self.grants[name], action, command, True, phase="started")
        if problem is None:
            return None
        return ToolResult(f"not run: cannot write the audit log ({problem})", True)

    def _finished(
        self, name: str, action: str, command: str, result: SshResult, text: str, error: bool
    ) -> ToolResult:
        """The "finished" line after ssh returned; the result, an error if the line failed."""
        problem = self._log(name, self.grants[name], action, command, True, "", result, "finished")
        if problem is None:
            return ToolResult(text, error)
        return ToolResult(text + _audit_note(problem, ran=True), True)

    def _denied(self, name: str, action: str, command: str, decision: Decision) -> ToolResult:
        problem = self._log(name, self.grants[name], action, command, False, decision.reason)
        text = f"refused by jev's {self.grants[name]} access policy: {decision.reason}"
        return ToolResult(text + _audit_note(problem), True)

    @staticmethod
    def _timeout(value: float | None) -> float:
        # `not (value > 0)` also catches NaN: it would otherwise survive `min()`
        # and blow up `int()` inside `time_limited`.
        if value is None or not (value > 0):
            return DEFAULT_TIMEOUT_S
        return min(float(value), MAX_TIMEOUT_S)

    def _realpath(self, name: str, path: str) -> tuple[str | None, str]:
        """Resolve symlinks on the server; (resolved path, error)."""
        result = self.transport(name).realpath(path)
        text, _ = clip(result.stdout)
        resolved = text.strip()
        if not result.ok or not resolved.startswith("/"):
            err, _ = clip(result.stderr)
            return None, redact(err.strip() or "cannot resolve the path")
        return resolved, ""

    # ---- tools -----------------------------------------------------------------------------

    def list_servers(self) -> ToolResult:
        rows = []
        for name, level in self.grants.items():
            server = self.config.servers[name]
            rows.append(
                {
                    "name": name,
                    "access": level,
                    "description": server.description,
                    "workdir": server.workdir,
                    "writable_paths": (
                        [server.project_folder]
                        if level == "medium" and server.project_folder
                        else server.write_roots()
                        if level == "medium"
                        else []
                    ),
                }
            )
        return ToolResult(json.dumps(rows, indent=2))

    def run_command(self, name: str, command: str, timeout_s: float | None = None) -> ToolResult:
        if (refused := self._unknown(name, "run_command", command)) is not None:
            return refused
        ctx = self.context(name)
        decision = check_command(command, ctx)
        if not decision.allowed:
            return self._denied(name, "run_command", command, decision)
        if ctx.access == "medium":
            # The lexical check cannot see server symlinks: re-check every
            # scope-sensitive operand (compose files, recursive cp sources,
            # pip -r/-e files) against its server-resolved path before anything
            # runs, exactly as the file tools do (issue #204 I-3). A path that
            # cannot be resolved, or that resolves outside the scope or into a
            # credential file, never runs.
            for scoped in command_scope_paths(decision.command, ctx):
                if not scoped.path:
                    return self._denied(
                        name,
                        "run_command",
                        decision.command,
                        Decision.deny(
                            f"{scoped.kind}: use an absolute path (or one relative to the workdir)"
                        ),
                    )
                real, error = self._realpath(name, scoped.path)
                if real is None:
                    problem = self._log(
                        name, ctx.access, "run_command", decision.command, False, error
                    )
                    return ToolResult(
                        f"cannot run {decision.command}: cannot resolve {scoped.path} ({error})"
                        + _audit_note(problem),
                        True,
                    )
                if problem := sensitive(real):
                    return self._denied(name, "run_command", real, Decision.deny(problem))
                if (violation := scope_violation(real, ctx, strictly=scoped.strictly)) is not None:
                    return self._denied(name, "run_command", real, Decision.deny(violation))
                if scoped.kind == "recursive cp source" and any(
                    p.lower() == ".git" for p in real.split("/")
                ):
                    return self._denied(
                        name,
                        "run_command",
                        real,
                        Decision.deny(f"{real}: nothing is copied from or into .git"),
                    )
        if (failed := self._started(name, "run_command", decision.command)) is not None:
            return failed
        server = self.config.servers[name]
        result = self.transport(name).run(
            decision.command, cwd=server.workdir, timeout_s=self._timeout(timeout_s)
        )
        text = format_result(result)
        return self._finished(name, "run_command", decision.command, result, text, not result.ok)

    def read_file(self, name: str, path: str, max_bytes: int | None = None) -> ToolResult:
        if (refused := self._unknown(name, "read_file", path)) is not None:
            return refused
        ctx = self.context(name)
        decision = check_read_path(path, ctx)
        if not decision.allowed:
            return self._denied(name, "read_file", path, decision)
        real, error = self._realpath(name, decision.path)
        if real is None:
            problem = self._log(name, ctx.access, "read_file", decision.path, False, error)
            return ToolResult(f"cannot read {decision.path}: {error}" + _audit_note(problem), True)
        if problem := sensitive(real):
            return self._denied(name, "read_file", real, Decision.deny(problem))
        if not within_boundary(real, ctx):
            return self._denied(
                name,
                "read_file",
                real,
                Decision.deny(f"{real} is outside the project folder"),
            )
        cap = MAX_READ_BYTES if not max_bytes else max(1, min(int(max_bytes), MAX_READ_BYTES))
        if (failed := self._started(name, "read_file", real)) is not None:
            return failed
        result = self.transport(name).read(real, cap)
        data = result.stdout
        if not result.ok:
            out, is_error = format_result(result), True
        elif b"\x00" in data[:cap]:
            out, is_error = f"{real} looks binary; only text files can be read", True
        else:
            text, _ = clip(data[:cap], cap)
            note = f"\n[truncated at {cap} bytes]" if len(data) > cap else ""
            out, is_error = redact(text) + note, False
        return self._finished(name, "read_file", real, result, out, is_error)

    def write_file(self, name: str, path: str, content: str) -> ToolResult:
        data = content.encode("utf-8")
        return self._write(name, "write_file", path, data, f"{path} ({len(data)} bytes)")

    def upload(self, name: str, local_path: str, remote_path: str) -> ToolResult:
        """Copy a file from the local repo (or the agent's worktree, inside it) to the server."""
        if (refused := self._unknown(name, "upload", remote_path)) is not None:
            return refused
        source, problem = self._local_source(local_path)
        if source is None:
            decision = Decision.deny(problem)
            return self._denied(name, "upload", f"{local_path} -> {remote_path}", decision)
        data = source.read_bytes()
        summary = f"{source} -> {remote_path} ({len(data)} bytes)"
        return self._write(name, "upload", remote_path, data, summary)

    def _local_source(self, local_path: str) -> tuple[Path | None, str]:
        """The file to upload: inside the repo, or inside a task worktree under
        `.jev/worktrees/` (relative paths are resolved against the agent's working directory,
        which is its worktree). Never `.git/`, other `.jev/` files or credential files."""
        candidate = Path(local_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            source = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, f"{local_path}: no such local file"
        if not source.is_relative_to(self.repo):
            return None, f"{local_path}: only files inside the repository can be uploaded"
        parts = source.relative_to(self.repo).parts
        if parts[:2] == (".jev", "worktrees"):
            parts = parts[2:]
        if _SKIP_UPLOAD_DIRS & set(parts):
            return None, f"{local_path}: files under .git/ or .jev/ cannot be uploaded"
        if problem := sensitive("/" + "/".join(parts)):
            return None, problem
        if not source.is_file():
            return None, f"{local_path}: not a regular file"
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            return None, f"{local_path}: larger than {MAX_UPLOAD_BYTES} bytes"
        return source, ""

    def _write(self, name: str, action: str, path: str, data: bytes, summary: str) -> ToolResult:
        if (refused := self._unknown(name, action, path)) is not None:
            return refused
        ctx = self.context(name)
        decision = check_write_path(path, ctx)
        if not decision.allowed:
            return self._denied(name, action, summary, decision)
        limit = MAX_UPLOAD_BYTES if action == "upload" else MAX_WRITE_BYTES
        if len(data) > limit:
            return self._denied(name, action, summary, Decision.deny(f"more than {limit} bytes"))
        real, error = self._realpath(name, decision.path)
        if real is None:
            problem = self._log(name, ctx.access, action, summary, False, error)
            return ToolResult(f"cannot write {decision.path}: {error}" + _audit_note(problem), True)
        if real != decision.path:  # a symlink: check where it really points
            recheck = check_write_path(real, ctx)
            if not recheck.allowed:
                return self._denied(name, action, f"{summary} (resolves to {real})", recheck)
        # `unrestricted` is documented as "no checks at all": it skips this
        # post-resolution re-check. Medium keeps it as a second verification
        # after symlinks are resolved on the server.
        if ctx.access != "unrestricted" and not within(real, ctx.write_roots, strictly=True):
            return self._denied(
                name, action, summary, Decision.deny(f"{real} is outside allowed_paths")
            )
        if (failed := self._started(name, action, summary)) is not None:
            return failed
        result = self.transport(name).write(real, data)
        if not result.ok:
            return self._finished(name, action, summary, result, format_result(result), True)
        return self._finished(
            name, action, summary, result, f"wrote {len(data)} bytes to {real}", False
        )


def _audit_note(problem: str | None, *, ran: bool = False) -> str:
    """What the agent is told when an audit line could not be written."""
    if problem is None:
        return ""
    done = "it ran, but " if ran else ""
    return f"\n[{done}the audit log could not be written: {problem}]"


def format_result(result: SshResult) -> str:
    """Exit status, stdout and stderr as text for the agent, clipped and redacted."""
    out, out_cut = clip(result.stdout)
    err, err_cut = clip(result.stderr)
    if result.timed_out:
        head = f"timed out after {result.duration_s:.0f}s"
    elif result.exit_code == 255:
        head = "exit code 255 (ssh could not connect or the remote command failed; see stderr)"
    else:
        head = f"exit code {result.exit_code}"
    parts = [head]
    if out:
        parts += ["--- stdout ---", out + ("\n[output truncated]" if out_cut else "")]
    if err:
        parts += ["--- stderr ---", err + ("\n[output truncated]" if err_cut else "")]
    return redact("\n".join(parts))
