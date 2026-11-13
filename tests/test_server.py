import json
import unittest

from mcp.server import MCPServer

from codexd_bridge.server import create_server, preserve_literal_arguments


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = {tool.name: tool for tool in await create_server().list_tools()}

    async def test_cross_provider_destinations_and_selection_are_explicit(self):
        for name in ("create_session", "send_message", "fork_session"):
            schema = self.tools[name].input_schema
            self.assertIn("backend", schema["required"])
            self.assertIn("model", schema["properties"])
            self.assertIn("effort", schema["properties"])

    async def test_derivation_exposes_task_fields_but_not_parent_identity_overrides(self):
        for name in ("create_session", "fork_session"):
            schema = self.tools[name].input_schema
            self.assertIn("prompt", schema["required"])
            self.assertIn("session_type", schema["properties"])
            self.assertIn("acceptance_criteria", schema["properties"])
            self.assertNotIn("parent", schema["properties"])
            self.assertNotIn("caller", schema["properties"])

    async def test_mutations_are_never_advertised_as_read_only(self):
        for name in ("create_session", "send_message", "fork_session", "steer_session"):
            self.assertFalse(self.tools[name].annotations.read_only_hint)
            self.assertFalse(self.tools[name].annotations.idempotent_hint)
            self.assertNotIn("ctx", self.tools[name].input_schema["properties"])
            self.assertNotIn("caller", self.tools[name].input_schema["properties"])

    async def test_reads_are_advertised_as_read_only(self):
        for name in (
            "list_backends",
            "list_models",
            "list_provider_models",
            "list_sessions",
            "read_session",
            "read_messages",
        ):
            self.assertTrue(self.tools[name].annotations.read_only_hint)
            self.assertFalse(self.tools[name].annotations.destructive_hint)

    async def test_optional_text_keeps_json_literals_and_explicit_null_distinct(self):
        server = MCPServer("literal-input-regression")

        @server.tool()
        def echo(value: str | None = None) -> dict:
            return {"value": value}

        preserve_literal_arguments(server)
        for value in ('{"tests": true}', '["a"]', "null", "true", None):
            result = await server.call_tool("echo", {"value": value})
            self.assertEqual(json.loads(result.content[0].text), {"value": value})


if __name__ == "__main__":
    unittest.main()
