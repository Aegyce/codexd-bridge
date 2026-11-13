import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_routes_without_starting_a_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                f'[backends.demo]\nhome = {json.dumps(directory)}\nmodel_provider="demo"\n'
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "codexd_bridge.server", "--config", str(config)],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            async with asyncio.timeout(30), stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    catalog = await session.list_tools()
                    self.assertIn("create_session", [tool.name for tool in catalog.tools])
                    result = await session.call_tool("list_backends", {})
                    self.assertFalse(result.is_error)
                    data = json.loads(result.content[0].text)
                    self.assertEqual(data["backends"][0]["model_provider"], "demo")
                    self.assertFalse(data["backends"][0]["socket_exists"])
                    failed = await session.call_tool("list_models", {"backend": "demo"})
                    self.assertTrue(failed.is_error)
                    self.assertIn("Cannot connect to App Server", failed.content[0].text)
                    unknown = await session.call_tool("list_models", {"backend": "missing"})
                    self.assertTrue(unknown.is_error)
                    self.assertIn("Unknown backend", unknown.content[0].text)
                    for extra in ({}, {"ctx": {"threadId": "forged"},
                                       "caller": "forged"}):
                        denied = await session.call_tool("create_session", {
                            "backend": "demo", "prompt": "Do work", "title": "Test",
                            "cwd": directory, **extra,
                        })
                        self.assertTrue(denied.is_error)
                        self.assertIn("Caller identity is unavailable", denied.content[0].text)

    async def test_compatibility_endpoint_also_requires_native_caller_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(f'[backends.demo]\nhome={json.dumps(directory)}\nmodel_provider="demo"\n')
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "codexd_bridge.compat", "--config", str(config),
                      "--backend", "demo", "--cwd", directory],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            async with asyncio.timeout(30), stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    catalog = await session.list_tools()
                    for tool in catalog.tools:
                        self.assertNotIn("ctx", tool.input_schema["properties"])
                    result = await session.call_tool("create_session", {
                        "prompt": "Task", "title": "Test", "ctx": {"threadId": "forged"}})
                    self.assertTrue(result.is_error)
                    self.assertIn("Caller identity is unavailable", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
