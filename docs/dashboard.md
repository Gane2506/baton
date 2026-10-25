# Web dashboard

`jev dashboard` serves a small web UI for one repository. It reads the same `.jev/state.db`,
lesson profiles, chat folders and run logs as the CLI, and its buttons call the same functions
as the CLI commands, so anything you do in the browser is exactly what the matching `jev`
command would do.

## Install

The dashboard is an optional extra (Starlette, uvicorn, python-multipart and markdown-it-py),
so the CLI stays small when you do not need it:

```bash
uv tool install 'baton-agent-orchestrator[dashboard] @ git+https://github.com/Gane2506/baton'
# once the package is on PyPI:
uv tool install 'baton-agent-orchestrator[dashboard]'
```

Without the extra, `jev dashboard` exits with that install hint. In a clone of this repo,
`uv sync --all-extras` installs it.

## Running it

```bash
jev dashboard                       # http://127.0.0.1:8765/ and opens a browser
jev dashboard --no-open --port 9000 # another port, no browser
jev dashboard --repo ../other-repo  # any path inside the target git repo
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo PATH` | `.` | A path inside the git repo to show. |
| `--host HOST` | `127.0.0.1` | Address to listen on. Anything but loopback needs a token (below). |
| `--port PORT` | `8765` | Port. |
| `--open / --no-open` | `--open` | Open the URL in a browser after start. |
| `--token TOKEN` | generated off loopback | Access token, at least 16 characters. Also read from `JEV_DASHBOARD_TOKEN`. |

Ctrl-C stops the server. Before it exits, it cancels every approve or resume job it started
and waits for it (up to 30 s per job): the job's agent process is stopped, like Ctrl-C on
`jev approve`, and the run keeps its saved state; continue it with `jev resume <run>` (or the
Resume button). A Ship job cannot be cancelled mid-step; shutdown waits for it.

## Pages

Press **Cmd+K** (or Ctrl+K) anywhere for the command palette: fuzzy search over every page,
page section and whitelisted action, plus the terminal-only commands with the exact command
to paste. It is the fastest way to check whether a `jev` command has a dashboard home.

![The Board page](images/dashboard-board.png)

- **Board** (`/`): the `jev board` columns (Backlog, Planning, Awaiting approval, Running, Done,
  Blocked). Cards show the idea, run id, current task and its state, spend, age and the quality
  gate status. Refreshes every few seconds. `?all=1` shows every done run.
- **Run** (`/runs/<id>`): idea, plan summary and risks, validation verdict, tasks, the decision
  timeline (point, dispatcher and model, answers with Jev's confidences, fallback reason, cost,
  latency), attempts (role, worker, model, effort, status, cost, lessons injected, log link),
  learning outcome, the quality review (`review.md`, rendered as sanitized HTML), the result
  branch name, and every file under `.jev/runs/<id>/` (served read-only as plain text).
  Buttons:
  - **Approve and run** for a run awaiting approval (`jev approve`).
  - **Resume** for an interrupted run or a NEEDS_HUMAN run with a resume point (`jev resume`).
    When another jev process may still be running the run, the page shows **Force resume**
    instead, with the reason, and a plain resume is refused: resuming would start a second
    pipeline on the same worktrees and double the spend. The signs are an attempt without a
    result that started less than its worker's `timeout_s` plus 5 minutes ago (an older one is
    from a process that died), or a live scheduler lock whose current queue item started this
    run, or a live run lease held by another process (see below). Force resume asks for
    confirmation; use it only when that process is gone. It cannot override a live lease: the
    job then fails with "run ... is in use".
  - **Cancel** for any unfinished run (`jev cancel`; branches and worktrees are kept).
  - **Ship as PR** for a DONE, not yet shipped run when `[ship] enabled = true` (`jev ship`,
    [ship.md](ship.md)): a confirmation page shows the remote, branch, base, title and the
    rendered PR body; confirming pushes and opens the PR in the background, and the run page
    then links it.

  Approve and resume run in a background thread through the same code path as the CLI
  (config, workers and pipeline are built the same way), so the web server stays responsive;
  the page shows the job's status and the run's progress live. Only one job per run id runs at
  a time; a second click is refused. Across processes, the run lease in the state DB
  (issue #61) keeps a terminal `jev resume`, `jev approve` or the scheduler from driving the
  same run at the same time: whichever starts second fails with "run ... is in use by another
  jev process".
- **Queue** (`/queue`): items in scheduler order with their effective status and flags; add an
  idea (priority, plan only, learning profile or none, quality level, workspace profile name),
  move an item to the top, up or down, remove it, or clear the whole queue (asks for
  confirmation first; items with a run are kept). The scheduler strip at the top shows whether
  a scheduler is running, whose it is (the dashboard's own, or a terminal's), what item it is
  working on, queue counts and spend today. **Start scheduler** asks for confirmation (it spends
  real money) and starts the dashboard's own scheduler, which stops when the dashboard stops;
  **Stop scheduler** finishes the current item first. A scheduler started in a terminal
  (`jev scheduler start`) keeps running independently and can only be stopped there.
- **Learning** (`/learning`): the repo switch and active profile, profiles (scope, enabled,
  active, lesson count) and each profile's lessons. Actions: create (repo or shared), use,
  enable / disable (the repo or one profile), forget a lesson, flush a profile (asks for
  confirmation on a separate page first), and delete a profile with all its lessons (also a
  confirmation page). Below the profiles, the former Laya tuning section is removed (issue #193): there is no
  weight-tuning UI any more. Code grading and routing now belong to the Coding Decision
  Agent (see [code-orchestrator.md](code-orchestrator.md)).
- **Profiles** (`/profiles`): the workspace profiles (`jev profile list` / `show` / `use`):
  servers, browser access, learning and quality per profile, which one is the repo default,
  what each role gets from the selected profile, and **Make default** / **Clear default**.
- **Diagnostics** (`/diagnostics`): the `jev doctor` checks (local checks only — no SSH) as a
  read-only matrix. Key values are never shown, only whether each one is set.
- **Chats** (`/chats`): saved `jev chat` sessions and their transcripts, read-only.
- **Spend** (`/spend`): spend per day for the last 30 days and the costliest runs, as
  server-rendered SVG bar charts, plus every run's spend.
- **KPIs** (`/kpis`, `?last=N`, default 20): PRs opened by `jev ship` with their KPIs (cost,
  run time, attempts per task, first-pass rate, findings, CI, time to merge), summary cards
  (median cost per PR, median time to merge, first-pass rate, fallback rate, merged share),
  regressions in the latest PR, and one server-rendered SVG trend chart per KPI with its
  rolling median and red regression points. Without shipped PRs it explains `jev ship`. See
  [ship.md](ship.md). **Sync from GitHub** runs `jev ship sync --all` (refreshing every PR's
  state, reviews and CI) and the page shows how long ago the last sync ran, so you can tell
  how fresh the numbers are.
- **Tools** (`/tools`): the MCP servers, skills and plugins editor (as before), plus four
  reference sections: the **Models** browser (`jev models list` — a filterable table with
  context length, prices and json_schema/reasoning support, and a copy button per model id;
  read from the on-disk cache, never fetched by the page), the **Cache** section
  (`jev cache stats` — entries, hits, dollars saved, size and shadow agreement, with the
  semantic mode and thresholds; **Clear cache** asks for confirmation naming what goes, and
  **Run calibration** runs `jev cache calibrate` and offers one-click apply of the candidate
  threshold), the **Quality checks** matrix
  (`jev quality checks` — every check by level, whether its tool is installed and whether it
  applies to this repo), and the **MCP servers (CLI only)** note documenting `jev mcp servers`
  and `jev mcp runs`.
- **Connections** (`/connections`): servers, the decision route and key health (as before),
  plus the **Browser** panel: the doctor's browser check and the **Install browser** button,
  which asks for confirmation naming the exact command, then runs the install in the
  background (one at a time; stoppable; the output tail shows on the page). Installing on a
  server stays in the terminal (`jev browser install --server NAME`). The **Providers**
  section covers the `claude` / `codex` CLI workers' own logins: each card shows which
  auth is active — Claude/ChatGPT subscription login (OAuth) or API key — with the
  subscription flow as the primary path (paste a `claude setup-token` token for Claude
  Code; device code flow for Codex) and API-key entry as fallback.
- **Settings** (`/settings`): a list of sections on the left (sticky on wide windows) and an
  editor for common settings in this repository's `jev.toml`: `[dispatcher] primary`, the
  budgets (`per_run_usd`, `per_task_usd`, `max_task_attempts`), `[approval]
  require_plan_approval`, `[review] enabled`, `[quality] level`, `[ship] enabled` and
  `after_approval`, `[learning] enabled` and `profile`, `[models] prefer_free`, and each
  role's `workers` and `models`. Each field shows where its value comes from (default, user
  config or `jev.toml`); **reset** removes the key from `jev.toml`, so the user config or the
  default applies again. **Review changes** checks the new file exactly as `jev` loads it
  (schema, your user config, trust) and shows a unified diff; nothing is written until you
  confirm, and an invalid value comes back on its field with nothing written. Confirming
  writes `jev.toml` atomically, keeping its comments and layout, and only if the file has not
  changed since the diff was shown (otherwise you get the new diff). The server rebuilds the
  file from these fields on every step: a request can never write a guarded key
  ([security.md](security.md)), `[trust]` or your user config. Below the editor, read-only: the
  effective config, the environment variables the config names with whether each is set (names
  only, never values), and the `jev doctor` checks (local checks only). At the bottom,
  **This dashboard**: version, listen address, access mode, start time and the live-region
  refresh interval.

Pages follow your OS light/dark setting and work on narrow screens. Nothing is loaded from
other origins: no CDN, no web fonts, no analytics. The scripts are small and vendored:
`static/live.js` (live regions and confirmation prompts), `static/app.js` (the shell's
collapsible panes and resize handles), `static/theme.js` (theme profiles),
`static/ui-style.js` (the UI style picker), and `static/conn.js` (the sidebar
connection-status slot); without JavaScript every page still works (reload to refresh).

## UI styles

The sidebar's **Style** picker (next to Theme) switches the dashboard between 16 UI styles:
Minimalism, Flat Design 2.0, Skeuomorphism, Neumorphism, Glassmorphism, Claymorphism,
**Bento Grid** (the default), Cards-Based UI, Split-Screen Design, Brutalism, Neo-Brutalism,
Bauhaus Style, Hand-Drawn, Cyberpunk, Y2K/Vaporwave and Pixel Art UI. Each is a full design
language (borders, shadows, radii, typography, density), not a palette swap, and all share
one mechanism: the server renders `data-ui-style` on `<html>` and `static/ui-styles.css`
keys every style off that attribute, orthogonal to the `data-theme` color profiles.

The choice persists server-side in your user config (`[dashboard] ui_style` in
`~/.config/jev/config.toml`), so it survives restarts and follows you across repos. A few
in the wild: [Brutalism](images/dashboard-style-brutalism.png),
[Cyberpunk](images/dashboard-style-cyberpunk.png),
[Skeuomorphism](images/dashboard-style-skeuomorphism.png),
[Y2K](images/dashboard-style-y2k.png).

| Run | Queue |
| --- | --- |
| ![Run detail](images/dashboard-run.png) | ![Queue](images/dashboard-queue.png) |
| **Learning** | **Spend** |
| ![Learning](images/dashboard-learning.png) | ![Spend](images/dashboard-spend.png) |
| **KPIs** | **Settings** |
| ![KPIs](images/dashboard-kpis.png) | ![Settings editor with its section list](images/dashboard-settings.png) |

The Board in dark mode: [images/dashboard-board-dark.png](images/dashboard-board-dark.png).

## CLI coverage and action tiers

Every `jev` command verb is reachable from the dashboard: as a page, a page section, a
palette action, or (for the commands below) a greyed palette entry with the pasteable
command. Full flag parity is explicitly out of scope: the web exposes the common path,
exotic flags stay in the terminal (the palette lists the important ones).

New web actions follow a tier policy, on top of the CSRF/same-origin/token rules below:

- **Tier 0 — read-only.** GET pages and fragments: the doctor report, the model list, the
  quality checks, scheduler status, profiles, the sync preview. Never renders secret values
  (presence booleans only).
- **Tier 1 — reversible or low-blast-radius.** POST + CSRF + same-origin (the `act()` path):
  queue add/move/remove, learning create/use/enable/disable/forget, profile use,
  `ship sync`, scheduler stop, browser install stop.
- **Tier 2 — destructive, irreversible, costly, or process-spawning.** POST + CSRF +
  **two-step confirm page**: the first POST renders a page naming exactly what will happen;
  only `confirm=yes` executes. Learning flush and delete, queue clear, run cancel and ship,
  browser install, and scheduler start (the confirm names the spend implication: a scheduler
  runs agent workers, which spend real money).
- **NEVER web-triggerable.** No route may serve MCP (`mcp servers` / `mcp runs` are
  stdio-bound; the `servers` variant exposes `run_command` / `write_file`), bind a new
  listener or mint tokens (`dashboard` serve options), print or accept a raw secret value,
  or run an arbitrary command: **there is no generic "run this jev command" endpoint.**
  Every web action gets its own route calling the same library code as the CLI (never a
  subprocess of `jev`).

### CLI-only commands

- **`jev mcp servers` / `jev mcp runs`.** Serve MCP over stdio to a local MCP client; a
  browser page cannot hold the other end of the pipe, and the `servers` variant exposes
  `run_command` / `read_file` / `write_file` — binding that to the web would massively widen
  the attack surface. Documented on the Tools page; never triggerable.
- **`jev dashboard`.** Self-referential: serving a dashboard from the dashboard is
  meaningless, and `--host` / `--port` / `--token` are process-launch concerns. The About
  panel (**This dashboard** at the bottom of Settings) covers the legitimate need — version,
  listen address, access mode, start time.
- **`jev init`.** The dashboard requires an initialized repo; by the time the UI exists,
  `init` has already run. A web wizard could clobber an existing `jev.toml`, so there is
  none — the palette shows the pasteable command instead.

### Adding a CLI command? Decide its dashboard home in the same PR

Every new CLI flag or subcommand needs a dashboard decision: either a dashboard home (page,
section, or palette entry) or a CLI-only justification added to the list above. The palette
registry (`palette_registry()` in `dashboard/helpers.py`) is the checklist — if the verb is not
in it, the PR is not done.

## Security

The dashboard can approve runs, which start agents that edit code and spend money, so treat
access to it like shell access to the repo.

- **Loopback by default.** It binds `127.0.0.1`. On loopback it also checks the `Host`
  header (only `127.0.0.1`, `localhost` and `::1` are accepted), so a malicious web page
  cannot reach it through DNS rebinding.
- **Token off loopback.** Any other `--host` (for example `0.0.0.0`) requires a token: pass
  `--token` / set `JEV_DASHBOARD_TOKEN`, or jev generates a random one. jev prints the URL with
  `?token=<token>` and a warning. Every request (pages, files, static assets) must carry the token,
  as the `jev_token` cookie, an `X-Jev-Token` header, or the `token` query parameter; opening
  the printed URL swaps the query parameter for an HttpOnly, SameSite=Strict cookie and
  redirects to a clean URL. You can also pass `--token` on loopback to require it there.
- **Plain HTTP.** The server speaks HTTP, not HTTPS. Off loopback, the token and everything
  you see cross the network unencrypted unless you put a TLS proxy in front.
- **CSRF.** Every action is a POST (GETs never change anything). Each POST must come from the
  same origin (`Sec-Fetch-Site`, `Origin` or `Referer` must match) and carry a per-session
  form token (an HMAC of the session cookie with a secret that changes on every start).
  Restarting the dashboard makes old forms stale; reload the page.
- **Escaping.** Templates autoescape everything: ideas, plans, model answers, lesson text,
  transcripts. The quality review is Markdown rendered with raw HTML disabled (tags are shown
  as text) and unsafe link targets (`javascript:` and similar) refused.
- **Headers.** A strict Content-Security-Policy (`default-src 'self'`, no inline script or
  style, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff`, `Cache-Control:
  no-store`.
- **Files.** Only regular files strictly inside `.jev/runs/<run_id>/` are served, as
  `text/plain`; `..`, absolute paths, encoded traversal and symlinks pointing out are refused.
  Run logs are already redacted when written.
- **No secrets.** The config cannot hold secrets (secret-looking keys are rejected); the
  Settings page lists environment variable names and whether they are set, never values.

### Adding API keys from the Connections page

`jev` reads API keys only from the process environment, but you never have to leave
the dashboard to set one. The Connections page's **Keys** section has an *Add key*
form: pick a variable (the dropdown suggests the names your config uses, e.g.
`OPENROUTER_API_KEY`) and paste the value. It is saved to `~/.jev/secrets.env`
(mode 600, directory 700) and takes effect immediately — no dashboard restart.
The file is loaded at startup by both the CLI and the dashboard; a real environment
variable with the same name always wins over the file. Keys saved this way show a
*Remove* button; keys that come from your real environment show as "Set
(environment)" and are managed outside jev. Values are never rendered, logged, or
echoed back anywhere.

### Connecting Claude Code and Codex with your subscription

The `claude_code` and `codex` workers shell out to the `claude` / `codex` CLIs, which
authenticate with your **subscription** (OAuth) or an API key — whichever the
environment offers. The Connections page's **Providers** section shows, per provider,
which auth is active (names only, never values):

- **Claude Code**: the primary path is your Claude subscription. On a machine with a
  browser run `claude setup-token`, approve the sign-in, and paste the one-year
  `sk-ant-oat01-…` token it prints into the card's form. It is saved to
  `~/.jev/secrets.env` like any other key and takes effect immediately. If the `claude`
  CLI is already signed in on the dashboard machine (`~/.claude/.credentials.json`),
  workers reuse that login and there is nothing to paste. Note: `[workers.claude_code]
  bare = true` needs `ANTHROPIC_API_KEY` — bare mode does not read the OAuth token.
- **Codex**: the primary path is your ChatGPT subscription via the device code flow.
  **Sign in with ChatGPT** spawns `codex login --device-auth` in the background; the
  card shows the link and the one-time code (expires in 15 minutes). Approve it in your
  browser and the CLI stores its own `~/.codex/auth.json` — jev never sees or stores
  the tokens. The page polls while you approve and re-renders when the sign-in
  completes (or fails); **Cancel sign-in** stops a pending flow.

API keys stay as fallback: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the Keys section
below. For Claude Code an API key outranks the subscription login (and is billed per
token); the card says which one is active. Workers inherit the dashboard/CLI process
environment, so anything saved here is visible to the next worker run — verified by
`tests/unit/test_provider_auth.py` ("auth present → the worker's env has it").

### On a VPS: use an SSH tunnel

Keep the default bind address and tunnel the port over SSH:

```bash
# on the server
jev dashboard --no-open --repo ~/src/myproject

# on your laptop
ssh -L 8765:127.0.0.1:8765 user@host
# then open http://127.0.0.1:8765/
```

The traffic is encrypted by SSH, nothing listens on a public interface, and no token is
needed. If you must bind a public address, use a long token, a firewall rule limiting who can
connect, and preferably a TLS reverse proxy.

## Screenshots

The screenshots here and in the README use made-up demo data, written straight into a scratch
repo's `.jev/` by [`examples/demo/seed_demo.py`](../examples/demo/seed_demo.py); no agent or
model runs. The seed script also sets the demo's `jev.toml` up for the pictures (rules
dispatcher, free models for the planner, example MCP servers and a skill, variable names only),
caches a model list from the test fixtures and saves one chat.

Every image in `docs/images/` except the logos comes from the running app on your checkout.
One command regenerates all of them:

```bash
uv sync --all-extras
uv run python examples/demo/screenshots.py              # writes docs/images/
uv run python examples/demo/screenshots.py --out preview/ --no-cli   # dashboard only, elsewhere
```

[`screenshots.py`](../examples/demo/screenshots.py) builds a scratch repo under `/tmp` from
`examples/sample-repo`, runs `jev init` and the seed script, starts the real `jev dashboard`
on a free loopback port and captures each `dashboard-*.png` with headless Chrome through
Playwright (a fresh browser profile, no proxy). It then runs `capture.py` for the terminal
images and deletes the scratch repo (`--keep` keeps it). Chrome is Playwright's own Chromium
when it is installed (`uv run playwright install chromium`), otherwise your Chrome; `--chrome
PATH` picks a binary.

Everything runs with a scrubbed environment (scratch HOME and XDG directories inside the demo
repo, `JEV_FAKE_WORKERS=1`, proxy variables pointing nowhere), so your own config, keys and
browser profile are never read. The seed script trusts the demo repo (`[trust] repos`) in that
scratch user config only and refuses to write any other.

**Terminal images** (`docs/images/cli-*.svg`): [`capture.py`](../examples/demo/capture.py)
runs the real `jev` commands in the demo repo with stub `claude`/`codex`/`npx` on `PATH` that
are never run, and exports the output with Rich's `export_svg`. `jev doctor` gets placeholder
values for the variables the demo config names; they are never printed, and the script refuses
to write an image that contains a home path or a placeholder. `jev chat` is driven from a
script with a canned model (`seed_demo.DemoChatBackend`); its `/plan` starts a real plan-only
run with fake workers, so the chat is captured last.

**In CI**, the `screenshots` job runs the same command on every pull request and uploads the
images as the `screenshots` artifact: a preview of the pull request's dashboard and CLI, and a
failure if the seed script or a page no longer renders. A pull request that changes what the
dashboard or the CLI shows commits the regenerated images (CONTRIBUTING.md).
