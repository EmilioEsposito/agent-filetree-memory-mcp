"""Run a command against a disposable PostgreSQL container (no persistent volume)."""

from __future__ import annotations

from contextlib import contextmanager
import os
import secrets
import subprocess
import sys
import time
from uuid import uuid4


@contextmanager
def disposable_postgres():
    name = f"afm-dev-{uuid4().hex[:12]}"
    password = secrets.token_hex(24)
    try:
        subprocess.run(
            ["docker", "run", "--detach", "--rm", "--name", name,
             "--publish", "127.0.0.1::5432", "--env", "POSTGRES_PASSWORD",
             "--env", "POSTGRES_DB=afm_test", "postgres:17"],
            env={**os.environ, "POSTGRES_PASSWORD": password},
            check=True, stdout=subprocess.DEVNULL,
        )
        for _ in range(120):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "afm_test"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        address = subprocess.check_output(
            ["docker", "port", name, "5432/tcp"], text=True
        ).strip()
        yield f"postgresql+asyncpg://postgres:{password}@{address}/afm_test"
    finally:
        subprocess.run(["docker", "rm", "--force", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        command = ["uv", "run", "--locked", "pytest", "-m", ""]
    with disposable_postgres() as url:
        result = subprocess.run(command, env={
            **os.environ, "AGENT_FILETREE_MEMORY_TEST_DATABASE_URL": url,
        })
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
