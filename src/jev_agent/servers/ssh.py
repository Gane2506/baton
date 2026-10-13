"""The SSH transport: the system `ssh`, non-interactive, with strict host-key checking
(docs/verification.md section 9).

Every call is

    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10
        -o ControlMaster=auto -o ControlPath=<.jev/ssh>/%C -o ControlPersist=60
        [-p PORT] [-l USER] [-i FILE -o IdentitiesOnly=yes] -T -- HOST REMOTE_COMMAND

- `BatchMode=yes`: never prompt (passwords, passphrases, unknown host keys) - fail instead.
- `StrictHostKeyChecking=yes`: never add or accept a host key; the host must already be in
  `known_hosts` (connect once by hand). `no` / `accept-new` are never used.
- Connection reuse: one master per destination under `.jev/ssh/` (mode 0700), named by `%C`
  (a hash of the destination), kept 60 s after the last use. When that socket path would be
  too long for a Unix socket, reuse is turned off (`ControlPath=none`).
- Keys come from ssh-agent or `~/.ssh/config`; `identity_file` adds `-i` with
  `IdentitiesOnly=yes`. jev never reads key files or passwords.

`REMOTE_COMMAND` always comes from `servers.policy` (re-quoted words) or from the fixed file
helpers below; it is never text an agent wrote. It runs under the server's `timeout` (see
`time_limited`), so a command the local timeout gave up on does not keep running remotely.
Output is clipped and decoded here; callers redact before logging. The runner is injectable
so tests never open a connection.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from jev_agent.config.servers_schema import ServerConfig

CONNECT_TIMEOUT_S = 10
CONTROL_PERSIST_S = 60
DEFAULT_TIMEOUT_S = 120.0
MAX_OUTPUT_BYTES = 64 * 1024
# macOS allows 104 bytes for a Unix socket path, Linux 108; ssh appends a 17-character random
# suffix while it sets the master up.
_MAX_SOCKET_PATH = 104 - 18
SSH_COMMAND = "ssh"
REMOTE_KILL_GRACE_S = 2  # `timeout -k`: SIGKILL this long after SIGTERM on the server
REMOTE_TIMEOUT_EXIT = 124  # what `timeout` exits with when it stopped the command
SSH_DIRNAME = "ssh"


@dataclass(frozen=True)
class SshResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SshRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, stdin: bytes | None, timeout_s: float
    ) -> SshResult: ...


def run_process(argv: Sequence[str], *, stdin: bytes | None, timeout_s: float) -> SshResult:
    """Run `argv` (no shell) in its own process group; kill the group on timeout."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return SshResult(127, b"", f"command not found: {argv[0]}".encode(), False, 0.0)
    try:
        out, err = proc.communicate(stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return SshResult(None, out, err, True, time.monotonic() - started)
    return SshResult(proc.returncode, out, err, False, time.monotonic() - started)


def time_limited(script: str, timeout_s: float) -> str:
    """`script` for the remote login shell, stopped by the server's `timeout` before the local
    timeout gives up on ssh (killing the local ssh would leave it running there). Servers
    without `timeout` (stock macOS, some minimal images) run it unlimited, as before."""
    limit = max(1, int(timeout_s) - REMOTE_KILL_GRACE_S - 1)
    quoted = shlex.quote(script)
    return (
        "if command -v timeout >/dev/null 2>&1 ; "
        f"then exec timeout -k {REMOTE_KILL_GRACE_S} {limit} sh -c {quoted} ; "
        f"else exec sh -c {quoted} ; fi"
    )


def control_dir(state_dir: Path) -> Path:
    return state_dir / SSH_DIRNAME


def ssh_options(server: ServerConfig, sockets: Path | None) -> list[str]:
    """The `-o`/`-p`/`-l`/`-i` options for `server` (no destination, no command)."""
    opts = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
    ]  # fmt: skip
    socket = None if sockets is None else str(sockets / "%C")
    if socket is not None and len(str(sockets)) + 1 + 40 <= _MAX_SOCKET_PATH:
        opts += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={socket}",
            "-o", f"ControlPersist={CONTROL_PERSIST_S}",
        ]  # fmt: skip
    else:
        opts += ["-o", "ControlPath=none"]
    if server.port is not None:
        opts += ["-p", str(server.port)]
    if server.user is not None:
        opts += ["-l", server.user]
    if server.identity_file is not None:
        opts += ["-i", str(Path(server.identity_file).expanduser()), "-o", "IdentitiesOnly=yes"]
    return opts


def clip(data: bytes, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """`data` decoded as UTF-8 (invalid bytes replaced), cut to `limit` bytes."""
    clipped = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), clipped


class SshTransport:
    """Runs fixed remote commands on one server."""

    def __init__(
        self,
        server: ServerConfig,
        *,
        sockets: Path | None = None,
        runner: SshRunner = run_process,
        command: str = SSH_COMMAND,
    ) -> None:
        self.server = server
        self.sockets = sockets
        self.runner = runner
        self.command = command

    def argv(self, remote_command: str) -> list[str]:
        opts = ssh_options(self.server, self.sockets)
        return [self.command, *opts, "-T", "--", self.server.host, remote_command]

    def _prepare(self) -> None:
        if self.sockets is not None:
            self.sockets.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                self.sockets.chmod(0o700)

    def run(
        self,
        remote_command: str,
        *,
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> SshResult:
        """Run `remote_command` (already quoted) on the server, in `cwd` when given, for at
        most `timeout_s`. A remote command stopped by the server's `timeout` (exit 124) is
        reported as timed out, like one the local timeout stopped."""
        if cwd is not None:
            remote_command = f"cd -- {shlex.quote(cwd)} && {remote_command}"
        self._prepare()
        argv = self.argv(time_limited(remote_command, timeout_s))
        result = self.runner(argv, stdin=stdin, timeout_s=timeout_s)
        if result.exit_code == REMOTE_TIMEOUT_EXIT and not result.timed_out:
            return replace(result, timed_out=True)
        return result

    def check(self, timeout_s: float = CONNECT_TIMEOUT_S + 5) -> SshResult:
        """`ssh ... HOST true`: can we log in without any prompt?"""
        return self.run("true", timeout_s=timeout_s)

    def read(self, path: str, max_bytes: int, timeout_s: float = DEFAULT_TIMEOUT_S) -> SshResult:
        """Up to `max_bytes + 1` bytes of `path` (one more, to tell that it was cut)."""
        return self.run(f"head -c {max_bytes + 1} -- {shlex.quote(path)}", timeout_s=timeout_s)

    def realpath(self, path: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> SshResult:
        """`realpath -m -- path`: where `path` really points (symlinks resolved)."""
        return self.run(f"realpath -m -- {shlex.quote(path)}", timeout_s=timeout_s)

    def write(self, path: str, data: bytes, timeout_s: float = DEFAULT_TIMEOUT_S) -> SshResult:
        """Write `data` to `path` (absolute, symlinks resolved) through a temporary file in the
        same folder, then rename. `mktemp` creates the temporary file itself (O_EXCL, a fresh
        random name), so a symlink planted in the folder can never redirect the write (issue
        #45); it is given the usual `0666 & ~umask` mode (mktemp makes it 0600) and removed if
        anything fails."""
        target = shlex.quote(path)
        template = shlex.quote(f"{posixpath.dirname(path)}/.jev-tmp.XXXXXXXXXX")
        command = (
            f"tmp=$(mktemp {template}) || exit 1 ; "
            'chmod "$(printf %o "$(( 0666 & ~$(umask) ))")" "$tmp" '
            f'&& cat > "$tmp" && mv -f -- "$tmp" {target} '
            '|| { rm -f -- "$tmp" ; exit 1 ; }'
        )
        return self.run(command, stdin=data, timeout_s=timeout_s)
