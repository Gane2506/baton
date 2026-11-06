# Stage 9 — Implementation Readiness, codebase-details lens

Scope reviewed: `docs/designs/cli-autoimport-usage/design.md` lines 698–814
(§9 Detailed implementation plan and §10 Verification plan), checked against
the code at `/home/hatch/workspace/baton-issue396` (read-only).

Verdict: not ready to implement as written. The plan names real files and
real functions in most places, but five points leave the implementer to invent
a route registration, a JSON shape, a data carrier, a function signature, and
a version source. Those are design decisions, and under the
implementation-readiness standard the plan fails while they are open. The
`[usage]` config section and the refresher lifecycle are also each missing a
file the code actually requires.

All file/line references below are to the checked-out code unless marked
"design".

---

## Blockers

### B1 — The new POST refresh route has no registration point in the plan

- Design §9, "Modified files", `routes_connections.py` bullet (design.md:734–738):
  "new `POST /connections/providers/<provider>/usage/refresh`".
- Actual code: route handlers live in `routes_connections.py`, but the routes
  themselves are registered in `src/jev_agent/dashboard/app.py`, lines 257–298
  (the connections block, e.g. `Route("/connections/refresh",
  dash.connections_refresh, methods=["POST"])` at line 259 and
  `Route("/connections/servers/{name}/stats/refresh", ...)` at lines 261–263).
  `app.py` does not appear anywhere in §9's modified-files list.
- An implementer following §9 exactly would write a handler method with no
  route pointing at it, and the endpoint would 404.
- The same bullet says the refresh is "rate-limited", but no rate-limit
  mechanism exists anywhere in the dashboard code (no rate-limit, throttle,
  or per-client limiter in `src/jev_agent/dashboard/` or `src/jev_agent/app/`).
  The plan names no limit value, no window, no key (per provider? per
  session? per IP?), and no store for the counters. The implementer must
  design the whole mechanism.

### B2 — The plan never says where the `usage` key goes in the live poll JSON

- Design §9 (design.md:734–735): "`connections_live` include cached usage
  (additive `usage` key)"; (design.md:739–740): "`conn.js` — live poll updates
  the usage block in place from the cached `usage` key."
- Actual code: `connections_live` (`src/jev_agent/dashboard/routes_connections.py:219–256`)
  builds a top-level dict whose `providers` entry is a list of per-provider
  dicts with exactly four keys — `provider`, `auth`, `label`, `installed`
  (lines 241–250). There is no `usage` key at either level today.
- "Additive `usage` key" fits two different shapes: a top-level
  `"usage": {...}` next to `"providers"`, or a `"usage"` key inside each
  `providers[]` entry. The JavaScript differs in each case:
  `conn.js` `updateProviders` (`src/jev_agent/dashboard/static/conn.js`,
  ~lines 117–136) iterates `json.providers` and reads `p.label` / `p.auth`
  only. A top-level key needs new JS that walks a separate object; a
  per-provider key needs the dict shape in `connections_live` specified.
  The plan specifies neither, so two implementers would build two different
  contracts.

### B3 — `cli_providers` "gains" usage and import metadata, but no carrier is specified

- Design §9 (design.md:730–733): "`views.cli_providers` gains the cached
  usage snapshot (cache read only, never a fetch) and import metadata
  (source, detected_at)."
- Actual code: `cli_providers` (`src/jev_agent/dashboard/views.py:1616–1624`)
  takes only a config and returns `list[ProviderAuth]`. `ProviderAuth`
  (`src/jev_agent/core/provider_auth.py:89–100`) is a frozen dataclass with
  exactly eight fields — `provider`, `title`, `command`, `installed`,
  `auth`, `label`, `detail`, `warning`. There is no usage field, no source
  field, no `detected_at` field, and no `cli_version` field.
- The plan does not say whether the implementer should add fields to
  `ProviderAuth`, return a new wrapper type, return a tuple, or return a
  dict. That choice changes the template (`templates/connections.html:209–214`
  reads `p.provider` / `p.label` / `p.auth` off the same objects), the live
  poll dicts in `routes_connections.py:241–250`, and every existing test
  that constructs or compares `ProviderAuth`.
- `detected_at` has no source in the code: detection is computed live on
  every call — `claude_code_auth` (`provider_auth.py:130`) and `codex_auth`
  (`provider_auth.py:179`) re-read the environment and the credential files
  each time. Nothing persists a detection time, so `detected_at` would either
  be "now" on every render (meaningless) or need new persistence the plan
  never mentions.

### B4 — `get_login_source` is named but never specified, and the new labels are never stated

- Design §9 (design.md:726–727): "shared credential-source accessor
  (`get_login_source`) used by detection and adapters".
- Actual code: no function of that name exists anywhere in `src/` or
  `tests/` (the only mentions are in this design doc). The existing
  detection is split between private helpers `_credentials_login`
  (`provider_auth.py:106`, reads `~/.claude/.credentials.json`) and
  `_codex_auth_file` (`provider_auth.py:115`, reads `~/.codex/auth.json`),
  and the public `claude_code_auth` / `codex_auth`, which already encode
  both precedence orders (Claude: API key, then setup-token env var, then
  CLI login; Codex: API key env var, then `auth.json` tokens, then
  `auth.json` api_key).
- The plan gives no signature, no return shape, and no per-provider
  behaviour for `get_login_source`, and does not say whether
  `claude_code_auth` / `codex_auth` refactor onto it or whether the
  precedence logic is duplicated in a third place. Duplication would let
  the accessor and the detectors disagree.
- The same bullet promises "distinct labels for saved OAuth token vs CLI
  login (D1)". The plan is right that this is a real gap — today both
  branches return the identical label `"Subscription login (OAuth)"`
  (`provider_auth.py:147` for the saved token, `:155` for the CLI login),
  so the page cannot tell them apart — but §9 never states the new label
  strings. User-visible copy is a design decision the implementer would
  have to invent.

### B5 — `cli_version` has no source in the codebase

- Design §9 (design.md:726–727): "additive `cli_version` field on
  `ProviderAuth`".
- Actual code: nothing in the tree runs a CLI with `--version` or parses a
  CLI version anywhere (no `--version` subprocess call in `src/`).
  `resolve_executable` (`src/jev_agent/core/executables.py`) only reports
  whether the binary is on PATH. `ProviderAuth` is built from
  `command` + `installed` only.
- The plan names no command to run, no output format to parse, no behaviour
  when the CLI is missing or its version output changes, and no caching
  (a version probe on every page render would be a new subprocess per
  provider per render). §10 (design.md:~805) cites "Codex known-good
  0.164.0-alpha.9 from the VM" as a fact to record, but §9 never turns
  that into a rule — it is not stated as a minimum version, a warning
  threshold, or anything the code should check.

## Major

### M1 — The `[usage]` config section is missing two of the three files it needs, and its key names break the codebase convention

- Design §9 (design.md:728–729): "`src/jev_agent/config/schema.py` +
  `defaults.py` — `[usage]` section: `claude` / `codex` booleans, default
  true (kill switches)."
- Actual code:
  - `JevConfig` (`src/jev_agent/config/schema.py:1486`) has no `usage`
    field, and every model in the file inherits `_Strict`
    (`schema.py:101–102`, `extra="forbid"`). A `[usage]` table in a config
    file fails validation today, so the schema change is real — but the
    plan never names the `JevConfig.usage` field itself.
  - A new section also needs registering in `TOP_SECTIONS`
    (`src/jev_agent/app/settings.py:52–78`). The settings editor only
    shows sections listed there, and the Settings page only picks up
    sections no other page claims (`settings.py:155`). Without a
    `TOP_SECTIONS` entry the kill switches are valid in the file but
    invisible in every editor; with one and no page assignment they land
    on Settings, not Connections. `settings.py` is not in §9's modified
    list at all.
  - `defaults.py` holds the commented `jev.toml` template
    (`DEFAULT_CONFIG_TOML`); every existing section has commented lines
    there (e.g. `[cache]`, `[dashboard]`). The plan names the file but
    never specifies the commented lines for `[usage]`.
- Key naming: the plan uses bare `claude` / `codex`, but the codebase's
  canonical provider keys are `claude_code` / `codex` — `ProviderAuth.provider`
  is `"claude_code" | "codex"` (`provider_auth.py:92`), the worker config
  is `workers.claude_code` / `workers.codex` (`schema.py:864,910`), and the
  routes use `/connections/providers/claude/...` only for the Claude-specific
  setup-token flow. An implementer has to guess whether `[usage] claude`
  or `[usage] claude_code` is the key, and a wrong guess fails strict
  validation.
- What fits: kill switches defaulting to true match precedent
  (`CacheSettings.enabled` defaults true, `schema.py:1404`;
  `WorkflowsConfig.enabled` defaults true).

### M2 — The refresher lifecycle names the wrong file and no startup/shutdown hook

- Design §9 (design.md:734–738): under the `routes_connections.py`
  bullet, "refresher lifecycle (start on dashboard start, stop on
  shutdown)".
- Actual code: lifecycle does not live in `routes_connections.py`. The
  dashboard's long-lived objects are constructed in
  `DashboardCore.__init__` (`src/jev_agent/dashboard/core.py:57–113`),
  where the existing analogues sit together — `self.codex_flow`,
  `self.claude_flow`, `self.server_stats` (`core.py:111–113`). Startup
  work runs in the app lifespan (`src/jev_agent/dashboard/app.py:330–344`,
  which calls `validate_presets_at_startup` then yields), and shutdown
  runs through `shutdown_jobs` (`core.py:218–239`), which explicitly shuts
  down each runner and flow (`core.py:224–234`, including
  `self.codex_flow.shutdown()` and `self.claude_flow.shutdown()`).
- Neither `core.py` nor `app.py` appears in §9's modified-files list, so
  there is no stated place to start the refresher and no stated place to
  call its `shutdown()`. The refresher's inputs are also unstated: it
  needs config (cadence, kill switches) and a home/state directory, and
  `DashboardCore` only carries `settings.root` — the plan never says how
  the refresher gets them.

### M3 — The plan reinvents the cache instead of reusing or explicitly diverging from `ServerStatsCache`

- Design §9 (design.md:702–720): new `core/usage/cache.py` (host-level
  cache record `~/.jev/usage-cache.json`, mode 600, atomic write,
  fingerprint invalidation, single-flight state, staleness rules) plus a
  separate `core/usage/refresher.py`.
- Actual code: the closest existing pattern is `ServerStatsCache`
  (`src/jev_agent/servers/stats.py:310`), and §9 never mentions it:
  - `get(state_dir, name) -> (stats, state)` (`stats.py:344`) never
    blocks and never opens a connection — the same "cache read only,
    never a fetch" rule the plan wants for `cli_providers`.
  - `refresh(...) -> bool` (`stats.py:354`) is single-flight on
    `state == "collecting"` (lines 362–366).
  - JSON lives under `<state_dir>/server-stats/<name>.json`
    (`stats.py:322–324`), i.e. per-repo state, not the home directory.
  - It is held as `self.server_stats` (`core.py:113`), kicked from
    `connections_page` when state is `"never"`
    (`routes_connections.py:187–195`), and has its own POST refresh
    handler `server_stats_refresh` (`routes_connections.py:277`).
- The plan never states why this pattern is not reused, or how the new
  cache differs from it (per-repo `state_dir` vs host-level `~/.jev`;
  in-memory single-flight vs the plan's "cross-instance in-progress
  marker" in the test list). Two parallel background-cache systems on
  the same page, with different homes and different concurrency rules,
  is a design decision that needs stating.
- The plan's "mode 600, atomic write" also has no named precedent in the
  stats cache, which writes with plain `write_text` (`stats.py:380–389`)
  and no permission setting. The one atomic-write precedent in the tree
  is `write_secret` in `src/jev_agent/core/secrets.py:191`
  (`os.replace` of a temp file, file mode 0600, directory 0700) — the
  plan never says to reuse it.

### M4 — The idle rule has no signal source

- Design §9 test list (design.md:~766): "idle rule (no refresh when UI
  not viewed in 30 min)".
- Actual code: no "last viewed" tracking exists in the dashboard code.
  The one candidate signal is the live poll itself — `conn.js` polls
  `/connections/live` every 15 seconds (`POLL_MS = 15000`,
  `src/jev_agent/dashboard/static/conn.js`) and pauses while the tab is
  hidden — but the plan never says the refresher reads poll activity, a
  timestamp file, or anything else to learn when the UI was last viewed.
  Without a named signal the implementer must invent both the mechanism
  and what counts as "viewed".

### M5 — The test list introduces state and rules that §9's file descriptions never define

- Design §9 test list (design.md:~750–774) vs the file descriptions
  above it:
  - Cache tests require "restart preserves `next_retry_at` /
    `consecutive_failures`", but the `cache.py` bullet (design.md:712–714)
    never lists those fields in the cache record. The implementer must
    decide the record's fields.
  - "Cross-instance in-progress marker" names no mechanism. The tree has
    two: `core/file_lock.py` (used by `core/scheduler.py`) and the
    ledger's own `_FileLock` (`src/jev_agent/code_orchestrator/ledger.py:583`).
    The plan names neither, and the `ServerStatsCache` single-flight it
    most resembles is in-memory only, so it cannot satisfy a
    cross-instance test as written.
  - "Manual refresh rate limits" (test list) repeats the B1 problem: no
    limit values anywhere in §9.
  - Codex "wrong version" (test list) repeats the B5 problem: no minimum
    version anywhere in §9.
  - Claude "setup-token rejected once (recorded, never retried)" names no
    store for the recording — cache file, `state.db`, or elsewhere.
- Each of these is a decision the readiness standard requires the plan
  to have made already.

### M6 — The new package fits the subpackage precedent, but creates a third module called `cache`

- Design §9 (design.md:702–720): `src/jev_agent/core/usage/` with
  `__init__.py`, `claude.py`, `codex.py`, `cache.py`, `refresher.py`.
- Actual code: a subpackage under `core/` fits — `core/learning/`,
  `core/store/`, `core/ship/`, `core/suggestions/`, and
  `core/workflow_profiles/` all exist, and the `__init__.py` re-export
  convention (`core/suggestions/__init__.py` re-exports the public API)
  matches the plan's `__init__.py` contents (protocol, dataclasses,
  reason-code constants). The name `usage` itself collides with no
  module (only local variables and `usage_from` in
  `workers/claude_code.py`).
- But the tree already has two modules called `cache` doing different
  jobs: `core/cache.py` (exact-match decision/role cache) and
  `core/store/cache.py`. Adding `core/usage/cache.py` makes three. Fully
  qualified imports keep it working, but the plan never acknowledges the
  confusion or states the import paths, and a reader searching for "the
  cache" now has three candidates on the same page feature.
- Supporting fact: injected `httpx` transport is feasible — `httpx>=0.27`
  is already a core dependency (`pyproject.toml:26`) — and injected
  spawners fit the existing pattern: `CodexDeviceFlow` and
  `ClaudeSetupTokenFlow` (`provider_auth.py`) both take a `popen`
  callable and run a daemon watcher thread. The Codex app-server spawn
  itself has no in-repo precedent (no `app-server` spawn anywhere in
  `src/`), so the plan is right to spell out that contract — but it
  should say so explicitly rather than leaving the reader to assume a
  precedent exists.

## Nits

### N1 — `docs/designs/SITEMAP.md` is listed as a file to modify, but the entry already exists

- Design §9 (design.md:748): "`docs/designs/SITEMAP.md` — register this
  design."
- Actual code: the entry is already there (`docs/designs/SITEMAP.md:7`),
  dated 2026-11-06 — one day after today (2026-11-05). The step is a
  no-op / verify-only, not a modification. The future date is also worth
  correcting.

### N2 — §9's file lists are otherwise correctly split into new vs modified

- The plan does mark Create vs Modify at section level ("New files" /
  "Modified files"), which satisfies the review rule that each path be
  marked. The gaps above are missing files (B1, M1, M2), not unmarked
  ones.

## What checked out

- `views.cli_providers` exists where the plan says it does
  (`views.py:1616`), and `routes_connections.py` (`connections_page`
  `:172`, `connections_live` `:219`) and `templates/connections.html`
  (provider loop `:209–214`, `data-provider-auth` badge) are the right
  files for the page and poll changes.
- `conn.js` poll shape matches the plan's assumption: 15-second poll of
  `/connections/live`, `json.providers` iterated in `updateProviders`,
  `codex_flow` state driving a reload.
- `tests/unit/test_provider_auth.py` exists; `docs/config.md` and
  `docs/dashboard.md` exist, so the doc updates have real targets.
- §10's "8 required checks" counts correctly: `ci.yml` has five jobs
  (lint, test with a 3.11/3.12/3.13 matrix, build, screenshots,
  windows-latest) and `principles.yml` adds the sixth job, which is
  eight checks counting the three test-matrix runs.
- The "What is NOT in the implementation" list is consistent with the
  code: no `state.db` migration is needed for a host-level JSON cache,
  and no worker changes are named.
