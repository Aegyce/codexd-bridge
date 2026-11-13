import unittest

from codexd_bridge.server import create_server


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = {tool.name: tool for tool in await create_server().list_tools()}

    async def test_cross_provider_destinations_and_selection_are_explicit(self):
        for name in ("create_session", "send_message", "fork_session"):
            schema = self.tools[name].input_schema
            self.assertIn("backend", schema["required"])
            self.assertIn("model", schema["properties"])
            self.assertIn("effort", schema["properties"])

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


if __name__ == "__main__":
    unittest.main()
