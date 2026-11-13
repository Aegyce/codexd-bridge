"""Offline tests for codexd_bridge.providers: no daemon started, no real inference."""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codexd_bridge import providers
from codexd_bridge.integration import IntegrationError
from codexd_bridge.probe import ProbeError
from codexd_bridge.providers import ProviderError, add_provider, connect_provider, run_provider
from codexd_bridge.provisioning import ProvisioningError, set_backend_enabled


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ProvidersTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

        self.registry = self.root / "bridge.toml"
        self.homes = self.root / "homes"
        self.homes.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.executable = self.bin / "codexd-bridge"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.data = self.root / ".local" / "share" / "codexd-bridge"

        run = patch("codexd_bridge.provisioning.subprocess.run")
        self.subprocess_run = run.start()
        self.addCleanup(run.stop)
        self.subprocess_run.side_effect = AssertionError("daemon commands must not run in unit tests")

        self.daemon_state = "running"
        self.daemon_action = self.patch(
            "codexd_bridge.providers.daemon_action", side_effect=self._daemon_action
        )
        self.check_model = self.patch(
            "codexd_bridge.providers.check_model",
            return_value={"available": True, "model": "demo-model", "deleted": True},
        )
        self.install_skill = self.patch(
            "codexd_bridge.providers.install_skill",
            side_effect=lambda home, **kw: Path(home) / "skills" / "codexd-bridge",
        )
        self.register_bridge = self.patch(
            "codexd_bridge.providers.register_bridge",
            return_value={"effective": True, "changed": True, "server": "codexd-bridge"},
        )

    def patch(self, target, **kw):
        patcher = patch(target, **kw)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _daemon_action(self, backend, action, *, timeout=45):
        sock = str(backend.home / "app-server-control" / "app-server-control.sock")
        base = {"backend": backend.name, "home": str(backend.home), "socketPath": sock}
        if action == "status":
            return {**base, "action": "status", "status": self.daemon_state}
        return {**base, "action": action, "status": "started" if action == "start" else "stopped"}

    def add(self, name="demo", **kw):
        defaults = dict(
            base_url="https://api.example.invalid/v1",
            env_key="DEMO_API_KEY",
            model="demo-model",
            context_window=131072,
            home=self.homes / name,
            launcher_dir=self.bin,
            executable=self.executable,
            registry_path=self.registry,
        )
        defaults.update(kw)
        return add_provider(name, **defaults)

    def connect(self, name="demo", **kw):
        defaults = dict(
            launcher_dir=self.bin, executable=self.executable, registry_path=self.registry
        )
        defaults.update(kw)
        return connect_provider(name, **defaults)

    def external_catalog(self, model="demo-model", cw=131072, compact=None, name="external.json"):
        compact = cw * 8 // 10 if compact is None else compact
        path = self.root / name
        path.write_text(
            json.dumps({"models": [{"slug": model, "context_window": cw, "auto_compact_token_limit": compact}]}),
            encoding="utf-8",
        )
        return path

    def read_registry(self):
        return tomllib.loads(self.registry.read_text(encoding="utf-8"))["backends"]

    def read_home_config(self, name="demo"):
        return tomllib.loads((self.homes / name / "config.toml").read_text(encoding="utf-8"))

    def read_catalog(self, name="demo"):
        return json.loads((self.homes / name / "model-catalog.json").read_text(encoding="utf-8"))

    def launcher(self, name="demo"):
        return self.bin / f"codex-{name}"

    def socket(self, name="demo"):
        return str(self.homes / name / "app-server-control" / "app-server-control.sock")


class AddProviderSuccess(ProvidersTestCase):
    def test_creates_enabled_route_home_and_launcher(self):
        result = self.add()
        self.assertTrue(result["enabled"])
        entry = self.read_registry()["demo"]
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["model_provider"], "demo")
        config = self.read_home_config()
        self.assertEqual(config["model_provider"], "demo")
        self.assertEqual(config["model_providers"]["demo"]["env_key"], "DEMO_API_KEY")
        self.assertEqual(config["model_catalog_json"], str(self.homes / "demo" / "model-catalog.json"))
        launcher = self.launcher()
        self.assertEqual(mode(launcher), 0o755)
        self.assertIn('provider run demo "$@"', launcher.read_text(encoding="utf-8"))
        self.check_model.assert_called_once()
        self.register_bridge.assert_called_once()
        self.subprocess_run.assert_not_called()

    def test_default_home_and_catalog_in_home(self):
        result = self.add(home=None)
        home = self.data / "homes" / "demo"
        self.assertEqual(result["home"], str(home))
        self.assertTrue((home / "config.toml").is_file())
        self.assertTrue((home / "model-catalog.json").is_file())
        self.assertFalse((self.data / "catalogs").exists())

    def test_effort_advertised_only_for_user_choice(self):
        self.add(effort="high")
        entry = self.read_catalog()["models"][0]
        self.assertEqual([x["effort"] for x in entry["supported_reasoning_levels"]], ["high"])
        self.assertEqual(entry["default_reasoning_level"], "high")
        self.assertEqual(self.read_home_config()["model_reasoning_effort"], "high")

    def test_minimal_catalog_shape(self):
        self.add()
        catalog = self.read_catalog()
        self.assertEqual(len(catalog["models"]), 1)
        entry = catalog["models"][0]
        self.assertEqual(entry["slug"], "demo-model")
        self.assertEqual(entry["context_window"], 131072)
        self.assertEqual(entry["auto_compact_token_limit"], 131072 * 8 // 10)
        self.assertEqual(entry["supported_reasoning_levels"], [])
        for field in (
            "display_name", "shell_type", "visibility", "supported_in_api", "priority",
            "support_verbosity", "truncation_policy", "experimental_supported_tools", "base_instructions",
        ):
            self.assertIn(field, entry)
        self.assertNotIn("qwen", entry["base_instructions"].lower())

    def test_external_catalog_referenced_when_bound_matches(self):
        full = self.external_catalog()
        result = self.add(model_catalog=full)
        self.assertEqual(result["catalog"], str(full))
        self.assertEqual(result["context_window"], 131072)
        self.assertEqual(result["auto_compact_token_limit"], 131072 * 8 // 10)
        self.assertEqual(self.read_home_config()["model_catalog_json"], str(full))
        self.assertFalse((self.homes / "demo" / "model-catalog.json").exists())

    def test_only_env_var_name_stored_never_value(self):
        with patch.dict(os.environ, {"DEMO_API_KEY": "sk-LIVE-SECRET-VALUE"}):
            self.add()
        for path in (
            self.registry, self.homes / "demo" / "config.toml",
            self.homes / "demo" / "model-catalog.json", self.launcher(),
        ):
            self.assertNotIn("sk-LIVE-SECRET-VALUE", path.read_text(encoding="utf-8"))


class AddProviderValidation(ProvidersTestCase):
    def assertRejected(self, **kw):
        with self.assertRaises((ProvisioningError, ProviderError, ValueError)):
            self.add(**kw)
        self.assertFalse(self.registry.exists())
        self.assertFalse((self.homes / "demo").exists())
        self.assertFalse(self.launcher().exists())

    def test_bad_names(self):
        for name in ("Demo", "1bad", "bad name", "", "bad/name"):
            with self.assertRaises(ProvisioningError):
                self.add(name=name)
        self.assertFalse(self.registry.exists())

    def test_env_key_must_be_variable_name(self):
        for key in ("sk-ABC123", "DEMO KEY", "DEMO_API_KEY=sk-x", "123", "", "a.b"):
            self.assertRejected(env_key=key)

    def test_base_url_rejects_credentials_query_and_scheme(self):
        for url in (
            "https://user:pass@api.example.invalid/v1",
            "https://api.example.invalid/v1?key=1",
            "https://api.example.invalid/v1#frag",
            "ftp://api.example.invalid", "not a url",
        ):
            self.assertRejected(base_url=url)

    def test_context_window_must_be_positive_int(self):
        for cw in (0, -1, True, 1.5, "131072", None):
            self.assertRejected(context_window=cw)

    def test_wire_api_enum(self):
        self.assertRejected(wire_api="grpc")

    def test_model_required_nonempty(self):
        self.assertRejected(model="  ")

    def test_external_catalog_must_match_bound(self):
        self.assertRejected(model_catalog=self.external_catalog(cw=999))
        self.assertRejected(model_catalog=self.external_catalog(compact=1))
        self.assertRejected(model_catalog=self.external_catalog(model="other-model"))


class AddProviderConflicts(ProvidersTestCase):
    def test_existing_route_refused(self):
        self.add()
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertEqual(len(self.read_registry()), 1)

    def test_existing_home_refused(self):
        (self.homes / "demo").mkdir()
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertFalse(self.registry.exists())

    def test_dangling_home_symlink_refused(self):
        (self.homes / "demo").symlink_to(self.root / "missing-target")
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertFalse(self.registry.exists())

    def test_existing_launcher_refused(self):
        self.launcher().write_text("old", encoding="utf-8")
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertEqual(self.launcher().read_text(), "old")
        self.assertFalse(self.registry.exists())

    def test_dangling_launcher_symlink_refused(self):
        self.launcher().symlink_to(self.root / "missing-launcher")
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertFalse(self.registry.exists())

    def test_create_failure_leaves_no_orphan_catalog(self):
        self.patch("codexd_bridge.providers.create_daemon", side_effect=ProvisioningError("boom"))
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertFalse((self.homes / "demo").exists())
        self.assertEqual(list(self.homes.glob(".codexd-*.tmp")), [])
        self.assertFalse(self.registry.exists())

    def test_create_refusal_preserves_foreign_home(self):
        def fake_create(*a, **kw):
            home = self.homes / "demo"
            home.mkdir(exist_ok=True)
            (home / "config.toml").write_text("foreign", encoding="utf-8")
            raise ProvisioningError("home already exists; refusing to overwrite")
        self.patch("codexd_bridge.providers.create_daemon", side_effect=fake_create)
        with self.assertRaises(ProvisioningError):
            self.add()
        self.assertEqual((self.homes / "demo" / "config.toml").read_text(), "foreign")
        self.assertEqual(list(self.homes.glob(".codexd-*.tmp")), [])


class AddProviderFailure(ProvidersTestCase):
    def assert_disabled_no_launcher(self):
        with self.assertRaises(ProviderError):
            self.add()
        self.assertFalse(self.read_registry()["demo"]["enabled"])
        self.assertFalse(self.launcher().exists())
        self.assertTrue((self.homes / "demo" / "config.toml").is_file())

    def test_probe_failure(self):
        self.check_model.side_effect = ProbeError("no marker reply")
        self.assert_disabled_no_launcher()

    def test_registration_failure(self):
        self.register_bridge.side_effect = IntegrationError("write refused")
        self.assert_disabled_no_launcher()

    def test_registration_not_effective(self):
        self.register_bridge.return_value = {"effective": False, "warning": "overridden"}
        self.assert_disabled_no_launcher()

    def test_skill_install_failure(self):
        self.install_skill.side_effect = IntegrationError("skill conflict")
        self.assert_disabled_no_launcher()

    def test_unreachable_daemon_no_restart(self):
        self.daemon_state = "unreachable"
        self.assert_disabled_no_launcher()
        self.assertNotIn("start", [c.args[1] for c in self.daemon_action.call_args_list])

    def test_enable_failure_removes_launcher(self):
        self.patch(
            "codexd_bridge.providers.set_backend_enabled",
            side_effect=ProvisioningError("registry write failed"),
        )
        with self.assertRaises(ProviderError):
            self.add()
        self.assertFalse(self.read_registry()["demo"]["enabled"])
        self.assertFalse(self.launcher().exists())


class ConnectProvider(ProvidersTestCase):
    def test_retry_enables_disabled_route_and_installs_launcher(self):
        self.add()
        set_backend_enabled("demo", False, registry_path=self.registry)
        self.launcher().unlink()
        result = self.connect()
        self.assertTrue(result["enabled"])
        self.assertTrue(self.read_registry()["demo"]["enabled"])
        self.assertTrue(self.launcher().is_file())

    def test_failure_preserves_disabled_state(self):
        self.add()
        set_backend_enabled("demo", False, registry_path=self.registry)
        self.launcher().unlink()
        self.check_model.side_effect = ProbeError("down")
        with self.assertRaises(ProviderError):
            self.connect()
        self.assertFalse(self.read_registry()["demo"]["enabled"])
        self.assertFalse(self.launcher().exists())

    def test_failure_preserves_enabled_state_and_launcher(self):
        self.add()
        self.check_model.side_effect = ProbeError("down")
        with self.assertRaises(ProviderError):
            self.connect()
        self.assertTrue(self.read_registry()["demo"]["enabled"])
        self.assertTrue(self.launcher().is_file())

    def test_idempotent_identical_launcher(self):
        self.add()
        before = self.launcher().read_bytes()
        result = self.connect()
        self.assertTrue(result["enabled"])
        self.assertEqual(self.launcher().read_bytes(), before)

    def test_refuses_differing_launcher(self):
        self.add()
        self.launcher().write_text("different", encoding="utf-8")
        with self.assertRaises((ProviderError, ProvisioningError)):
            self.connect()
        self.assertEqual(self.launcher().read_text(), "different")

    def test_nondefault_launcher_dir(self):
        self.add()
        self.launcher().unlink()
        other = self.root / "other-bin"
        result = self.connect(launcher_dir=other)
        self.assertEqual(result["launcher"], str(other / "codex-demo"))
        self.assertTrue((other / "codex-demo").is_file())

    def test_refuses_non_executable_identical_launcher(self):
        self.add()
        self.launcher().chmod(0o644)
        with self.assertRaises((ProviderError, ProvisioningError)):
            self.connect()

    def test_retry_error_includes_nondefault_quoted_flags(self):
        self.add()
        self.check_model.side_effect = ProbeError("down")
        with self.assertRaises(ProviderError) as ctx:
            self.connect()
        msg = str(ctx.exception)
        self.assertIn("provider connect demo", msg)
        self.assertIn("--config " + shlex.quote(str(self.registry)), msg)
        self.assertIn("--launcher-dir " + shlex.quote(str(self.bin)), msg)
        self.assertIn("--executable " + shlex.quote(str(self.executable)), msg)

    def test_unknown_route(self):
        self.add()
        with self.assertRaises(ProviderError):
            self.connect(name="nope")


class ProgressStages(ProvidersTestCase):
    STAGES = ["configuration", "daemon", "verification", "integration", "launcher", "ready"]

    def failed_stages(self, expected, **kw):
        stages = []
        with self.assertRaises((ProviderError, ProvisioningError)):
            self.add(progress=stages.append, **kw)
        self.assertEqual(stages, expected)

    def test_add_reports_all_stages_in_order(self):
        stages = []
        result = self.add(progress=stages.append)
        self.assertTrue(result["enabled"])
        self.assertEqual(stages, self.STAGES)

    def test_connect_reports_stages_without_configuration(self):
        self.add()
        stages = []
        result = self.connect(progress=stages.append)
        self.assertTrue(result["enabled"])
        self.assertEqual(stages, self.STAGES[1:])

    def test_progress_none_matches_omitted_behavior(self):
        result = self.add(progress=None)
        self.assertTrue(result["enabled"])
        self.assertTrue(self.launcher().is_file())

    def test_stages_precede_their_actual_operations(self):
        events = []
        real_create = providers.create_daemon
        real_install = providers._install_launcher
        real_enable = set_backend_enabled

        def record(label, fn):
            def wrapped(*a, **kw):
                events.append(label)
                return fn(*a, **kw)
            return wrapped

        self.patch(
            "codexd_bridge.providers.create_daemon",
            side_effect=record("create_daemon", real_create),
        )
        self.daemon_action.side_effect = record("daemon_status", self._daemon_action)
        self.check_model.side_effect = record("check_model", lambda *a, **kw: {"available": True})
        self.install_skill.side_effect = record(
            "install_skill", lambda home, **kw: Path(home) / "skills" / "codexd-bridge"
        )
        self.register_bridge.side_effect = record(
            "register_bridge", lambda *a, **kw: {"effective": True}
        )
        self.patch(
            "codexd_bridge.providers._install_launcher",
            side_effect=record("install_launcher", real_install),
        )
        self.patch(
            "codexd_bridge.providers.set_backend_enabled",
            side_effect=record("enable", real_enable),
        )
        self.add(progress=lambda stage: events.append("stage:" + stage))
        self.assertEqual(
            events,
            [
                "stage:configuration", "create_daemon",
                "stage:daemon", "daemon_status",
                "stage:verification", "check_model",
                "stage:integration", "install_skill", "register_bridge",
                "stage:launcher", "install_launcher", "enable",
                "stage:ready",
            ],
        )

    def test_validation_failure_reports_nothing(self):
        self.failed_stages([], wire_api="grpc")

    def test_create_failure_stops_after_configuration(self):
        self.patch("codexd_bridge.providers.create_daemon", side_effect=ProvisioningError("boom"))
        self.failed_stages(self.STAGES[:1])

    def test_daemon_failure_stops_at_daemon(self):
        self.daemon_state = "unreachable"
        self.failed_stages(self.STAGES[:2])

    def test_verification_failure_stops_at_verification(self):
        self.check_model.side_effect = ProbeError("no marker reply")
        self.failed_stages(self.STAGES[:3])

    def test_skill_failure_stops_at_integration(self):
        self.install_skill.side_effect = IntegrationError("skill conflict")
        self.failed_stages(self.STAGES[:4])

    def test_registration_failure_stops_at_integration(self):
        self.register_bridge.side_effect = IntegrationError("write refused")
        self.failed_stages(self.STAGES[:4])

    def test_registration_not_effective_stops_at_integration(self):
        self.register_bridge.return_value = {"effective": False, "warning": "overridden"}
        self.failed_stages(self.STAGES[:4])

    def test_launcher_install_failure_stops_at_launcher(self):
        self.patch("codexd_bridge.providers._install_launcher", side_effect=OSError("disk full"))
        self.failed_stages(self.STAGES[:5])

    def test_enable_failure_stops_at_launcher(self):
        self.patch(
            "codexd_bridge.providers.set_backend_enabled",
            side_effect=ProvisioningError("registry write failed"),
        )
        self.failed_stages(self.STAGES[:5])

    def test_connect_failure_never_reports_ready(self):
        self.add()
        set_backend_enabled("demo", False, registry_path=self.registry)
        self.launcher().unlink()
        self.check_model.side_effect = ProbeError("down")
        stages = []
        with self.assertRaises(ProviderError):
            self.connect(progress=stages.append)
        self.assertEqual(stages, ["daemon", "verification"])

    def test_retry_connect_reuses_home_without_recreating_it(self):
        self.add()
        set_backend_enabled("demo", False, registry_path=self.registry)
        self.launcher().unlink()
        config = self.homes / "demo" / "config.toml"
        before = config.read_bytes()
        inode = config.stat().st_ino
        create = self.patch(
            "codexd_bridge.providers.create_daemon",
            side_effect=AssertionError("connect must not recreate the home"),
        )
        stages = []
        result = self.connect(progress=stages.append)
        self.assertTrue(result["enabled"])
        self.assertEqual(stages, self.STAGES[1:])
        create.assert_not_called()
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(config.stat().st_ino, inode)
        self.assertEqual(result["home"], str(self.homes / "demo"))
        self.assertEqual(len(self.read_registry()), 1)


class RetryCommand(ProvidersTestCase):
    def test_config_is_global_flag_before_provider_connect(self):
        command = providers._retry_command("demo", self.registry, self.bin, self.executable)
        tokens = shlex.split(command)
        self.assertEqual(tokens[:2], ["codexd-bridge", "--config"])
        self.assertEqual(tokens[2], str(self.registry))
        self.assertEqual(tokens[3:6], ["provider", "connect", "demo"])

    def test_quoting_round_trips_with_spaces(self):
        registry = self.root / "reg dir;rm" / "bridge config.toml"
        launcher_dir = self.root / "my bin"
        executable = self.root / "opt dir" / "codexd-bridge"
        command = providers._retry_command("demo", registry, launcher_dir, executable)
        self.assertEqual(
            shlex.split(command),
            [
                "codexd-bridge", "--config", str(registry),
                "provider", "connect", "demo",
                "--launcher-dir", str(launcher_dir),
                "--executable", str(executable),
            ],
        )

    def test_default_paths_omit_optional_flags(self):
        try:
            default_exe = providers._resolve_executable(None)
        except ProviderError:
            self.skipTest("no default codexd-bridge executable on this machine")
        registry = providers._resolve_config_path(None)[0].absolute()
        launcher_dir = providers._expand_user(providers.DEFAULT_LAUNCHER_DIR, "launcher_dir")
        command = providers._retry_command("demo", registry, launcher_dir, default_exe)
        self.assertEqual(shlex.split(command), ["codexd-bridge", "provider", "connect", "demo"])

    def test_connect_error_places_config_before_subcommand(self):
        self.add()
        self.check_model.side_effect = ProbeError("down")
        with self.assertRaises(ProviderError) as ctx:
            self.connect()
        msg = str(ctx.exception)
        self.assertLess(msg.index("--config"), msg.index("provider connect"))


class ProviderErrorDetail(ProvidersTestCase):
    def test_connect_failure_detail_is_cause_only(self):
        self.add()
        set_backend_enabled("demo", False, registry_path=self.registry)
        self.launcher().unlink()
        self.check_model.side_effect = ProbeError("no marker reply")
        with self.assertRaises(ProviderError) as ctx:
            self.connect()
        error = ctx.exception
        self.assertEqual(error.detail, "no marker reply")
        self.assertNotIn("Retry with", error.detail)
        self.assertNotIn("codexd-bridge", error.detail)
        msg = str(error)
        self.assertIn("connect failed; route state preserved", msg)
        self.assertIn("Retry with: codexd-bridge", msg)
        self.assertIn("Cause: no marker reply", msg)

    def test_registration_detail_carries_warning_cause(self):
        self.add()
        self.register_bridge.return_value = {"effective": False, "warning": "overridden"}
        with self.assertRaises(ProviderError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.detail, "MCP registration is not effective; overridden")

    def test_detail_defaults_to_none(self):
        self.assertIsNone(ProviderError("plain").detail)
        self.add()
        with self.assertRaises(ProviderError) as ctx:
            self.connect(name="nope")
        self.assertIsNone(ctx.exception.detail)


class LauncherSafety(ProvidersTestCase):
    def test_paths_are_shell_quoted(self):
        text = providers._launcher_text(
            Path("/opt/my bridge/codexd-bridge"), Path("/tmp/reg;rm -rf.toml"), "demo"
        )
        self.assertIn("'/opt/my bridge/codexd-bridge'", text)
        self.assertIn("'/tmp/reg;rm -rf.toml'", text)
        self.assertIn('provider run demo "$@"', text)

    def test_name_cannot_inject(self):
        with self.assertRaises(ProvisioningError):
            self.add(name="demo;rm")

    def test_install_is_atomic_no_overwrite(self):
        path = self.bin / "codex-x"
        providers._install_launcher(path, "first")
        with self.assertRaises(OSError):
            providers._install_launcher(path, "second")
        self.assertEqual(path.read_text(), "first")
        self.assertEqual(list(self.bin.glob(".codexd-*.tmp")), [])


class RunProvider(ProvidersTestCase):
    def setUp(self):
        super().setUp()
        self.add()
        self.daemon_action.reset_mock()
        self.exec_runner = Mock(return_value=None)

    def run_(self, args, name="demo"):
        return run_provider(name, args, registry_path=self.registry, exec_runner=self.exec_runner)

    def argv(self):
        return self.exec_runner.call_args.args[1]

    def test_interactive_attaches_remote_with_correct_home(self):
        self.run_([])
        file, argv, env = self.exec_runner.call_args.args
        self.assertEqual(file, "codex")
        self.assertEqual(argv, ["codex", "--remote", "unix://" + self.socket()])
        self.assertEqual(env["CODEX_HOME"], str(self.homes / "demo"))

    def test_changed_home_provider_cannot_start_interactive_or_exec_session(self):
        (self.homes / "demo" / "config.toml").write_text('model_provider="foreign"\n')
        for argv in ([], ["exec", "task"], ["review"]):
            with self.assertRaisesRegex(ProvisioningError, "model_provider does not match"):
                self.run_(argv)
        self.exec_runner.assert_not_called()
        self.daemon_action.assert_not_called()

    def test_arguments_preserved(self):
        self.run_(["-m", "other", "a prompt"])
        self.assertEqual(self.argv(), ["codex", "--remote", "unix://" + self.socket(), "-m", "other", "a prompt"])

    def test_exec_passthrough_no_daemon(self):
        self.run_(["exec", "hello"])
        self.assertEqual(self.argv(), ["codex", "exec", "hello"])
        self.daemon_action.assert_not_called()

    def test_help_and_review_passthrough(self):
        for args in (["--help"], ["help"], ["review"], ["-h"]):
            self.exec_runner.reset_mock()
            self.daemon_action.reset_mock()
            self.run_(args)
            self.assertNotIn("--remote", self.argv())
            self.daemon_action.assert_not_called()

    def test_resume_and_fork_attach(self):
        for args in (["resume", "abc"], ["fork"], ["agents"], ["queue", "--thread", "t", "--message", "m"]):
            self.exec_runner.reset_mock()
            self.run_(args)
            self.assertIn("--remote", self.argv())

    def test_prompt_word_exec_is_interactive(self):
        self.run_(["please exec this"])
        self.assertIn("--remote", self.argv())

    def test_option_value_not_misclassified_as_subcommand(self):
        self.run_(["-m", "exec"])
        self.assertIn("--remote", self.argv())
        self.assertEqual(self.argv()[-2:], ["-m", "exec"])

    def test_reject_remote_to_other_endpoint(self):
        with self.assertRaises(ProviderError):
            self.run_(["--remote", "unix://other"])
        self.exec_runner.assert_not_called()

    def test_allow_remote_to_own_socket(self):
        self.run_(["--remote", "unix://" + self.socket()])
        self.assertEqual(self.argv(), ["codex", "--remote", "unix://" + self.socket()])

    def test_reject_provider_profile_oss_overrides(self):
        for args in (
            ["--profile", "x"], ["-p", "x"], ["--oss"], ["--local-provider", "ollama"],
            ["-c", "model_provider=other"], ["--config=model_provider=other"],
            ["-cmodel_provider=other"], ["-c", "model_providers.demo.base_url=x"],
        ):
            self.exec_runner.reset_mock()
            with self.assertRaises(ProviderError, msg=args):
                self.run_(args)
            self.exec_runner.assert_not_called()

    def test_allow_model_and_non_provider_config(self):
        self.run_(["-m", "other", "-c", "sandbox_mode=read-only"])
        self.assertIn("--remote", self.argv())

    def test_starts_stopped_daemon_once(self):
        calls = {"n": 0}

        def flip(backend, action, *, timeout=45):
            if action == "status":
                calls["n"] += 1
                state = "stopped" if calls["n"] == 1 else "running"
                return {"status": state, "socketPath": self.socket(), "backend": backend.name}
            return {"status": "started", "socketPath": self.socket(), "backend": backend.name}

        self.daemon_action.side_effect = flip
        self.run_([])
        self.assertEqual([c.args[1] for c in self.daemon_action.call_args_list], ["status", "start", "status"])
        self.assertIn("--remote", self.argv())

    def test_unreachable_no_restart_no_exec(self):
        self.daemon_state = "unreachable"
        with self.assertRaises(ProviderError):
            self.run_([])
        self.exec_runner.assert_not_called()
        self.assertNotIn("start", [c.args[1] for c in self.daemon_action.call_args_list])

    def test_unknown_status_requires_running(self):
        self.daemon_state = "weird"
        with self.assertRaises(ProviderError):
            self.run_([])
        self.exec_runner.assert_not_called()

    def test_server_status_start_stop(self):
        self.assertEqual(self.run_(["server", "status"])["status"], "running")
        self.run_(["server", "stop"])
        self.assertEqual(self.daemon_action.call_args.args[1], "stop")
        self.run_(["server", "start"])
        self.exec_runner.assert_not_called()

    def test_server_restart_rejected(self):
        with self.assertRaises(ProviderError):
            self.run_(["server", "restart"])
        self.exec_runner.assert_not_called()

    def test_server_usage(self):
        self.assertIn("usage", self.run_(["server"]))

    def test_unknown_backend(self):
        with self.assertRaises(ProviderError):
            self.run_([], name="nope")
        self.exec_runner.assert_not_called()

    def test_binding_selects_correct_home_among_routes(self):
        self.add(name="beta")
        self.run_([], name="beta")
        env = self.exec_runner.call_args.args[2]
        self.assertEqual(env["CODEX_HOME"], str(self.homes / "beta"))
        self.assertIn("unix://" + self.socket("beta"), self.argv())


if __name__ == "__main__":
    unittest.main()
