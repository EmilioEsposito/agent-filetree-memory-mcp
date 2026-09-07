"""Exercise the shipped MCP Apps renderer against real MCP tool responses."""

import json
import os
from pathlib import Path

import prefab_ui
import pytest
from fastmcp import Client
from playwright.async_api import async_playwright, expect

from agent_filetree_memory.mcp import create_mcp_server

pytestmark = pytest.mark.skipif(
    os.environ.get("AFM_BROWSER_TESTS") != "1",
    reason="set AFM_BROWSER_TESTS=1 after playwright install chromium",
)


@pytest.mark.parametrize("structured", [True, False])
async def test_rendered_browser_navigates_and_reads_nested_memory(
    service, resolver, structured
):
    server = create_mcp_server(service, resolver, include_app=True)
    async with Client(server) as client, async_playwright() as playwright:
        opened = await client.call_tool("memory_browse", {})
        advertised = {tool.name: tool for tool in await client.list_tools()}
        calls = []
        reject_next = False

        async def backend(params):
            nonlocal reject_next
            # Real hosts discover schemas before allowing an embedded app's calls.
            if params["name"] not in advertised:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "App tool is not advertised"}],
                }
            calls.append(params)
            if reject_next:
                reject_next = False
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "Temporary test failure"}],
                }
            result = await client.call_tool(
                params["name"], params.get("arguments", {}), raise_on_error=False
            )
            response = {
                "content": [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in result.content
                ],
                "isError": result.is_error,
            }
            if structured:
                response["structuredContent"] = result.structured_content
            return response

        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.expose_function("backend", backend)
            renderer = (
                Path(prefab_ui.__file__).parent / "renderer/app.html"
            ).read_text()
            payload = json.dumps(
                {"content": [], "structuredContent": opened.structured_content}
            )
            host = """<iframe src="/renderer" style="width:100%;height:100vh"></iframe>
            <script>
            addEventListener('message', async ({data, source}) => {
              if (data.jsonrpc !== '2.0') return;
              const send = value => source.postMessage({jsonrpc:'2.0', ...value}, '*');
              if (data.method === 'ui/initialize') {
                send({id:data.id, result:{protocolVersion:data.params.protocolVersion,
                  hostInfo:{name:'Synthetic test host', version:'1'},
                  hostCapabilities:{serverTools:{}}, hostContext:{theme:'light'}}});
              } else if (data.method === 'ui/notifications/initialized') {
                send({method:'ui/notifications/tool-result', params:PAYLOAD});
              } else if (data.method === 'tools/call') {
                send({id:data.id, result:await window.backend(data.params)});
              } else if (data.id !== undefined) send({id:data.id, result:{}});
            });
            </script>""".replace("PAYLOAD", payload)

            async def route(request):
                await request.fulfill(
                    body=renderer
                    if request.request.url.endswith("/renderer")
                    else host,
                    content_type="text/html",
                )

            await page.route("http://mcp-app.test/**", route)
            await page.goto("http://mcp-app.test/host")
            app = page.frame_locator("iframe")
            await expect(
                app.get_by_role("button", name="private", exact=True)
            ).to_be_visible()
            await app.get_by_role("button", name="private", exact=True).click()
            await expect(
                app.get_by_role("button", name="canary.md", exact=True)
            ).to_be_visible()
            await app.get_by_role("button", name="canary.md", exact=True).click()
            await expect(
                app.get_by_text("PRIVATE-CONTENT-CANARY", exact=False)
            ).to_be_visible()
            await app.get_by_role("button", name="Up", exact=True).click()
            await expect(
                app.get_by_role("button", name="private", exact=True)
            ).to_be_visible()
            assert [call["arguments"]["path"] for call in calls] == [
                "/",
                "/private",
                "/private/canary.md",
                "/",
            ]
            reject_next = True
            await app.get_by_role("button", name="Refresh", exact=True).click()
            await expect(
                app.get_by_text("Memory unavailable", exact=True)
            ).to_be_visible()
            await expect(
                app.get_by_text("This directory is empty.", exact=True)
            ).not_to_be_visible()
            await app.get_by_role("button", name="Refresh", exact=True).click()
            await expect(
                app.get_by_text("Memory unavailable", exact=True)
            ).not_to_be_visible()
            await expect(
                app.get_by_role("button", name="private", exact=True)
            ).to_be_visible()
        finally:
            await browser.close()
