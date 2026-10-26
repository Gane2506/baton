# Figma

Baton connects to Figma two ways, and the split matters — it is the difference
between what Figma's APIs allow and what people assume they allow.

## What Figma allows, by path

| Capability | REST API (`api.figma.com`) | MCP `use_figma` (Plugin API) | Dedicated Baton plugin |
| --- | --- | --- | --- |
| Validate a token (`GET /v1/me`) | yes | — | — |
| Read a file (nodes, components, styles, images, comments) | yes | yes (`get_design_context`, `get_metadata`) | yes |
| Read variables | Enterprise-only | yes, no Enterprise gate | yes |
| Create/edit variables, styles, components, variants, text, images | **no** | **yes** | yes |
| Auto-layout, vector paths | no | yes | yes |
| Prototype reactions (click/hover, SMART_ANIMATE, Set Variable, Conditional) | no | yes (`setReactionsAsync`) | yes |
| Screenshot the file to verify a change | no | yes (`get_screenshot`) | yes |
| Capture a live web UI as editable frames | no | yes (`generate_figma_design`) | no |

The REST API is largely read-only — Figma's own compare page says so. It
cannot create files, frames, nodes or components. **Baton never pretends
otherwise**: the dashboard says so wherever Figma appears, and the export
below is Plugin API JavaScript, not a REST call.

## Setup (browser-first)

1. **Token** — Tools → APIs → Figma. Paste a Personal Access Token from
   Figma (Settings → Security; it starts with `figd_`). It is saved to
   `~/.jev/secrets.env` (mode 0600) through the same key flow as every
   other provider key, applies immediately, and is never rendered. Validate
   runs `GET /v1/me` and shows who the token belongs to.
2. **MCP server** — Tools → APIs → Figma → Add Figma MCP server. One click
   adds `[tools.mcp.figma]` at `https://mcp.figma.com/mcp` (the official
   remote server, OAuth, all seats/plans including free for reads). Agents
   then get `get_design_context`, `get_metadata`, `get_screenshot`,
   `search_design_system`, `use_figma` and `generate_figma_design`.
   The desktop server (`http://127.0.0.1:3845/mcp`) needs a Dev/Full seat
   and the desktop app; Baton does not add it for you.
3. **Agents** — when a message is about Figma, the chat's skill pass finds
   `figma-design` / `figma-import`, and the agent loads the `figma-use`
   skill (Figma's own skill for using `use_figma` reliably) before touching
   the file.

## Gating — stated honestly in the UI

- Writing needs a **Full seat** (a Dev seat is read-only) and edit access
  to the specific file.
- The write tools are **beta**: free now, planned usage-based paid later.
- Rate limits: Starter plans get about 20 MCP tool calls a month; a Full
  seat on Professional or above gets about 200 a day.
- Work on a **duplicate or draft** before touching a production file, and
  keep a backup — agent writes are beta.

## The two-way binding

A library system can be **linked** to a Figma file (its Figma page, or the
`figma_file_url` / `figma_file_key` in its `meta.json`). The binding is
living, not a one-shot export:

- **Baton → Figma (export).** The system's Figma page (or
  `jev design figma export <name>`) generates the Plugin API script that
  builds the system in Figma: a `Baton / <name>` variable collection with
  Light + Dark modes, one paint style per color, one text style per type
  size, a Button and a Card component, and a Home → Detail prototype whose
  button navigates with SMART_ANIMATE. An agent runs it through
  `use_figma`; it can also be pasted into a Figma plugin console.
  `generate_figma_design` is the other direction of the same idea: point
  it at a system's rendered showcase URL and it captures the live UI as
  editable Figma frames — real layers, not a screenshot.
- **Figma → Baton (sync).** Sync from Figma reads the linked file over
  REST, maps its styles onto Baton tokens, and saves the result as a new
  library version. Added and changed tokens are applied; tokens removed
  in Figma are reported but **kept** in Baton — deleting a token is a
  deliberate edit, not a sync side effect. Sync after every write
  (`get_design_context` / `get_screenshot`) and on demand when the file
  changes outside Baton, so the two sides never silently drift apart.
- **Import.** Design Studio → Reverse → From a Figma file (or
  `jev design figma import <url>`) reads a file's paint styles, text
  styles, auto-layout spacing and corner radii into a new linked system.
  Colors come from named paint styles first, then the most-used solid
  fills; spacing/radii fall back to the starter scale when the file
  declares none, and the system markdown says so.

## Granular edits — every minor aspect

The system's Figma page (or `jev design figma edit <kind> …`) generates
the Plugin API snippet for **one** change:

- `variable` — set a variable's value in Light or Dark mode
  (`color/primary`, a spacing number, …)
- `node-fill` — one node's fill color; `node-text` — one node's text;
  `node-size` — one node's size; `corner-radius` — one node's radius
- `reaction` — link two nodes: trigger (click, hover, press, drag),
  SMART_ANIMATE / dissolve / move / push / instant transition

Every snippet looks the variable or node up first and throws when it is
not there, so a stale id fails loudly instead of editing the wrong thing.
Figma's own guidance for agent writes is built in as rails: **inspect
first, change in small steps, verify with `get_screenshot` after** — large
single-shot changes go wrong, so Baton generates one change per snippet
and the export script itself orders variables/styles before components
before reactions, so a failure late in the script still leaves the tokens.

## What still needs a dedicated plugin

Nothing on the canvas does — `use_figma` covers every object the Plugin
API can make. A Baton Figma plugin is the **fallback**, not the path: if
the official MCP server is unavailable (no Full seat, plan gates, or the
beta limits bite), the same generated script runs in the open file through
a thin plugin that evaluates pasted Plugin API code. The community
Figwright pattern (MCP server + local plugin over WebSocket, works on the
free tier) is the other fallback. Neither is built now; the scripts Baton
generates are the same either way, so switching paths costs nothing.

## Files

- `src/jev_agent/core/figma.py` — REST client, file→tokens mapping,
  sync diff/apply, `use_figma` script + edit-snippet generation.
- `src/jev_agent/core/skills.py` — `figma-design` and `figma-import`
  skills, found by the chat's skill pass.
- Dashboard: Tools → APIs → Figma (token + MCP server),
  `/design-systems/<name>/figma` (link, export, edit, sync),
  Design Studio → Reverse → From a Figma file.
- CLI: `jev design figma export|import|sync|edit`.
