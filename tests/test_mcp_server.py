"""Offline tests for the MCP server (skipped if the [mcp] extra isn't installed)."""

import asyncio
import json

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed (pip install -e '.[mcp]')")

from assessor_lookup import mcp_server as srv  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _tool_json(result):
    """Extract the JSON dict a FastMCP tool returned, from its content list."""
    content = result[0] if isinstance(result, tuple) else result
    # content is a list of content blocks; the text block holds the JSON
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("no text content in tool result")


class TestServerSurface:
    def test_expected_tools_registered(self):
        tools = _run(srv.mcp.list_tools())
        names = {t.name for t in tools}
        assert {"lookup_property", "check_mls_csv", "list_counties",
                "discover_county", "run_regression", "benchmark",
                "probe_county", "onboard_county"} <= names

    def test_expected_resources_registered(self):
        res = _run(srv.mcp.list_resources())
        uris = {str(r.uri) for r in res}
        assert "assessor://operating-manual" in uris
        assert "assessor://architecture" in uris
        assert "assessor://counties" in uris

    def test_expected_prompts_registered(self):
        prompts = _run(srv.mcp.list_prompts())
        names = {p.name for p in prompts}
        assert {"appraisal_check", "add_new_county", "onboard_locale"} <= names

    def test_instructions_present(self):
        assert srv.mcp.instructions
        assert "coordinator" in srv.mcp.instructions.lower()
        assert "api-first" in srv.mcp.instructions.lower()


class TestToolsOffline:
    def test_list_counties(self):
        out = _tool_json(_run(srv.mcp.call_tool("list_counties", {})))
        assert out["count"] >= 7
        assert "CO:Clear Creek" in out["counties"]

    def test_lookup_requires_input(self):
        out = _tool_json(_run(srv.mcp.call_tool(
            "lookup_property", {"county": "El Paso"})))
        assert out["status"] == "invalid_request"

    def test_benchmark_runs_offline(self):
        out = _tool_json(_run(srv.mcp.call_tool("benchmark", {})))
        assert len(out["parser_bench"]) == 3

    def test_run_regression_offline_mode(self):
        out = _tool_json(_run(srv.mcp.call_tool(
            "run_regression", {"offline": True})))
        assert out["mode"] == "offline"
        assert out["parser_bench"]

    def test_live_regression_uses_onboarded_cases(self, monkeypatch):
        class Harness:
            @staticmethod
            def load_golden():
                return {"user-case": {"platform": "stub", "expect": {}}}

            @staticmethod
            def load_cases():
                return [{"id": "user-case", "county": "User", "state": "co"}]

            @staticmethod
            def run_regression(cases, golden, delay, progress):
                assert [case["id"] for case in cases] == ["user-case"]
                return [{"id": "user-case", "county": "User",
                         "platform": "stub", "status": "success",
                         "elapsed_s": 0.1, "result": "PASS", "findings": []}]

            @staticmethod
            def platform_aggregates(results):
                return []

        monkeypatch.setattr(srv, "_import_harness", lambda: Harness)
        out = _tool_json(_run(srv.mcp.call_tool("run_regression", {})))
        assert out["overall"] == "PASS"
        assert out["cases"][0]["id"] == "user-case"

    def test_check_mls_csv_missing_file(self):
        out = _tool_json(_run(srv.mcp.call_tool(
            "check_mls_csv", {"subject_csv": "/nonexistent/subject.csv"})))
        assert "error" in out

    def test_check_mls_csv_allows_file_in_configured_root(
            self, tmp_path, monkeypatch):
        subject = tmp_path / "subject.csv"
        subject.write_text("Address,County,State\n123 Main St,Adams,CO\n")
        monkeypatch.setenv("ASSESSOR_LOOKUP_MCP_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(srv, "check_public_records", lambda *a, **k: [])

        out = _tool_json(_run(srv.mcp.call_tool(
            "check_mls_csv", {"subject_csv": "subject.csv"})))

        assert out == {"results": [], "count": 0, "flagged": []}

    def test_check_mls_csv_rejects_path_outside_configured_root(
            self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside.csv"
        outside.write_text("Address\n123 Main St\n")
        monkeypatch.setenv("ASSESSOR_LOOKUP_MCP_DATA_DIR", str(allowed))

        out = _tool_json(_run(srv.mcp.call_tool(
            "check_mls_csv", {"subject_csv": str(outside)})))

        assert "error" in out
        assert "inside the configured MCP data directory" in out["error"]

    def test_check_mls_csv_rejects_symlink_escape(
            self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside.csv"
        outside.write_text("Address\n123 Main St\n")
        (allowed / "linked.csv").symlink_to(outside)
        monkeypatch.setenv("ASSESSOR_LOOKUP_MCP_DATA_DIR", str(allowed))

        out = _tool_json(_run(srv.mcp.call_tool(
            "check_mls_csv", {"subject_csv": "linked.csv"})))

        assert "error" in out
        assert "inside the configured MCP data directory" in out["error"]


class TestResourcesOffline:
    def test_counties_resource_is_json(self):
        content = _run(srv.mcp.read_resource("assessor://counties"))
        block = content[0]
        data = json.loads(block.content)
        assert "CO:Clear Creek" in data

    def test_operating_manual_mentions_workflow(self):
        content = _run(srv.mcp.read_resource("assessor://operating-manual"))
        text = content[0].content
        assert "add a new county" in text.lower()
        assert "api-first" in text.lower()

    def test_architecture_resource_is_nonempty(self):
        content = _run(srv.mcp.read_resource("assessor://architecture"))
        text = content[0].content
        assert len(text) > 500
        assert "not found" not in text.lower()

    def test_golden_resource_is_privacy_minimized(self):
        content = _run(srv.mcp.read_resource("assessor://golden-records"))
        data = json.loads(content[0].content)
        assert data
        for entry in data.values():
            assert "owner" not in entry["expect"]
            assert "legal" not in entry["expect"]
            assert "market_value" not in entry["expect"]


class TestPromptsOffline:
    def test_add_new_county_prompt_renders(self):
        result = _run(srv.mcp.get_prompt(
            "add_new_county", {"county": "Summit", "state": "co"}))
        text = result.messages[0].content.text
        assert "Summit" in text
        assert "API-first" in text

    def test_appraisal_check_prompt_renders(self):
        result = _run(srv.mcp.get_prompt(
            "appraisal_check", {"subject_csv": "s.csv", "county": "Adams"}))
        text = result.messages[0].content.text
        assert "check_mls_csv" in text

    def test_onboard_locale_prompt_renders(self):
        result = _run(srv.mcp.get_prompt(
            "onboard_locale", {"counties": "El Paso, Douglas", "state": "co"}))
        text = result.messages[0].content.text
        assert "probe_county" in text
        assert "onboard_county" in text
        assert "El Paso" in text
