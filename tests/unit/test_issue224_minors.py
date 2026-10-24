"""Minor backlog from the swarm review (issue #224): one test per fix.

Items 7 (dispatch failover), 8 (gate AI review) and 9 (tuning
`collect_samples`) needed no code change: #7 was already fixed structurally
in #214 (see `test_multi_provider_jev.py`, which keys failover on the
`provider_failure` flag, not message text); #8 is deliberately not fixed —
the review's findings feed the retry loop, so skipping it would starve the
rework (pinned by the test below and by
`test_blocked_task_retries_with_findings`); and #9's code was deleted with
`core/tuning.py` in #217 — there is no sample-collection N+1 left to
paginate. All three are recorded here as documentation, not code.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jev_agent.core.cache import OpenRouterEmbedder
from jev_agent.core.design_codegen import css_variables
from jev_agent.core.design_showcase import VIBE_PROFILES
from jev_agent.core.design_showcase_html import render_showcase
from jev_agent.core.models import Idea, Plan, TaskCard
from jev_agent.core.store import Store, _check_update_assignments
from jev_agent.core.workspace import _is_secret_path
from jev_agent.quality.base import BaseCheck
from jev_agent.quality.models import CheckResult, GateResult
from jev_agent.quality.review import ReviewOutcome
from jev_agent.quality.scoring import score_gate

# ---- 1. store.py: _update_run / _update_task allowlist -----------------------------


def test_update_assignments_allowlist_accepts_the_real_callers() -> None:
    for fragment in (
        "learning_profile = ?",
        "learning_outcome = ?",
        "quality_level = ?",
        "spend_usd = spend_usd + ?",
        "lessons_scored = 1",
    ):
        _check_update_assignments(
            fragment,
            frozenset(
                {
                    "learning_profile",
                    "learning_outcome",
                    "quality_level",
                    "spend_usd",
                    "lessons_scored",
                }
            ),
        )


def test_update_assignments_allowlist_rejects_unknown_columns() -> None:
    allowed = frozenset({"spend_usd"})
    for fragment in (
        "state = ?",  # a real column, but not one these helpers may set
        "spend_usd = 1, state = ?",  # one bad piece poisons the whole fragment
        "spend_usd = 1; DROP TABLE runs",  # not a plain assignment
        "1 = 1",
        "",
    ):
        with pytest.raises(ValueError, match="allowlist"):
            _check_update_assignments(fragment, allowed)


def test_store_update_helpers_enforce_the_allowlist(tmp_path: Path) -> None:
    idea = Idea(text="x", repo_path=tmp_path / "repo", context_files=[])
    with Store.open(tmp_path / ".jev" / "state.db") as store:
        run_id = store.create_run(idea).id
        store.upsert_tasks(
            run_id,
            Plan(
                summary="s",
                tasks=[
                    TaskCard(
                        id="T1",
                        title="t",
                        description="d",
                        files_hint=[],
                        acceptance_criteria=["a"],
                        test_command="pytest",
                    )
                ],
                risks=[],
            ),
        )
        # The real callers still work through the helpers.
        store.add_spend(run_id, "T1", 1.5)
        store.add_task_spend(run_id, "T1", 0.5)
        # A smuggled column is refused before any SQL runs.
        with pytest.raises(ValueError, match="allowlist"):
            store._update_run(run_id, "state = ?", ("FAILED",))
        with pytest.raises(ValueError, match="allowlist"):
            store._update_task(run_id, "T1", "state = ?", ("done",))
        assert store.get_run(run_id).spend_usd == pytest.approx(1.5)


# ---- 2. quality/scoring.py: tool_fit ignores not-applicable skips ------------------


def _check(
    name: str = "tests", status: str = "pass", required: bool = True, detail: str = ""
) -> CheckResult:
    return CheckResult(status=cast(Any, status), name=name, required=required, detail=detail)


def _gate(checks: list[CheckResult]) -> GateResult:
    return GateResult(level="basic", scope="integration", checks=checks)


_SMALL_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
    '@@ -1,2 +1,4 @@\n import os\n+GREETING = "hello"\n print(os.name)\n+print(GREETING)\n'
)


def test_tool_fit_ignores_not_applicable_skips() -> None:
    gate = _gate(
        [
            _check("tests", "pass"),
            _check("semgrep", "skipped", detail="not applicable: needs local rules"),
            _check("trivy", "skipped", detail="runs on the result branch only"),
            _check(
                "coverage", "skipped", detail="no test suite detected; set [quality] test_command"
            ),
        ]
    )
    score = score_gate(gate, diff=_SMALL_DIFF)
    assert score.criteria["tool_fit"] == 5
    assert "not applicable" in score.details["tool_fit"]


def test_tool_fit_still_penalises_a_missing_tool() -> None:
    gate = _gate(
        [
            _check("tests", "pass"),
            _check("bandit", "skipped", detail="bandit is not installed"),
        ]
    )
    score = score_gate(gate, diff=_SMALL_DIFF)
    assert score.criteria["tool_fit"] == 4


def test_tool_fit_half_skipped_rule_counts_only_missing_tools() -> None:
    # Three not-applicable skips out of four checks must not trip the
    # "more than half the checks skipped" deduction.
    gate = _gate(
        [
            _check("tests", "pass"),
            _check("a", "skipped", detail="not applicable: needs x"),
            _check("b", "skipped", detail="not applicable: needs y"),
            _check("c", "skipped", detail="not applicable: needs z"),
        ]
    )
    score = score_gate(gate, diff=_SMALL_DIFF)
    assert score.criteria["tool_fit"] == 5
    assert "more than half" not in score.details["tool_fit"]


# ---- 3. design_codegen.py: derived roles reach the dark palette --------------------


def _sparse_tokens() -> dict[str, object]:
    """Rich tokens whose dark palette carries only the base roles."""
    return {
        "design_tokens": {
            "colors": {
                "light": {
                    "background": "#FFFFFF",
                    "surface": "#F8FAFC",
                    "text": "#0F172A",
                    "primary": "#2563EB",
                    "border": "#E2E8F0",
                },
                "dark": {
                    "background": "#0F172A",
                    "surface": "#1E293B",
                    "text": "#F8FAFC",
                    "primary": "#60A5FA",
                    "border": "#334155",
                },
            },
            "typography": {},
            "spacing": {},
            "radii": {},
            "shadows": {},
            "motion": {},
        }
    }


def test_dark_block_gets_derived_roles() -> None:
    css = css_variables(_sparse_tokens())
    dark_block = css.split('[data-theme="dark"]', 1)[1]
    for var in (
        "--color-primary-hover:",
        "--color-primary-subtle:",
        "--color-primary-text:",
        "--color-surface-raised:",
        "--color-text-muted:",
        "--color-text-faint:",
        "--color-border-strong:",
        "--color-success:",
        "--color-success-subtle:",
        "--color-warning-subtle:",
        "--color-danger-subtle:",
        "--color-info-subtle:",
    ):
        assert var in dark_block, f"dark block is missing derived {var}"
    # Dark derivations use dark constants, not the light ones: the dark
    # success role is the brightened #22C55E, and the dark subtle is a dark
    # surface mix — not the light #16A34A / #DCFCE7.
    assert "--color-success: #22C55E;" in dark_block
    assert "--color-success: #16A34A;" not in dark_block


def test_light_block_unchanged_by_dark_derivation() -> None:
    css = css_variables(_sparse_tokens())
    light_block = css.split('[data-theme="dark"]', 1)[0]
    assert "--color-success: #16A34A;" in light_block
    assert "--color-primary-hover:" in light_block


# ---- 4. design_showcase_html.py: type specimens from the token scale ---------------


def _showcase_tokens(display_xl_size: int = 100) -> dict[str, object]:
    return {
        "colors": {
            "light": {"background": "#FFFFFF", "text": "#0F172A", "primary": "#2563EB"},
            "dark": {"background": "#0F172A", "text": "#F8FAFC", "primary": "#60A5FA"},
        },
        "typography": {
            "font_display": "system-ui, sans-serif",
            "font_body": "system-ui, sans-serif",
            "font_mono": "monospace",
            "display_xl": {
                "size": display_xl_size,
                "weight": 800,
                "tracking": -0.04,
                "leading": 1.0,
            },
            "body_md": {"size": 18, "weight": 400, "tracking": 0.02, "leading": 1.7},
        },
        "spacing": {},
        "radii": {},
        "shadows": {},
        "motion": {},
    }


def test_showcase_type_scale_comes_from_tokens() -> None:
    html = render_showcase("Demo", VIBE_PROFILES["minimal"], _showcase_tokens(), "both")
    assert "--tw-display-xl-size: 100px;" in html
    assert "--tw-display-xl-weight: 800;" in html
    assert "--tw-display-xl-tracking: -0.04em;" in html
    assert "--tw-display-xl-leading: 1.0;" in html
    assert "--tw-body-md-size: 18px;" in html
    # The specimen rules read the variables (with the old sizes as fallbacks).
    assert "font-size:var(--tw-display-xl-size,76px);" in html
    assert "font-size:var(--tw-body-md-size,15px);" in html


def test_showcase_flat_tokens_keep_the_old_sizes() -> None:
    html = render_showcase("Demo", VIBE_PROFILES["minimal"], {"colors": {}}, "light")
    assert "--tw-display-xl-size:" not in html
    assert "font-size:var(--tw-display-xl-size,76px);" in html


# ---- 5. core/cache.py: no empty Bearer header ---------------------------------------


def test_openrouter_embedder_skips_empty_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen: dict[str, Any] = {}

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> Any:
        seen["headers"] = headers
        body = {"data": [{"embedding": [0.1, 0.2]}]}

        class _Resp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return body

        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    OpenRouterEmbedder(api_key="").fingerprint("hello")
    assert seen["headers"] == {}
    OpenRouterEmbedder(api_key="sk-test").fingerprint("hello")
    assert seen["headers"] == {"Authorization": "Bearer sk-test"}


# ---- 6. core/workspace.py: secret denylist covers credentials.txt -------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("credentials.txt", True),
        ("credentials.env", True),
        ("secrets.yaml", True),
        ("secrets.json", True),
        ("secret.txt", True),
        ("config/credentials.txt", True),
        (".env", True),
        (".env.local", True),
        ("id_rsa", True),
        ("key.pem", True),
        ("my-credentials.txt", False),  # a doc about credentials, not the file
        ("README.md", False),
        ("src/app.py", False),
    ],
)
def test_secret_path_stem_match(path: str, expected: bool) -> None:
    assert _is_secret_path(path) is expected


# ---- 8. quality/gate.py: AI review NOT skipped when checks block -----------------
#
# Deliberately not fixed (issue #224, documented with reasoning). Skipping the
# review when the checks already block looks like a saved worker call, but the
# review's findings are the retry loop's input: D5 feeds `result.findings()`
# (checks *and* review) into the coder's next attempt as "Quality gate
# findings to fix". `test_blocked_task_retries_with_findings` pins this — a
# ruff failure plus a CRITICAL review ("divides by zero") must both reach the
# retry prompt. Skipping the review would starve the rework of the reviewer's
# diagnosis to save one call. The test below pins the decision: the review
# still runs on a blocked gate.


class _FakeStore:
    def __init__(self) -> None:
        self.reports: list[SimpleNamespace] = []

    def add_quality_report(self, run_id: str, result: Any, path: Path) -> None:
        self.reports.append(SimpleNamespace(run_id=run_id, result=result, path=path))

    def list_quality_reports(self, run_id: str) -> list[SimpleNamespace]:
        return [r for r in self.reports if r.run_id == run_id]

    def get_run(self, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(quality_level="basic")


class _GateHarness:
    def __init__(self, tmp_path: Path) -> None:
        from jev_agent.config import JevConfig

        self.config = JevConfig()
        self.commands = None
        self.repo = tmp_path
        self.state_dir = tmp_path / ".jev"
        self._store = _FakeStore()

    @property
    def store(self) -> _FakeStore:
        return self._store


class _FailCheck(BaseCheck):
    """A required check that fails, so the gate blocks on checks alone."""

    name = "tests"
    tool = None
    gate_from = "basic"
    levels = ("basic",)

    async def run(self, workdir: Path, runner: Any) -> CheckResult:
        return CheckResult(status="fail", detail="1 test failed")


async def _run_gate(harness: Any, checks: list[Any], diff: str) -> Any:
    from jev_agent.quality.gate import run_gate

    return await run_gate(
        harness,
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=harness.repo,
        diff=diff,
        checks=checks,
    )


async def test_gate_runs_ai_review_even_when_checks_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.quality.gate as gate_mod

    calls: list[str] = []

    async def _fake_review(*args: Any, **kwargs: Any) -> ReviewOutcome:
        calls.append("reviewed")
        return ReviewOutcome("done", "reviewed", None)

    monkeypatch.setattr(gate_mod, "ai_review", _fake_review)
    result = await _run_gate(_GateHarness(tmp_path), [_FailCheck()], diff="x\n")
    assert calls == ["reviewed"]
    assert result.blocked
    assert result.review_status == "done"


# ---- 10. dashboard templates: re-grade invariants -----------------------------------


def _template_dir() -> Path:
    import jev_agent.dashboard

    return Path(jev_agent.dashboard.__file__).parent / "templates"


def test_templates_have_no_inline_scripts() -> None:
    """Every `<script>` loads an external file, except the single JSON data
    block (`palette-registry`, `tojson`-escaped) in base.html."""
    for path in sorted(_template_dir().glob("*.html")):
        # Jinja comments are not rendered: strip them before scanning, so a
        # comment *mentioning* `<script>` is not mistaken for one.
        text = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.DOTALL)
        for match in re.finditer(r"<script([^>]*)>", text):
            attrs = match.group(1)
            if 'src="' in attrs or "src='" in attrs:
                continue
            assert 'type="application/json"' in attrs, f"{path.name}: inline script: {attrs}"


def test_post_forms_all_carry_csrf() -> None:
    for path in sorted(_template_dir().glob("*.html")):
        text = path.read_text()
        if re.search(r'<form[^>]*method="post"', text):
            assert "csrf" in text, f"{path.name}: POST form without a CSRF token"


def test_safe_filter_is_only_the_audited_detail_html() -> None:
    """The one `| safe` renders `detail_html`, which app.py builds from
    `markupsafe.escape`d lines — never from raw user input."""
    for path in sorted(_template_dir().glob("*.html")):
        for line in path.read_text().splitlines():
            if "| safe" in line:
                assert path.name == "_chat_live.html" and "detail_html" in line, (
                    f"{path.name}: unexpected | safe: {line.strip()}"
                )


def test_buttons_all_declare_their_type() -> None:
    for path in sorted(_template_dir().glob("*.html")):
        for match in re.finditer(r"<button([^>]*)>", path.read_text()):
            assert "type=" in match.group(1), f"{path.name}: typeless button: {match.group(0)}"
