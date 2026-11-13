"""Offline CLI tests: mocked probe/integration/provisioning, temporary paths only."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from codexd_bridge import cli
from codexd_bridge.config import Backend
from codexd_bridge.probe import ProbeError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "registry.toml"
        self.executable = self.home / "codexd-bridge"
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        self.backend = Backend("demo", self.home, "demo")
        self.skill = self.home / "SKILL.md"
        self.verification = {"available": True, "provider": "demo", "deleted": True}
        self.registration = {"effective": True, "registered": True}

        self.load = self.start(
            patch("codexd_bridge.cli.load_backends", return_value={"demo": self.backend})
        )
        self.check = self.start(
            patch("codexd_bridge.probe.check_model", return_value=self.verification)
        )
        self.install = self.start(
            patch("codexd_bridge.integration.install_skill", return_value=self.skill)
        )
        self.register = self.start(
            patch("codexd_bridge.integration.register_bridge", return_value=self.registration)
        )
        self.enable = self.start(patch("codexd_bridge.provisioning.set_backend_enabled"))
        self.bridge_cls = self.start(patch("codexd_bridge.sessions.Bridge"))
        self.bridge = self.bridge_cls.return_value
        self.bridge.list_provider_models.return_value = {
            "providers": [{"provider": "demo", "models": []}]
        }
        self.bridge.list_models.return_value = {
            "backend": "demo",
            "models": [],
            "next_cursor": None,
        }
        self.manager = Mock()
        self.manager.attach_mock(self.check, "check")
        self.manager.attach_mock(self.install, "install")
        self.manager.attach_mock(self.register, "register")
        self.manager.attach_mock(self.enable, "enable")

    def start(self, item):
        self.addCleanup(item.stop)
        return item.start()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["--config", str(self.config), *argv])
        return out.getvalue(), err.getvalue()

    def run_cli_error(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--config", str(self.config), *argv])
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def connect(self, *extra):
        return self.run_cli("connect", "demo", "--executable", str(self.executable), *extra)

    def connect_error(self, *extra):
        return self.run_cli_error("connect", "demo", "--executable", str(self.executable), *extra)

    def test_connect_verifies_model_before_install_register_enable(self):
        out, _ = self.connect()
        result = json.loads(out)
        self.assertEqual(result["backend"], "demo")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["verification"], self.verification)
        self.assertEqual(result["skill"], str(self.skill))
        self.assertEqual(result["registration"], self.registration)
        self.assertEqual(
            [name for name, _, _ in self.manager.mock_calls],
            ["check", "install", "register", "enable"],
        )
        self.check.assert_called_once_with(self.backend)
        self.install.assert_called_once_with(self.home, update=False)
        self.register.assert_called_once_with(
            self.home, self.executable, self.config.resolve(), allow_session_writes=False
        )
        self.enable.assert_called_once_with("demo", True, registry_path=self.config.resolve())

    def test_connect_model_failure_blocks_install_register_enable(self):
        self.check.side_effect = ProbeError("Backend demo: probe reply was wrong")
        err = self.connect_error()
        self.assertIn("probe reply was wrong", err)
        self.install.assert_not_called()
        self.register.assert_not_called()
        self.enable.assert_not_called()

    def test_connect_registration_failure_keeps_skill_and_does_not_enable(self):
        self.register.side_effect = RuntimeError("registry daemon refused")
        err = self.connect_error()
        self.assertIn("MCP registration failed", err)
        self.assertIn(str(self.skill), err)
        self.install.assert_called_once_with(self.home, update=False)
        self.enable.assert_not_called()

    def test_connect_policy_override_does_not_enable(self):
        self.register.return_value = {"effective": False}
        err = self.connect_error()
        self.assertIn("not effective", err)
        self.enable.assert_not_called()

    def test_connect_session_writes_require_explicit_flag(self):
        self.connect()
        self.connect("--allow-session-writes")
        flags = [kwargs["allow_session_writes"] for _, kwargs in self.register.call_args_list]
        self.assertEqual(flags, [False, True])
        self.assertEqual(self.enable.call_count, 2)

    def test_skill_upgrade_requires_explicit_flag(self):
        self.connect("--update-skill")
        self.install.assert_called_once_with(self.home, update=True)

    def test_enable_failure_reports_completed_integration_steps(self):
        self.enable.side_effect = ValueError("registry unavailable")
        err = self.connect_error()
        self.assertIn("MCP registered", err)
        self.assertIn("Enabling backend demo failed", err)

    def test_connect_requires_existing_absolute_executable(self):
        for executable in (str(self.home / "missing-exe"), "relative-exe"):
            err = self.run_cli_error("connect", "demo", "--executable", executable)
            self.assertIn("--executable", err)
        self.check.assert_not_called()
        self.install.assert_not_called()
        self.register.assert_not_called()
        self.enable.assert_not_called()

    def test_connect_unknown_backend_is_rejected(self):
        err = self.run_cli_error("connect", "ghost", "--executable", str(self.executable))
        self.assertIn("Unknown backend", err)
        self.check.assert_not_called()
        self.install.assert_not_called()
        self.register.assert_not_called()
        self.enable.assert_not_called()

    def test_models_list_defaults_to_dynamic_aggregation(self):
        out, _ = self.run_cli("models", "list")
        self.assertEqual(json.loads(out), self.bridge.list_provider_models.return_value)
        self.bridge.list_provider_models.assert_called_once_with(20)
        self.bridge.list_models.assert_not_called()

    def test_models_list_backend_uses_cursor_pagination(self):
        out, _ = self.run_cli(
            "models", "list", "--backend", "demo", "--cursor", "page-2", "--limit", "5"
        )
        self.assertEqual(json.loads(out), self.bridge.list_models.return_value)
        self.bridge.list_models.assert_called_once_with("demo", "page-2", 5)
        self.bridge.list_provider_models.assert_not_called()

    def test_models_list_cursor_without_backend_is_rejected(self):
        err = self.run_cli_error("models", "list", "--cursor", "page-2")
        self.assertIn("--backend", err)
        self.bridge.list_models.assert_not_called()
        self.bridge.list_provider_models.assert_not_called()


if __name__ == "__main__":
    unittest.main()
