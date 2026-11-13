"""Interactive administration of providers, daemons, models and sessions."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Dimension,
    DynamicContainer,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Checkbox, Frame, Label, RadioList, TextArea

from . import __version__
from .config import load_backends
from .delegation import OperatorGuard
from .sessions import Bridge

STAGES = {
    "configuration": "Prepare the provider configuration",
    "daemon": "Start or connect to the daemon",
    "verification": "Verify a real model response",
    "integration": "Connect the bridge and install its skill",
    "launcher": "Install the launcher and enable the provider",
    "ready": "Ready",
    "detach": "Disable this daemon's bridge integration",
    "stop": "Stop the selected daemon",
    "remove": "Remove its launcher and registration",
}
STYLE = Style.from_dict(
    {
        "title": "bold",
        "button.focused": "reverse bold",
        "radio-selected": "reverse",
        "error": "ansired",
    }
)


def clean(value) -> str:
    """Render remote text as plain text without terminal control sequences."""
    return "".join(c for c in str(value) if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


@dataclass
class Field:
    key: str
    label: str
    value: str = ""
    help: str = ""
    choices: tuple = ()
    multiline: bool = False


class BridgeUI:
    def __init__(self, registry: Path, *, input=None, output=None, bridge=None):
        self.registry = registry
        self.bridge = bridge or Bridge(registry, guard=OperatorGuard(registry))
        self.routes = {}
        self.selected = None
        self.provider_draft = {}
        self.setup_draft = {}
        self.connect_drafts = {}
        self.fields = {}
        self.buttons = {}
        self.screen = ""
        self.title = ""
        self.notice = ""
        self.busy = False
        self.close_when_done = False
        self.stage = ""
        self.completed = []
        self.submit = None
        self.back = self.home
        self.body = HSplit([Label("")])
        self.worker = None
        keys = KeyBindings()

        @keys.add("tab")
        def next_field(_event):
            self.move_focus(1)

        @keys.add("s-tab")
        def previous_field(_event):
            self.move_focus(-1)

        @keys.add("c-s", filter=Condition(lambda: not self.busy and self.submit is not None))
        def submit_form(_event):
            self.submit()

        @keys.add("c-q")
        @keys.add("c-c")
        def quit_app(_event):
            self.quit()

        @keys.add("escape", filter=Condition(lambda: not self.busy))
        def go_back(_event):
            self.back()

        @keys.add("c-n", filter=Condition(lambda: not self.busy))
        def new_provider(_event):
            self.new_provider()

        root = HSplit(
            [
                Label("  CODEXD BRIDGE " + __version__, style="class:title"),
                Label(lambda: "  " + self.title, style="class:subtitle"),
                Window(height=1),
                VSplit([Window(width=3), DynamicContainer(self.visible_body), Window(width=3)]),
                Label(lambda: "  " + self.notice, style="class:error"),
                Label(
                    lambda: (
                        "  "
                        + (
                            "Finishing the current operation..."
                            if self.close_when_done
                            else "Ctrl+Q exit  Tab move  Enter choose  Ctrl+S submit  Esc back"
                        )
                    ),
                    style="class:footer",
                    dont_extend_height=True,
                ),
            ]
        )
        self.app = Application(
            layout=Layout(root),
            key_bindings=keys,
            style=STYLE,
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )
        self.home()

    def visible_body(self):
        if not hasattr(self, "app"):
            return self.body
        size = self.app.output.get_size()
        if size.columns < 60 or size.rows < 18:
            return Label(
                "Resize the terminal to at least 60 columns by 18 rows.\nCtrl+Q exits; your form stays intact while resizing."
            )
        return self.body

    def quit(self):
        if self.busy:
            self.close_when_done = True
            self.notice = "The current operation will finish before the interface closes."
            self.app.invalidate()
        else:
            self.app.exit()

    def show(self, title, content, actions, *, focus=None, back=None, screen="view"):
        self.title, self.screen, self.notice = title, screen, ""
        self.submit = None
        self.back = back or self.home
        self.buttons = {
            label: Button(label, handler=lambda fn=handler: self.invoke(fn), width=23)
            for label, handler in actions
        }
        buttons = list(self.buttons.values())

        def action_rows():
            count = max(1, min(3, (self.app.output.get_size().columns - 4) // 25))
            return HSplit(
                [
                    VSplit([*buttons[i : i + count], Window()], padding=2)
                    for i in range(0, len(buttons), count)
                ]
            )

        self.body = HSplit(
            [
                HSplit([content], height=Dimension(min=1, weight=1)),
                Window(height=1),
                DynamicContainer(action_rows),
            ],
            padding=0,
        )
        if focus is not None or buttons:
            self.app.layout.focus(focus if focus is not None else buttons[0])
        self.app.layout.update_parents_relations()
        self.app.invalidate()

    def invoke(self, action):
        """Keep recoverable local errors inside the manager, including stale registry entries."""
        try:
            action()
        except (ValueError, OSError, RuntimeError) as error:
            self.text("Request failed", str(error), screen="error")

    def text(self, title, text, actions=(), *, back=None, screen="view"):
        area = TextArea(text=clean(text), read_only=True, scrollbar=True)
        self.show(
            title, area, list(actions) + [("Back", back or self.home)], back=back, screen=screen
        )

    def form(self, title, fields, submit, *, values=None, back=None, label="Continue", intro=""):
        values = values or {}
        self.fields = {}
        widgets = [Label(intro, style="class:subtitle"), Window(height=1)] if intro else []
        for spec in fields:
            value = values.get(spec.key, spec.value)
            if spec.choices:
                widget = RadioList(spec.choices, default=value, select_on_focus=True)
            else:
                widget = TextArea(
                    text=str(value),
                    multiline=spec.multiline,
                    height=5 if spec.multiline else 1,
                    style="class:input",
                    accept_handler=None if spec.multiline else self._advance,
                )
            self.fields[spec.key] = widget

            def title_text(spec=spec, widget=widget):
                return [
                    (
                        "class:title" if self.app.layout.has_focus(widget) else "class:subtitle",
                        spec.label,
                    )
                ]

            widgets.append(Frame(widget, title=title_text))
            if spec.help:
                widgets.append(Label(spec.help, style="class:subtitle"))
            widgets.append(Window(height=1))

        def accept():
            data = {
                key: (field.current_value if isinstance(field, RadioList) else field.text.strip())
                for key, field in self.fields.items()
            }
            try:
                submit(data)
            except (ValueError, OSError, RuntimeError) as error:
                self.notice = clean(error)
                self.app.invalidate()

        self.show(
            title,
            ScrollablePane(HSplit(widgets)),
            [(label, accept), ("Back", back or self.home)],
            focus=next(iter(self.fields.values())),
            back=back,
            screen="form",
        )
        self.submit = accept

    def _advance(self, _buffer):
        self.move_focus(1)
        return True

    def move_focus(self, step):
        # Include fields below the viewport, even before the next screen redraw.
        # ScrollablePane brings the newly focused field into view.
        layout = self.app.layout
        windows = list(layout.get_focusable_windows())
        if windows:
            current = (
                windows.index(layout.current_window) if layout.current_window in windows else -1
            )
            layout.focus(windows[(current + step) % len(windows)])

    def choose(self, title, choices, actions, *, back=None, description=""):
        if not choices:
            self.text(title, description or "No entries found.", back=back)
            return
        listing = RadioList(choices, select_on_focus=True)
        content = HSplit(
            [Label(description, style="class:subtitle"), Window(height=1), Frame(listing)]
        )
        wrapped = [(label, lambda fn=fn: fn(listing.current_value)) for label, fn in actions]
        self.show(title, content, wrapped + [("Back", back or self.home)], focus=listing, back=back)

    def work(self, title, function, success, *, retry=None, failure=None, back=None):
        """Keep the renderer responsive while bounded native operations run in a worker."""
        if self.busy:
            return
        self.busy, self.stage, self.completed = True, "", []
        self.show(
            title,
            Label(lambda: self.progress_text()),
            [("Close when done", self.quit)],
            back=back,
            screen="working",
        )
        loop = asyncio.get_running_loop()

        def progress(stage):
            loop.call_soon_threadsafe(self.set_stage, stage)

        async def run():
            try:
                result = await asyncio.to_thread(function, progress)
            except Exception as error:
                self.busy = False
                if self.close_when_done:
                    self.app.exit()
                elif failure:
                    failure(error)
                else:
                    actions = [("Retry", retry)] if retry else []
                    self.text(
                        "Request failed",
                        getattr(error, "detail", None) or str(error),
                        actions,
                        back=back,
                        screen="error",
                    )
            else:
                self.busy = False
                if self.close_when_done:
                    self.app.exit()
                else:
                    success(result)

        self.worker = self.app.create_background_task(run())

    def set_stage(self, stage):
        if self.stage and self.stage != "ready" and self.stage != stage:
            self.completed.append(self.stage)
        self.stage = stage
        self.app.invalidate()

    def progress_text(self):
        lines = ["Completed steps", ""]
        lines.extend("  [done] " + STAGES.get(s, s) for s in self.completed)
        lines += [
            "",
            "  [working] " + STAGES.get(self.stage, self.stage or "Contacting the backend"),
            "",
            "The interface remains available while this operation completes.",
        ]
        return "\n".join(lines)

    def home(self):
        try:
            self.routes = load_backends(self.registry) if os.path.lexists(self.registry) else {}
        except (ValueError, OSError) as error:
            self.text(
                "Cannot read provider registry",
                str(error),
                [("Choose registry", self.choose_registry), ("Diagnostics", self.diagnostics), ("Reload", self.home)],
                back=self.quit,
                screen="error",
            )
            return
        names = list(self.routes)
        if self.selected not in self.routes:
            self.selected = names[0] if names else None
        choices = [
            (
                name,
                f"{name}  /  {b.model_provider}  /  "
                + ("enabled" if b.enabled else "needs verification"),
            )
            for name, b in self.routes.items()
        ]
        listing = (
            RadioList(choices, default=self.selected, select_on_focus=True) if choices else None
        )

        def selected(fn):
            def invoke():
                if listing:
                    self.selected = listing.current_value
                    fn()
                else:
                    self.notice = "Create a provider or attach an existing Codex home first."

            return invoke

        actions = [
            ("Set up this Codex", self.setup_codex),
            ("New provider", self.new_provider),
            ("Attach existing", self.attach),
            ("All models", self.all_models),
        ]
        if listing:
            actions += [
                ("Daemon status", selected(lambda: self.daemon("status"))),
                ("Start daemon", selected(lambda: self.daemon("start"))),
                ("Stop daemon", selected(self.confirm_stop)),
                ("Remove daemon", selected(self.remove_daemon)),
                ("Connect bridge", selected(self.connect)),
                ("Install launcher", selected(self.install_launcher)),
                ("Models", selected(self.models)),
                ("Sessions", selected(self.sessions)),
                ("Open Codex", selected(self.open_codex)),
            ]
        actions += [("More tools", selected(self.more_tools) if listing else self.more_tools),
                    ("Refresh", self.home), ("Quit", self.quit)]
        content = HSplit(
            [
                Label(
                    "Choose a provider to manage its daemon, models and sessions.",
                    style="class:subtitle",
                ),
                Window(height=1),
                Frame(listing)
                if listing
                else Label("Welcome! Choose Set up this Codex to use your existing account.\n"
                           "Choose New provider to connect another model service."),
            ]
        )
        self.show(
            "Provider manager", content, actions, focus=listing, back=self.quit, screen="home"
        )

    def setup_codex(self):
        specs = [
            Field("home", "Codex directory", os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                  help="The default is your current Codex. Keep it to enable discovery in new sessions."),
            Field("name", "Name in the manager", "codex", help="Existing routes are reused."),
            Field("provider", "Provider", "", choices=(
                ("", "Use the provider already configured in this directory"),
                ("openai", "OpenAI / existing ChatGPT or OpenAI account")),
                  help="For standard Codex without a provider setting, select OpenAI."),
            Field("update", "Installed bridge guide", "keep", choices=(
                ("keep", "Install if missing; keep local changes"),
                ("replace", "Update to this version (replace local guide changes)"))),
        ]

        def install(data):
            self.setup_draft = data.copy()
            directory = Path(data["home"]).expanduser()
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError("Choose an existing absolute Codex directory.")
            if not data["name"]:
                raise ValueError("Enter a name for this Codex in the manager.")

            def operation(progress):
                from .integration import install_bridge
                from .providers import _resolve_executable

                return install_bridge(directory, _resolve_executable(None), self.registry,
                                      name=data["name"], provider=data["provider"] or None,
                                      update_skill=data["update"] == "replace", progress=progress)

            def ready(result):
                self.selected = result["backend"]
                self.text("Codex setup complete",
                          "The model responded, the bridge tools are available, and its guide is enabled.\n\n"
                          "New Codex sessions using this directory will discover the bridge automatically.\n"
                          "Existing sessions may need to reconnect to refresh their tools.\n\n"
                          + str(directory),
                          [("Open Codex", self.open_codex), ("Provider manager", self.home)],
                          screen="success")

            def failed(error):
                message = str(error)
                if "differing skill content" in message:
                    message = ("A different bridge guide is already installed. Select Update guide "
                               "to replace it with this version, or keep it and return to the manager.")

                    def update_guide():
                        self.setup_draft["update"] = "replace"
                        self.setup_codex()

                    actions = [("Update guide", update_guide)]
                else:
                    if "model_provider" in message and "explicit" in message:
                        message = ("This directory has no provider selection. Return to setup and "
                                   "choose OpenAI for standard Codex. For another service, "
                                   "use New provider in the manager.")
                    actions = []
                self.text("Setup incomplete", message, actions + [("Retry setup", self.setup_codex)],
                          screen="error")

            self.work("Setting up this Codex", operation, ready, failure=failed)

        self.form("Set up this Codex", specs, install, values=self.setup_draft, label="Set up",
                  intro="Connect your existing Codex with one step. A stopped backend will be started.\n"
                        "Setup checks the model and installs the tools and guide for future sessions.")

    def new_provider(self):
        from .providers import _validate_name
        from .provisioning import _base_url, _env_name

        specs = [
            Field("name", "Provider name", help="Creates a launcher named codex-NAME."),
            Field("base_url", "API endpoint", help="The provider's full Responses API base URL."),
            Field(
                "env_key",
                "API key environment variable",
                help="Variable name only, for example DEMO_API_KEY. Export it before opening this interface.",
            ),
            Field("model", "Model ID"),
            Field(
                "context_window",
                "Context limit (tokens)",
                help="Use the model's documented limit. Automatic compaction starts at 80%.",
            ),
            Field(
                "effort",
                "Reasoning effort (optional)",
                help="Leave blank for the provider default.",
            ),
            Field(
                "home",
                "New Codex home (optional)",
                help="Leave blank for an independent home in ~/.local/share/codexd-bridge/homes/.",
            ),
            Field("launcher_dir", "Launcher directory", str(Path.home() / ".local/bin")),
            Field(
                "model_catalog",
                "Existing model catalog (optional)",
                help="Otherwise only the selected model is declared.",
            ),
        ]

        def submit(data):
            self.provider_draft = data.copy()
            _validate_name(data["name"])
            _base_url(data["base_url"], "API endpoint")
            _env_name(data["env_key"], "Credential variable")
            if data["env_key"] not in os.environ:
                raise ValueError(
                    "The credential variable is not exported in this terminal. Reopen the manager after making it available."
                )
            if not data["model"]:
                raise ValueError("Enter a model ID.")
            try:
                context = int(data["context_window"].replace(",", ""))
            except ValueError:
                raise ValueError("Context limit must be a positive number of tokens.") from None
            if context < 2:
                raise ValueError("Context limit must be at least two tokens.")
            for key in ("home", "launcher_dir", "model_catalog"):
                if data[key] and not Path(data[key]).expanduser().is_absolute():
                    raise ValueError(f"{key.replace('_', ' ').title()} must be an absolute path.")
            self.create_provider(data, context)

        self.form(
            "Create a provider",
            specs,
            submit,
            values=self.provider_draft,
            label="Create provider",
            intro="Connection settings stay with an independent Codex home.\n"
                  "Creation includes one short model request to verify access.",
        )

    def create_provider(self, data, context):
        from .providers import _DATA_BASE, add_provider

        def create(progress):
            return add_provider(
                data["name"],
                data["base_url"],
                data["env_key"],
                data["model"],
                context,
                effort=data["effort"] or None,
                home=Path(data["home"]).expanduser() if data["home"] else None,
                launcher_dir=Path(data["launcher_dir"]).expanduser(),
                model_catalog=Path(data["model_catalog"]).expanduser()
                if data["model_catalog"]
                else None,
                registry_path=self.registry,
                progress=progress,
            )

        def failed(error):
            self.selected = data["name"]
            expected = (
                Path(data["home"]).expanduser()
                if data["home"]
                else (_DATA_BASE / "homes" / data["name"]).expanduser()
            )
            try:
                route = load_backends(self.registry).get(data["name"])
                retry = route is not None and route.home.resolve() == expected.resolve()
            except (ValueError, OSError):
                retry = False
            actions = (
                [
                    (
                        "Retry connection",
                        lambda: self.finish_provider_setup(data["name"], data["launcher_dir"]),
                    )
                ]
                if retry
                else []
            )
            self.text(
                "Provider setup incomplete",
                (getattr(error, "detail", None) or str(error))
                + (f"\n\nIf the connection settings are wrong, correct {route.home / 'config.toml'} "
                   "before retrying. The existing home is retained." if retry else ""),
                actions,
                back=self.new_provider,
                screen="error",
            )

        self.work(
            "Creating " + data["name"],
            create,
            self.provider_ready,
            failure=failed,
            back=self.new_provider,
        )

    def provider_ready(self, result):
        self.selected = result["backend"]
        self.provider_draft = {}
        self.text(
            "Provider ready",
            f"{result['backend']} is verified and enabled.\n\n"
            f"Home       {result['home']}\nLauncher   {result['launcher']}\n\n"
            "The temporary verification session has been deleted.",
            [
                ("Open sessions", self.sessions),
                ("Open Codex", self.open_codex),
                ("Providers", self.home),
            ],
            screen="success",
        )

    def open_codex(self, args=()):
        from .providers import run_provider

        if self.busy:
            return
        self.busy = True
        name = self.selected

        def native_exec(_file, argv, env):
            return subprocess.run(argv, env=env, check=False).returncode

        async def open_native():
            try:
                code = await run_in_terminal(
                    lambda: run_provider(
                        name, args, registry_path=self.registry, exec_runner=native_exec
                    ),
                    in_executor=True,
                )
            except (RuntimeError, ValueError, OSError) as error:
                self.text("Could not open Codex", str(error))
            else:
                if isinstance(code, dict):
                    self.text("Daemon result", "\n".join(f"{key}: {clean(value)}" for key, value in code.items()))
                else:
                    self.home()
                    if code:
                        self.notice = f"Codex exited with status {code}."
            finally:
                self.busy = False

        self.worker = self.app.create_background_task(open_native())

    def finish_provider_setup(self, name, directory):
        from .providers import connect_provider

        self.work(
            "Finishing setup / " + name,
            lambda progress: connect_provider(
                name,
                registry_path=self.registry,
                launcher_dir=Path(directory).expanduser(),
                progress=progress,
            ),
            self.provider_ready,
            retry=lambda: self.finish_provider_setup(name, directory),
        )

    def install_launcher(self, name=None, directory=None):
        from .providers import install_provider_launcher

        name = name or self.selected
        if directory is None:
            self.form("Install provider launcher",
                      [Field("directory", "Launcher directory", str(Path.home() / ".local/bin"))],
                      lambda data: self.install_launcher(name, data["directory"]), label="Install launcher")
            return
        self.work("Installing launcher / " + name,
                  lambda _p: install_provider_launcher(name, registry_path=self.registry,
                      launcher_dir=Path(directory).expanduser()),
                  lambda result: self.text("Launcher installed", result["launcher"],
                      [("Open Codex", self.open_codex)], screen="success"),
                  retry=lambda: self.install_launcher(name, directory))

    def attach(self):
        def save(data):
            from .provisioning import attach_daemon

            self.work(
                "Attaching existing home",
                lambda _p: attach_daemon(
                    data["name"], Path(data["home"]).expanduser(), registry_path=self.registry
                ),
                lambda result: self.attached(result),
            )

        self.form(
            "Attach an existing Codex home",
            [
                Field("name", "Provider alias"),
                Field(
                    "home",
                    "Existing Codex home",
                    help="Its config.toml must declare the exact model_provider.",
                ),
            ],
            save,
            label="Attach",
            intro="The home and its credentials remain in place. The route starts disabled.",
        )

    def attached(self, backend):
        self.selected = backend.name
        self.text(
            "Provider attached",
            f"{backend.name}\n{backend.home}\n\nConnect the bridge to verify and enable this provider.",
            [("Connect bridge", self.connect)],
        )

    def daemon(self, action, *, expected_backend=None):
        from .provisioning import daemon_action

        target = self.bridge.backend(self.selected)
        if expected_backend is not None and target != expected_backend:
            raise ValueError("The backend changed after selection; refresh and review the action again")
        self.work(
            action.title() + " daemon: " + target.name,
            lambda _p: daemon_action(target, action),
            lambda result: self.text(
                "Daemon " + action,
                "\n".join(f"{key}: {clean(value)}" for key, value in result.items()),
            ),
        )

    def confirm_stop(self):
        target = self.bridge.backend(self.selected)
        self.text(
            "Stop " + target.name + "?",
            str(target.home) + "\n\nStopping this daemon can interrupt its active sessions.",
            [("Stop daemon", lambda: self.daemon("stop", expected_backend=target))],
        )

    def remove_daemon(self):
        from .removal import remove_daemon

        target = self.bridge.backend(self.selected)

        def submit(data):
            purge = data["data"] == "delete"
            if purge and data["confirmation"] != target.name:
                raise ValueError("Type the exact backend name to confirm deleting its data.")

            def removed(result):
                self.selected = None
                details = ("Daemon removed. Its data was deleted." if result["data_deleted"] else
                           "Daemon removed. Configuration, credentials and sessions were retained.\n"
                           "Use Attach existing to connect it again.")
                if result["kept_files"]:
                    details += "\n\nKept modified or unrelated files:\n" + "\n".join(result["kept_files"])
                self.text("Daemon removed", details, [("Provider manager", self.home)], screen="success")

            self.work("Removing " + target.name,
                      lambda progress: remove_daemon(target.name, registry_path=self.registry,
                          purge=purge, expected_backend=target,
                          launcher_dir=Path(data["launcher_dir"]).expanduser() if data["launcher_dir"] else None,
                          progress=progress), removed, back=self.home)

        self.form("Remove daemon / " + target.name, [
            Field("data", "Daemon data", "keep", choices=(
                ("keep", "Keep configuration and sessions"),
                ("delete", "Delete configuration, credentials and sessions"))),
            Field("confirmation", "Type backend name to confirm data deletion"),
            Field("launcher_dir", "Legacy custom launcher directory (optional)"),
        ], submit, label="Remove daemon", intro=f"{target.name}\n{target.home}\n\n"
                  "This stops the selected daemon, including any active tasks, and removes its registration.\n"
                  "Other daemons and the bridge application remain available.")

    def more_tools(self):
        actions = [("Create daemon", self.create_daemon), ("Choose registry", self.choose_registry),
                   ("Diagnostics", self.diagnostics), ("MCP connection", self.mcp_connection),
                   ("Uninstall bridge", self.confirm_uninstall)]
        if self.selected:
            actions += [("Verify model ID", self.verify_model_id), ("Codex arguments", self.codex_arguments)]
        self.text("More tools", "Additional setup, native Codex and application management.\n\n"
                  + "Current registry: " + str(self.registry), actions)

    def diagnostics(self):
        from .diagnostics import diagnose

        def display(result):
            lines = ["Codex Daemon Bridge " + result["version"], ""]
            for check in result["checks"]:
                lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
                if check["advice"]:
                    lines.append("  " + check["advice"])
            self.text("Installation diagnostics", "\n".join(lines),
                      [("Set up this Codex", self.setup_codex), ("Choose registry", self.choose_registry)],
                      back=self.more_tools)

        self.work("Checking installation", lambda _p: diagnose(self.registry), display, back=self.more_tools)

    def create_daemon(self):
        from .provisioning import create_daemon

        def create(data):
            self.work("Creating daemon", lambda _p: create_daemon(
                data["name"], Path(data["home"]).expanduser(), data["provider"], data["model"],
                registry_path=self.registry, effort=data["effort"] or None,
                template_home=Path(data["template"]).expanduser() if data["template"] else None),
                self.attached, back=self.more_tools)

        self.form("Create daemon configuration", [Field("name", "Provider alias"),
            Field("home", "New absolute Codex directory"), Field("provider", "Exact provider ID"),
            Field("model", "Model ID"), Field("effort", "Reasoning effort (optional)"),
            Field("template", "Template Codex directory (optional)")], create,
            label="Create daemon", back=self.more_tools,
            intro="Create an independent configuration without starting a daemon or making a model request.\n"
                  "For an external provider, select a template with its connection settings.")

    def choose_registry(self):
        def choose(data):
            registry = Path(data["registry"]).expanduser()
            if not registry.is_absolute():
                raise ValueError("Choose an absolute registry path.")
            if registry.exists():
                load_backends(registry)
            self.registry = registry
            self.bridge = Bridge(registry, guard=OperatorGuard(registry))
            self.selected = None
            self.home()

        self.form("Choose registry", [Field("registry", "Registry path", str(self.registry))],
                  choose, back=self.more_tools, label="Open registry")

    def mcp_connection(self):
        from .providers import _resolve_executable

        command = shlex.join([str(_resolve_executable(None)), "--config", str(self.registry), "serve"])
        self.text("MCP connection", "Use Set up this Codex or Connect bridge for automatic registration.\n\n"
                  "For another MCP client, use this stdio server command:\n\n" + command,
                  back=self.more_tools)

    def verify_model_id(self):
        from .probe import check_model

        target = self.bridge.backend(self.selected)
        self.form("Verify model / " + target.name, [Field("model", "Model ID", target.default_model or ""),
                  Field("effort", "Reasoning effort (optional)")],
                  lambda data: self.work("Verifying model", lambda _p: check_model(
                      target, model=data["model"] or None, effort=data["effort"] or None),
                      lambda _r: self.text("Model verified", "A real response was verified; the temporary session was deleted.")),
                  label="Run verification", back=self.more_tools, intro="This sends one short model request.")

    def codex_arguments(self):
        self.form("Native Codex / " + self.selected, [Field("args", "Codex arguments (optional)")],
                  lambda data: self.open_codex(shlex.split(data["args"])), label="Open Codex",
                  back=self.more_tools, intro="Arguments go to the selected provider's Codex CLI.\n"
                  "Provider and remote endpoint overrides remain restricted.")

    def confirm_uninstall(self):
        self.text("Uninstall Codex Daemon Bridge?",
                  "Remove the bridge application, its commands and owned integrations.\n"
                  "Independent daemons keep running; their configuration and sessions are retained.\n\n"
                  "This action closes the manager. For a pip installation, use its package manager.",
                  [("Uninstall bridge", lambda: self.app.exit(result="uninstall"))], back=self.more_tools)

    def connect(self):
        key = (str(self.registry.resolve()), self.selected)
        draft = self.connect_drafts.get(key, {})
        update = Checkbox("Update the installed bridge skill", checked=draft.get("update_skill", False))
        unattended = Checkbox(
            "Allow unattended delegation (four session write tools)", checked=draft.get("unattended", False)
        )
        self.fields = {"update_skill": update, "unattended": unattended}
        content = HSplit(
            [
                Label("Verify model access and connect MCP. A stopped daemon will be started."),
                Window(height=1),
                update,
                unattended,
                Window(height=1),
                Label(
                    "Existing approval settings are preserved unless you select unattended delegation.",
                    style="class:subtitle",
                ),
            ]
        )

        def connect():
            from .integration import connect_backend
            from .providers import _ensure_running, _resolve_executable

            target = self.bridge.backend(self.selected)
            self.connect_drafts[key] = {"update_skill": update.checked, "unattended": unattended.checked}

            def operation(progress):
                progress("daemon")
                _ensure_running(target, 45)
                return connect_backend(
                    target,
                    _resolve_executable(None),
                    self.registry,
                    update_skill=update.checked,
                    allow_session_writes=unattended.checked,
                    progress=progress,
                )

            self.work(
                "Connecting " + target.name,
                operation,
                lambda _r: self.text(
                    "Bridge connected",
                    f"{target.name} is verified and enabled.\n"
                    "New Codex sessions using this directory can discover the bridge automatically.",
                    [("Install launcher", self.install_launcher), ("Sessions", self.sessions)],
                ),
                retry=self.connect,
            )

        self.show(
            "Connect " + str(self.selected),
            content,
            [
                ("Verify and connect", connect),
                ("Install launcher", self.install_launcher),
                ("Back", self.home),
            ],
        )

    def models(self, cursor=None):
        self.work(
            "Loading models",
            lambda _p: self.bridge.list_models(self.selected, cursor, 30),
            self.model_list,
            retry=lambda: self.models(cursor),
        )

    def model_list(self, result):
        models = result["models"]

        def details(index):
            model = models[index]
            efforts = [r["reasoningEffort"] for r in model.get("supportedReasoningEfforts") or []]
            text = f"{model['model']}\n\n{model.get('description') or ''}\n\nReasoning: {', '.join(efforts) or 'provider default'}\n\nListed by the daemon; access still needs a real inference check."
            self.text(
                "Model details",
                text,
                [
                    ("Verify model", lambda: self.verify_model(model)),
                    ("New session", lambda: self.new_session(model)),
                ],
                back=self.models,
            )

        actions = [("Details", details)]
        if result.get("next_cursor"):
            actions.append(("Next page", lambda _i: self.models(result["next_cursor"])))
        self.choose(
            "Models / " + self.selected,
            [(i, clean(m["model"])) for i, m in enumerate(models)],
            actions,
            description="Available model IDs come from this daemon's current catalog.",
        )

    def all_models(self):
        def display(result):
            lines = []
            for provider in result["providers"]:
                lines += [
                    provider["backend"],
                    "  "
                    + (
                        ", ".join(m["model"] for m in provider.get("models", []))
                        if provider.get("reachable")
                        else provider.get("error", "Unavailable")
                    ),
                    "",
                ]
            self.text("Models across providers", "\n".join(lines), [("Refresh", self.all_models)])

        self.work(
            "Discovering provider models", lambda _p: self.bridge.list_provider_models(), display
        )

    def verify_model(self, model):
        from .probe import check_model

        target = self.bridge.backend(self.selected)
        choices = (("", "Provider default"),) + tuple(
            (r["reasoningEffort"], r["reasoningEffort"])
            for r in model.get("supportedReasoningEfforts") or []
        )
        default_effort = model.get("defaultReasoningEffort") or ""
        if default_effort not in {value for value, _label in choices}:
            choices += ((default_effort, default_effort),)

        def verify(data):
            self.work(
                "Verifying " + model["model"],
                lambda _p: check_model(target, model=model["model"], effort=data["effort"] or None),
                lambda _r: self.text(
                    "Model verified",
                    "A real response was verified and the test session was deleted.",
                    back=self.models,
                ),
                back=self.models,
            )

        self.form(
            "Verify model access",
            [Field("effort", "Reasoning effort", default_effort, choices=choices)],
            verify,
            back=self.models,
            label="Run verification",
            intro="This sends one short model request.",
        )

    def sessions(self, cursor=None):
        def display(result):
            sessions = result["sessions"]
            actions = [
                ("Open session", self.session),
                ("New session", lambda _id: self.new_session()),
            ]
            if result.get("next_cursor"):
                actions.append(("Next page", lambda _id: self.sessions(result["next_cursor"])))
            if not sessions:
                self.text(
                    "Sessions / " + self.selected,
                    "No sessions yet.",
                    [("New session", self.new_session)],
                )
                return
            self.choose(
                "Sessions / " + self.selected,
                [
                    (
                        s["id"],
                        clean(
                            f"{s.get('name') or s['id']}  [{s.get('status', {}).get('type', 'unknown')}]"
                        )[:150],
                    )
                    for s in sessions
                ],
                actions,
            )

        self.work(
            "Loading sessions",
            lambda _p: self.bridge.list_sessions(self.selected, cursor),
            display,
            retry=lambda: self.sessions(cursor),
        )

    def new_session(self, model=None):
        specs = [
            Field("title", "Session title"),
            Field("cwd", "Working directory", str(Path.cwd())),
            Field("model", "Model (optional)", (model or {}).get("model", "")),
            Field(
                "effort",
                "Reasoning effort (optional)",
                (model or {}).get("defaultReasoningEffort") or "",
            ),
            Field(
                "sandbox",
                "File access",
                "read-only",
                choices=(("read-only", "Read only"), ("workspace-write", "Workspace write")),
            ),
            Field("session_type", "Session type", "worker"),
            Field("prompt", "Task", multiline=True),
            Field("acceptance_criteria", "Acceptance criteria (optional)", multiline=True),
        ]

        def create(data):
            if not data["title"] or not data["prompt"]:
                raise ValueError("Enter a title and a task.")
            if not Path(data["cwd"]).is_absolute() or not Path(data["cwd"]).is_dir():
                raise ValueError("Working directory must be an existing absolute path.")
            self.work(
                "Starting session",
                lambda _p: self.bridge.create_session(
                    self.selected,
                    data["prompt"],
                    data["title"],
                    data["cwd"],
                    data["model"] or None,
                    data["effort"] or None,
                    data["sandbox"],
                    session_type=data["session_type"],
                    acceptance_criteria=data["acceptance_criteria"] or None,
                ),
                self.accepted,
                back=self.sessions,
            )

        self.form(
            "New session / " + self.selected, specs, create, back=self.sessions, label="Start task"
        )

    def accepted(self, result):
        self.text(
            "Task accepted",
            "The request was accepted. Open the session to follow its result.\n\n"
            + result["thread_id"],
            [("Open session", lambda: self.session(result["thread_id"]))],
            back=self.sessions,
        )

    def session(self, thread_id, cursor=None):
        self.work(
            "Reading session",
            lambda _p: self.bridge.read_session(self.selected, thread_id, 3, 16000, cursor),
            lambda result: self.session_view(result),
            back=self.sessions,
        )

    def session_view(self, result):
        session = result["session"]
        thread_id = session["id"]
        lines = [
            session.get("name") or thread_id,
            f"State: {session.get('status', {}).get('type', 'unknown')}",
            "",
        ]
        assignment = session.get("delegation")
        if assignment:
            parent = assignment.get("parent")
            source = assignment.get("fork_source")
            lines += [f"Assignment: {assignment['delegation_id']}",
                      f"Type: {assignment['session_type']} / Depth: {assignment['depth']}"]
            if parent:
                lines.append(f"Parent: {parent['thread_id']} ({parent['home']})")
            if source and source != parent:
                lines.append(f"History source: {source['thread_id']} ({source['home']})")
            lines += ["Task: " + assignment["task_requirements"],
                      "Acceptance criteria: " + assignment["acceptance_criteria"], ""]
        for turn in reversed(result["turns"]):
            lines += [f"Turn {turn['id']} / {turn['status']}"]
            if turn.get("error"):
                lines.append(turn["error"])
            for message in turn["messages"]:
                lines += [message["type"], message["text"], ""]
        active = next((t for t in result["turns"] if t["status"] == "inProgress"), None)
        actions = [("Refresh", lambda: self.session(thread_id))]
        if active:
            actions.append(
                ("Add instructions", lambda: self.message_session(thread_id, "steer", active["id"]))
            )
        elif session.get("status", {}).get("type") != "active":
            actions.append(("Continue", lambda: self.message_session(thread_id, "continue")))
        actions += [
            ("Fork session", lambda: self.message_session(thread_id, "fork")),
            (
                "Read a turn",
                lambda: self.choose(
                    "Choose a turn",
                    [(t["id"], f"{t['id']} / {t['status']}") for t in result["turns"]],
                    [("Read messages", lambda turn: self.messages(thread_id, turn))],
                    back=lambda: self.session(thread_id),
                ),
            ),
        ]
        if result.get("next_cursor"):
            actions.append(("Older turns", lambda: self.session(thread_id, result["next_cursor"])))
        self.text("Session / " + self.selected, "\n".join(lines), actions, back=self.sessions)

    def messages(self, thread, turn, cursor=None):
        def display(result):
            lines = [m["type"] + (" (continued)" if m.get("text_offset") else "")
                     + "\n" + m["text"] + ("\n[Continues on the next page]" if m.get("truncated") else "")
                     for m in result["messages"]]
            actions = (
                [("More messages", lambda: self.messages(thread, turn, result["next_cursor"]))]
                if result.get("next_cursor")
                else []
            )
            self.text(
                "Turn messages",
                "\n\n".join(lines) or "No visible messages.",
                actions,
                back=lambda: self.session(thread),
            )

        self.work(
            "Reading messages",
            lambda _p: self.bridge.read_messages(
                self.selected, thread, turn, cursor, text_limit=16000
            ),
            display,
            back=lambda: self.session(thread),
        )

    def message_session(self, thread, mode, turn=None):
        fields = [Field("title", "Fork title"), Field("session_type", "Session type", "worker"),
                  Field("acceptance_criteria", "Acceptance criteria (optional)", multiline=True)
                  ] if mode == "fork" else []
        if mode != "steer":
            fields += [
                Field("model", "Model (blank keeps the current selection)"),
                Field("effort", "Reasoning effort (optional)"),
            ]
        fields.append(Field("prompt", "Instructions", multiline=True))

        def send(data):
            if not data["prompt"] or (mode == "fork" and not data["title"]):
                raise ValueError("Enter instructions and a title for a fork.")

            def operation(_p):
                if mode == "steer":
                    return self.bridge.steer_session(self.selected, thread, turn, data["prompt"])
                if mode == "fork":
                    return self.bridge.fork_session(
                        self.selected,
                        thread,
                        data["prompt"],
                        data["title"],
                        data["model"] or None,
                        data["effort"] or None,
                        session_type=data["session_type"],
                        acceptance_criteria=data["acceptance_criteria"] or None,
                    )
                return self.bridge.send_message(
                    self.selected,
                    thread,
                    data["prompt"],
                    data["model"] or None,
                    data["effort"] or None,
                )

            self.work(
                "Sending instructions", operation, self.accepted, back=lambda: self.session(thread)
            )

        self.form(
            mode.title() + " session",
            fields,
            send,
            back=lambda: self.session(thread),
            label="Send instructions",
        )


def run_ui(registry: Path):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "The TUI needs an interactive terminal. Run codexd-bridge from a terminal window."
        )
    result = BridgeUI(registry).app.run()
    if result == "uninstall":
        from .installation import uninstall_current

        uninstall_current()
