"""Sidebar navigation, the Jinja environment and its formatting filters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from jinja2 import Environment, PackageLoader

from jev_agent import __version__
from jev_agent.app.format import format_age, format_time
from jev_agent.core.benchmarks import format_value
from jev_agent.dashboard.render import render_daily_spend, render_trend_chart
from jev_agent.dashboard.security import CSRF_FIELD


@dataclass(frozen=True)
class NavChild:
    """One entry in a nav parent's submenu panel.

    `method="get"` renders a plain link; `method="post"` renders a small
    CSRF-protected form (for actions like "New chat", which create server-side
    state and are POST-only routes).
    """

    href: str
    label: str
    method: Literal["get", "post"] = "get"


@dataclass(frozen=True)
class NavItem:
    """One sidebar entry. Entries with `children` render as a toggle button that
    opens a submenu panel adjacent to the collapsed sidebar instead of a plain
    link, so any section can adopt a submenu by declaring its children here."""

    href: str
    label: str
    children: tuple[NavChild, ...] = ()

    def _href_active(self, href: str, path: str) -> bool:
        if href == "/":
            return path == "/" or path.startswith("/runs/")
        return path == href or path.startswith(href)

    def is_active(self, path: str) -> bool:
        """Whether this entry's page (or one of its descendants) is current."""
        return self._href_active(self.href, path)

    def child_active(self, child: NavChild, path: str) -> bool:
        """Whether a submenu child matches the current path."""
        return self._href_active(child.href, path)

    def submenu_open(self, path: str) -> bool:
        """Whether the submenu panel renders open for this path: only on the
        parent's own page or a declared child's exact page. Deeper pages under
        a child's prefix (e.g. a chat detail view) keep the parent highlighted
        via is_active() but do not auto-open the panel."""
        if not self.children:
            return False
        if path == self.href:
            return True
        return any(path == child.href for child in self.children)


NAV: tuple[NavItem, ...] = (
    NavItem("/", "Board"),
    NavItem("/queue", "Queue"),
    NavItem("/artifacts", "Artifacts"),
    NavItem("/agents", "Agents"),
    NavItem("/fleet", "Fleet"),
    NavItem(
        "/chats",
        "Chats",
        children=(
            NavChild("/chats", "All chats"),
            NavChild("/chats/new", "New chat", method="post"),
        ),
    ),
    NavItem("/learning", "Learning"),
    NavItem("/profiles", "Profiles"),
    NavItem("/design-systems", "Design Systems"),
    NavItem("/design-studio", "Design Studio"),
    NavItem("/tools", "Tools"),
    NavItem("/code-grade", "Code Grade"),
    NavItem("/connections", "Connections"),
    NavItem("/spend", "Spend"),
    NavItem("/kpis", "KPIs"),
    NavItem("/settings", "Settings"),
    NavItem("/diagnostics", "Diagnostics"),
)


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _time(value: datetime | str | float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            return format_time(datetime.fromisoformat(value))
        except ValueError:
            return value
    if isinstance(value, (float, int)):
        return format_time(datetime.fromtimestamp(value, tz=UTC))
    return format_time(value)


def _age(value: datetime) -> str:
    return format_age(value, datetime.now(UTC))


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("jev_agent.dashboard", "templates"),
        autoescape=True,  # every value is escaped unless it is explicitly Markup
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["usd"] = _usd
    env.filters["time"] = _time
    env.filters["age"] = _age
    env.globals["nav"] = NAV
    env.globals["version"] = __version__
    env.globals["csrf_field"] = CSRF_FIELD
    env.globals["daily_bars_svg"] = render_daily_spend
    env.globals["trend_chart_svg"] = render_trend_chart
    env.globals["fmt"] = format_value
    return env
