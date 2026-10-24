"""Module-path helpers: normalizing, qualifying, and splitting `slug:path` refs.

Shared by the lesson store (path-looking tags become `applies-to` edges) and the
graph (explicit `--module` filters and edge endpoints).
"""

from __future__ import annotations

import re
from pathlib import Path


def normalize_module_path(tag: str) -> str | None:
    """`tag` as a normalized module path, or None when it is not path-looking.

    Path-looking means containing `/` or ending in `.py` (the design's rule). The path is
    lowercased with leading `./` and surrounding slashes stripped, mirroring how
    `context_terms` lowercases and prefix-matches paths. Used for deriving modules from
    tags, where a strict rule keeps keyword tags from becoming module nodes.
    """
    path = tag.strip().removeprefix("./").strip("/")
    if not path or ("/" not in path and not path.lower().endswith(".py")):
        return None
    return path.lower()


def normalize_module_ref(ref: str) -> str | None:
    """Lenient module normalization for explicit input (CLI args, the learner's
    `applies_to`, `--module` filters): lowercased with `./` and slashes stripped. Bare
    directory names (`src`) are allowed; empty or whitespace-containing refs are not."""
    path = ref.strip().removeprefix("./").strip("/")
    if not path or any(ch.isspace() for ch in path):
        return None
    return path.lower()


def qualify_module(path: str, repo_slug: str | None) -> str:
    """A module path as stored on edges: `slug:path` for shared profiles, plain otherwise."""
    return f"{repo_slug}:{path}" if repo_slug else path


def split_module_ref(ref: str) -> tuple[str | None, str]:
    """A module reference (`path` or `slug:path`) into its slug and path parts."""
    ref = ref.strip()
    if ":" in ref:
        head, tail = ref.split(":", 1)
        if "/" in tail or tail.lower().endswith(".py"):
            return head.strip().lower() or None, tail
    return None, ref


def repo_slug_for(repo: Path | str) -> str:
    """Stable, human-meaningful repo slug from a directory name (design risk 4).

    `…/code/baton` → `baton`. Derived from the directory name, not the remote URL: it is
    visible without git, stable across clones that keep the checkout name, and readable in
    qualified module paths like `baton:src/auth/login.py`. Non-alphanumerics collapse to
    one `-`; an empty name falls back to `repo`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", Path(repo).name.strip().lower()).strip("-")
    return slug or "repo"


__all__ = [
    "normalize_module_path",
    "normalize_module_ref",
    "qualify_module",
    "repo_slug_for",
    "split_module_ref",
]
