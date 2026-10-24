"""The dashboard's Design Studio reverse flow (workstream DS-02, issue #124).

Covers: the PNG uploader validation (content sniffing, size/count limits,
filename sanitizing), the URL SSRF rejection (the CLI's guards stay in effect),
the name/target validation shared with the CLI, the evidence pipeline with a
mocked designer, the background runner, and the dashboard routes.
"""

from __future__ import annotations

import io
import re
import shutil
import struct
import tempfile
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.datastructures import FormData, UploadFile
from starlette.testclient import TestClient

from jev_agent.app.design_reverse import (
    ReverseOutcome,
    checked_reverse_target,
    extract_with_designer,
    gather_evidence,
    run_reverse,
)
from jev_agent.app.errors import JevError
from jev_agent.core.design_library import load_library_system
from jev_agent.core.design_reverse import fetch_url_evidence
from jev_agent.core.harness import Harness
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.design_reverse import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOADS,
    ReverseBusy,
    ReverseRunner,
    ReverseUploadError,
    sanitize_upload_filename,
    save_png_uploads,
)
from jev_agent.dashboard.security import DashboardSettings

TOKENS = {
    "colors": {"primary": "#e8590c", "ink": "#1a1b1e"},
    "type_scale": {"base": 16},
    "spacing": {"md": 16},
    "radii": {"md": 8},
}
MARKDOWN = "# Acme\n\nA reversed marketing system.\n"
DESIGNER_REPLY = (
    "```tokens.json\n"
    '{"colors": {"primary": "#e8590c", "ink": "#1a1b1e"}, '
    '"type_scale": {"base": 16}, "spacing": {"md": 16}, "radii": {"md": 8}}\n'
    "```\n" + MARKDOWN
)


# ---- fixtures -----------------------------------------------------------------------------


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _encode_png(width: int, height: int, rows: list[list[tuple[int, int, int]]]) -> bytes:
    """Minimal stdlib truecolor PNG encoder (no filter), for upload fixtures."""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data))
        )

    raw = bytearray()
    for row in rows:
        raw.append(0)
        for r, g, b in row:
            raw.extend((r, g, b))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def _png_bytes() -> bytes:
    return _encode_png(2, 1, [[(255, 0, 0), (0, 0, 255)]])


def _write_png(tmp_path: Path, name: str, data: bytes = b"") -> Path:
    path = tmp_path / name
    path.write_bytes(data or _png_bytes())
    return path


def _upload(filename: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


def _form(*uploads: UploadFile) -> FormData:
    return FormData([("screenshots", upload) for upload in uploads])


# ---- uploader validation ------------------------------------------------------------------


async def test_save_png_uploads_accepts_real_png(tmp_path: Path) -> None:
    saved = await save_png_uploads(_form(_upload("Acme Home.png", _png_bytes())))
    try:
        assert len(saved.paths) == 1
        assert saved.paths[0].parent == saved.tmpdir
        assert saved.paths[0].name == "Acme-Home.png"
        assert saved.paths[0].read_bytes() == _png_bytes()
    finally:
        _rmtree(saved.tmpdir)


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


async def test_save_png_uploads_rejects_non_png_content() -> None:
    with pytest.raises(ReverseUploadError, match="not a PNG"):
        await save_png_uploads(_form(_upload("shot.png", b"this is not a png")))


async def test_save_png_uploads_rejects_gif_bytes() -> None:
    gif = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
    with pytest.raises(ReverseUploadError, match="not a PNG"):
        await save_png_uploads(_form(_upload("shot.png", gif)))


async def test_save_png_uploads_rejects_empty_file() -> None:
    with pytest.raises(ReverseUploadError, match="empty"):
        await save_png_uploads(_form(_upload("shot.png", b"")))


async def test_save_png_uploads_rejects_no_files() -> None:
    with pytest.raises(ReverseUploadError, match="at least one"):
        await save_png_uploads(FormData())


async def test_save_png_uploads_enforces_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jev_agent.dashboard.design_reverse.MAX_UPLOAD_BYTES", 32)
    data = _png_bytes()
    assert len(data) > 32  # the fixture really is over the stubbed limit
    with pytest.raises(ReverseUploadError, match="10MB"):
        await save_png_uploads(_form(_upload("big.png", data)))


async def test_save_png_uploads_enforces_count_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jev_agent.dashboard.design_reverse.MAX_UPLOADS", 2)
    uploads = tuple(_upload(f"s{i}.png", _png_bytes()) for i in range(3))
    with pytest.raises(ReverseUploadError, match="at most 2"):
        await save_png_uploads(_form(*uploads))


async def test_save_png_uploads_sanitizes_traversal_filename(tmp_path: Path) -> None:
    saved = await save_png_uploads(_form(_upload("../../etc/evil.png", _png_bytes())))
    try:
        assert saved.paths[0].parent == saved.tmpdir
        assert ".." not in saved.paths[0].name
        assert saved.paths[0].name.endswith(".png")
    finally:
        _rmtree(saved.tmpdir)


async def test_save_png_uploads_removes_tmpdir_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _tracking_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path: str = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
    with pytest.raises(ReverseUploadError):
        await save_png_uploads(_form(_upload("bad.png", b"nope")))
    assert created and not any(path.exists() for path in created)


def test_sanitize_upload_filename() -> None:
    assert sanitize_upload_filename("../../etc/passwd", 1) == "passwd.png"
    assert sanitize_upload_filename("Acme Home.PNG", 1) == "Acme-Home.PNG"
    assert sanitize_upload_filename("", 3) == "screenshot-3.png"
    assert sanitize_upload_filename("a/b\\c.png", 1) == "b-c.png"  # no separators survive
    assert len(sanitize_upload_filename("x" * 200 + ".png", 1)) <= 64


def test_upload_limits_are_sane() -> None:
    assert MAX_UPLOAD_BYTES == 10_000_000
    assert MAX_UPLOADS == 8


# ---- SSRF rejection (the CLI's guards stay in effect) --------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:9/status",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_fetch_url_evidence_refuses_unsafe_targets(url: str) -> None:
    """No network is touched: unsafe hosts fail the guard before any fetch."""
    result = fetch_url_evidence(url)
    assert isinstance(result.get("error"), str), url


def test_gather_evidence_raises_jev_error_on_refused_fetch() -> None:
    target = checked_reverse_target(url="http://127.0.0.1/", screenshots=[], name="acme")
    with pytest.raises(JevError, match="refusing to fetch"):
        gather_evidence(target)


# ---- target + name validation (the CLI's rules) --------------------------------------------


def test_checked_reverse_target_defaults_name_from_url_host() -> None:
    target = checked_reverse_target(url="https://www.acme.co/pricing", screenshots=[], name=None)
    assert target.name == "acme-co"
    assert target.url == "https://www.acme.co/pricing"
    assert target.screenshots == ()


def test_checked_reverse_target_defaults_name_from_image_stem(tmp_path: Path) -> None:
    png = _write_png(tmp_path, "Acme Homepage.png")
    target = checked_reverse_target(url=None, screenshots=[png], name="  ")
    assert target.name == "acme-homepage"


def test_checked_reverse_target_rejects_both_or_neither() -> None:
    with pytest.raises(ValueError, match="not both"):
        checked_reverse_target(url="https://example.com", screenshots=[Path("a.png")], name=None)
    with pytest.raises(ValueError, match="at least one"):
        checked_reverse_target(url=None, screenshots=[], name=None)


def test_checked_reverse_target_rejects_non_http_url() -> None:
    with pytest.raises(ValueError, match="only http"):
        checked_reverse_target(url="ftp://example.com/x", screenshots=[], name=None)


def test_checked_reverse_target_rejects_bad_names(tmp_path: Path) -> None:
    png = _write_png(tmp_path, "a.png")
    for bad in ("has space", "../evil", "-leading", "x" * 65, ""):
        if bad == "":
            continue  # blank means "use the default"
        with pytest.raises(ValueError, match="invalid library design system name"):
            checked_reverse_target(url=None, screenshots=[png], name=bad)


def test_checked_reverse_target_rejects_missing_or_non_png(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"only \.png"):
        checked_reverse_target(url=None, screenshots=[tmp_path / "a.jpg"], name="acme")
    with pytest.raises(ValueError, match="image not found"):
        checked_reverse_target(url=None, screenshots=[tmp_path / "missing.png"], name="acme")


# ---- evidence pipeline with a mocked designer ----------------------------------------------


def _fake_designer(h: Any, root: Path, brief: str) -> tuple[dict[str, object], str]:
    assert "Design evidence" in brief
    return dict(TOKENS), MARKDOWN


@contextmanager
def _stub_context(root: Path) -> Iterator[Any]:
    yield SimpleNamespace(has_workers=True, pipeline=SimpleNamespace(h=object()), root=root)


def test_run_reverse_screenshots_with_mocked_designer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    png = _write_png(tmp_path, "Acme Home.png")
    target = checked_reverse_target(url=None, screenshots=[png], name=None)
    monkeypatch.setattr("jev_agent.app.design_reverse.open_context", _stub_context)
    outcome = run_reverse(tmp_path, target, designer=_fake_designer)
    assert outcome.name == "acme-home"
    assert outcome.version == 1
    assert outcome.source == "reverse:screenshots"
    assert outcome.evidence["kind"] == "screenshots"
    system = load_library_system("acme-home")
    assert system is not None
    assert system.tokens["colors"]["primary"] == "#e8590c"  # type: ignore[index]


def test_run_reverse_url_with_mocked_designer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    def _fake_fetch(url: str, timeout_s: int = 15) -> dict[str, object]:
        return {
            "url": url,
            "title": "Acme",
            "theme_color": "#0f172a",
            "fonts": [],
            "colors": {"#0f172a": 3},
            "styles": "",
        }

    monkeypatch.setattr("jev_agent.app.design_reverse.fetch_url_evidence", _fake_fetch)
    monkeypatch.setattr("jev_agent.app.design_reverse.open_context", _stub_context)
    target = checked_reverse_target(url="https://acme.example/", screenshots=[], name="acme-site")
    outcome = run_reverse(tmp_path, target, designer=_fake_designer)
    assert outcome.source == "reverse:https://acme.example/"
    assert outcome.evidence["kind"] == "url"
    assert load_library_system("acme-site") is not None


def test_run_reverse_saves_nothing_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    target = checked_reverse_target(url="http://127.0.0.1/", screenshots=[], name="acme-fail")
    monkeypatch.setattr("jev_agent.app.design_reverse.open_context", _stub_context)
    with pytest.raises(JevError, match="refusing to fetch"):
        run_reverse(tmp_path, target, designer=_fake_designer)
    assert load_library_system("acme-fail") is None


class _StubWorker:
    def __init__(self, reply: str, seen: dict[str, Any], status: str = "success") -> None:
        self._reply = reply
        self._seen = seen
        self._status = status

    async def run(self, job: Any) -> Any:
        self._seen["job"] = job
        return SimpleNamespace(result=SimpleNamespace(status=self._status, summary=self._reply))


class _StubHarness:
    """Just enough of `Harness` for `extract_with_designer`."""

    def __init__(
        self,
        seen: dict[str, Any],
        reply: str = DESIGNER_REPLY,
        *,
        workers: list[str] | None = None,
        status: str = "success",
    ) -> None:
        self._seen = seen
        self._reply = reply
        self._workers = ["fake-designer"] if workers is None else workers
        self._status = status
        self.config = SimpleNamespace(
            roles=SimpleNamespace(designer=SimpleNamespace(prompt_addendum="")),
            workers={},
        )

    def options(self, role: str) -> Any:
        return SimpleNamespace(workers=list(self._workers))

    def assignment(self, role: str, answers: dict[str, str]) -> Any:
        return SimpleNamespace(worker="fake-designer", model="m", effort="e")

    @property
    def workers(self) -> _StubHarness:
        return self

    def get(self, name: str) -> _StubWorker:
        assert name == "fake-designer"
        return _StubWorker(self._reply, self._seen, status=self._status)


def test_extract_with_designer_invocation(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    harness = cast(Harness, _StubHarness(seen))
    tokens, markdown = extract_with_designer(harness, tmp_path, "BRIEF")
    job = seen["job"]
    assert job.role == "designer"
    assert job.workdir == tmp_path
    assert Path(job.log_path).name == "designer-reverse.log"
    assert job.output_schema is None
    assert "BRIEF" in job.prompt
    assert tokens["colors"]["primary"] == "#e8590c"  # type: ignore[index]
    assert markdown == MARKDOWN.strip()  # parse_extraction_output strips the markdown


def test_extract_with_designer_rejects_failed_worker(tmp_path: Path) -> None:
    harness = cast(Harness, _StubHarness({}, status="error", reply="boom"))
    with pytest.raises(JevError, match="produced no extraction"):
        extract_with_designer(harness, tmp_path, "BRIEF")


def test_extract_with_designer_rejects_unparsable_output(tmp_path: Path) -> None:
    harness = cast(Harness, _StubHarness({}, reply="no fence here"))
    with pytest.raises(JevError):
        extract_with_designer(harness, tmp_path, "BRIEF")


def test_extract_with_designer_needs_a_designer_worker(tmp_path: Path) -> None:
    harness = cast(Harness, _StubHarness({}, workers=[]))
    with pytest.raises(JevError, match="no available worker for role designer"):
        extract_with_designer(harness, tmp_path, "BRIEF")


# ---- the background runner -----------------------------------------------------------------


def _ok_executor(
    root: Path, url: str | None, screenshots: tuple[Path, ...], name: str
) -> ReverseOutcome:
    return ReverseOutcome(
        name=name or "acme",
        version=1,
        source=f"reverse:{url or 'screenshots'}",
        evidence={"kind": "url" if url else "screenshots"},
        message="done",
    )


def test_runner_single_flight(tmp_path: Path) -> None:
    import threading

    release = threading.Event()
    started = threading.Event()

    def _slow(
        root: Path, url: str | None, screenshots: tuple[Path, ...], name: str
    ) -> ReverseOutcome:
        started.set()
        assert release.wait(timeout=10)
        return _ok_executor(root, url, screenshots, name)

    runner = ReverseRunner(tmp_path, executor=_slow)
    assert runner.status().state == "idle"
    runner.start(
        url="https://example.com",
        screenshots=(),
        name="acme",
        tmpdir=None,
        target="https://example.com",
    )
    assert started.wait(timeout=10)
    assert runner.busy()
    with pytest.raises(ReverseBusy):
        runner.start(
            url="https://example.com",
            screenshots=(),
            name="acme",
            tmpdir=None,
            target="https://example.com",
        )
    release.set()
    runner.shutdown()
    status = runner.status()
    assert status.state == "done"
    assert status.outcome is not None and status.outcome.name == "acme"


def test_runner_records_failure_and_cleans_tmpdir(tmp_path: Path) -> None:
    tmpdir = tmp_path / "upload-tmp"
    tmpdir.mkdir()
    (tmpdir / "shot.png").write_bytes(_png_bytes())

    def _boom(
        root: Path, url: str | None, screenshots: tuple[Path, ...], name: str
    ) -> ReverseOutcome:
        raise JevError("the designer produced no extraction: boom")

    runner = ReverseRunner(tmp_path, executor=_boom)
    runner.start(url=None, screenshots=(), name="acme", tmpdir=tmpdir, target="shot.png")
    runner.shutdown()
    status = runner.status()
    assert status.state == "failed"
    assert "produced no extraction" in status.message
    assert not tmpdir.exists()


# ---- dashboard routes ----------------------------------------------------------------------


def _post(
    client: TestClient, data: dict[str, str] | None = None, files: dict[str, Any] | None = None
) -> httpx.Response:
    response = client.post(
        "/design-studio/reverse",
        data={"csrf_token": _csrf(client), **(data or {})},
        files=files,
        follow_redirects=True,
    )
    assert isinstance(response, httpx.Response)
    return response


def test_reverse_page_renders_both_modes(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio/reverse")
    assert page.status_code == 200
    assert "From a website" in page.text
    assert "From screenshots" in page.text
    assert 'name="screenshots"' in page.text
    assert 'enctype="multipart/form-data"' in page.text


def test_reverse_post_rejects_empty_submission(client: TestClient, home: Path) -> None:
    response = _post(client, data={"url": "", "name": ""})
    assert "give a website URL or at least one .png screenshot" in response.text


def test_reverse_post_rejects_url_and_screenshots(client: TestClient, home: Path) -> None:
    response = _post(
        client,
        data={"url": "https://example.com", "name": ""},
        files={"screenshots": ("shot.png", _png_bytes(), "image/png")},
    )
    assert "not both" in response.text


def test_reverse_post_rejects_non_http_url(client: TestClient, home: Path) -> None:
    response = _post(client, data={"url": "ftp://example.com/x", "name": ""})
    assert "only http(s) URLs are supported" in response.text


def test_reverse_post_rejects_bad_name(client: TestClient, home: Path) -> None:
    response = _post(client, data={"url": "https://example.com", "name": "has space"})
    assert "invalid library design system name" in response.text


def test_reverse_post_rejects_non_png_upload(client: TestClient, home: Path) -> None:
    response = _post(
        client,
        data={"url": "", "name": ""},
        files={"screenshots": ("shot.png", b"not a png", "image/png")},
    )
    assert "not a PNG file" in response.text


def test_reverse_post_ssrf_target_fails_the_job(
    client: TestClient, home: Path, fixture_repo: Path
) -> None:
    """The real executor: the guard refuses the fetch, the job fails loudly."""
    response = _post(client, data={"url": "http://127.0.0.1/", "name": "acme"})
    assert response.request.url.path == "/design-studio/reverse/status"
    live = _wait_for_live(client, "refusing to fetch")
    assert "127.0.0.1" in live


def _client_with_fake_executor(
    fixture_repo: Path,
) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    seen: dict[str, Any] = {}

    def _fake(
        root: Path, url: str | None, screenshots: tuple[Path, ...], name: str
    ) -> ReverseOutcome:
        seen["screenshots"] = screenshots
        target = checked_reverse_target(url=url, screenshots=screenshots, name=name)
        evidence: dict[str, object] = {
            "target": url or "shots",
            "kind": "url" if url else "screenshots",
            "url": url,
            "url_evidence": {
                "url": url,
                "title": "Acme",
                "theme_color": "#0f172a",
                "fonts": ["Inter, sans-serif"],
                "colors": {"#0f172a": 3, "#ffffff": 2},
                "styles": "body { color: #0f172a; }",
            }
            if url
            else None,
            "screenshots": [
                {"path": str(path), "palette": [("#ff0000", 0.5), ("#0000ff", 0.5)]}
                for path in screenshots
            ]
            if screenshots
            else None,
        }
        return ReverseOutcome(
            name=target.name,
            version=1,
            source=f"reverse:{url}" if url else "reverse:screenshots",
            evidence=evidence,
            message=f'reversed "{target.name}" into the library (v1)',
        )

    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        reverse_executor=_fake,
    )
    with TestClient(app) as client:
        yield client, seen


def _wait_for_live(client: TestClient, text: str, timeout_s: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body: str = client.get("/design-studio/reverse/live").text
        if text in body:
            return body
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {text!r} in the live fragment")


def test_reverse_url_flow_shows_evidence_and_library_link(fixture_repo: Path, home: Path) -> None:
    for client, _seen in _client_with_fake_executor(fixture_repo):
        response = _post(client, data={"url": "https://acme.example/", "name": "acme-site"})
        assert response.request.url.path == "/design-studio/reverse/status"
        body = _wait_for_live(client, "Reversed into the library")
        assert 'href="/design-systems/acme-site"' in body
        assert "Acme" in body  # the page title from the evidence
        assert "#0f172a" in body  # theme color + top colors
        assert "Inter, sans-serif" in body


def test_reverse_upload_flow_sanitizes_and_cleans_up(fixture_repo: Path, home: Path) -> None:
    for client, seen in _client_with_fake_executor(fixture_repo):
        response = _post(
            client,
            data={"url": "", "name": ""},
            files={"screenshots": ("../../evil.png", _png_bytes(), "image/png")},
        )
        assert response.request.url.path == "/design-studio/reverse/status"
        body = _wait_for_live(client, "Reversed into the library")
        assert "evil.png" in body  # the evidence summary names the shot
        assert "#ff0000" in body and "50%" in body  # the palette swatches
        shots = seen["screenshots"]
        assert len(shots) == 1
        assert not shots[0].exists()  # the temp dir was cleaned up
        assert ".." not in shots[0].name


def test_reverse_status_idle(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio/reverse/status")
    assert page.status_code == 200
    assert "No reverse has run yet" in page.text
