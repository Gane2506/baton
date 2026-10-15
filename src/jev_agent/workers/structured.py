"""Shared helpers for workers that speak structured output.

JSON schemas for strict structured outputs, fenced-JSON parsing, and the JSON-lines and
quote helpers the CLI workers share. These used to live in `workers/api.py`, which codex
and summary imported as if it were a utils module (issue #37).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel


def normalize_quotes(text: str) -> str:
    """Straighten curly apostrophes so message matching works on either form."""
    return text.replace("\u2019", "'").replace("\u2018", "'")


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    """The JSON objects in `text`, one per line; other lines are ignored."""
    objs: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model.model_json_schema()` made acceptable to strict structured outputs.

    Every object gets `additionalProperties: false` and lists all its properties as required,
    and `default` keywords are dropped (strict mode rejects or ignores them). Fields with
    defaults therefore become required in the wire schema; the model can still send the default
    value, and pydantic validates the result against the real model afterwards.
    """
    schema = model.model_json_schema()
    _make_strict(schema)
    return schema


def _make_strict(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties", {})
            node["additionalProperties"] = False
            node["required"] = list(props)
        for value in node.values():
            _make_strict(value)
    elif isinstance(node, list):
        for item in node:
            _make_strict(item)


_FENCED_JSON = re.compile(r"```(?:json)?[ \t]*\n(?P<body>.*?)\n?```", re.DOTALL | re.I)


def parse_json_content(content: str) -> Any:
    """Parse a JSON reply, tolerating a ```json fenced block around it."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = _FENCED_JSON.search(content)
        if match is None:
            raise
        return json.loads(match.group("body"))


def structured_summary(parsed: BaseModel) -> str:
    """A one-line summary of a structured reply, e.g. "plan with 4 tasks"."""
    tasks = getattr(parsed, "tasks", None)
    if isinstance(tasks, list):
        return f"plan with {len(tasks)} task{'s' if len(tasks) != 1 else ''}"
    verdict = getattr(parsed, "verdict", None)
    if isinstance(verdict, str):
        issues = getattr(parsed, "issues", None) or []
        n = len(issues)
        return f"verdict: {verdict} ({n} issue{'s' if n != 1 else ''})"
    return f"{type(parsed).__name__} parsed"
