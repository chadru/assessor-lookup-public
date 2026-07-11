"""MCP boundary tests that spawn the real ``assessor-lookup-mcp`` stdio
server as a subprocess — the way an actual MCP client connects.

The contract-surface tests are offline (no county API is touched); one live
lookup runs under the ``network`` marker.
"""

import asyncio
import json
import shutil
import sys

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "lookup_property", "check_mls_csv", "list_counties", "discover_county",
    "probe_county", "onboard_county", "run_regression", "benchmark",
}
EXPECTED_RESOURCES = {
    "assessor://operating-manual", "assessor://architecture",
    "assessor://counties", "assessor://golden-records",
    "assessor://harness-guide",
}
EXPECTED_PROMPTS = {"appraisal_check", "onboard_locale", "add_new_county"}

pytestmark = pytest.mark.skipif(
    shutil.which("assessor-lookup-mcp") is None
    and not shutil.which(sys.executable),
    reason="no way to launch the MCP server",
)


def _server_params():
    # Prefer the console entry point (tests the packaging too); fall back to
    # module execution so the test also runs in envs without the script.
    if shutil.which("assessor-lookup-mcp"):
        return StdioServerParameters(command="assessor-lookup-mcp", args=[])
    return StdioServerParameters(
        command=sys.executable, args=["-m", "assessor_lookup.mcp_server"])


def _run(coro):
    return asyncio.run(coro)


async def _with_session(fn):
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


class TestStdioContractSurface:
    """Offline: the spawned server exposes the full MCP contract."""

    def test_contract_surface_and_registry_tool(self):
        async def check(s):
            tools = {t.name for t in (await s.list_tools()).tools}
            assert EXPECTED_TOOLS <= tools, EXPECTED_TOOLS - tools

            resources = {str(r.uri)
                         for r in (await s.list_resources()).resources}
            assert EXPECTED_RESOURCES <= resources, EXPECTED_RESOURCES - resources

            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert EXPECTED_PROMPTS <= prompts, EXPECTED_PROMPTS - prompts

            out = json.loads((await s.call_tool(
                "list_counties", {})).content[0].text)
            assert out["count"] >= 7
            assert "US/CO/county:el-paso" in out["counties"]

            counties = json.loads((await s.read_resource(
                "assessor://counties")).contents[0].text)
            assert counties["US/CO/county:jefferson"]["platform"] == "aumentum"

            prompt = await s.get_prompt("appraisal_check",
                                        {"subject_csv": "subject.csv"})
            assert prompt.messages
            return True

        assert _run(_with_session(check))


@pytest.mark.network
class TestStdioLive:
    def test_live_lookup_through_stdio(self):
        async def check(s):
            for attempt in range(3):
                out = json.loads((await s.call_tool("lookup_property", {
                    "county": "El Paso",
                    "address": "1675 W Garden of the Gods Rd",
                })).content[0].text)
                if out["status"] not in ("timeout", "api_error"):
                    break
                await asyncio.sleep(2)
            assert out["status"] == "success", out
            assert out["parcel_number"] == "7326201002"
            return True

        assert _run(_with_session(check))
