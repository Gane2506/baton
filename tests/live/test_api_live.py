"""Live smoke test for ApiWorker against real OpenRouter. Never runs in CI.

Run with: JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... \
    uv run pytest tests/live/test_api_live.py
Optional: JEV_LIVE_API_MODEL (default openai/gpt-4o-mini). Costs a fraction of a cent.
"""

import os
from pathlib import Path

import pytest

from jev_agent.core.models import ValidationResult
from jev_agent.workers.api import ApiWorker
from jev_agent.workers.base import WorkerJob

_ENABLED = os.environ.get("JEV_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not (_ENABLED and os.environ.get("OPENROUTER_API_KEY")),
    reason="live test: set JEV_LIVE_TESTS=1 and OPENROUTER_API_KEY",
)


async def test_validator_round_trip(tmp_path: Path) -> None:
    worker = ApiWorker(
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        default_model=os.environ.get("JEV_LIVE_API_MODEL", "openai/gpt-4o-mini"),
    )
    job = WorkerJob(
        role="validator",
        prompt=(
            "You validate plans. Plan: 'T1: add a --version flag to the CLI, tested by "
            "pytest tests/test_cli.py'. Reply with JSON: verdict approve or revise, and issues."
        ),
        workdir=tmp_path,
        log_path=tmp_path / "api.log",
        effort="low",
        timeout_s=120,
        output_schema=ValidationResult,
    )
    out = await worker.run(job)
    log = job.log_path.read_text()
    assert out.result.status == "success", f"{out.result.summary}\n{log}"
    assert isinstance(out.parsed, ValidationResult)
    assert out.result.cost_usd is not None
    assert os.environ["OPENROUTER_API_KEY"] not in log
