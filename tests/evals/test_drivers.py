import pytest

pytest.importorskip("pydantic_ai")

from fastmcp import FastMCP
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from evals.drivers import Recorder, api_agent
from evals.graders import Outcome


@pytest.mark.parametrize("fail", [False, True])
async def test_real_mcp_schema_calls_and_partial_failure_traces_without_network(fail):
    server = FastMCP("test", instructions="Use the tool to inspect state.")

    @server.tool
    async def inspect_value(value: int) -> dict:
        """Return a synthetic value."""
        return {"value": value}

    recorder = Recorder()
    server.add_middleware(recorder)
    requests = 0

    def model(messages, info):
        nonlocal requests
        requests += 1
        assert [t.name for t in info.function_tools] == ["inspect_value"]
        if requests == 1:
            return ModelResponse(
                [ToolCallPart("inspect_value", {"value": 17})],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
            )
        if fail:
            raise RuntimeError("synthetic provider failure")
        return ModelResponse(
            [TextPart("The value is 17.")],
            usage=RequestUsage(input_tokens=20, output_tokens=5),
        )

    outcome = Outcome("", {})
    if fail:
        with pytest.raises(RuntimeError, match="synthetic provider failure"):
            await api_agent(server, "Inspect 17", FunctionModel(model), 3, outcome)
    else:
        await api_agent(server, "Inspect 17", FunctionModel(model), 3, outcome)
        assert outcome.answer == "The value is 17."
    assert recorder.calls[0]["arguments"] == {"value": 17}
    assert outcome.usage["input_tokens"] >= 10
    assert outcome.messages
    assert any(
        p.get("tool_name") == "inspect_value"
        for m in outcome.messages
        for p in m["parts"]
    )


async def test_recorder_enforces_tool_budget():
    from fastmcp import Client

    server = FastMCP("budget")

    @server.tool
    async def ping() -> str:
        return "pong"

    recorder = Recorder(limit=1)
    server.add_middleware(recorder)
    async with Client(server) as client:
        await client.call_tool("ping")
        result = await client.call_tool("ping", raise_on_error=False)
    assert result.is_error
    assert len(recorder.calls) == 1
