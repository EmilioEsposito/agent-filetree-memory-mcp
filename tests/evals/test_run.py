import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_evals")

from fastmcp import FastMCP

from evals import run as runner
from evals.cases import scenarios
from evals.compare import compare


def arguments(tmp_path, **overrides):
    values = dict(
        split="all",
        case=["find-decision"],
        driver="reference",
        model=None,
        provider=None,
        max_calls=40,
        timeout=1,
        repeat=2,
        label="test",
        logfire=False,
        output=tmp_path / "report.json",
    )
    return SimpleNamespace(**(values | overrides))


def fake_environment(case, *, setup=False, snapshot=False, cleanup=False):
    @asynccontextmanager
    async def environment(files):
        if setup:
            raise RuntimeError("setup failed")

        async def export(invocation):
            if snapshot:
                raise RuntimeError("snapshot failed")
            return [
                SimpleNamespace(path=p, content=c) for p, c in case.expected.items()
            ]

        try:
            yield FastMCP("fixture"), SimpleNamespace(export=export), object()
        finally:
            if cleanup:
                raise RuntimeError("cleanup failed")

    return environment


@pytest.mark.parametrize("phase", ["setup", "agent", "snapshot", "cleanup"])
async def test_every_failed_trial_is_recorded_and_fails_run(
    tmp_path, monkeypatch, phase
):
    case = scenarios()[0]
    monkeypatch.setattr(
        runner,
        "environment",
        fake_environment(case, **{phase: True} if phase != "agent" else {}),
    )

    async def reference(server, scenario):
        if phase == "agent":
            raise RuntimeError("agent failed")
        return "Mira PostgreSQL", {}

    monkeypatch.setattr(runner, "reference_agent", reference)
    args = arguments(tmp_path)
    assert await runner.run(args) == 1
    report = json.loads(args.output.read_text())
    assert report["status"] == "complete"
    assert len(report["runs"]) == report["expected_trials"] == 2
    assert [r["trial"] for r in report["runs"]] == [1, 2]
    assert all(not row["success"] for row in report["runs"])
    assert all(row["errors"][0]["phase"] == phase for row in report["runs"])
    assert report["case_errors"] == (0 if phase == "agent" else 2)
    assert compare(report, report) == 1


async def test_timeout_retains_saved_state_and_runs_cleanup(tmp_path, monkeypatch):
    case = scenarios()[0]
    cleaned = []

    @asynccontextmanager
    async def environment(files):
        async def export(invocation):
            return [
                SimpleNamespace(path=p, content=c) for p, c in case.expected.items()
            ]

        try:
            yield FastMCP("timeout"), SimpleNamespace(export=export), object()
        finally:
            cleaned.append(True)

    async def reference(server, scenario):
        await asyncio.sleep(10)

    monkeypatch.setattr(runner, "environment", environment)
    monkeypatch.setattr(runner, "reference_agent", reference)
    outcome, record = await runner.execute_case(
        case, arguments(tmp_path, timeout=0.05), []
    )
    assert "TimeoutError" in outcome.error
    assert outcome.files == case.expected
    assert cleaned == [True]
    assert not record["success"]


async def test_grader_failure_cannot_produce_successful_experiment(
    tmp_path, monkeypatch
):
    case = scenarios()[0]
    monkeypatch.setattr(runner, "environment", fake_environment(case))

    def broken_grader(self, ctx):
        raise RuntimeError("broken grader")

    monkeypatch.setattr(runner.TaskSuccess, "evaluate", broken_grader)
    args = arguments(tmp_path)
    assert await runner.run(args) == 1
    report = json.loads(args.output.read_text())
    assert report["case_errors"] == 2
    assert len(report["framework_errors"]) == 2


def test_case_selection_rejects_typos_and_silent_split_exclusion():
    with pytest.raises(ValueError, match="unknown cases"):
        runner.select_cases("all", ["find-decision", "typo"])
    with pytest.raises(ValueError, match="outside selected split"):
        runner.select_cases("dev", ["unicode-preservation"])


async def test_trial_checkpoints_are_written_before_experiment_finishes(
    tmp_path, monkeypatch
):
    case = scenarios()[0]
    monkeypatch.setattr(runner, "environment", fake_environment(case))
    args = arguments(tmp_path)
    calls = 0

    async def reference(server, scenario):
        nonlocal calls
        calls += 1
        if calls == 2:
            report = json.loads(args.output.read_text())
            assert report["status"] == "running"
            assert len(report["runs"]) == 1
        return "Mira PostgreSQL", {}

    monkeypatch.setattr(runner, "reference_agent", reference)
    assert await runner.run(args) == 0
