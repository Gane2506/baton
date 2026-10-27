# Deploy: one command per platform

Baton deploys the same way everywhere: install `jev`, point it at a git repo,
open the dashboard. The three commands below are the whole story — the Mac
dashboard is a normal case of it, not a special case. What differs per
platform is only the reboot mechanism.

## The commands

### Linux server — Docker

```bash
docker run -d --name jev --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -e JEV_DASHBOARD_TOKEN="$(openssl rand -hex 24)" \
  -v /srv/myproject:/data \
  ghcr.io/gane2506/baton:0.5.0
docker logs jev   # the dashboard URL and access token
```

Prerequisites:

- Docker Engine, daemon running. Nothing in the repo installs Docker.
- A git repo at the mount path — run `jev init` in it first. The dashboard
  refuses to start when `/data` is not inside a git repo, and an empty
  volume is not a repo.
- A long random token (at least 16 characters). The container always binds
  `0.0.0.0`, so it is always in the token case (see [Tokens](#tokens)).
- Port 8765 free on loopback, or change the `-p` mapping.

Reboot survival is `--restart unless-stopped`: Docker restarts the container
when the daemon starts after a reboot. Prefer `docker compose`? The
`docker-compose.yml` in the repo is the same deploy (published image
`ghcr.io/gane2506/baton:0.5.0`, `restart: unless-stopped`, loopback publish);
`docker compose up -d`, then `docker logs jev-dashboard`.

The image runs as the non-root user `jev` (uid 1000): the bind-mounted repo
must be writable by that uid or the state database cannot be created.

### macOS — native

```bash
curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash
cd ~/code/<your-repo> && jev init && jev dashboard
```

Prerequisites:

- Python 3.11+ on `PATH`; `pipx` or `pip`; `curl`, `git`.
- Node.js + `npx` and the `claude`/`codex` CLIs for workers and for the
  browser install. The installer does not install them — install them the
  way you normally would (Homebrew, nvm, npm). The dashboard finds Homebrew
  and nvm locations on its own at startup, without a login shell, and hands
  them to the workers it spawns — there is no PATH ritual to run first.

Reboot survival is a second, one-time command:

```bash
jev dashboard --install-service   # launchd agent, starts at login
```

It records the absolute path to the `jev` binary found at install time and
the worker CLIs found then, so the service never depends on the service
manager's minimal PATH. It serves the repo you pass (`--repo`, or the current
directory), refuses to serve your home directory, and is idempotent —
installing twice rewrites the same definition. Remove it with
`jev dashboard --uninstall-service`. The installer offers this step at the
end when run inside a git repo, with your yes, never silently.

Docker Desktop is the alternate on macOS, with the same `docker run` command
as Linux — but it is dashboard-only (see below), so it is not a substitute
for fixing the native path.

### Windows — native

```powershell
irm https://raw.githubusercontent.com/Gane2506/baton/main/install.ps1 | iex
cd <your-repo>; jev init; jev dashboard
```

Prerequisites:

- Python 3.11+ findable as `py -3.11`, `py -3`, or `python`;
  PowerShell 5.1+; `pipx` or `pip`; `git`.
- Node.js + `npx` and the `claude`/`codex` CLIs for workers. The installer
  does not install them. The dashboard finds the usual Windows Node/npm/nvm
  locations on its own at startup.

Reboot survival is a second, one-time command:

```powershell
jev dashboard --install-service   # Scheduled Task at logon
```

Same contract as macOS: absolute `jev` path, records the repo, idempotent,
`jev dashboard --uninstall-service` removes it. One honest limit: a
Scheduled Task has no stop-timeout setting, so stopping the task ends the
dashboard at once and running jobs lose their graceful shutdown (their state
is saved and resumable). Docker Desktop is the alternate, with the same
`docker run` command as Linux.

The Windows install + boot path is proven by CI (`windows-latest`: install,
`jev init` in a temp repo, start the dashboard, `curl` it, stop it).

## Reboot survival, per platform

| Platform | Mechanism | Definition |
| --- | --- | --- |
| Linux (Docker) | `--restart unless-stopped` | the `docker run` / compose file |
| Linux (native) | systemd user unit (`Restart=always`) | `~/.config/systemd/user/jev-dashboard.service` |
| macOS (native) | launchd agent (`RunAtLoad`, `KeepAlive`) | `~/Library/LaunchAgents/com.baton.jev-dashboard.plist` |
| Windows (native) | Scheduled Task at logon | definition kept at `~/.jev/jev-dashboard-task.xml` |

All four are installed by the same flag, `jev dashboard --install-service`
(Docker's is the `--restart` flag on the one command). The native service
gives the dashboard 120 s to stop (launchd `ExitTimeOut`, systemd
`TimeoutStopSec`) because shutdown waits up to 30 s per job. The service
runs as you, not as root or SYSTEM, because the CSRF secret and `secrets.env`
live in `~/.jev` and must stay readable by the service user. Logs for the
macOS agent are `~/.jev/dashboard-service.out.log` and
`~/.jev/dashboard-service.err.log`.

## Tokens

- On loopback (`127.0.0.1`, the default) no token is needed. Anything else
  needs one: pass `--token` (at least 16 characters) or set
  `JEV_DASHBOARD_TOKEN`. Off loopback with no token, `jev` generates one at
  startup and prints the URL carrying it — usable, but it changes on every
  restart, so a stable token is part of the one command.
- In Docker the bind is always `0.0.0.0`, so the container is always in the
  token case even though the port is published on loopback only. Always set
  `JEV_DASHBOARD_TOKEN` (the one command does); the generated token lives
  only in `docker logs`.
- The dashboard speaks plain HTTP. For remote access, tunnel it
  (`ssh -L 8765:127.0.0.1:8765 <host>`) or put a TLS-terminating reverse
  proxy in front and keep the container port off the public interface.

## What the image does not contain

Agent worker CLIs (`claude`, `codex`) and Node/`npx` are not in the image —
it is the dashboard. Consequences: the browser-install button cannot work in
the container (`npx` absent), local `claude`/`codex` workers cannot run, and
`jev doctor` inside the container reports them missing. Workers run wherever
you point them: on SSH servers configured in `jev.toml`
([docs/servers.md](servers.md)), or extend the image with the CLIs you need.
Shipping a second, worker-capable image is an open product decision, not
something this doc settles.

Tags: `:0.5.0`-style tags pin a version. `:latest` moves on every release —
and also on a manual workflow dispatch, so it can point at unreleased `main`.
Pin the version tag for a stable deploy. Images are published automatically
for every `v*` tag (`.github/workflows/docker.yml`).

The healthcheck probes with the real `JEV_DASHBOARD_TOKEN` when one is set, and falls back to a TCP connect probe when no token is set (the generated token lives only in `docker logs`, so the probe cannot know it).

## Upgrade / uninstall

### Native (macOS, Linux, Windows)

- Upgrade: re-run the install script. It installs when missing, upgrades
  when present. **Your state is never deleted on upgrade.** The state
  database (`<repo>/.jev/state.db` — runs, queue, lessons, spend history)
  survives every update: migrations are forward-only, and `jev doctor`
  reports its schema version and integrity read-only. If a database was
  written by a newer `jev` than the one opening it, the dashboard says
  "upgrade jev to open it" — do not delete `state.db`.
- After upgrading, restart the dashboard so the new code is what serves.
  Manually started: `jev dashboard stop --repo <repo>`, then start it
  again. Service-managed: macOS — reinstalling the service restarts it
  (`jev dashboard --install-service` unloads then loads the agent); Linux
  — `systemctl --user restart jev-dashboard.service`; Windows —
  `schtasks /end /tn jev-dashboard`, then `schtasks /run /tn jev-dashboard`.
  (`jev dashboard stop` alone does not stop a service-managed dashboard:
  the service manager restarts it.)
- Uninstall the service first if you are removing Baton:
  `jev dashboard --uninstall-service`.
- Uninstall the package (pipx): `pipx uninstall baton-agent-orchestrator`;
  (pip): `python3 -m pip uninstall baton-agent-orchestrator`.

### Docker

```bash
docker compose pull          # pulls ghcr.io/gane2506/baton:0.5.0
docker compose up -d         # or: docker stop jev && docker rm jev, then the one command again
docker compose down          # stop; state stays in the repo's .jev/
docker compose down -v       # also removes the named state volume, if enabled
```

State lives in the repo's `.jev/` directory by default. The compose file
offers an optional named volume (`jev-state`) that moves state out of the
repo — only enable it for a repo with no `.jev/` yet: it shadows an existing
`.jev/`, so a repo with real state would silently get an empty state DB.

## Troubleshooting

- **Port already in use.** Starting a second dashboard on the same port
  names the running one — its pid, start time and full command — and points
  at `jev dashboard stop --repo <repo>` instead of sending you to `lsof`.
  `jev dashboard stop` finds the dashboard through
  `<repo>/.jev/dashboard.pid` and stops it; a stale pid file (crashed
  dashboard) never names a dead process as the owner. When there is no usable
  pid file, `stop` probes the port instead (`--port`, default 8765): a
  dashboard listening there is identified and stopped only when it serves
  this repo; anything else on the port is reported, never killed. (A
  dashboard bound off loopback with a token answers the probe with 401, so
  it is reported, not stopped — stop that one through its pid file.
  Likewise, a dashboard bound only to a non-loopback address is not found
  by the port probe.)
  `stop` waits until the
  port is bindable again before reporting success (bounded: 5 s), so a start
  right after a stop works first time; if the port stays busy it fails with a
  port-busy error instead of claiming success.
- **A worker CLI is reported missing.** `jev doctor` names what it could not
  find. The dashboard resolves `npx`, `node`, `claude`, `codex`, `gh`, `ssh`
  and `git` through well-known install locations (Homebrew and nvm on macOS,
  npm/nodejs/nvm on Windows, `/usr/local/bin` and nvm on Linux) even when
  `PATH` is minimal — but two locations are not covered yet on macOS:
  `~/.local/bin` and npm's global bin `~/.npm-global/bin` (issue #350). If
  your CLI lives there, put that directory on `PATH` for now.
- **The dashboard will not start after an update.** Do not delete
  `state.db`. Run `jev doctor`: its state-database row names the condition
  (a newer-schema database means upgrade `jev`; a failed integrity check
  names the recovery). If the dashboard was running through the old code,
  stop it first — a long-running process serving stale code is what the old
  "delete the database" ritual was papering over, and `stop` + restart is
  the fix.
- **Docker container exits at once.** It is almost always the repo mount:
  `/data` must be a git repo (`jev init` in it first). Check
  `docker logs jev` — the error names the path.

## Install-script details

`install.sh` / `install.ps1` (see [docs/packaging.md](packaging.md) for the
full option table) probe PyPI for `baton-agent-orchestrator` first and fall
back to `git+https://github.com/Gane2506/baton` — the package is not on PyPI
yet, so the GitHub path is the live one. Pin a version with `--ref v0.5.0`
(Unix; Windows takes it from `$env:JEV_REF`). They install with `pipx` when
present else `pip --user`, with the `dashboard` extra unless
`--no-dashboard`, then verify `jev --version` and run `jev doctor`. If `jev`
is not on `PATH` yet in that shell they print the exact `export PATH=…` line
instead of failing. The Python floor is 3.11 everywhere: installer, package
(`pyproject.toml`), and CI (3.11–3.13); the Docker image is Python 3.12.
