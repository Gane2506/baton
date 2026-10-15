# Demo project (`jev init --demo`)

One command scaffolds a demo project pre-populated with realistic state, so you can try
the dashboard immediately without running any agents:

```bash
cp -R examples/sample-repo ~/textstats && cd ~/textstats
git init && git add . && git commit -m "Initial commit"
jev init --demo
jev board        # sample runs and queue items
jev dashboard    # the web dashboard, with a demo banner
```

## What it creates

`jev init --demo` runs a normal `jev init` first, then seeds `.jev/` with made-up data via
`jev_agent.demo.seeder` (the same seeder behind `examples/demo/seed_demo.py`, which the
screenshots pipeline uses):

- **Runs** — several weeks of them, in every state: done, executing, awaiting approval,
  planning, and one blocked waiting on a human.
- **Queue items** — four ideas at different priorities, one of them plan-only.
- **Lessons** — a `textstats` learning profile with six lessons, plus a disabled
  `python-style` profile.
- **Spend data** — per-attempt costs that feed `jev kpi` and the dashboard's spend page.
- **Shipped PRs** — ten made-up pull requests (merged, closed, open) with KPI snapshots.
- **A saved chat** — a short conversation that ends with a brief.
- **Quality reports** — gate results and an AI review on the richest run.
- **An offline model list** — cached from the test fixtures, so `jev models list --free`
  works without network.

Nothing is pushed and GitHub is never called. No agent, model or network is called.

## Clearly marked as demo

- `.jev/demo.json` — a marker file recording when the data was seeded and how to remove it.
- The dashboard shows a **"Demo data"** banner on every page while the marker exists.
- Seeded run logs are labeled `[demo]` and note that no agent ran.

## Safety

The seeder never touches anything outside the target repo: no user config, no credentials,
no network. It deliberately does **not** apply the screenshots pipeline's demo `jev.toml`
dressing (rules dispatcher, example MCP servers and skills): those change guarded keys,
which would need the repo trusted in your own config (`[trust] repos`, docs/security.md).
A demo works immediately, with no trust step. `jev init --demo` also refuses to seed a repo
whose `.jev/` already holds state — start from a clean repo instead.

## Reset and re-seed

`jev demo reset` removes the demo state, leaving a clean `.jev/` (it asks first; `--yes`
skips the question). It refuses when the repo has no demo marker, so it can never delete
real data. `jev.toml` is left untouched.

```bash
jev demo reset          # ask first
jev demo reset --yes    # no question
```

Manual equivalent: `rm -rf .jev` (and `jev init` again if you want a fresh state dir).
To re-seed after a reset: `jev init --demo --force` (the config already exists, so
`--force` is needed to rewrite it).
