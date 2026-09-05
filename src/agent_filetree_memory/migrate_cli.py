"""Host-operated migration command; never invoked by server startup."""

import argparse
import asyncio
from dataclasses import asdict
import json
import os

from .domain.errors import ConfigurationError


async def run_command(action: str, schema: str, constraint_namespace: str):
    try:
        from .postgres import PostgresRuntime
        from .postgres.migrations.runner import schema_status, upgrade_schema
    except ImportError:
        raise ConfigurationError(
            "migration runner requires agent-filetree-memory-mcp[postgres]"
        ) from None
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigurationError("required migration setting is missing: DATABASE_URL")
    runtime = PostgresRuntime.from_url(url, schema=schema)
    try:
        async with runtime.engine.begin() as connection:
            if action == "upgrade":
                return await connection.run_sync(
                    upgrade_schema,
                    schema=schema,
                    constraint_namespace=constraint_namespace,
                )
            return await connection.run_sync(schema_status, schema=schema)
    finally:
        await runtime.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["upgrade", "check"])
    parser.add_argument(
        "--schema",
        default=os.environ.get(
            "AGENT_FILETREE_MEMORY_DATABASE_SCHEMA", "agent_filetree_memory"
        ),
    )
    parser.add_argument("--constraint-namespace", default="afm")
    args = parser.parse_args(argv)
    try:
        status = asyncio.run(
            run_command(args.action, args.schema, args.constraint_namespace)
        )
    except (ConfigurationError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        # Driver exceptions can contain connection strings and SQL parameters.
        parser.exit(
            1,
            f"Database migration operation failed ({type(exc).__name__}). Check database connectivity, permissions, and schema compatibility.\n",
        )
    print(
        json.dumps({**asdict(status), "is_current": status.is_current}, sort_keys=True)
    )
    return 0 if status.is_current else 1


if __name__ == "__main__":
    raise SystemExit(main())
