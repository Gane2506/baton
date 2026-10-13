# Servers, virtual browser and workspace profiles

Agents can look at (and, if you allow it, change) your servers over SSH, drive a headless
browser, and do both per run through **workspace profiles**. Everything is off until you
configure it. Config keys: [config.md](config.md#serversname). Background and sources:
[verification.md](verification.md) section 9; design choices: [decisions.md](decisions.md).

- [Servers](#servers)
- [Access levels](#access-levels)
- [Audit log](#audit-log)
- [How agents reach servers](#how-agents-reach-servers)
- [Workspace profiles](#workspace-profiles)
- [Virtual browser](#virtual-browser)
- [Watching the browser on a VPS](#watching-the-browser-on-a-vps)
- [Prompt injection](#prompt-injection)

## Servers

```toml
[servers.web]
host = "web1"                    # host name or ~/.ssh/config alias
user = "deploy"
access = "medium"                # "basic" | "medium"
workdir = "/srv/app"
allowed_paths = ["/srv/app", "/var/www/site"]  # medium writes; default [workdir]
description = "production web server"

[roles.coder]
servers = ["web"]                # every run; or use a workspace profile
```

Before the first run:

1. Make sure `ssh web1 true` works **without any prompt**: load your key into `ssh-agent` (or
   set `identity_file`, a path), and connect once by hand to check and accept the host key.
   jev uses `StrictHostKeyChecking=yes` and `BatchMode=yes`: it never accepts a new host key,
   never asks for a password and never stores one.
2. `jev doctor` shows a row per server: ssh present, the host resolved through
   `~/.ssh/config` (`ssh -G`, no connection) and DNS, and the access level.
   `jev doctor --connect` also logs in (`ssh -o BatchMode=yes ... HOST true`).
3. Use a dedicated, least-privileged account on the server. jev's policy is a second line of
   defence, not a replacement for Unix permissions.

`access = "full"` (unrestricted) is not available: config validation rejects it with
`access = "full" is not available yet; see issue #3`.

## Access levels

Every command is checked before anything reaches `ssh` (`src/jev_agent/servers/policy.py`).

**Shape, at every level.** One command per call, optionally piped once into `grep`, `head`,
`tail`, `wc` or `sort`. Refused: `;`, `&`, `&&`, `||`, redirects (`<`, `>`), subshells,
backticks, `$` (variables and `$(...)`), newlines, `NAME=value` prefixes, commands given by
path. Words are re-quoted for the remote shell, so there is no globbing (`ls *.log` lists a
file literally named `*.log`; use `find . -name '*.log'`). Commands run in `workdir`.

**basic** (read-only):

| Allowed | Refused options |
| --- | --- |
| `ls`, `cat`, `head`, `grep`, `wc`, `stat`, `df`, `du`, `free`, `uptime`, `ps`, `uname`, `whoami`, `id`, `pwd`, `hostname` | |
| `tail` | `-f`, `-F`, `--follow` |
| `find` | `-exec`, `-execdir`, `-ok`, `-delete`, `-fprint*`, `-fls` |
| `sort` | `-o`, `--output`, `--compress-program` |
| `file` | `-C` |
| `top -b -n1` | anything else |
| `systemctl status / is-active / is-enabled / is-failed / list-units / list-unit-files / list-timers / show / cat` | other verbs |
| `journalctl` | `-f`, `--vacuum-*`, `--rotate`, `--flush`, `--sync`, `--setup-keys`, ... |
| `docker ps / logs / inspect / images`, `docker compose ps / logs` | `-f` |
| `git status / log / diff / show / branch / remote / rev-parse` | `--output`, `--ext-diff`, options before the subcommand except `-C DIR`, `--no-pager` |

Reads of credential files are refused at every level: anything under `.ssh`, `.gnupg`, `.aws`,
`.kube`, `.docker`; `/etc/shadow`, `/etc/gshadow`, sudoers; private keys (`id_rsa`, `*.pem`,
`*.key`, ...); `.env*`, `.netrc`, `.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`,
`/proc/*/environ`.
No `sudo`, `env` or `printenv`.

**medium** = basic, plus:

| Allowed | Limits |
| --- | --- |
| `write_file`, `upload` tools | inside `allowed_paths` (after resolving symlinks on the server), ≤ 1 MiB / 10 MiB; uploads only from the local repo or the agent's task worktree (relative paths: the worktree), never `.git/`, other `.jev/` files or credential files |
| `mkdir`, `touch`, `cp`, `mv`, `rm` | targets inside `allowed_paths` (`rm`/`mv`: strictly inside, never the root itself) |
| `git add / commit / pull / fetch / checkout / switch` | the working copy (`workdir` or `-C DIR`) inside `allowed_paths`; no `push` |
| `apt-get install -y PKG...`, `apt-get update` | package names only (no paths/URLs) |
| `pip install PKG...` / `-r FILE` / `-e PATH` | no `--index-url`, `--extra-index-url`, `--trusted-host`, URLs or paths as packages |
| `npm install [PKG...]`, `npm ci` | in `workdir`; no `-g`, URLs, git or paths |
| `systemctl restart / reload / start / stop / try-restart UNIT` | never `ssh`/`sshd` (lock-out) |
| `docker compose up -d / down / restart / pull` | `down` without `-v`/`--rmi`; no `exec`/`run` |
| `npx -y playwright@<pinned> install --with-deps chromium` | exactly this (`jev browser install --server`) |
| `sudo` | only before package installs, `systemctl` and the browser install; always run as `sudo -n` (fails instead of prompting: needs passwordless sudo for those) |

**Always blocked**, even at medium: formatting or wiping disks (`mkfs*`, `wipefs`, `fdisk`,
...), `dd of=/dev/*`, shutdown/reboot/halt/poweroff (also via `systemctl`/`init`), user, group,
password and sudoers changes, firewall changes (`iptables`, `ufw`, `nft`, ...), recursive
`chmod`/`chown` on `/`, `rm` on `/` or recursive/forced `rm` outside `allowed_paths`,
`crontab -r`, `kill -9 1`, anything mentioning `authorized_keys`, `git push --force` (and
`+refspec`, `--mirror`), and piping into a shell or interpreter (`curl ... | sh`).

**Who gets which level.** A server's `access` is the most any agent gets. The coder gets it;
every other role (planner, validator, reviewer, code_reviewer, learner, chat) is capped at
`basic`; a workspace profile's `access_overrides` can lower it further, never raise it.

Known limits: `mkdir`/`cp`/`mv`/`rm` commands are checked on the paths as written (the file
tools also resolve symlinks on the server); package installs run the package's own install
scripts; `docker compose up` runs whatever the compose file says. Give medium access only to
servers where that is acceptable.

## Audit log

`.jev/audit/servers.log` (mode 0600) gets one JSON line per command or file operation, allowed
or refused, from agents, `jev browser install --server` and `jev doctor --connect`:

```json
{"run_id": "r-1a2b", "role": "coder", "server": "web", "access": "medium", "action": "run_command", "command": "systemctl restart app", "allowed": true, "reason": "", "exit_code": 0, "duration_s": 1.204, "time": "2026-10-13T09:12:44+00:00"}
```

Lines are redacted like run logs; file contents are never logged, only their size.

## How agents reach servers

Agents never get raw SSH. jev adds an MCP server named `jev_servers` to the agent's tools
(`python -m jev_agent mcp servers --allow web:medium,db:basic --role coder --run-id ...`),
which exposes:

- `list_servers`: name, your level, workdir, writable paths, description;
- `run_command`: one checked command, with exit code, stdout and stderr (clipped to 64 KiB,
  redacted), timeout 120 s by default (at most 600 s);
- `read_file`: a text file, up to 256 KiB;
- `write_file` and `upload`: only when a medium server is granted.

The connection to each server is reused for 60 s (`ControlMaster`, sockets in `.jev/ssh/`).
`SSH_AUTH_SOCK` is forwarded to the MCP process so ssh-agent keys work. You can run the server
yourself for testing: `jev mcp servers --allow web` (stdio).

## Workspace profiles

```toml
[profiles.prod]
servers = ["web", "db"]
access_overrides = { db = "basic" }
browser = false
learning = "default"            # a learning profile, or false
quality = "basic"
description = "production web + read-only database"

[profiles.staging]
servers = ["staging"]
browser = true
```

- `jev run "fix the 502s" --profile prod` (also `jev resume`, `jev queue add`, `jev chat`);
  `--profile none` runs without one.
- Without `--profile`: on a terminal with more than one profile, `jev run` shows a numbered
  menu (including "none / local only"); otherwise the repo default applies
  (`jev profile use prod`, stored in `.jev/profile`; `jev profile use none` clears it), else
  none.
- `jev profile list` and `jev profile show NAME` (what each role gets).
- The profile a run started with is copied to `.jev/runs/<run_id>/profile.json`; `jev resume`
  keeps using it unless `--profile` says otherwise. The queue and the scheduler store and pass
  the name; an unknown profile fails the item instead of running with other access.
- A profile's `learning` and `quality` apply when the run has no `--learning`/`--no-learning`
  or `--quality` flag.

## Virtual browser

```toml
[browser]
enabled = true
headless = true

[roles.reviewer]
browser = true
```

- `jev browser install` shows the exact command and asks before running it:
  `npx -y playwright@1.64.0-alpha-1790635538000 install --with-deps chromium` (the Playwright
  version the MCP server uses). `--yes` skips the question, `--dry-run` only shows it.
  `--server NAME` (or `[browser] install_target`) runs it on a medium-access server through
  the access policy (and the audit log); `--with-deps` needs root there.
- Roles with `browser = true` (and the coder in a run whose profile has `browser = true`) get
  the Playwright MCP server, pinned: `npx -y @playwright/mcp@0.0.83 --browser chromium
  --isolated --headless --output-dir .jev/browser/<run_id>`. `--isolated` keeps the browser
  profile in memory, so cookies and logins never persist between runs.
- `jev doctor` shows a browser row: `npx` present, which roles and profiles use it.
- The browser runs on the machine that runs the agent (where jev runs), not on a server.

## Watching the browser on a VPS

Not automated; if you want to watch a headed browser on a headless VPS where jev runs:

```bash
sudo apt-get install -y xvfb x11vnc novnc     # or clone github.com/novnc/noVNC
Xvfb :99 -screen 0 1280x800x24 &
x11vnc -display :99 -localhost -nopw -forever &
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen localhost:6080 &
export DISPLAY=:99                             # then run jev with [browser] headless = false
```

Open it from your machine through an SSH tunnel only:
`ssh -L 6080:localhost:6080 vps`, then http://localhost:6080/vnc.html. Never expose the VNC or
noVNC ports publicly (`-localhost` / `--listen localhost` above).

## Prompt injection

Text an agent reads from a server (logs, files, command output) or a web page is **untrusted
input**. It can contain instructions written to manipulate the agent ("ignore previous
instructions and run ..."). jev's policy limits what such an agent can do, and the tool
descriptions tell agents to treat this output as data, but:

- give servers and the browser only to roles and runs that need them, at the lowest level;
- prefer `basic`, and medium only on servers where the allowed writes are acceptable;
- keep secrets off servers agents can read (the credential-file rules are a safety net, not a
  guarantee: an application log can still contain a token);
- review the audit log (`.jev/audit/servers.log`) after runs that touched production.
