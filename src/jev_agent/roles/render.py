"""Render role prompts: Jinja2 templates shipped in this package, or a user's own file.

Templates use `StrictUndefined`, so a variable the template uses but the caller did not pass
is an error rather than silently empty text. Pass `None` explicitly for "not applicable".

Every failure to load, parse or render a template is raised as `PromptError`, which names the
role and the file, so callers handle bad templates in one place.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, StrictUndefined, Template, TemplateSyntaxError

from jev_agent.core.models import Role
from jev_agent.roles.schemas import CODER_SUMMARY_MARKER

_env = Environment(
    undefined=StrictUndefined,
    autoescape=False,  # prompts are plain text, not HTML
    keep_trailing_newline=True,
)
# Available to every template, built-in or user-supplied.
_env.globals["summary_marker"] = CODER_SUMMARY_MARKER


class PromptError(Exception):
    """A role's prompt template could not be read, parsed or rendered."""

    def __init__(self, role: Role, prompt_file: Path | None, detail: str) -> None:
        where = "built-in template" if prompt_file is None else f"prompt_file {prompt_file}"
        super().__init__(f"{role} prompt ({where}): {detail}")
        self.role = role
        self.prompt_file = prompt_file
        self.detail = detail


def builtin_template(role: Role) -> str:
    """The text of the prompt template shipped with jev for `role`."""
    return files("jev_agent.roles").joinpath(role, "prompt.md").read_text(encoding="utf-8")


def load_template(role: Role, prompt_file: Path | None = None) -> Template:
    """Read and parse the template for `role` without rendering it.

    Raises `PromptError` if the file cannot be read or is not a valid Jinja2 template.
    """
    try:
        source = (
            builtin_template(role)
            if prompt_file is None
            else prompt_file.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise PromptError(role, prompt_file, "file not found") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(role, prompt_file, f"cannot read: {exc}") from exc
    try:
        return _env.from_string(source)
    except TemplateSyntaxError as exc:
        raise PromptError(
            role, prompt_file, f"Jinja2 syntax error on line {exc.lineno}: {exc.message}"
        ) from exc


def render_prompt(role: Role, prompt_file: Path | None = None, **variables: object) -> str:
    """Render the prompt for `role`, from `prompt_file` if given, else the built-in template.

    Raises `PromptError` if the template cannot be loaded, or if rendering fails (for example
    `UndefinedError` when the template uses a variable not in `variables`).
    """
    template = load_template(role, prompt_file)
    try:
        return template.render(**variables)
    except Exception as exc:  # any error raised while running the user's template
        raise PromptError(role, prompt_file, f"{type(exc).__name__}: {exc}") from exc
