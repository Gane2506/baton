"""The application layer: use cases shared by the CLI and the web UI (issue #92).

Everything here is front-end agnostic: typed inputs, typed results, user-facing errors
(`JevError`). No printing, no `typer`/`rich`, no exits, no prompts. `cli/` and `dashboard/`
are thin adapters on top: they parse input, call these functions, and render the result.
"""
