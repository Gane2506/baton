"""The Design Studio reverse flow behind the dashboard (workstream DS-02, issue #124).

`GET /design-studio/reverse` renders the entry form (a website URL xor PNG
screenshots); `POST /design-studio/reverse` validates the submission, saves
uploads to a temp dir and starts the reverse job in a background thread. The
status page polls `/design-studio/reverse/live` (the dashboard's `live.js`
region) until the job finishes, then shows the evidence summary and a link to
`/design-systems/{name}`.

Uploads are validated by content, not by extension: the 8-byte PNG signature
must be present, each file is capped at 10MB, at most 8 files per job, and the
client-supplied filename is never trusted (one sanitized path segment in a
fresh temp dir, cleaned up when the job ends).

The SSRF guards stay where the CLI has them: the URL path goes through
`core.design_reverse.fetch_url_evidence`, whose `_validate_fetch_url` refuses
non-http(s) schemes, credentialed URLs and non-public hosts — redirect hops
included. The dashboard adds its own scheme check first, for a fast clear
error, but never fetches a URL itself.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from starlette.datastructures import FormData, UploadFile

from jev_agent.app.design_reverse import (
    ReverseOutcome,
    checked_reverse_target,
    run_reverse,
)

__all__ = [
    "MAX_UPLOADS",
    "MAX_UPLOAD_BYTES",
    "ReverseBusy",
    "ReverseExecutor",
    "ReverseRunner",
    "ReverseStatus",
    "ReverseUploadError",
    "SavedUploads",
    "sanitize_upload_filename",
    "save_png_uploads",
]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_UPLOAD_BYTES = 10_000_000  # per file
MAX_UPLOADS = 8
_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class ReverseUploadError(ValueError):
    """A screenshot upload the dashboard refuses (not a PNG, too big, too many)."""


class ReverseBusy(RuntimeError):
    """A reverse job is already running."""


def sanitize_upload_filename(filename: str, index: int) -> str:
    """One safe path segment for an upload, always ending in `.png`.

    The client-supplied filename is never trusted: directory components are
    dropped, unsafe characters become `-`, and the result is capped at 64 chars.
    """
    raw = Path(filename or "").name  # drop any directory components
    safe = _SAFE_NAME_CHARS.sub("-", raw).strip("-") or f"screenshot-{index}"
    if not safe.lower().endswith(".png"):
        safe = f"{safe}.png"
    return safe[:64]


@dataclass(frozen=True)
class SavedUploads:
    """Validated uploads on disk. The caller deletes `tmpdir` after the job."""

    tmpdir: Path
    paths: tuple[Path, ...]


async def save_png_uploads(form: FormData) -> SavedUploads:
    """Validate the `screenshots` multipart uploads and save them to a temp dir.

    PNG by content sniffing (the 8-byte signature), not the extension; empty
    files, files over 10MB and more than 8 files are refused with
    `ReverseUploadError`. The temp dir is removed when validation fails; on
    success the caller (the reverse runner) removes it after the job.
    """
    uploads = [
        value
        for value in form.getlist("screenshots")
        if isinstance(value, UploadFile) and (value.filename or "").strip()
    ]
    if not uploads:
        raise ReverseUploadError("choose at least one .png screenshot to upload.")
    if len(uploads) > MAX_UPLOADS:
        raise ReverseUploadError(
            f"at most {MAX_UPLOADS} screenshots per reverse; got {len(uploads)}."
        )
    tmpdir = Path(tempfile.mkdtemp(prefix="jev-reverse-"))
    saved: list[Path] = []
    try:
        for index, upload in enumerate(uploads, start=1):
            name = sanitize_upload_filename(upload.filename or "", index)
            target = tmpdir / name
            size = 0
            head = b""
            with target.open("wb") as handle:
                while True:
                    chunk = await upload.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ReverseUploadError(f"{name}: larger than the 10MB per-file limit.")
                    if len(head) < len(_PNG_SIGNATURE):
                        head += chunk[: len(_PNG_SIGNATURE) - len(head)]
                    handle.write(chunk)
            if size == 0:
                raise ReverseUploadError(f"{name}: the file is empty.")
            if head != _PNG_SIGNATURE:
                raise ReverseUploadError(
                    f"{name}: not a PNG file (checked the file's contents, not its extension)."
                )
            saved.append(target)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return SavedUploads(tmpdir=tmpdir, paths=tuple(saved))


#: (repo root, url or None, screenshot paths, library name) -> the saved system.
#: Tests inject a fake instead of running the evidence/designer pipeline.
ReverseExecutor = Callable[[Path, str | None, tuple[Path, ...], str], ReverseOutcome]


def _default_executor(
    root: Path, url: str | None, screenshots: tuple[Path, ...], name: str
) -> ReverseOutcome:
    target = checked_reverse_target(url=url, screenshots=screenshots, name=name)
    return run_reverse(root, target)


@dataclass(frozen=True)
class ReverseStatus:
    """What the status page shows: the job's state and, when done, its outcome."""

    state: str  # "idle" | "running" | "done" | "failed"
    target: str  # the URL or the screenshot file names, for display
    message: str  # one-line outcome, when finished
    started_at: datetime | None
    finished_at: datetime | None
    outcome: ReverseOutcome | None


class ReverseRunner:
    """One design reverse at a time, owned by the dashboard process.

    The job runs in a background thread (the designer worker call may take
    minutes); the status page polls `status()`. Uploaded screenshots live in a
    temp dir that is always removed when the job ends, success or failure.
    """

    def __init__(self, root: Path, executor: ReverseExecutor = _default_executor) -> None:
        self.root = root
        self.executor = executor
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._target = ""
        self._message = ""
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._outcome: ReverseOutcome | None = None

    # ---- state -------------------------------------------------------------------

    def status(self) -> ReverseStatus:
        with self._guard:
            return ReverseStatus(
                state=self._state,
                target=self._target,
                message=self._message,
                started_at=self._started_at,
                finished_at=self._finished_at,
                outcome=self._outcome,
            )

    def busy(self) -> bool:
        with self._guard:
            return self._state == "running"

    # ---- control -------------------------------------------------------------------

    def start(
        self,
        *,
        url: str | None,
        screenshots: tuple[Path, ...],
        name: str,
        tmpdir: Path | None,
        target: str,
    ) -> None:
        """Start the reverse in a background thread; `ReverseBusy` when one runs."""
        with self._guard:
            if self._state == "running":
                raise ReverseBusy("a design reverse is already running")
            self._state = "running"
            self._target = target
            self._message = ""
            self._outcome = None
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            thread = threading.Thread(
                target=self._work,
                args=(url, screenshots, name, tmpdir),
                name="jev-design-reverse",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._state = "idle"
                self._thread = None
                raise

    def _work(
        self,
        url: str | None,
        screenshots: tuple[Path, ...],
        name: str,
        tmpdir: Path | None,
    ) -> None:
        try:
            outcome = self.executor(self.root, url, screenshots, name)
        except Exception as exc:  # surfaced on the status page; nothing is saved
            with self._guard:
                self._state = "failed"
                self._message = str(exc) or type(exc).__name__
                self._finished_at = datetime.now(UTC)
        else:
            with self._guard:
                self._state = "done"
                self._outcome = outcome
                self._message = outcome.message
                self._finished_at = datetime.now(UTC)
        finally:
            self._thread = None
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Wait for a running reverse (the lifespan calls this)."""
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
