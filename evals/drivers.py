"""Model runners receive the real MCP catalog, never fixture/grader internals."""

from dataclasses import asdict
import json

from fastmcp import Client
from fastmcp.server.middleware import Middleware
from pydantic_ai import Agent, Tool, capture_run_messages
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage, UsageLimits


class Recorder(Middleware):
    def __init__(self, limit=40):
        self.calls = []
        self.limit = limit

    async def on_call_tool(self, context, call_next):
        if len(self.calls) >= self.limit:
            raise RuntimeError("evaluation tool-call budget exhausted")
        call = {"tool": context.message.name, "arguments": context.message.arguments}
        self.calls.append(call)
        try:
            result = await call_next(context)
            call["result"] = result.structured_content or [
                block.model_dump(mode="json") for block in result.content
            ]
            call["error"] = result.is_error
            call["response_bytes"] = len(json.dumps(call["result"]).encode())
            return result
        except Exception as exc:
            call["error"] = type(exc).__name__
            raise


async def api_agent(
    server, prompt, model, max_calls, outcome, *, openrouter=False, provider=None
):
    settings = {"max_tokens": 4096}
    if openrouter:
        import os
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "set OPENROUTER_API_KEY in the environment or ignored .env"
            )
        model = OpenAIChatModel(
            model,
            provider=OpenAIProvider(
                base_url="https://openrouter.ai/api/v1", api_key=key
            ),
        )
        settings["extra_body"] = {"reasoning": {"effort": "low"}}
        if provider:
            settings["extra_body"]["provider"] = {
                "only": [provider],
                "allow_fallbacks": False,
            }
    async with Client(server) as client:
        tools = []
        for definition in await client.list_tools():

            def bind(name):
                async def invoke(**kwargs):
                    result = await client.call_tool(name, kwargs, raise_on_error=False)
                    return result.structured_content or [
                        item.model_dump(mode="json") for item in result.content
                    ]

                return invoke

            tools.append(
                Tool.from_schema(
                    bind(definition.name),
                    name=definition.name,
                    description=definition.description,
                    json_schema=definition.inputSchema,
                    sequential=True,
                )
            )
        agent = Agent(
            model,
            tools=tools,
            instructions=server.instructions,
            model_settings=settings,
            retries=2,
        )
        usage = RunUsage()
        with capture_run_messages() as messages:
            try:
                result = await agent.run(
                    prompt,
                    usage=usage,
                    usage_limits=UsageLimits(
                        request_limit=30,
                        tool_calls_limit=max_calls,
                        total_tokens_limit=100000,
                    ),
                )
                outcome.answer = result.output
            finally:
                # Preserve partial usage and model-side validation failures on timeout/error.
                outcome.usage = asdict(usage)
                outcome.messages = ModelMessagesTypeAdapter.dump_python(
                    messages, mode="json"
                )


async def reference_agent(server, case):
    """Known solution for checking the environment and graders; not an LLM eval."""
    if case.provenance.get("benchmark") == "AgentBench-OS":
        from .reference_search import reference_search

        return await reference_search(server, case.name), {}
    async with Client(server) as client:
        for index, (path, content) in enumerate(case.writes.items()):
            args = {
                "path": path,
                "content": content,
                "idempotency_key": f"reference-{index}",
            }
            if path in case.files:
                read = await client.call_tool("memory_read", {"path": path})
                args["expected_version"] = read.structured_content["version"]
            await client.call_tool("memory_write", args)
    return " ".join(case.answer_contains), {}
