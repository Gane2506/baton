"""`jev design reverse` as a callable pipeline, shared by the CLI and the dashboard.

The CLI (`cli.design_cmds.reverse`) is the reference flow; the dashboard runs the
same steps in a background thread (`dashboard/design_reverse.py`). The pipeline:

1. `checked_reverse_target` — validate exactly one URL xor one-or-more PNG paths,
   and the library name (the CLI's rules, raising `ValueError` instead of `fail`).
2. `gather_evidence` — `fetch_url_evidence` (its SSRF guards stay intact: only
   http(s), no credentials, every resolved address globally routable, redirects
   re-checked) or `palette_from_png` per screenshot, then `evidence_to_brief`.
3. `extract_with_designer` — the designer role's extraction: the same `WorkerJob`
   invocation the CLI uses (plain-text reply, log at
   `<repo>/.jev/design/designer-reverse.log`), then `parse_extraction_output` +
   `validate_extraction_tokens`.
4. `run_reverse` — the full flow: evidence, the designer (injectable for tests),
   then `save_library_system`.

Nothing here touches the terminal: failures raise `JevError` / `ValueError`,
never `typer.Exit`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jev_agent.app.context import NO_WORKERS_MESSAGE, open_context
from jev_agent.app.errors import JevError
from jev_agent.core.design_library import save_library_system, validate_library_name
from jev_agent.core.design_reverse import (
    evidence_to_brief,
    fetch_url_evidence,
    is_url_target,
    palette_from_png,
    parse_extraction_output,
    suggest_library_name,
    validate_extraction_tokens,
)
from jev_agent.core.design_system import design_dir
from jev_agent.core.harness import DEFAULT_TIMEOUT_S, Harness, with_prompt_addendum
from jev_agent.dispatch.requests import DEFAULT_EFFORT, DEFAULT_MODEL
from jev_agent.roles.render import PromptError, render_prompt
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.registry import WorkerNotAvailable

__all__ = [
    "DesignerFn",
    "ReverseEvidence",
    "ReverseOutcome",
    "ReverseTarget",
    "checked_reverse_target",
    "extract_with_designer",
    "extraction_prompt",
    "gather_evidence",
    "run_reverse",
]


@dataclass(frozen=True)
class ReverseTarget:
    """A validated reverse request: exactly one URL xor one-or-more PNG paths."""

    url: str | None
    screenshots: tuple[Path, ...]
    name: str  # a validated library name


def checked_reverse_target(
    *,
    url: str | None,
    screenshots: Iterable[str | Path],
    name: str | None,
) -> ReverseTarget:
    """Validate a reverse request with the CLI's rules (`ValueError` on failure).

    Exactly one of `url` / `screenshots`; the URL must be http(s) (the fetch's
    SSRF guard re-checks this and more); screenshots must exist and be `.png`.
    A blank `name` falls back to `suggest_library_name`, which always passes
    `validate_library_name`.
    """
    clean_url = (url or "").strip()
    shots = [Path(s) for s in screenshots]
    if clean_url and shots:
        raise ValueError("pass either one URL or only .png image paths, not both.")
    if not clean_url and not shots:
        raise ValueError("give a website URL or at least one .png image.")
    if clean_url:
        if not is_url_target(clean_url):
            raise ValueError(f"{clean_url!r}: only http(s) URLs are supported.")
        targets: Sequence[str] = [clean_url]
    else:
        for path in shots:
            if path.suffix.lower() != ".png":
                raise ValueError(f"{path}: only .png screenshots are supported.")
            if not path.is_file():
                raise ValueError(f"image not found: {path}")
        targets = [str(path) for path in shots]
    clean_name = (name or "").strip()
    system_name = (
        validate_library_name(clean_name)
        if clean_name
        else validate_library_name(suggest_library_name(list(targets)))
    )
    return ReverseTarget(url=clean_url or None, screenshots=tuple(shots), name=system_name)


@dataclass(frozen=True)
class ReverseEvidence:
    """The gathered evidence: the designer brief plus the raw evidence dict."""

    name: str
    source: str  # `reverse:<url>` or `reverse:screenshots`
    brief: str
    evidence: dict[str, object]


def gather_evidence(target: ReverseTarget) -> ReverseEvidence:
    """Collect evidence for `target`; raises `JevError` when it cannot be read.

    The URL path goes through `fetch_url_evidence`, whose guards refuse
    non-http(s) schemes, credentialed URLs and non-public hosts (redirect hops
    included). A refused/failed fetch is a `JevError` with the guard's message,
    exactly like the CLI's `error:` line.
    """
    if target.url is not None:
        url_evidence = fetch_url_evidence(target.url)
        if isinstance(url_evidence.get("error"), str):
            raise JevError(str(url_evidence["error"]))
        evidence: dict[str, object] = {
            "target": target.url,
            "kind": "url",
            "url": target.url,
            "url_evidence": url_evidence,
            "screenshots": None,
        }
        source = f"reverse:{target.url}"
    else:
        shots: list[dict[str, object]] = []
        for path in target.screenshots:
            try:
                palette = palette_from_png(path)
            except (OSError, ValueError) as exc:
                raise JevError(f"could not read {path}: {exc}") from exc
            shots.append({"path": str(path), "palette": palette})
        evidence = {
            "target": ", ".join(str(path) for path in target.screenshots),
            "kind": "screenshots",
            "url": None,
            "url_evidence": None,
            "screenshots": shots,
        }
        source = "reverse:screenshots"
    return ReverseEvidence(
        name=target.name,
        source=source,
        brief=evidence_to_brief(evidence),
        evidence=evidence,
    )


def extraction_prompt(h: Harness, brief: str) -> str:
    """The designer role's extraction prompt for an evidence brief.

    Mirrors `Harness.prompt` but loads `extract.md` instead of the role's
    `prompt.md`: the reverse flow extracts a system from evidence rather than
    designing screens.
    """
    extract_md = Path(str(files("jev_agent.roles").joinpath("designer", "extract.md")))
    try:
        text = render_prompt("designer", extract_md, brief=brief, lessons=[])
    except PromptError as exc:
        raise JevError(str(exc)) from exc
    return with_prompt_addendum(text, h.config.roles.designer.prompt_addendum)


def extract_with_designer(h: Harness, root: Path, brief: str) -> tuple[dict[str, object], str]:
    """Run the designer role to extract `tokens.json` + system markdown from `brief`.

    The same `WorkerJob` invocation `jev design reverse` uses: the designer
    role, a plain-text reply (a ```tokens.json fence + markdown), the attempt
    logged to `<repo>/.jev/design/designer-reverse.log`. Raises `JevError` when
    no designer worker is available, the worker fails, or its output does not
    parse/validate — nothing is saved then.
    """
    options = h.options("designer")
    if not options.workers:
        raise JevError(
            "no available worker for role designer: enable one of its workers in "
            "config (`jev doctor` shows which are usable)"
        )
    prompt = extraction_prompt(h, brief)
    assignment = h.assignment(
        "designer",
        {
            "worker": options.workers[0],
            "model": DEFAULT_MODEL,
            "effort": DEFAULT_EFFORT,
        },
    )
    try:
        worker = h.workers.get(assignment.worker)
    except WorkerNotAvailable as exc:
        raise JevError(str(exc)) from exc
    timeout = (
        getattr(getattr(h.config.workers, assignment.worker, None), "timeout_s", 0)
        or DEFAULT_TIMEOUT_S
    )
    design_dir(root).mkdir(parents=True, exist_ok=True)
    job = WorkerJob(
        role="designer",
        prompt=prompt,
        workdir=root,
        log_path=design_dir(root) / "designer-reverse.log",
        model=assignment.model,
        effort=assignment.effort,
        timeout_s=timeout,
        output_schema=None,  # plain-text reply: a ```tokens.json fence + markdown
        attempt=1,
    )
    output = asyncio.run(worker.run(job))
    if output.result.status != "success":
        raise JevError(
            f"the designer produced no extraction: {output.result.summary or output.result.status}"
        )
    summary = output.result.summary or ""
    try:
        tokens, markdown = parse_extraction_output(summary)
    except ValueError as exc:
        raise JevError(str(exc)) from exc
    try:
        tokens = validate_extraction_tokens(tokens)
    except ValueError as exc:
        raise JevError(
            f"the designer's tokens failed validation and nothing was saved: {exc}"
        ) from exc
    return tokens, markdown


#: The designer step of `run_reverse`, injectable in tests: (harness, repo root,
#: evidence brief) -> (validated tokens, system markdown).
DesignerFn = Callable[[Harness, Path, str], tuple[dict[str, object], str]]


@dataclass(frozen=True)
class ReverseOutcome:
    """A finished reverse: the saved library system and the evidence behind it."""

    name: str
    version: int
    source: str
    evidence: dict[str, object]
    message: str


def run_reverse(
    root: Path,
    target: ReverseTarget,
    *,
    designer: DesignerFn | None = None,
) -> ReverseOutcome:
    """The full reverse pipeline: evidence → designer extraction → library save.

    `designer` replaces the worker call in tests (the "mocked designer"); tests
    stub the network by monkeypatching this module's `open_context` /
    `fetch_url_evidence`, the same pattern `test_design_reverse.py` uses.
    Raises `JevError`/`ValueError` on any failure; nothing is saved then.
    """
    evidence = gather_evidence(target)
    with open_context(root) as ctx:
        if not ctx.has_workers:
            raise JevError(NO_WORKERS_MESSAGE)
        extract = designer or extract_with_designer
        tokens, markdown = extract(ctx.pipeline.h, ctx.root, evidence.brief)
    version = save_library_system(
        target.name,
        markdown,
        dict(tokens),
        meta_extra={"name": target.name, "source": evidence.source, "app_type": "website"},
    )
    return ReverseOutcome(
        name=target.name,
        version=version,
        source=evidence.source,
        evidence=evidence.evidence,
        message=f'reversed "{target.name}" into the library '
        f"(v{version}, source: {evidence.source})",
    )
