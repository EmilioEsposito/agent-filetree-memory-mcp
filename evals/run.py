"""Run local, state-graded agent experiments: python -m evals.run --help."""

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import time

from fastmcp import Client
from pydantic_evals import Case, Dataset

from .cases import scenarios
from .drivers import Recorder, api_agent, reference_agent
from .environment import environment
from .graders import Outcome, TaskSuccess, grade_outcome, state_difference

FINALIZATION_TIMEOUT = 30


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def select_cases(split, names, suite="memory"):
    cases = scenarios(suite)
    unknown = set(names or ()) - {case.name for case in cases}
    if unknown:
        raise ValueError(f"unknown cases: {', '.join(sorted(unknown))}")
    selected = [case for case in cases if split == "all" or case.split == split]
    if names:
        excluded = set(names) - {case.name for case in selected}
        if excluded:
            raise ValueError(
                f"cases outside selected split: {', '.join(sorted(excluded))}; use --split all"
            )
        selected = [case for case in selected if case.name in names]
    if not selected:
        raise ValueError("no cases selected")
    return selected


def write_report(path, result):
    """Checkpoint every trial so interruptions preserve completed work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, default=str) + "\n")
    temporary.replace(path)


async def execute_case(case, args, catalog):
    started = time.monotonic()
    outcome = Outcome("", {})
    recorder = Recorder(args.max_calls)
    resources = AsyncExitStack()
    service = invocation = None
    phase = "setup"

    def record_error(stage, exc):
        message = f"{type(exc).__name__}: {exc}"
        outcome.errors.append({"phase": stage, "message": message})
        if outcome.error is None:
            outcome.error = f"{stage}: {message}"

    try:
        async with asyncio.timeout(args.timeout):
            server, service, invocation = await resources.enter_async_context(
                environment(case.files)
            )
            server.add_middleware(recorder)
            async with Client(server) as client:
                definitions = [
                    tool.model_dump(mode="json") for tool in await client.list_tools()
                ]
            current_catalog = {
                "instructions": server.instructions,
                "tools": definitions,
            }
            if not catalog:
                catalog.append(current_catalog)
            elif catalog[0] != current_catalog:
                raise RuntimeError("MCP catalog changed between trials")
            phase = "agent"
            if args.driver == "reference":
                outcome.answer, outcome.usage = await reference_agent(server, case)
            else:
                await api_agent(
                    server,
                    case.prompt,
                    args.model,
                    args.max_calls,
                    outcome,
                    openrouter=args.driver == "openrouter",
                    provider=args.provider,
                )
    except Exception as exc:
        record_error(phase, exc)
    finally:
        # Even a timed-out model may have changed data. Capture that state and
        # clean up under separate bounds after the execution timeout expires.
        if service is not None:
            try:
                async with asyncio.timeout(FINALIZATION_TIMEOUT):
                    snapshots = await service.export(invocation)
                    outcome.files = {item.path: item.content for item in snapshots}
            except Exception as exc:
                record_error("snapshot", exc)
        try:
            async with asyncio.timeout(FINALIZATION_TIMEOUT):
                await resources.aclose()
        except Exception as exc:
            record_error("cleanup", exc)
        outcome.calls = recorder.calls
    checks = grade_outcome(case, outcome)
    return outcome, {
        "case": case.name,
        "provenance": case.provenance,
        "success": all(checks.values()),
        "checks": checks,
        "state_difference": state_difference(case, outcome.files),
        "duration_seconds": time.monotonic() - started,
        **asdict(outcome),
    }


async def run(args):
    from dotenv import load_dotenv

    selected = select_cases(args.split, args.case, args.suite)
    load_dotenv(".env", override=False)
    if args.logfire:
        import logfire

        logfire.configure(service_name="agent-filetree-memory-evals")
        logfire.instrument_pydantic_ai()
    records, catalog = [], []
    harness = Path(__file__).parent
    result = {
        "format_version": 2,
        "status": "running",
        "label": args.label,
        "driver": args.driver,
        "suite": args.suite,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True)
        ),
        "dataset_sha256": fingerprint([asdict(c) for c in selected]),
        "harness_sha256": fingerprint(
            {
                name: (harness / name).read_text()
                for name in (
                    "run.py",
                    "drivers.py",
                    "environment.py",
                    "graders.py",
                    "reference_search.py",
                )
            }
        ),
        "runtime_versions": {
            name: version(name)
            for name in (
                "pydantic-ai-slim",
                "pydantic-evals",
                "fastmcp",
                "SQLAlchemy",
                "alembic",
            )
        },
        "selected_cases": [case.name for case in selected],
        "expected_trials": len(selected) * args.repeat,
        "catalog_sha256": None,
        "catalog": catalog,
        "settings": {
            "max_calls": args.max_calls,
            "timeout": args.timeout,
            "finalization_timeout": FINALIZATION_TIMEOUT,
            "repeat": args.repeat,
            "provider": args.provider,
            "max_tokens": 4096,
            "request_limit": 30,
            "total_tokens_limit": 100000,
            "reasoning_effort": "low" if args.driver == "openrouter" else None,
        },
        "runs": records,
        "case_errors": 0,
        "framework_errors": [],
    }
    write_report(args.output, result)
    trials = Counter()

    async def task(case):
        outcome, record = await execute_case(case, args, catalog)
        trials[case.name] += 1
        record["trial"] = trials[case.name]
        records.append(record)
        result["catalog_sha256"] = fingerprint(catalog)
        result["case_errors"] = sum(
            any(e["phase"] != "agent" for e in r["errors"]) for r in records
        )
        write_report(args.output, result)
        return outcome

    dataset = Dataset(
        name=f"memory-tools-{args.suite}",
        cases=[Case(name=c.name, inputs=c) for c in selected],
        evaluators=[TaskSuccess()],
    )
    report = await dataset.evaluate(
        task, name=args.label, max_concurrency=1, repeat=args.repeat
    )
    framework_errors = [
        {"case": failure.name, "message": failure.error_message}
        for failure in report.failures
    ]
    for case in report.cases:
        for failure in case.evaluator_failures:
            framework_errors.append({"case": case.name, "message": str(failure)})
    framework_errors.extend(
        {"case": None, "message": str(failure)}
        for failure in report.report_evaluator_failures
    )
    result["framework_errors"] = framework_errors
    result["case_errors"] += len(framework_errors)
    result["status"] = (
        "complete" if len(records) == result["expected_trials"] else "incomplete"
    )
    write_report(args.output, result)
    report.print(include_input=False, include_output=False)
    passed = sum(record["success"] for record in records)
    print(f"{passed}/{result['expected_trials']} successful; report: {args.output}")
    return (
        0
        if result["status"] == "complete"
        and not result["case_errors"]
        and all(r["success"] for r in records)
        else 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--driver", choices=["reference", "api", "openrouter"], default="reference"
    )
    parser.add_argument(
        "--model", help="Pydantic AI provider:model for api, or OpenRouter model ID"
    )
    parser.add_argument(
        "--provider",
        help="pin an OpenRouter provider slug, disabling fallbacks for comparable runs",
    )
    parser.add_argument("--split", choices=["dev", "validation", "all"], default="dev")
    parser.add_argument(
        "--suite",
        choices=["memory", "public-search", "all"],
        default="memory",
        help="original memory tasks, adapted AgentBench search tasks, or both",
    )
    parser.add_argument(
        "--case", action="append", help="select a named case (repeatable)"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="setup and agent wall-clock seconds; snapshot and cleanup each have a separate 30-second bound",
    )
    parser.add_argument("--label", default="local")
    parser.add_argument("--output", type=Path, default=Path("eval-results/local.json"))
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="opt in to uploading synthetic experiment data",
    )
    args = parser.parse_args()
    if args.driver == "api" and not args.model:
        parser.error("--model provider:model is required for the api driver")
    if args.driver == "openrouter" and not args.model:
        args.model = "z-ai/glm-5.3-flash"
    if args.provider and args.driver != "openrouter":
        parser.error("--provider is only supported by the openrouter driver")
    if min(args.repeat, args.max_calls, args.timeout) < 1:
        parser.error("repeat, max-calls and timeout must be positive")
    try:
        select_cases(args.split, args.case, args.suite)
    except ValueError as exc:
        parser.error(str(exc))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
