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

    def test_uninstall_dispatches_without_loading_provider_configuration(self):
        with patch("codexd_bridge.installation.uninstall_current") as uninstall, patch(
            "codexd_bridge.cli.registry_path", side_effect=AssertionError("must not read registry")
        ):
            out, err = self.run_cli("uninstall")
        uninstall.assert_called_once_with()
        self.assertEqual((out, err), ("", ""))

    def test_version_does_not_load_registry(self):
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("codexd-bridge ", out.getvalue())
        self.load.assert_not_called()

    def test_doctor_returns_nonzero_with_machine_readable_findings(self):
        with patch("codexd_bridge.diagnostics.diagnose", return_value={"ok": False, "checks": []}), \
                redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            cli.main(["--config", str(self.config), "doctor"])
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.load.assert_not_called()

    def test_provider_add_passes_connection_settings_and_registry(self):
        with patch("codexd_bridge.providers.add_provider", return_value={"enabled": True}) as add:
            out, _ = self.run_cli(
                "provider", "add", "demo", "--base-url", "https://provider.example/v1",
                "--env-key", "DEMO_API_KEY", "--model", "model", "--context-window", "131072",
                "--effort", "high", "--wire-api", "responses", "--home", str(self.home / "new"),
            )
        self.assertTrue(json.loads(out)["enabled"])
        self.assertEqual(add.call_args.args,
                         ("demo", "https://provider.example/v1", "DEMO_API_KEY", "model", 131072))
        self.assertEqual(add.call_args.kwargs["registry_path"], self.config)
        self.assertEqual(add.call_args.kwargs["wire_api"], "responses")
        with patch("codexd_bridge.providers.add_provider") as add:
            self.assertIn("invalid choice", self.run_cli_error("provider", "add", "demo", "--wire-api", "chat"))
            add.assert_not_called()

    def test_daemon_remove_defaults_to_retaining_data(self):
        with patch("codexd_bridge.removal.remove_daemon", return_value={"removed": True}) as remove:
            out, _ = self.run_cli("daemon", "remove", "demo")
        self.assertTrue(json.loads(out)["removed"])
        self.assertEqual(remove.call_args.args, ("demo",))
        self.assertFalse(remove.call_args.kwargs["purge"])
        self.assertEqual(remove.call_args.kwargs["registry_path"], self.config)

    def test_daemon_remove_passes_purge_without_additional_confirmation(self):
        with patch("codexd_bridge.removal.remove_daemon", return_value={"removed": True}) as remove:
            self.run_cli("daemon", "remove", "demo", "--purge")
        self.assertTrue(remove.call_args.kwargs["purge"])

    def test_provider_run_forwards_flags_and_double_dash(self):
        from functools import partial

        from codexd_bridge.providers import run_provider

        (self.home / "config.toml").write_text('model_provider="demo"\n')
        for tail, attaches in ((["--help"], False), (["exec", "task"], False),
                               (["--", "exec"], True), (["--", "--help"], True),
                               (["--", "--no-daemon"], True),
                               (["--", "--", "task"], True),
                               (["--no-daemon", "task"], False)):
            execute = Mock(return_value=None)
            with self.subTest(tail=tail), patch("codexd_bridge.providers.load_backends",
                    return_value={"demo": self.backend}), patch(
                    "codexd_bridge.providers._ensure_running") as start, patch(
                    "codexd_bridge.providers.run_provider",
                    side_effect=partial(run_provider, exec_runner=execute)):
                out, _ = self.run_cli("provider", "run", "demo", *tail)
                remote = ["--remote", "unix://" + str(self.backend.socket_path)] if attaches else []
                self.assertEqual(execute.call_args.args[1], ["codex", *remote, *tail])
                self.assertEqual(execute.call_args.args[2]["CODEX_HOME"], str(self.home))
                self.assertEqual(start.call_count, int(attaches))
                self.assertEqual(out, "")
        self.assertIn("Choose a backend", self.run_cli_error("provider", "run"))

    def test_provider_connect_retries_existing_route(self):
        with patch("codexd_bridge.providers.connect_provider", return_value={"enabled": True}) as finish:
            self.run_cli("provider", "connect", "demo", "--launcher-dir", str(self.home / "bin"), "--update-skill")
        self.assertEqual(finish.call_args.args, ("demo",))
        self.assertEqual(finish.call_args.kwargs["registry_path"], self.config)
        self.assertTrue(finish.call_args.kwargs["update_skill"])

    def test_provider_launcher_installs_command_without_verification(self):
        with patch("codexd_bridge.providers.install_provider_launcher", return_value={"created": True}) as install:
            out, _ = self.run_cli("provider", "launcher", "demo", "--launcher-dir", str(self.home / "bin"))
        self.assertTrue(json.loads(out)["created"])
        install.assert_called_once_with("demo", registry_path=self.config,
                                        launcher_dir=self.home / "bin", executable=None)
        self.check.assert_not_called()

    def test_provider_add_requires_finite_context_information(self):
        err = self.run_cli_error("provider", "add", "demo", "--base-url", "https://example.com",
                                 "--env-key", "DEMO_KEY", "--model", "model")
        self.assertIn("--context-window", err)

    def test_terminal_default_and_provider_entry_open_ui(self):
        with patch("codexd_bridge.cli.sys.stdin.isatty", return_value=True), patch(
                "codexd_bridge.cli.sys.stdout.isatty", return_value=True), patch("codexd_bridge.tui.run_ui") as ui:
            for argv in ([], ["ui"], ["provider"], ["provider", "add"]):
                cli.main(["--config", str(self.config), *argv])
            self.assertEqual(ui.call_count, 4)
            ui.assert_called_with(self.config)

    def test_piped_default_and_explicit_serve_keep_mcp_protocol(self):
        with patch("codexd_bridge.cli.sys.stdin.isatty", return_value=False), patch(
                "codexd_bridge.server.create_server") as server, patch("codexd_bridge.tui.run_ui") as ui:
            self.run_cli()
            self.run_cli("serve")
            self.assertEqual(server.call_count, 2)
            ui.assert_not_called()

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
        self.enable.assert_called_once_with("demo", True, registry_path=self.config.resolve(),
                                            expected_backend=self.backend)

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
