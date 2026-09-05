"""Scripted fixture checks through MCP, not an LLM performance measurement.

Answers are calculated from actual tool results, independently of the gold
answers. Only this reference driver knows a solution; model drivers do not.
"""

import json
from pathlib import PurePosixPath

from fastmcp import Client


async def reference_search(server, name):
    async with Client(server) as client:

        async def pages(tool, args, field):
            items, offset = [], 0
            while True:
                result = await client.call_tool(tool, {**args, "offset": offset})
                payload = result.structured_content
                if "scan_limit" in payload["limit_reasons"]:
                    raise RuntimeError("reference search hit an incomplete scan")
                items.extend(payload[field])
                if not payload["truncated"]:
                    return items
                next_offset = payload["next_offset"]
                if next_offset is None or next_offset <= offset:
                    raise RuntimeError("reference search did not advance")
                offset = next_offset

        async def glob(pattern, path="/"):
            return await pages(
                "memory_glob", {"pattern": pattern, "path": path}, "paths"
            )

        if name == "search-exact-basename":
            answer = {"paths": await glob("**/TOOLS.md")}
        elif name == "search-file-absence":
            answer = {"exists": bool(await glob("**/workspace.md", "/working"))}
        elif name == "search-hidden-filter":
            paths = await glob(".*", "/usr")
            answer = {"count": sum("u" not in PurePosixPath(p).name for p in paths)}
        elif name == "search-recursive-suffix":
            answer = {"count": len(await glob("**/*.decision.md", "/projects"))}
        elif name == "search-word-lines":
            matches = await pages(
                "memory_grep",
                {
                    "path": "/logs",
                    "glob": "*.log.md",
                    "pattern": r"\berror\b",
                    "literal": False,
                    "case_sensitive": False,
                    "context_lines": 0,
                },
                "matches",
            )
            answer = {"count": len(matches)}
        elif name == "search-log-set-difference":
            matches = await pages(
                "memory_grep",
                {
                    "path": "/trades/stock.log.md",
                    "pattern": r"^Bob \|",
                    "literal": False,
                    "context_lines": 0,
                },
                "matches",
            )
            sold, bought = set(), set()
            for match in matches:
                _, action, symbol, _ = [
                    part.strip() for part in match["text"].split("|")
                ]
                (sold if action == "Sell" else bought).add(symbol)
            symbols = sorted(sold - bought)
            answer = {"count": len(symbols), "symbols": symbols}
        else:
            raise ValueError(f"unknown reference search case: {name}")
    return json.dumps(answer)
