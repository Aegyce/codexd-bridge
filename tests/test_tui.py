"""Drive the real terminal widgets with pipe input; native operations are mocked."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from codexd_bridge.config import Backend
from codexd_bridge.delegation import DelegationGuard, OperatorGuard
from codexd_bridge.providers import ProviderError
from codexd_bridge.sessions import Bridge
from codexd_bridge.transport import BridgeError
from codexd_bridge.tui import BridgeUI, clean, run_ui


class ResizableOutput(DummyOutput):
    size = Size(rows=32, columns=100)

    def get_size(self):
        return self.size


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "bridge.toml"
        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.addCleanup(self.pipe_context.__exit__, None, None, None)
        self.output = ResizableOutput()
        self.ui = BridgeUI(self.registry, input=self.pipe, output=self.output)
        self.task = asyncio.create_task(self.ui.app.run_async())
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        if self.ui.worker and not self.ui.worker.done():
            await asyncio.wait_for(self.ui.worker, 3)
        if not self.task.done():
            self.ui.app.exit()
        await asyncio.wait_for(self.task, 3)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    def activate(self, label):
        self.ui.buttons[label].handler()

    def fill(self, **values):
        for key, value in values.items():
            self.ui.fields[key].text = value

    def setup_provider(self):
        self.ui.new_provider()
        self.fill(
            name="demo",
            base_url="https://provider.example/v1",
            env_key="DEMO_API_KEY",
            model="model",
            context_window="131,072",
            home=str(self.root / "provider"),
            launcher_dir=str(self.root / "bin"),
        )

    def register(self):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        self.registry.write_text(
            f'[backends.demo]\nhome={json.dumps(str(home))}\nmodel_provider="demo"\n'
        )
        self.ui.home()
        return Backend("demo", home, "demo")

    def rendered(self):
        screen = self.ui.app.renderer._last_screen
        return "\n".join(
            "".join(screen.data_buffer[y][x].char for x in range(self.output.size.columns))
            for y in range(self.output.size.rows)
        )

    async def test_keyboard_opens_form_and_preserves_fields_on_resize(self):
        self.pipe.send_text("\x0e")  # Ctrl+N
        await self.until(lambda: self.ui.screen == "form")
        self.pipe.send_text("demo\t")
        await self.until(lambda: self.ui.fields["name"].text == "demo")
        await self.until(lambda: self.ui.app.layout.has_focus(self.ui.fields["base_url"]))
        self.output.size = Size(rows=10, columns=40)
        self.ui.app.invalidate()
        await asyncio.sleep(0.05)
        self.assertIn("Resize the terminal", self.rendered())
        self.output.size = Size(rows=24, columns=80)
        self.ui.app.invalidate()
        await asyncio.sleep(0.05)
        self.assertEqual(self.ui.fields["name"].text, "demo")
        self.assertIn("Create a provider", self.rendered())

    async def test_first_screen_and_keyboard_complete_current_codex_setup(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            self.pipe.send_text("\r")
            await self.until(lambda: self.ui.title == "Set up this Codex")
        self.assertEqual(self.ui.fields["home"].text, str(self.root))
        self.assertFalse(self.ui.app.mouse_support())
        self.ui.fields["provider"].current_value = "openai"
        self.fill(name="current")
        with patch("codexd_bridge.providers._resolve_executable", return_value=self.root / "bridge"), patch(
            "codexd_bridge.integration.install_bridge", return_value={"backend": "current"}
        ) as install:
            self.pipe.send_text("\x13")
            await self.until(lambda: self.ui.screen == "success")
        self.assertEqual(install.call_args.args, (self.root, self.root / "bridge", self.registry))
        self.assertEqual(install.call_args.kwargs["provider"], "openai")
        self.assertFalse(install.call_args.kwargs["update_skill"])
        self.assertEqual(self.ui.selected, "current")
        self.assertIn("Open Codex", self.ui.buttons)

    async def test_setup_failure_keeps_values_and_offers_guide_upgrade_in_ui(self):
        self.activate("Set up this Codex")
        self.fill(home=str(self.root), name="current")
        with patch("codexd_bridge.providers._resolve_executable", return_value=self.root / "bridge"), patch(
            "codexd_bridge.integration.install_bridge",
            side_effect=BridgeError("refusing to overwrite differing skill content: use connect --update-skill"),
        ):
            self.activate("Set up")
            await self.until(lambda: self.ui.screen == "error")
        self.assertIn("Update guide", self.ui.buttons)
        self.activate("Update guide")
        self.assertEqual(self.ui.fields["name"].text, "current")
        self.assertEqual(self.ui.fields["home"].text, str(self.root))
        self.assertEqual(self.ui.fields["update"].current_value, "replace")

    async def test_setup_missing_provider_offers_a_choice_without_config_editing(self):
        self.activate("Set up this Codex")
        self.fill(home=str(self.root))
        with patch("codexd_bridge.providers._resolve_executable", return_value=self.root / "bridge"), patch(
            "codexd_bridge.integration.install_bridge",
            side_effect=ValueError("home config.toml must set 'model_provider' explicitly"),
        ):
            self.activate("Set up")
            await self.until(lambda: self.ui.screen == "error")
        self.activate("Retry setup")
        self.assertEqual(self.ui.fields["home"].text, str(self.root))
        self.assertIn("provider", self.ui.fields)

    async def test_missing_credential_reference_blocks_creation(self):
        self.setup_provider()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("codexd_bridge.providers.add_provider") as add,
        ):
            self.activate("Create provider")
        self.assertIn("not exported", self.ui.notice)
        add.assert_not_called()
        self.assertFalse(self.registry.exists())

    async def test_creation_uses_form_values_progress_and_real_completion(self):
        self.setup_provider()
        result = {
            "backend": "demo",
            "home": str(self.root / "provider"),
            "launcher": str(self.root / "bin/codex-demo"),
        }

        def create(*args, **kwargs):
            for stage in (
                "configuration",
                "daemon",
                "verification",
                "integration",
                "launcher",
                "ready",
            ):
                kwargs["progress"](stage)
            return result

        with (
            patch.dict(os.environ, {"DEMO_API_KEY": "SECRET-SENTINEL"}),
            patch("codexd_bridge.providers.add_provider", side_effect=create) as add,
        ):
            self.pipe.send_text("\x13")
            await self.until(lambda: self.ui.screen == "success")
            self.assertNotIn("SECRET-SENTINEL", self.rendered())
            self.assertEqual(add.call_count, 1)
        self.assertEqual(
            add.call_args.args,
            ("demo", "https://provider.example/v1", "DEMO_API_KEY", "model", 131072),
        )
        self.assertEqual(add.call_args.kwargs["registry_path"], self.registry)
        self.assertEqual(
            self.ui.completed,
            ["configuration", "daemon", "verification", "integration", "launcher"],
        )
        self.assertEqual(self.ui.stage, "ready")

    async def test_partial_setup_retries_connect_without_creating_again(self):
        self.setup_provider()

        def fail(*_args, **_kwargs):
            self.registry.write_text(
                f'[backends.demo]\nhome={json.dumps(str(self.root / "provider"))}\nmodel_provider="demo"\nenabled=false\n'
            )
            raise ProviderError("CLI retry instructions", detail="Model access failed")

        with (
            patch.dict(os.environ, {"DEMO_API_KEY": "sentinel"}),
            patch("codexd_bridge.providers.add_provider", side_effect=fail) as add,
            patch(
                "codexd_bridge.providers.connect_provider",
                return_value={
                    "backend": "demo",
                    "home": str(self.root / "provider"),
                    "launcher": "codex-demo",
                },
            ) as connect,
        ):
            self.activate("Create provider")
            await self.until(lambda: self.ui.screen == "error")
            self.assertIn("Retry connection", self.ui.buttons)
            self.activate("Retry connection")
            await self.until(lambda: self.ui.screen == "success")
        self.assertEqual(add.call_count, 1)
        self.assertEqual(connect.call_args.args, ("demo",))
        self.assertEqual(connect.call_args.kwargs["launcher_dir"], self.root / "bin")

    async def test_work_cannot_be_submitted_twice_and_quit_waits_for_result(self):
        release = threading.Event()
        self.addCleanup(release.set)
        operation = Mock(side_effect=lambda _p: release.wait(timeout=2))
        self.ui.work("Blocking operation", operation, lambda _r: self.ui.home())
        self.ui.work("Duplicate", operation, lambda _r: self.ui.home())
        await self.until(lambda: operation.called)
        self.pipe.send_text("\x11")
        await self.until(lambda: self.ui.close_when_done)
        self.assertFalse(self.task.done())
        release.set()
        await asyncio.wait_for(self.task, 2)
        self.assertEqual(operation.call_count, 1)

    async def test_operator_mode_is_not_the_mcp_default(self):
        self.assertIsInstance(self.ui.bridge.guard, OperatorGuard)
        self.assertIsInstance(Bridge(self.registry).guard, DelegationGuard)

    async def test_stop_needs_explicit_confirmation_and_targets_selected_backend(self):
        backend = self.register()
        with patch(
            "codexd_bridge.provisioning.daemon_action", return_value={"status": "stopped"}
        ) as action:
            self.activate("Stop daemon")
            action.assert_not_called()
            self.activate("Stop daemon")
            await self.until(lambda: not self.ui.busy)
        action.assert_called_once_with(backend, "stop")
        self.ui.home()
        with patch("codexd_bridge.provisioning.daemon_action") as action:
            self.activate("Stop daemon")
            self.registry.write_text(self.registry.read_text().replace(str(backend.home),
                                                                       str(self.root / "replacement")))
            self.activate("Stop daemon")
            await self.until(lambda: not self.ui.busy)
            action.assert_not_called()
            self.assertEqual(self.ui.screen, "error")

    async def test_home_and_more_tools_expose_all_management_operations(self):
        self.register()
        self.assertTrue({"Set up this Codex", "New provider", "Attach existing", "All models",
            "Daemon status", "Start daemon", "Stop daemon", "Remove daemon", "Connect bridge",
            "Install launcher", "Models", "Sessions", "Open Codex", "More tools"}.issubset(self.ui.buttons))
        self.activate("More tools")
        self.assertTrue({"Create daemon", "Choose registry", "Diagnostics", "MCP connection", "Uninstall bridge",
                         "Verify model ID", "Codex arguments"}.issubset(self.ui.buttons))

    async def test_diagnostics_are_accessible_when_registry_is_corrupt(self):
        self.registry.write_text("invalid = [")
        self.ui.home()
        self.assertIn("Choose registry", self.ui.buttons)
        result = {"version": "test", "checks": [{"name": "Registry", "status": "error",
                  "detail": "Cannot load", "advice": "Choose registry"}]}
        with patch("codexd_bridge.diagnostics.diagnose", return_value=result) as diagnose:
            self.activate("Diagnostics")
            await self.until(lambda: self.ui.title == "Installation diagnostics")
        diagnose.assert_called_once_with(self.registry)
        self.assertIn("Choose registry", self.ui.buttons)

    async def test_remove_daemon_is_reviewed_and_retains_data_by_default(self):
        backend = self.register()
        with patch("codexd_bridge.removal.remove_daemon", return_value={
            "removed": True, "data_deleted": False, "kept_files": []}) as remove:
            self.activate("Remove daemon")
            remove.assert_not_called()
            self.assertEqual(self.ui.fields["data"].current_value, "keep")
            self.pipe.send_text("\x13")
            await self.until(lambda: self.ui.screen == "success")
        self.assertEqual(remove.call_args.args, ("demo",))
        self.assertEqual(remove.call_args.kwargs["expected_backend"], backend)
        self.assertFalse(remove.call_args.kwargs["purge"])

    async def test_purge_in_tui_requires_exact_typed_backend_name(self):
        self.register()
        with patch("codexd_bridge.removal.remove_daemon", return_value={
            "removed": True, "data_deleted": True, "kept_files": []}) as remove:
            self.activate("Remove daemon")
            self.ui.fields["data"].current_value = "delete"
            self.pipe.send_text("\x13")
            await self.until(lambda: "exact backend name" in self.ui.notice)
            remove.assert_not_called()
            self.fill(confirmation="demo")
            self.pipe.send_text("\x13")
            await self.until(lambda: self.ui.screen == "success")
        self.assertTrue(remove.call_args.kwargs["purge"])

    async def test_empty_registry_returns_to_welcome_screen(self):
        self.registry.write_text("[backends]\n")
        self.ui.home()
        self.assertEqual(self.ui.screen, "home")
        self.assertIn("New provider", self.ui.buttons)
        self.assertIsNone(self.ui.selected)

    async def test_all_home_actions_remain_keyboard_accessible_at_minimum_size(self):
        self.output.size = Size(rows=18, columns=60)
        self.register()
        visited = set()
        for _ in range(40):
            self.pipe.send_text("\t")
            await asyncio.sleep(.01)
            for name, button in self.ui.buttons.items():
                if self.ui.app.layout.has_focus(button):
                    visited.add(name)
                    self.assertIn(name, self.rendered())
            if visited == set(self.ui.buttons):
                break
        self.assertEqual(visited, set(self.ui.buttons))

    async def test_project_uninstall_is_confirmed_and_runs_after_tui_exit(self):
        self.activate("More tools")
        self.activate("Uninstall bridge")
        self.assertFalse(self.task.done())
        self.activate("Uninstall bridge")
        await self.until(self.task.done)
        self.assertEqual(self.task.result(), "uninstall")

    async def test_create_daemon_configuration_and_switch_registry_from_tui(self):
        self.ui.more_tools()
        with patch("codexd_bridge.provisioning.create_daemon",
                   return_value=Backend("demo", self.root / "new", "openai")) as create:
            self.activate("Create daemon")
            self.fill(name="demo", home=str(self.root / "new"), provider="openai", model="test")
            self.pipe.send_text("\x13")
            await self.until(lambda: not self.ui.busy and self.ui.title == "Provider attached")
        self.assertEqual(create.call_args.args, ("demo", self.root / "new", "openai", "test"))
        other = self.root / "other.toml"
        other.write_text("[backends]\n")
        self.ui.choose_registry()
        self.fill(registry=str(other))
        self.pipe.send_text("\x13")
        await self.until(lambda: self.ui.screen == "home")
        self.assertEqual(self.ui.registry, other)
        self.assertEqual(self.ui.bridge.config_path, other)

    async def test_model_discovery_and_verification_use_selected_model(self):
        backend = self.register()
        self.ui.bridge.list_models = Mock(
            return_value={
                "models": [
                    {
                        "model": "picked-model",
                        "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    }
                ],
                "next_cursor": "page-two",
            }
        )
        self.ui.models()
        await self.until(lambda: not self.ui.busy)
        self.activate("Details")
        with patch("codexd_bridge.probe.check_model", return_value={"deleted": True}) as check:
            self.activate("Verify model")
            self.ui.fields["effort"].current_value = "low"
            self.activate("Run verification")
            await self.until(lambda: not self.ui.busy)
        check.assert_called_once_with(backend, model="picked-model", effort="low")

    async def test_active_session_steers_expected_turn_and_does_not_retry_failure(self):
        self.register()
        self.ui.session_view(
            {
                "session": {"id": "thread", "status": {"type": "active"}},
                "turns": [{"id": "active-turn", "status": "inProgress", "messages": []}],
            }
        )
        self.assertNotIn("Continue", self.ui.buttons)
        self.activate("Add instructions")
        self.fill(prompt="Additional instructions")
        self.ui.bridge.steer_session = Mock(
            side_effect=BridgeError("Turn changed; refresh the session")
        )
        self.ui.bridge.send_message = Mock()
        self.activate("Send instructions")
        await self.until(lambda: self.ui.screen == "error")
        self.ui.bridge.steer_session.assert_called_once_with(
            "demo", "thread", "active-turn", "Additional instructions"
        )
        self.ui.bridge.send_message.assert_not_called()
        self.assertNotIn("Retry", self.ui.buttons)

    async def test_model_actions_use_advertised_defaults_and_keep_provider(self):
        target = self.register()
        for efforts, default in (([{"reasoningEffort": "low"}], "low"), ([], "none")):
            model = {"model": "selected-model", "defaultReasoningEffort": default,
                     "supportedReasoningEfforts": efforts}
            self.ui.new_session(model)
            self.fill(title="Task", cwd=str(self.root), prompt="Review code")
            self.ui.bridge.create_session = Mock(return_value={"thread_id": "created"})
            self.activate("Start task")
            await self.until(lambda: not self.ui.busy)
            self.ui.bridge.create_session.assert_called_once_with(
                "demo", "Review code", "Task", str(self.root), "selected-model", default, "read-only",
                session_type="worker", acceptance_criteria=None,
            )
            with patch("codexd_bridge.probe.check_model", return_value={"available": True}) as check:
                self.ui.verify_model(model)
                self.activate("Run verification")
                await self.until(lambda: not self.ui.busy)
            check.assert_called_once_with(target, model="selected-model", effort=default)

    async def test_attachment_is_an_explicit_operation(self):
        self.activate("Attach existing")
        self.fill(name="demo", home=str(self.root / "home"))
        with patch(
            "codexd_bridge.provisioning.attach_daemon",
            return_value=Backend("demo", self.root / "home", "demo"),
        ) as attach:
            self.activate("Attach")
            await self.until(lambda: not self.ui.busy)
        attach.assert_called_once_with("demo", self.root / "home", registry_path=self.registry)
        self.assertEqual(self.ui.selected, "demo")

    async def test_connection_changes_approvals_only_when_selected(self):
        backend = self.register()
        for checked in (False, True):
            self.ui.connect()
            self.ui.fields["unattended"].checked = checked
            with (
                patch("codexd_bridge.providers._ensure_running"),
                patch(
                    "codexd_bridge.integration.connect_backend", return_value={"enabled": True}
                ) as connect,
            ):
                self.activate("Verify and connect")
                await self.until(lambda: not self.ui.busy)
            self.assertEqual(connect.call_args.args[0], backend)
            self.assertEqual(connect.call_args.kwargs["allow_session_writes"], checked)
            self.assertFalse(connect.call_args.kwargs["update_skill"])

    async def test_install_launcher_does_not_repeat_provider_setup(self):
        self.register()
        with patch("codexd_bridge.providers.install_provider_launcher", return_value={
            "backend": "demo", "launcher": str(self.root / "bin/codex-demo")}) as install, patch(
            "codexd_bridge.providers.connect_provider") as connect:
            self.activate("Install launcher")
            self.fill(directory=str(self.root / "bin"))
            self.activate("Install launcher")
            await self.until(lambda: self.ui.screen == "success")
        install.assert_called_once_with("demo", registry_path=self.registry, launcher_dir=self.root / "bin")
        connect.assert_not_called()
        self.assertEqual(self.ui.title, "Launcher installed")

    async def test_connection_retry_preserves_options_for_only_that_backend_and_registry(self):
        self.register()
        self.ui.connect()
        self.ui.fields["update_skill"].checked = True
        self.ui.fields["unattended"].checked = True
        with patch("codexd_bridge.providers._ensure_running"), patch(
            "codexd_bridge.integration.connect_backend", side_effect=BridgeError("offline")):
            self.activate("Verify and connect")
            await self.until(lambda: self.ui.screen == "error")
        self.activate("Retry")
        self.assertTrue(self.ui.fields["update_skill"].checked)
        self.assertTrue(self.ui.fields["unattended"].checked)
        self.ui.selected = "other"
        self.ui.connect()
        self.assertFalse(self.ui.fields["unattended"].checked)
        self.ui.selected = "demo"
        self.ui.registry = self.root / "other.toml"
        self.ui.connect()
        self.assertFalse(self.ui.fields["update_skill"].checked)
        self.assertFalse(self.ui.fields["unattended"].checked)

    async def test_open_native_codex_returns_to_provider_manager(self):
        self.register()
        with (
            patch("codexd_bridge.providers.run_provider", return_value=0) as run,
            patch(
                "codexd_bridge.tui.run_in_terminal",
                new=AsyncMock(side_effect=lambda fn, **_kw: fn()),
            ),
        ):
            self.ui.open_codex()
            await self.until(lambda: not self.ui.busy)
        self.assertEqual(run.call_args.args, ("demo", ()))
        self.assertEqual(run.call_args.kwargs["registry_path"], self.registry)
        self.assertEqual(self.ui.screen, "home")
        with patch("codexd_bridge.providers.run_provider", return_value={"backend": "demo", "status": "running"}), \
                patch("codexd_bridge.tui.run_in_terminal", new=AsyncMock(side_effect=lambda fn, **_kw: fn())):
            self.ui.codex_arguments()
            self.fill(args="server status")
            self.activate("Open Codex")
            await self.until(lambda: not self.ui.busy)
        self.assertEqual(self.ui.title, "Daemon result")
        await asyncio.sleep(.05)
        self.assertIn("status: running", self.rendered())

    async def test_changed_registry_error_is_shown_in_the_interface(self):
        self.register()
        self.ui.bridge.backend = Mock(side_effect=ValueError("Provider was removed"))
        self.activate("Daemon status")
        self.assertEqual(self.ui.screen, "error")
        await asyncio.sleep(0.05)
        self.assertIn("Provider was removed", self.rendered())

    async def test_message_width_uses_the_terminal_even_with_one_button(self):
        self.ui.text("Result", "A" * 80)
        await asyncio.sleep(0.05)
        self.assertIn("A" * 80, self.rendered())

    async def test_keyboard_navigation_scrolls_long_forms_to_the_focused_field(self):
        self.ui.new_provider()
        self.pipe.send_text("\t" * 8)
        await self.until(lambda: self.ui.app.layout.has_focus(self.ui.fields["model_catalog"]))
        await asyncio.sleep(0.05)
        self.assertIn("Existing model catalog", self.rendered())
        self.pipe.send_text("\x1b[Z" * 8)
        await self.until(lambda: self.ui.app.layout.has_focus(self.ui.fields["name"]))
        await asyncio.sleep(0.05)
        self.assertIn("Provider name", self.rendered())

    async def test_continue_fork_and_message_paging_keep_explicit_session_ids(self):
        self.register()
        self.ui.bridge.send_message = Mock(return_value={"thread_id": "original"})
        self.ui.message_session("original", "continue")
        self.fill(prompt="Continue task", model="new-model", effort="low")
        self.activate("Send instructions")
        await self.until(lambda: not self.ui.busy)
        self.ui.bridge.send_message.assert_called_once_with(
            "demo", "original", "Continue task", "new-model", "low"
        )
        self.ui.bridge.fork_session = Mock(return_value={"thread_id": "fork"})
        self.ui.message_session("original", "fork")
        self.fill(title="Branch", prompt="Different task")
        self.activate("Send instructions")
        await self.until(lambda: not self.ui.busy)
        self.ui.bridge.fork_session.assert_called_once_with(
            "demo", "original", "Different task", "Branch", None, None,
            session_type="worker", acceptance_criteria=None,
        )
        self.ui.bridge.read_messages = Mock(
            return_value={
                "messages": [{"type": "agentMessage", "text": "Page"}],
                "next_cursor": "older",
            }
        )
        self.ui.messages("original", "turn")
        await self.until(lambda: not self.ui.busy)
        self.activate("More messages")
        await self.until(lambda: not self.ui.busy)
        self.assertEqual(
            self.ui.bridge.read_messages.call_args.args, ("demo", "original", "turn", "older")
        )


class EntryTests(unittest.TestCase):
    def test_uninstall_runs_only_after_the_terminal_app_has_returned(self):
        events = []
        with patch("codexd_bridge.tui.sys.stdin.isatty", return_value=True), patch(
            "codexd_bridge.tui.sys.stdout.isatty", return_value=True), patch(
            "codexd_bridge.tui.BridgeUI") as ui, patch(
            "codexd_bridge.installation.uninstall_current", side_effect=lambda: events.append("uninstall")):
            def finished():
                events.append("terminal restored")
                return "uninstall"
            ui.return_value.app.run.side_effect = finished
            run_ui(Path("/unused"))
        self.assertEqual(events, ["terminal restored", "uninstall"])

    def test_non_terminal_ui_is_rejected_without_starting_app(self):
        with (
            patch("codexd_bridge.tui.sys.stdin.isatty", return_value=False),
            patch("codexd_bridge.tui.BridgeUI") as ui,
        ):
            with self.assertRaisesRegex(ValueError, "interactive terminal"):
                run_ui(Path("/unused"))
        ui.assert_not_called()

    def test_remote_control_characters_are_not_sent_to_terminal(self):
        self.assertNotIn("\x1b", clean("hello\x1b[2J"))


if __name__ == "__main__":
    unittest.main()
