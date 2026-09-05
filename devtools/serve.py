"""Interactive stdio MCP sandbox containing synthetic memories only."""

import asyncio

from evals.cases import scenarios
from evals.environment import environment


async def main():
    async with environment(scenarios()[0].files) as (server, _, _):
        await server.run_async(transport="stdio", show_banner=False)


if __name__ == "__main__":
    asyncio.run(main())
