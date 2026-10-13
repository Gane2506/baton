# Security policy

jev runs AI coding agents on your repository, with your credentials, and can reach servers
over SSH and serve a local dashboard. Security reports are taken seriously. This file says
how to report a problem and what is in scope. How jev limits what agents can do is described
in [docs/security.md](docs/security.md).

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | Yes |
| 0.2.x | Yes |
| < 0.2 (the `jao` 0.1 CLI) | No |

Fixes land on `main` first and ship in the next release. The project is pre-1.0, so a fix may
come only in a new minor version rather than as a 0.2.x patch.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a security problem.**

Report it privately through GitHub: open the repository's
[Security tab](https://github.com/Gane2506/baton/security) and choose
**Report a vulnerability**
([direct link](https://github.com/Gane2506/baton/security/advisories/new)). Only the
maintainer can see the report. If that button is missing, open a public issue that says only
that you have a security report and ask for a private channel, without any details.

Please include:

- what an attacker can do, and what they need first (for example: a malicious `jev.toml` in a
  cloned repository, a crafted web page the browser tool opens, output from a server, network
  access to the dashboard);
- the jev version (`jev --version`) or commit, your OS and Python version;
- steps to reproduce, ideally on the sample repo (`examples/sample-repo`) with
  `JEV_FAKE_WORKERS=1`, plus the relevant config;
- the impact as you see it, and a suggested fix if you have one.

Do not include real API keys, tokens or private repository content. Do not test against
systems or accounts that are not yours.

## What to expect

jev has a single maintainer working on it in their own time, so these are aims, not
guarantees:

- an acknowledgement within about a week;
- a first assessment (accepted, needs more information, or out of scope) within about two
  weeks;
- for an accepted report, a fix and a GitHub security advisory, crediting you unless you
  prefer otherwise. Please keep the details private until the fix is released or 90 days have
  passed, whichever comes first; if a fix needs longer, we will say why and agree a date with
  you.

## Scope

In scope: problems in jev's own code that let something happen that
[docs/security.md](docs/security.md) says should not. For example:

- **Agent permissions.** A planner, validator, reviewer or code reviewer that can write files;
  a coder that escapes its worktree; jev passing permission or sandbox flags it documents it
  does not pass; a role getting tools, servers or a browser it was not configured with.
- **Repository config trust.** An untrusted repository's `jev.toml` changing where keys are
  sent, which credential variables are read, worker commands or permissions, or agent tools,
  servers and browser access.
- **Servers over SSH.** A command or file operation that gets past the access level, the
  always-blocked list or `allowed_paths`; a role above its access cap; weakened host-key
  checking; credential files readable through the server tools; operations missing from
  `.jev/audit/servers.log`.
- **The dashboard.** Bypassing the host check, token, CSRF or same-origin checks; cross-site
  scripting; reading files outside `.jev/runs/<id>/`.
- **Secrets.** API keys or tokens reaching argv, prompts, run logs, the state database, the
  dashboard, a shipped pull request or the dispatcher, where docs/security.md says they are
  redacted or never sent.
- **`jev ship`.** Pushing without the opt-in or confirmation, force-pushing, or pushing
  anything other than the run's result branch.

Out of scope:

- vulnerabilities in Claude Code, Codex, OpenRouter, TypeSafe, `gh`, Playwright or other tools
  jev runs: report those to their vendors (tell us too if jev should work around it);
- an agent doing what its configured permissions allow, including through prompt injection,
  when jev granted no more than documented (docs/security.md describes these limits, for
  example that read-only roles can call every tool of the MCP servers you give them);
- server access `"full"`, which is deliberately not implemented;
- issues that need an attacker who already controls your user account, your user config or
  your shell environment.

If you are unsure whether something is in scope, report it privately anyway.
