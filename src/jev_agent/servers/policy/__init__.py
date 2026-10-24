"""Access policy for server commands and file operations (docs/servers.md "Access levels").

Every command an agent sends is checked here before anything reaches `ssh`:

1. **Shape.** One line; no `;`, `&`, `&&`, `||`, redirects (`<`, `>`), subshells (`(`, `)`),
   backticks, `$` (variables and `$(...)`), or more than one pipe. Quotes must balance. The
   text is then split with `shlex` and every word is re-quoted (`shlex.join`) for the remote
   shell, so nothing the agent wrote is ever interpreted by a shell there (no globbing either).
2. **Always denied**, at every level: formatting disks, `dd of=/dev/*`, shutdown/reboot,
   user/group/password/sudoers changes, firewall changes, recursive chmod/chown on `/`,
   `rm` on `/` or recursive/forced `rm` outside `allowed_paths`, `crontab -r`, `kill -9 1`,
   anything touching `authorized_keys`, `git push --force`, and piping into a shell or
   interpreter (`curl ... | sh`).
3. **basic**: an allowlist of read-only commands (`READ_ONLY_COMMANDS`), each with its
   dangerous options refused (`find -exec`, `sort -o`, `tail -f`, ...), optionally piped into
   one read-only filter (`grep`, `head`, `tail`, `wc`, `sort`). No `sudo`. Reads of credential
   files (`~/.ssh`, private keys, `.env`, `/etc/shadow`, ...) are refused: by name, and for
   recursive `grep` (only inside workdir/`allowed_paths`, never `-R`) by credential-name
   excludes jev adds; nothing that prints environments (`docker inspect`, `systemctl show`,
   `ps e`). Every `git` runs with hooks and fsmonitor turned off.
4. **medium**: basic, plus `mkdir`/`cp`/`mv`/`rm`/`touch` inside the write roots
   (`allowed_paths`, default `[workdir]`), `git add|commit|pull|fetch|checkout|switch` in a
   working copy inside them, package installs (`apt-get install -y`, `pip install`,
   `npm install|ci`, the pinned Playwright browser install), `systemctl
   restart|reload|start|stop UNIT` (not ssh) and `docker compose up -d|down|restart|pull`.
   `sudo` is accepted only in front of package installs and `systemctl`, and always runs as
   `sudo -n` (never prompts). Nothing is ever written under a `.git` directory (hooks and
   config run code); recursive `cp` only copies from inside the write roots. Medium still
   means code execution as the server account: package installs run the packages' own code
   and a `pip install -r` file's own options are not checked (its *location* is checked,
   lexically here and against the server-resolved path before it runs — see below).

The lexical checks above cannot see server symlinks: a path inside the write scope may
point outside it on the server. File tools re-check the server-resolved path, and
`ServerService.run_command` does the same for the scope-sensitive operands of a command
(`command_scope_paths`: compose `-f` files, recursive `cp` sources, `pip -r`/`-e` files)
before anything runs, so a symlink escape there is refused just like a lexical one.
5. **unrestricted**: explicit opt-in only (`unrestricted_acknowledged = true` in the server
   config, so it is never granted by default, by typo or by omission). No policy checks at
   all: any command is passed through as written (`writes=True`), any path is allowed. The
   controls are the acknowledgment plus the prominent audit-log line the service layer
   writes when the tier is granted.

A medium server may also set `project_folder` (`PolicyContext.project_boundary`): the agent
may then read, write and run commands only inside that folder, nothing outside it. The
boundary is enforced lexically here — `..` escapes and absolute paths outside are refused,
and an unset or out-of-boundary workdir fails closed — and the file tools re-check the
server-resolved path against it (`within_boundary`), so a symlink pointing out of the
folder counts as a boundary violation.

`check_command`, `check_read_path` and `check_write_path` return a `Decision`; nothing here
runs anything.
"""

from .commands import SHELLS, check_command, check_read_path, check_write_path
from .deny import always_denied
from .paths import GREP_EXCLUDES, resolve, sensitive, within, within_boundary
from .read import FILTERS, GIT_SAFE_CONFIG, READ_ONLY_COMMANDS, Checker
from .scope import ScopedPath, command_scope_paths, scope_violation
from .shape import MAX_COMMAND_CHARS, split_pipeline
from .types import Decision, PolicyContext
from .write import BROWSER_INSTALL_PREFIX, MEDIUM_COMMANDS, SUDO_COMMANDS

__all__ = [
    "BROWSER_INSTALL_PREFIX",
    "FILTERS",
    "GIT_SAFE_CONFIG",
    "GREP_EXCLUDES",
    "MAX_COMMAND_CHARS",
    "MEDIUM_COMMANDS",
    "READ_ONLY_COMMANDS",
    "SHELLS",
    "SUDO_COMMANDS",
    "Checker",
    "Decision",
    "PolicyContext",
    "ScopedPath",
    "always_denied",
    "check_command",
    "check_read_path",
    "check_write_path",
    "command_scope_paths",
    "resolve",
    "scope_violation",
    "sensitive",
    "split_pipeline",
    "within",
    "within_boundary",
]
