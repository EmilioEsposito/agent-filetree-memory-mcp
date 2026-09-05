"""Run local, state-graded agent experiments: python -m evals.run --help."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from fastmcp import Client
from pydantic_evals import Case, Dataset

from .cases import scenarios
from .drivers import Recorder, api_agent, reference_agent
from .environment import environment
from .graders import Outcome, TaskSuccess, state_is_correct


async def run(args):
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
    if args.logfire:
        import logfire

        logfire.configure(service_name="agent-filetree-memory-evals")
        logfire.instrument_pydantic_ai()
    selected = [
        case for case in scenarios() if args.split == "all" or case.split == args.split
    ]
    if args.case:
        selected = [case for case in selected if case.name in args.case]
    if not selected:
        raise ValueError("no cases selected")
    records = []
    catalog = []

    async def task(case):
        started = time.monotonic()
        async with environment(case.files) as (server, service, invocation):
            recorder = Recorder(args.max_calls)
            server.add_middleware(recorder)
            async with Client(server) as client:
                definitions = [
                    tool.model_dump(mode="json") for tool in await client.list_tools()
                ]
            if not catalog:
                catalog.append(
                    {"instructions": server.instructions, "tools": definitions}
                )
            outcome = Outcome("", {})
            try:
                async with asyncio.timeout(args.timeout):
                    if args.driver == "reference":
                        outcome.answer, outcome.usage = await reference_agent(
                            server, case
                        )
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
                outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.calls = recorder.calls
            snapshots = await service.export(invocation)
            outcome.files = {item.path: item.content for item in snapshots}
        success = (
            outcome.error is None
            and state_is_correct(case, outcome.files)
            and all(
                t.casefold() in outcome.answer.casefold() for t in case.answer_contains
            )
        )
        records.append(
            {
                "case": case.name,
                "success": success,
                "duration_seconds": time.monotonic() - started,
                **asdict(outcome),
            }
        )
        return outcome

    dataset = Dataset(
        name="memory-tools",
        cases=[Case(name=c.name, inputs=c) for c in selected],
        evaluators=[TaskSuccess()],
    )
    report = await dataset.evaluate(
        task, name=args.label, max_concurrency=1, repeat=args.repeat
    )
    report.print(include_input=False, include_output=False)
    fingerprint = lambda value: hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()
    ).hexdigest()
    result = {
        "format_version": 1,
        "label": args.label,
        "driver": args.driver,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True)
        ),
        "dataset_sha256": fingerprint([asdict(c) for c in selected]),
        "catalog_sha256": fingerprint(catalog),
        "catalog": catalog,
        "settings": {
            "max_calls": args.max_calls,
            "timeout": args.timeout,
            "repeat": args.repeat,
            "provider": args.provider,
            "max_tokens": 4096,
            "reasoning_effort": "low" if args.driver == "openrouter" else None,
        },
        "runs": records,
        "case_errors": len(report.failures),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    passed = sum(record["success"] for record in records)
    print(f"{passed}/{len(selected) * args.repeat} successful; report: {args.output}")
    return (
        0
        if not report.failures and all(r["success"] for r in records) and records
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
        "--case", action="append", help="select a named case (repeatable)"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument(
        "--timeout", type=int, default=180, help="per-case wall-clock seconds"
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
    if min(args.repeat, args.max_calls, args.timeout) < 1:
        parser.error("repeat, max-calls and timeout must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
