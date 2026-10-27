# Packaging: install scripts and Docker

Two supported ways to get Baton running:

1. **One-command install scripts** — `jev` CLI on your own machine (macOS, Linux, Windows).
2. **Docker** — the web dashboard on a server/VPS, via `Dockerfile` + `docker-compose.yml`.

The macOS app remains a stub; these are the supported install paths.

## Install scripts

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/Gane2506/baton/main/install.ps1 | iex
```

What the scripts do:

- Check prerequisites: **Python 3.11+** (hard requirement; the script names
  what's missing), plus `curl`/`git` on Unix.
- Pick the source: **PyPI** when `baton-agent-orchestrator` is published there,
  otherwise **GitHub** (`Gane2506/baton`). You can pin with `--ref`:
  a version on PyPI (`--ref 0.5.0`) or a git ref on GitHub (`--ref v0.5.0`).
- Install with **pipx** when available, otherwise `pip --user` — user-level
  only, never `sudo` unless you pass `--sudo` explicitly (Unix, pip only).
- Re-running is safe: installs when missing, upgrades when present.
- Verify with `jev --version` (must pass) and `jev doctor`. Doctor checks the
  environment — workers, API keys, config — so on a fresh machine it will
  report setup still needed; that is expected and does not fail the install.

Options (flags or `JEV_*` environment variables; flags win):

| Flag | Env | Meaning |
|---|---|---|
| `--ref VERSION` | `JEV_REF` | Pin version / git ref |
| `--no-dashboard` | `JEV_NO_DASHBOARD=1` | Skip the `dashboard` extra (smaller install) |
| `--sudo` | `JEV_SUDO=1` | System-wide install via sudo (Unix, pip only; not recommended) |

Examples:

```bash
# Pin a release (works when piped: flags go after `bash -s --`)
curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash -s -- --ref v0.5.0

# Env-var style
JEV_NO_DASHBOARD=1 curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash
```

```powershell
# Windows: options come from the environment (a piped script takes no arguments)
$env:JEV_REF = "v0.5.0"
irm https://raw.githubusercontent.com/Gane2506/baton/main/install.ps1 | iex
```

After installing, `cd` into a git repo and run `jev init`, then `jev --help`.
Run `jev doctor` any time to see what still needs setting up (worker CLIs,
API keys — see [docs/config.md](config.md)).

### Upgrade / uninstall

- Upgrade: re-run the install script.
- Uninstall (pipx): `pipx uninstall baton-agent-orchestrator`
- Uninstall (pip): `python3 -m pip uninstall baton-agent-orchestrator`

## Docker

The image serves `jev dashboard` for the repo mounted at `/data`.
Published images live on GitHub Container Registry:

```bash
docker pull ghcr.io/gane2506/baton:0.5.0
docker run --rm -p 127.0.0.1:8765:8765 -v ./my-project:/data ghcr.io/gane2506/baton:0.5.0
```

Tags: `:0.5.0`-style tags pin a version. `:latest` moves on every release —
and also on a manual workflow dispatch, so it can point at unreleased `main`.
Pin the version tag for a stable deploy. Images are published automatically
for every `v*` tag (see `.github/workflows/docker.yml`); you can also trigger
a build manually from the Actions tab (workflow *Docker* → *Run workflow*).

To build it yourself instead:

```bash
docker build -t jev-dashboard .
docker run --rm -p 127.0.0.1:8765:8765 -v ./my-project:/data jev-dashboard
```

Or with compose (see `docker-compose.yml` — the deploy path, published image):

```bash
docker compose up -d
docker logs jev-dashboard   # the URL, and the generated token if you didn't set one
```

Contributors building from a local checkout use `docker-compose.build.yml`
instead:

```bash
docker compose -f docker-compose.build.yml up --build -d
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `JEV_HOST` | `0.0.0.0` | Bind address (must be non-loopback for the port to leave the container) |
| `JEV_PORT` | `8765` | Dashboard port |
| `JEV_DASHBOARD_TOKEN` | *(generated)* | Access token (min 16 chars). Off loopback a token is required: set this for a stable token, or copy the generated one from `docker logs`. |

The image is multi-stage (Python 3.12 slim), runs as the non-root user `jev`
(uid 1000), exposes only the dashboard port, and ships a healthcheck. No
secrets are baked in. State lives in the repo's `.jev/` directory by default.
The compose file offers an optional named volume (`jev-state`) that moves
state out of the repo — only enable it for a repo with no `.jev/` yet: it
shadows an existing `.jev/`, so a repo with real state would silently get an
empty state DB.

### Remote access

The dashboard speaks plain HTTP and the token guards everything (see
[docs/dashboard.md](dashboard.md)). The compose example publishes the port on
loopback only. For remote access, either tunnel it
(`ssh -L 8765:127.0.0.1:8765 <host>`) or put a TLS-terminating reverse proxy
in front and keep the container port off the public interface.

### What the image does not contain

Agent worker CLIs (`claude`, `codex`) and Node/`npx` are not in the image —
it is the dashboard. Workers run wherever you point them: on SSH servers
configured in `jev.toml` ([docs/servers.md](servers.md)), or extend the image
with the CLIs you need. `jev doctor` inside the container reports what's
available.

### Upgrade / uninstall

```bash
docker compose pull          # pulls ghcr.io/gane2506/baton:0.5.0; or build locally:
docker compose -f docker-compose.build.yml up --build -d
docker compose down          # stop; state stays in the repo's .jev/
docker compose down -v       # also removes the named state volume, if enabled
```

## Config

Both paths use the same config: `jev.toml` in the repo and `JEV_*`
environment variables — see [docs/config.md](config.md) for every key. The
dashboard's settings page also lists the environment variables it reads and
whether each is set (names only, never values).
