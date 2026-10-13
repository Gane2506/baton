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

Ctrl-C stops the server. A run being approved or resumed from the page at that moment is
interrupted where it is, like Ctrl-C on `jev approve`; it keeps its saved state and continues
with `jev resume <run>` (or the Resume button).

## Pages

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
  - **Cancel** for any unfinished run (`jev cancel`; branches and worktrees are kept).

  Approve and resume run in a background thread through the same code path as the CLI
  (config, workers and pipeline are built the same way), so the web server stays responsive;
  the page shows the job's status and the run's progress live. Only one job per run id runs at
  a time; a second click is refused. (This lock is per dashboard process: do not also run
  `jev resume` for the same run from a terminal at the same time.)
- **Queue** (`/queue`): items in scheduler order with their effective status and flags; add an
  idea (priority, plan only, learning profile or none, quality level, workspace profile name),
  move an item to the top, up or down, remove it; scheduler status (lock holder, current item,
  queue counts, spend today). The scheduler itself is started from a terminal
  (`jev scheduler start`).
- **Learning** (`/learning`): the repo switch and active profile, profiles (scope, enabled,
  active, lesson count) and each profile's lessons. Actions: create (repo or shared), use,
  enable / disable (the repo or one profile), forget a lesson, and flush a profile, which asks
  for confirmation on a separate page first.
- **Chats** (`/chats`): saved `jev chat` sessions and their transcripts, read-only.
- **Spend** (`/spend`): spend per day for the last 30 days and the costliest runs, as
  server-rendered SVG bar charts, plus every run's spend.
- **Settings** (`/settings`): the effective config (read-only), the environment variables the
  config names with whether each is set (names only, never values), and the `jev doctor`
  checks (local checks only).

Pages follow your OS light/dark setting and work on narrow screens. Nothing is loaded from
other origins: no CDN, no web fonts, no analytics. The only script is a small vendored
`static/live.js` for the live regions and confirmation prompts; without JavaScript every page
still works (reload to refresh).

| Run | Queue |
| --- | --- |
| ![Run detail](images/dashboard-run.png) | ![Queue](images/dashboard-queue.png) |
| **Learning** | **Spend** |
| ![Learning](images/dashboard-learning.png) | ![Spend](images/dashboard-spend.png) |

The Board in dark mode: [images/dashboard-board-dark.png](images/dashboard-board-dark.png).

## Security

The dashboard can approve runs, which start agents that edit code and spend money, so treat
access to it like shell access to the repo.

- **Loopback by default.** It binds `127.0.0.1`. On loopback it also checks the `Host`
  header (only `127.0.0.1`, `localhost` and `::1` are accepted), so a malicious web page
  cannot reach it through DNS rebinding.
- **Token off loopback.** Any other `--host` (for example `0.0.0.0`) requires a token: pass
  `--token` / set `JEV_DASHBOARD_TOKEN`, or jev generates a random one. jev prints the URL with
  `?token=...` and a warning. Every request (pages, files, static assets) must carry the token,
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

The images above use made-up demo data, written straight into a scratch repo's `.jev/` by
[`examples/demo/seed_demo.py`](../examples/demo/seed_demo.py); no agent or model runs. To
regenerate them from a clone of this repo (macOS paths; any Chrome or Chromium works):

```bash
DEMO=$(mktemp -d /tmp/baton-demo.XXXX)
cp -R examples/sample-repo/. "$DEMO"/
git -C "$DEMO" init -q && git -C "$DEMO" add -A
git -C "$DEMO" -c user.name=demo -c user.email=demo@example.com commit -qm "sample"
(cd "$DEMO" && uv run --project "$OLDPWD" --all-extras jev init)
sed -i '' 's/^primary = "jev"  /primary = "rules"/' "$DEMO/jev.toml"
export XDG_DATA_HOME="$DEMO/.xdg"   # keep your own shared learning profiles out of the shots
uv run --all-extras python examples/demo/seed_demo.py "$DEMO"
(cd "$DEMO" && uv run --project "$OLDPWD" --all-extras jev dashboard --no-open --port 8799) &

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
shot() {  # shot WIDTH HEIGHT OUT.png PATH [light=1|dark=0]
  profile=$(mktemp -d)   # a throwaway profile: never your own
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$profile" \
    --blink-settings=preferredColorScheme="${5:-1}" --window-size="$1,$2" \
    --screenshot="$3" "http://127.0.0.1:8799$4"
  rm -rf "$profile"
}
shot 1440 900 docs/images/dashboard-board.png /
shot 1440 900 docs/images/dashboard-board-dark.png / 0
shot 1440 1400 docs/images/dashboard-run.png /runs/RUN_ID   # the id the seed script prints
shot 1440 900 docs/images/dashboard-queue.png /queue
shot 1440 900 docs/images/dashboard-learning.png /learning
shot 1440 1300 docs/images/dashboard-spend.png /spend

kill %1 && rm -rf "$DEMO"
```
