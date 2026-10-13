"""The virtual browser: the pinned Playwright MCP server and the browser install command
(docs/verification.md section 9, checked 2026-10-13).

- `@playwright/mcp@0.0.83` (published 2026-09-28) depends on
  `playwright@1.64.0-alpha-1790635538000`; installing Chromium with that same Playwright
  version keeps the browser build the server expects. `--with-deps` also installs the system
  libraries (on Linux, through the package manager, which needs root).
- The server runs headed by default; jev passes `--headless` unless `[browser] headless = false`
  (for watching on a VPS through Xvfb + noVNC, docs/servers.md). `--browser chromium` selects
  Playwright's Chromium (the default is the Chrome channel, which the install does not provide)
  and `--isolated` keeps the profile in memory, so no cookies or logins persist between runs.
"""

from __future__ import annotations

from pathlib import Path

from jev_agent.config.schema import McpServerConfig

PLAYWRIGHT_MCP_VERSION = "0.0.83"
PLAYWRIGHT_VERSION = "1.64.0-alpha-1790635538000"  # the dependency of @playwright/mcp 0.0.83
BROWSER_MCP_NAME = "jev_browser"
BROWSER_DIRNAME = "browser"


def install_argv() -> list[str]:
    """`npx -y playwright@<pinned> install --with-deps chromium`."""
    return ["npx", "-y", f"playwright@{PLAYWRIGHT_VERSION}", "install", "--with-deps", "chromium"]


def mcp_args(*, headless: bool, output_dir: Path | None = None) -> list[str]:
    args = ["-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}", "--browser", "chromium"]
    args.append("--isolated")
    if headless:
        args.append("--headless")
    if output_dir is not None:
        args += ["--output-dir", str(output_dir)]
    return args


def browser_mcp_config(*, headless: bool, output_dir: Path | None = None) -> McpServerConfig:
    """The `[tools.mcp]`-style entry jev adds for a role with the browser."""
    return McpServerConfig(command="npx", args=mcp_args(headless=headless, output_dir=output_dir))
