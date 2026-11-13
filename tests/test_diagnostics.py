import importlib.metadata
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from codexd_bridge import __version__
from codexd_bridge.diagnostics import diagnose, inspect_codex


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "routes.toml"
        for item in (
            patch("codexd_bridge.diagnostics.shutil.which", return_value="/bin/codex"),
            patch("codexd_bridge.diagnostics.subprocess.run",
                  return_value=Mock(returncode=0, stdout="codex-cli 0.167.2\n")),
            patch("codexd_bridge.diagnostics._resolve_executable", return_value=self.root / "bridge"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_fresh_install_reports_setup_hint_without_a_model_request(self):
        result = diagnose(self.registry)
        self.assertTrue(result["ok"])
        self.assertFalse(result["model_requests"])
        self.assertIn("Set up this Codex", result["checks"][-1]["advice"])

    def test_corrupt_registry_does_not_echo_contents(self):
        self.registry.write_text('SECRET = "do not echo\n')
        result = diagnose(self.registry)
        self.assertFalse(result["ok"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["checks"][-1]["status"], "error")

    def test_codex_failure_and_missing_dependency_are_reported_without_raw_output(self):
        for response in (Mock(returncode=1, stdout="SECRET"), subprocess.TimeoutExpired("codex", 10)):
            kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with patch("codexd_bridge.diagnostics.subprocess.run", **kwargs), patch(
                "codexd_bridge.diagnostics.importlib.metadata.version",
                side_effect=importlib.metadata.PackageNotFoundError,
            ):
                result = diagnose(self.registry)
            self.assertFalse(result["ok"])
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(sum(c["status"] == "error" for c in result["checks"]), 4)

    def test_binding_mismatch_is_reported_without_daemon_changes(self):
        self.registry.write_text(f'[backends.demo]\nhome={json.dumps(str(self.root))}\nmodel_provider="demo"\n')
        (self.root / "config.toml").write_text('model_provider="different"\n')
        result = diagnose(self.registry)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"][-1]["name"], "demo")

    def test_stale_owned_registration_is_reported_without_claiming_worker_version(self):
        self.registry.write_text(f'[backends.demo]\nhome={json.dumps(str(self.root))}\nmodel_provider="demo"\n')
        for version in ("0.3.0", __version__):
            (self.root / "config.toml").write_text('model_provider="demo"\n[mcp_servers.codexd-bridge]\n'
                + 'command=' + json.dumps(str(self.root / "bridge")) + '\nargs='
                + json.dumps(["--config", str(self.registry), "serve"])
                + '\n[mcp_servers.codexd-bridge.env]\nCODEXD_BRIDGE_VERSION=' + json.dumps(version) + '\n')
            result = diagnose(self.registry)
            stale = [c for c in result["checks"] if c["name"] == "demo MCP registration"]
            self.assertEqual(bool(stale), version != __version__)
            if stale:
                self.assertEqual(stale[0]["status"], "warning")
                self.assertIn("Connect bridge", stale[0]["advice"])


class CodexAccountTests(unittest.TestCase):
    def test_missing_home_and_stopped_daemon_remain_unchecked(self):
        with tempfile.TemporaryDirectory() as directory, patch("codexd_bridge.transport.AppServer") as client:
            home = Path(directory) / "empty"
            result = inspect_codex(home)
            self.assertFalse(result["connected"])
            self.assertEqual(result["authentication"], "unknown")
            self.assertFalse(home.exists())
            home.mkdir()
            self.assertEqual(inspect_codex(home)["authentication"], "unknown")
            client.assert_not_called()

    def test_live_authentication_uses_provider_requirement_and_drops_account_details(self):
        cases = [
            (True, {"type": "apiKey"}, "apiKey"),
            (True, {"type": "chatgpt", "planType": "free"}, "chatgpt"),
            (True, {"type": "chatgpt", "planType": "unknown"}, "chatgpt"),
            (False, {"type": "chatgpt", "planType": "pro"}, "provider_managed"),
            (False, {"type": "apiKey"}, "provider_managed"),
            (False, {"type": "amazonBedrock"}, "amazonBedrock"),
            (True, None, "sign_in_required"),
            (None, {"type": "chatgpt"}, "unknown"),
            (True, {"type": "futureAccount"}, "unknown"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            socket = home / "app-server-control/app-server-control.sock"
            socket.parent.mkdir()
            socket.touch()
            for required, account, expected in cases:
                with self.subTest(required=required, account=account), patch("codexd_bridge.transport.AppServer") as factory:
                    if account is not None:
                        account.update(email="private@example.com", accessToken="SECRET-SENTINEL")
                    client = factory.return_value.__enter__.return_value
                    client.call.side_effect = [
                        {"config": {"model_provider": "custom", "model": "model", "api_key": "SECRET-SENTINEL"}},
                        {"requiresOpenaiAuth": required, "account": account},
                    ]
                    result = inspect_codex(home)
                    self.assertEqual(result["authentication"], expected)
                    self.assertTrue(result["connected"])
                    self.assertEqual(result["provider"], "custom")
                    if account and account.get("planType") == "unknown":
                        self.assertIsNone(result["plan"])
                    self.assertNotIn("SECRET-SENTINEL", json.dumps(result))
                    self.assertNotIn("private@example.com", json.dumps(result))
                    self.assertNotIn("device", json.dumps(result))
                    self.assertEqual(client.call.call_args_list, [
                        call("config/read", {"includeLayers": False}),
                        call("account/read", {"refreshToken": False}),
                    ])

    def test_stale_socket_errors_are_not_reported_as_signed_out(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            socket = home / "app-server-control/app-server-control.sock"
            socket.parent.mkdir()
            socket.touch()
            with patch("codexd_bridge.transport.AppServer", side_effect=RuntimeError("unreachable")):
                with self.assertRaisesRegex(RuntimeError, "unreachable"):
                    inspect_codex(home)
