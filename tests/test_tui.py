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

    async def test_missing_credential_reference_blocks_creation(self):
        self.setup_provider()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("codexd_bridge.providers.add_provider") as add,
        ):
            self.activate("Continue")
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
            await self.until(lambda: self.ui.screen == "review")
            self.assertNotIn("SECRET-SENTINEL", self.rendered())
            self.activate("Create provider")
            await self.until(lambda: self.ui.screen == "success")
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
            self.activate("Continue")
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

    async def test_new_session_keeps_provider_and_chosen_file_access(self):
        self.register()
        self.ui.new_session({"model": "selected-model", "defaultReasoningEffort": "low"})
        self.fill(title="Task", cwd=str(self.root), prompt="Review code")
        self.ui.bridge.create_session = Mock(return_value={"thread_id": "created"})
        self.activate("Start task")
        await self.until(lambda: not self.ui.busy)
        self.ui.bridge.create_session.assert_called_once_with(
            "demo", "Review code", "Task", str(self.root), "selected-model", "low", "read-only"
        )

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
        self.assertEqual(run.call_args.args, ("demo",))
        self.assertEqual(run.call_args.kwargs["registry_path"], self.registry)
        self.assertEqual(self.ui.screen, "home")

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
        self.pipe.send_text("\t" * 9)
        await self.until(lambda: self.ui.app.layout.has_focus(self.ui.fields["model_catalog"]))
        await asyncio.sleep(0.05)
        self.assertIn("Existing model catalog", self.rendered())
        self.pipe.send_text("\x1b[Z" * 9)
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
            "demo", "original", "Different task", "Branch", None, None
        )
        self.ui.bridge.read_messages = Mock(
            return_value={
                "messages": [{"type": "agentMessage", "text": "Page"}],
                "next_cursor": "older",
            }
        )
        self.ui.messages("original", "turn")
        await self.until(lambda: not self.ui.busy)
        self.activate("Older messages")
        await self.until(lambda: not self.ui.busy)
        self.assertEqual(
            self.ui.bridge.read_messages.call_args.args, ("demo", "original", "turn", "older")
        )


class EntryTests(unittest.TestCase):
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
