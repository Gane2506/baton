"""Servers over SSH with access levels, the virtual browser and workspace profiles (issue #3).

- `policy`: what each access level may run (the single place those rules live).
- `ssh`: the system `ssh` transport, non-interactive and with strict host-key checking.
- `audit`: `.jev/audit/servers.log`, one redacted JSON line per command or file operation.
- `service`: policy + transport + audit behind the tools agents call.
- `mcp`: a minimal stdio MCP server exposing those tools (`jev mcp servers`).
- `access`: which servers (at which level) and browser each role gets for a run.
- `profiles`: workspace profiles, the repo default and the profile a run started with.
- `browser`: the pinned Playwright MCP server and the browser install command.

See docs/servers.md for the user-facing description.
"""
