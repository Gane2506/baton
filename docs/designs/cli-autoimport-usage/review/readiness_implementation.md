# Stage 9 — Implementation Readiness Review: cli-autoimport-usage design, §9–§10

Scope reviewed: `docs/designs/cli-autoimport-usage/design.md`, lines 698–814
(§9 Detailed implementation plan, §10 Verification plan).
Repo checked read-only at `/home/hatch/workspace/baton-issue396`
(commit `61c5000`).

## Verdict

Not implementable as written. An engineer following §9 exactly would hit a
wrong file path on the first config edit, would have to invent the data
contract that three files must agree on, and would have to decide where the
background refresher lives, how it starts and stops, and how cross-process
single-flight works. The plan reads as a list of intentions, not a list of
instructions. It fails the zero-decision rule in the implementation-readiness
standard: several design choices are still open and are left to the
implementer.

## File-path check (verified against the repo)

| Path named in §9 | Status |
|---|---|
| `src/jev_agent/core/usage/__init__.py`, `claude.py`, `codex.py`, `cache.py`, `refresher.py` | Do not exist yet — correctly listed as new |
| `src/jev_agent/core/config/schema.py`, `src/jev_agent/core/config/defaults.py` | **Do not exist. Wrong paths** — the real files are `src/jev_agent/config/schema.py` and `src/jev_agent/config/defaults.py` |
| `src/jev_agent/core/provider_auth.py` | Exists |
| `src/jev_agent/dashboard/views.py`, `routes_connections.py`, `static/conn.js`, `templates/connections.html` | Exist |
| `tests/unit/test_provider_auth.py` | Exists |
| `docs/config.md`, `docs/dashboard.md`, `docs/designs/SITEMAP.md` | Exist |
| `src/jev_agent/dashboard/app.py` | Exists but **never listed**, although the plan needs it (see B4) |
| `src/jev_agent/dashboard/core.py` | Exists but **never listed**, although the plan needs it (see B4) |
| `tests/unit/test_dashboard_connections.py`, `test_dashboard_providers.py` | Exist but **never listed**, although the dashboard test bullet needs them (see M8) |

## Findings

### Blocker — B1. The config file paths are wrong (§9, lines 724–725)

The plan says to edit `src/jev_agent/core/config/schema.py` and
`src/jev_agent/core/config/defaults.py`. Neither file exists. The real
files are `src/jev_agent/config/schema.py` and
`src/jev_agent/config/defaults.py`. An implementer following the plan
would either create files in the wrong place or stop and ask. The plan
also never says what to add once the right files are found: no model
class name for the `[usage]` section, no `usage` field on `JevConfig`
(`schema.py` line 1486), no entry in the commented `DEFAULT_CONFIG_TOML`
template in `defaults.py`, and no validation behaviour (what happens if
a user writes `claude = "yes"` instead of a boolean).

### Blocker — B2. The shared data contract is never written down (§9, lines 700–721, 728–739)

Three files must agree on one shape, and the plan defines none of it:

- `UsageWindow`, `UsageSnapshot`, `UsageUnavailable` are named but their
  fields are never listed. Is a window a utilization percent or a
  remaining percent? What type is the reset time — unix seconds, ISO
  string, datetime? What marks a window as session vs weekly?
- `connections_live` gains an "additive `usage` key", but the JSON shape
  of that key is never specified: field names, timestamp format, where
  the reason code and `fetched_at` go.
- The template gains a "usage block" and `conn.js` "updates the usage
  block in place", but no element ids, classes, or `data-` attributes
  are named. `conn.js` today only updates provider badges through
  `[data-provider-auth]`; there is nothing for it to hook onto.
- The Claude adapter gets a "response parser (versioned)" — versioned
  how? There is no version field named anywhere in §9.

The implementer has to invent this contract, and the template, the JSON,
and the JavaScript will only agree by luck.

### Blocker — B3. `views.cli_providers` cannot "gain" fields the way §9 describes (§9, lines 728–731)

`views.cli_providers` returns `list[ProviderAuth]`
(`src/jev_agent/dashboard/views.py`, line 1616). `ProviderAuth` is a
frozen dataclass with a fixed set of fields
(`src/jev_agent/core/provider_auth.py`, lines 89–101); it has no usage
field and no import-metadata fields. The plan says the function "gains
the cached usage snapshot … and import metadata (source, detected_at)"
but never says how: add fields to `ProviderAuth`, wrap it in a new
dataclass, or change the return type. Each option breaks different
things — the template reads `p.label`/`p.auth`, `connections_live`
serializes the providers by hand, and `test_dashboard_providers.py`
asserts on the current shape. That test file is not even listed as
modified. The implementer must redesign a return type the plan treats
as a one-line edit.

### Blocker — B4. The refresher has nowhere to live, and the route has nowhere to register (§9, lines 732–737)

The plan puts "refresher lifecycle (start on dashboard start, stop on
shutdown)" under `routes_connections.py`. That module has no start or
stop hook. Dashboard startup is the `lifespan` function in
`src/jev_agent/dashboard/app.py` (lines 330–345); background-job
shutdown is `DashboardCore.shutdown_jobs` in
`src/jev_agent/dashboard/core.py` (line 218), which stops each runner
in turn. Neither file is listed in §9. The plan never says where the
refresher instance is held (a `DashboardCore` attribute? `app.state`?
a module global?), whether it runs on threads or as an asyncio task,
or how `shutdown()` is called from the existing shutdown path.

The same gap applies to the new route: `POST
/connections/providers/<provider>/usage/refresh` is listed under
`routes_connections.py`, but routes are registered in the route table
in `app.py` (lines 257–299). `app.py` is not listed as a modified file,
so the handler would be written and never reachable.

### Blocker — B5. Cross-process single-flight is undecided (§9, lines 762–765; §6.4)

§6.4 says "A file lock is the correct mechanism; a timestamp check is
the minimum." That is two different mechanisms, and §9 picks neither.
The test list asks for a "cross-instance in-progress marker" without
saying what the marker is — a field in the cache JSON, a separate lock
file, a `fetch_started_at` timestamp — or where it lives, or what
happens when a process dies mid-fetch and leaves the marker behind.
This is a design decision deferred to the implementer, which is exactly
what the zero-decision rule forbids.

### Blocker — B6. First view with an empty cache is a contradiction (§9, lines 728–737; D6; §6.4)

D6 says "a page view triggers a fetch only when the cache is older than
the interval". §6.4 and §9 say views serve cached usage only and "never
trigger or wait on a fetch". Both cannot be true for the first view,
when there is no cache at all. The plan never says what the user sees
then, whether the refresher fetches immediately on dashboard start, or
how long "Usage not checked yet" stays on screen. The implementer must
resolve the contradiction.

### Major — M1. `cli_version` has no source, and `get_login_source` has no shape (§9, lines 724–727)

- There is no code today that runs `claude --version` or `codex
  --version`. `provider_auth.py` only checks whether the command is on
  PATH. The plan adds an "additive `cli_version` field" but never says
  how the version is obtained, how its output is parsed, what timeout
  applies, what happens when the command fails, or whether the result
  is cached (running a subprocess on every page render would be slow).
- The "shared credential-source accessor (`get_login_source`)" is named
  but not specified: no module, no return type (the §6.2 sketch shows a
  dict-like `{kind, path-or-store, detected_at, cli_version}`), and no
  statement of whether `claude_code_auth`/`codex_auth` are refactored
  to use it or whether it sits alongside them. Two functions that must
  agree on login identity are left to the implementer to wire up.

### Major — M2. The idle rule has no mechanism (§9, lines 766–768)

"Idle rule (no refresh when UI not viewed in 30 min)" is listed as a
refresher test, but nothing in §9 records when the UI was last viewed.
The refresher is deliberately decoupled from the 15-second live poll
and from page renders, so it cannot see views by itself. No shared
"last viewed" timestamp, no writer of that timestamp (the connections
page? `connections_live`?), and no storage location are named. The
implementer must invent the cross-component signal.

### Major — M3. The manual refresh route is underspecified (§9, lines 734–736, 770–771)

- The rate-limit numbers exist only in D6 (ignored when the last
  attempt was under 5 minutes ago for Claude, under 60 seconds for
  Codex). §9 does not restate them, so the route implementer and the
  test writer must go hunting.
- What the route returns when rate-limited is undefined: HTTP status
  (429? 200 with a message?), redirect, flash message, or JSON? The
  dashboard's other POST actions go through `self.act` with a redirect
  back to the page; this route's response shape is never stated.
- The `<provider>` path parameter is never validated in §9: unknown
  provider names, and the case where the provider's kill switch is off,
  have no specified response.
- CSRF handling for the new form is never mentioned, although every
  other dashboard POST goes through the same protection.

### Major — M4. Claude token refresh inputs are undefined (§9, lines 704–708)

D8 says the adapter checks `expiresAt` and refreshes with the refresh
token, but §9 never says where the refresh token is read from (which
file, which field, which environment variable), what happens when the
credentials file has no `expiresAt`, or how long a refreshed token
lives in memory and which later fetches reuse it (adapter instance
lifetime is never defined). The test "refresh succeeds in memory;
refresh fails → reason code" cannot be written precisely without these
answers.

### Major — M5. The Codex spawn contract is incomplete (§9, lines 709–712)

- The JSON-RPC transport is never specified: stdin/stdout pipes, message
  framing (newline-delimited vs `Content-Length` headers), and how
  notifications are skipped on the wire.
- The temp-dir cwd is named in §6.3 but §9 never says who creates it,
  who deletes it, or what happens on a crash.
- "Env scrub" is never spelled out: §6.3 says preserve `HOME`, but
  which variables are removed (API keys? proxy settings?) is not
  listed.
- The injected "spawner" has no interface name or signature.
- The test "wrong version" has no minimum version in §9. The only
  version number anywhere is 0.164.0-alpha.9, given in §6.3/§10 as the
  version observed on the VM — not as a floor. The implementer cannot
  write the version check.

### Major — M6. The kill switches have no behaviour beyond the config keys (§9, lines 724–725)

`[usage] claude` / `codex` default true. The plan never says what
"off" does: is the adapter never constructed, does the refresher skip
that provider, does the card hide the usage block or show a disabled
state, and does a manual refresh on a disabled provider error or
silently do nothing? There is also no test for any of this (see M7).
A kill switch whose off-state is undefined is not a kill switch.

### Major — M7. The test list has gaps against the behaviours the design promises (§9, lines 750–773)

- 403: D7 says "401/403 retry once", but the test list only covers 401.
- `Retry-After` absent or unparsable: the list only covers the happy
  429 case. D7 says absence means "wait at least the base interval",
  which is untested.
- Window derivation: §6.3 derives session vs weekly from
  `windowDurationMins` (300 → session, 10080 → weekly). No test covers
  an unknown duration, a missing `windowDurationMins`, or a missing
  `resetsAt`.
- Kill switches: no test that off means no fetch, no refresher, and a
  defined UI state.
- Token handling: no test that a refreshed token is never written to
  disk, and no test that no token value appears in logs, the cache
  file, or the rendered page. §10 mentions "no token leakage" only as
  something the orchestrator checks by eye.
- Setup token: the test says "setup-token rejected once (recorded,
  never retried)" but never says where the rejection is recorded (cache
  record? process memory? a file?) — so "never retried" across a
  dashboard restart is untestable as written.
- Fingerprint: the invalidation test cannot be precise because the
  fingerprint inputs are undefined (see M9).
- Shutdown: `shutdown()` "joins threads", but no test covers
  terminating a live app-server on shutdown, and no join timeout value
  is given anywhere.
- No clock injection is named. Cadence (15/5 min), backoff
  (15→30→60), and the 1-hour circuit-breaker quiet period cannot be
  tested without waiting real minutes unless the refresher takes an
  injected clock. §9 says only "all with fakes (section 8
  testability)" without naming the fake-clock seam.

### Major — M8. No dashboard test file is listed as modified (§9, lines 722–748)

The dashboard test bullet requires render-serves-cache-only, live-poll
JSON, refresh-route rate limiting, and template rendering of every
state-matrix row. The existing homes for those tests are
`tests/unit/test_dashboard_connections.py` and
`tests/unit/test_dashboard_providers.py`. Neither is listed under
modified files, and no new dashboard test file is listed either. The
only test files named are the four new `test_usage_*` files and
`test_provider_auth.py`.

### Major — M9. The fingerprint inputs are undefined (§9, lines 713–714; D5)

D5 defines the fingerprint as a "login identity hash — source path +
account hint, never a token value". "Account hint" is never defined:
which field of which credential, in what form, hashed how? The
implementer cannot write the hash function or the invalidation test
("fingerprint invalidation on auth change") without guessing what
counts as an auth change — a different token value with the same path
and account? A new login of the same account? A changed file mtime?

### Major — M10. There are no phases or checkpoints (§9, whole section)

The standard requires the work to be split into incremental phases,
each leaving the codebase stable, buildable, and testable. §9 is a flat
list of files and tests with no order: cache before adapters, adapters
before refresher, refresher before UI, or any other sequence is left
to the implementer, and there is no stated checkpoint where the work is
verifiable on its own. (The design's own §4 notes the work splits into
independently verifiable pieces; §9 does not use that split.)

### Major — M11. Adapter wiring is never described (§9, lines 700–721)

Nothing says who constructs the Claude and Codex adapters, with what
arguments (config, home directory, transport, spawner), how the
refresher finds them, or how the kill switches gate construction. There
is no factory, registry, or mapping named. The four new files and the
refresher are listed as if they connect themselves.

### Nit — N1. Screenshots step names no script (§10, lines 785–787)

"Regenerate `docs/images/`" does not name the script
(`examples/demo/screenshots.py`, per the repo's own screenshot
protocol) or which shots cover the Connections page. Minor, but the
implementer has to go and find it.

### Nit — N2. `docs/designs/SITEMAP.md` is already done (§9, line 749)

The SITEMAP entry for this design already exists in the working tree
(`docs/designs/SITEMAP.md` is modified, entry present). Listing it as
work to do in the implementation plan is stale; it belongs to the
design PR, not the implementation.

### Nit — N3. Cache home-directory seam is unnamed (§9, lines 713–714)

The cache lives at `~/.jev/usage-cache.json`, but §9 never says how the
home directory is supplied (parameter, environment, `Path.home()` at
call time) or how tests redirect it. The cache tests (atomic write,
mode 600) need a fake home; the seam is not named.

## What §10 gets right, and what it still leaves open

- The Mac verification merge gate (live Claude and Codex success shapes,
  items 1 and 2) is stated clearly, and the honest-boundaries paragraph
  correctly labels the Claude endpoint and the Codex success shape as
  unverified until observed live.
- The "8 required checks" line checks out against the repo: `ci.yml`
  runs on `windows-latest` (line 72), alongside lint, build, three
  Python-version test jobs, screenshots, and `principles`.
- Open point: item 3 of Mac verification, "the setup-token acceptance
  question settled live", has no pass/fail definition — accepted and
  rejected are both possible outcomes, and §10 does not say which
  outcome blocks the merge or what the implementer does with either.
  Item 4 (a 429 "observed if it occurs naturally") is explicitly
  optional, which is honest, but it means the backoff path may ship
  unit-proven only; §10 says that plainly, so this is a noted limit
  rather than a gap.

## Summary of the most urgent fixes

1. Correct the config paths to `src/jev_agent/config/schema.py` and
   `src/jev_agent/config/defaults.py`, and add the missing steps: the
   `UsageConfig` model, the `usage` field on `JevConfig`, and the
   commented template entry.
2. Write down the data contract before implementation: the fields of
   `UsageWindow`/`UsageSnapshot`, the exact JSON shape of the `usage`
   key, and the template element ids `conn.js` updates.
3. Decide the refresher's home (`DashboardCore` vs `app.state`),
   thread vs asyncio, its start/stop wiring through `app.py`
   lifespan and `core.py shutdown_jobs`, and the cross-process
   single-flight mechanism (file lock or timestamp) — and put `app.py`
   and `core.py` in the modified-files list.
