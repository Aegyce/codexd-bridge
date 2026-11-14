"""Interactive administration of providers, daemons, models and sessions."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    DynamicContainer,
    Float,
    FloatContainer,
    HorizontalAlign,
    HSplit,
    Layout,
    ScrollablePane,
    VerticalAlign,
    VSplit,
    Window,
    WindowAlign,
    to_container,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Checkbox, Frame, Label, RadioList, TextArea

from . import __version__
from .config import load_backends
from .delegation import OperatorGuard
from .sessions import Bridge
from .tui_i18n import HELP, diagnostic_text, load_language, save_language, translate, wrap_text

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
        "": "bg:#11151c #e5e9f0",
        "title": "bold",
        "header": "bold",
        "muted": "#8894a4",
        "key": "bold #d8e0ea",
        "separator": "#434d5c",
        "tab": "#a8b3c2",
        "tab.active": "reverse bold",
        "frame.label": "bold #e5e9f0",
        "focused": "reverse bold",
        "button.focused": "bg:#11151c #e5e9f0 reverse bold",
        "radio-selected": "reverse",
        "error": "ansired",
        "card.focused": "reverse",
        "overview-panel": "bg:#171e27 #697687",
        "overview.value": "#e5e9f0",
        "overview.field": "#a8b3c2",
        "overview.enabled": "#adcab7",
        "overview.disabled": "#d4b58d",
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
    help: str | Callable[[], str] = ""
    choices: tuple = ()
    multiline: bool = False


class BridgeUI:
    def __init__(self, registry: Path, *, input=None, output=None, bridge=None, language=None):
        self.registry = registry
        self.language = language or load_language(registry)
        self.bridge = bridge or Bridge(registry, guard=OperatorGuard(registry))
        self.routes = {}
        self.selected = None
        self.provider_draft = {}
        self.setup_draft = {}
        self.connect_drafts = {}
        self.fields = {}
        self.buttons = {}
        self.action_groups = []
        self.group_focus = []
        self.menu = self.home
        self.screen = ""
        self.title = ""
        self.notice = ""
        self.busy = False
        self.close_when_done = False
        self.stage = ""
        self.completed = []
        self.submit = None
        self.back = self.home
        self.body = HSplit([self.label("")])
        self.worker = None
        self.help_open = False
        self.help_focus = None
        self.refresh_text = None
        self.help_button = Button("Close help", handler=self.toggle_help, width=20)
        self.help_area = TextArea(read_only=True, scrollbar=True)
        self.help_body = HSplit([
            self.frame(self.help_area, title="Keyboard shortcuts"),
            Window(height=1), VSplit([self.help_button], align=HorizontalAlign.CENTER),
        ], align=VerticalAlign.CENTER)
        keys = KeyBindings()

        menu_focus = Condition(lambda: not self.help_open and not self.busy and any(
            self.app.layout.has_focus(button) for button in self.buttons.values()))
        groups_active = Condition(lambda: bool(self.action_groups) and menu_focus())

        @keys.add("f2")
        def language_key(_event):
            self.toggle_language()

        @keys.add("f1")
        @keys.add("?", filter=Condition(lambda: not self.editing()))
        def help_key(_event):
            self.toggle_help()

        @keys.add("h", filter=groups_active)
        @keys.add("left", filter=groups_active)
        def previous_area(_event):
            self.move_focus(-1)

        @keys.add("l", filter=groups_active)
        @keys.add("right", filter=groups_active)
        def next_area(_event):
            self.move_focus(1)

        @keys.add("j", filter=menu_focus)
        @keys.add("down", filter=menu_focus)
        def down(_event):
            self.move_action(1) if self.action_groups else self.move_focus(1)

        @keys.add("k", filter=menu_focus)
        @keys.add("up", filter=menu_focus)
        def up(_event):
            self.move_action(-1) if self.action_groups else self.move_focus(-1)

        @keys.add("home", filter=menu_focus)
        @keys.add("end", filter=menu_focus)
        def edge(event):
            buttons = self.current_actions()
            self.app.layout.focus(buttons[0 if event.key_sequence[0].key == "home" else -1])

        for number in range(1, 10):
            @keys.add(str(number), filter=groups_active)
            def select_number(event, number=number):
                if number <= len(self.action_groups):
                    self.focus_group(number - 1)

        for number in range(1, 10):
            @keys.add("escape", str(number))
            def focus_footer_action(_event, number=number):
                # Consume the whole Alt sequence even in help/busy states: if
                # disabled by a filter, Escape could close help and insert a digit.
                if self.help_open or self.busy:
                    return
                buttons = self.current_actions()
                if number <= len(buttons):
                    self.app.layout.focus(buttons[number - 1])

        @keys.add("tab")
        def next_field(_event):
            self.move_focus(1)

        @keys.add("s-tab")
        def previous_field(_event):
            self.move_focus(-1)

        @keys.add("c-s", filter=Condition(lambda: not self.busy and not self.help_open and self.submit is not None))
        def submit_form(_event):
            self.submit()

        @keys.add("c-q")
        @keys.add("c-c")
        def quit_app(_event):
            self.quit()

        @keys.add("escape", filter=Condition(lambda: self.help_open or not self.busy))
        def go_back(_event):
            self.toggle_help() if self.help_open else self.back()

        @keys.add("c-n", filter=Condition(lambda: not self.busy and not self.help_open))
        def new_provider(_event):
            self.new_provider()

        page = HSplit(
            [
                self.label([("class:header", "CODEXD BRIDGE"), ("class:muted", "  " + __version__)], align=WindowAlign.CENTER),
                self.label(lambda: self.t("Keyboard shortcuts") if self.help_open else self.display_title(), style="class:title", align=WindowAlign.CENTER),
                Window(char="─", height=1, style="class:separator"),
                VSplit([Window(width=3), DynamicContainer(self.visible_body), Window(width=3)]),
                self.label(lambda: self.notice() if callable(self.notice) else self.t(self.notice),
                           style="class:error", align=WindowAlign.CENTER),
                self.label(self.footer, wrap=False,
                    style="class:footer",
                    align=WindowAlign.CENTER,
                    dont_extend_height=True,
                ),
            ],
            width=lambda: min(100, self.app.output.get_size().columns),
        )
        root = FloatContainer(VSplit([page], align=HorizontalAlign.CENTER), floats=[
            Float(content=ConditionalContainer(self.help_body, filter=Condition(lambda: self.help_open)),
                  width=lambda: min(86, self.app.output.get_size().columns - 8),
                  height=lambda: min(19, self.app.output.get_size().rows - 4)),
        ])
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

    def t(self, source, **values):
        return translate(self.language, source, **values)

    def label(self, text, *, wrap=True, **kwargs):
        def rendered():
            value = text() if callable(text) else self.t(text) if isinstance(text, str) else text
            return wrap_text(value, max(10, self.content_width() - 4)) if wrap and isinstance(value, str) else value
        return Label(rendered, **kwargs)

    def frame(self, body, *, title="", **kwargs):
        caption = title if not title or callable(title) else lambda: self.t(title)
        return Frame(body, title=caption, **kwargs)

    def display_title(self):
        translated = self.t(self.title)
        if translated != self.title:
            return translated
        if " / " in self.title:
            prefix, value = self.title.split(" / ", 1)
            return self.t(prefix) + " · " + value
        for prefix in ("Creating ", "Connecting ", "Connect ", "Removing ", "Verifying ",
                       "Start daemon: ", "Stop daemon: ", "Status daemon: "):
            if self.title.startswith(prefix):
                return self.t(prefix.rstrip()) + " " + self.title[len(prefix):]
        if self.title.startswith("Stop ") and self.title.endswith("?"):
            return self.t("Stop {name}?", name=self.title[5:-1])
        return self.t(self.title)

    def footer(self):
        if self.close_when_done:
            return self.t("Finishing the current operation...")
        zh = self.language == "zh-CN"
        language = ("F2", "English" if zh else "中文")
        quit_key = ("Ctrl+Q", "退出" if zh else "quit")
        if self.help_open:
            rows = [[("Esc", "关闭帮助" if zh else "close help"),
                     ("Tab", "切换" if zh else "move"), language, quit_key]]
        elif not self.action_groups:
            rows = [[("Alt+1–9", "定位" if zh else "focus"), ("Enter", "确定" if zh else "run"),
                     ("F1", "帮助" if zh else "help"), language, quit_key]]
        elif self.screen == "home":
            rows = [[("Tab/1–2", "切换" if zh else "move"), ("Enter", "确定" if zh else "select"),
                     ("?", "帮助" if zh else "help"), language, quit_key]]
        else:
            rows = [[(f"Tab / 1–{len(self.action_groups)}", "切区" if zh else "area"),
                     ("j/k", "选择" if zh else "select"), ("Enter", "确定" if zh else "run"),
                     ("Esc", "返回" if zh else "back")],
                    [("Alt+1–9", "定位操作" if zh else "action"), ("?", "帮助" if zh else "help"),
                     language, quit_key]]
        fragments = []
        for index, row in enumerate(rows):
            if index:
                fragments.append(("", "\n"))
            for column, (key, description) in enumerate(row):
                if column:
                    fragments.append(("", "  "))
                fragments += [("class:key", key), ("class:muted", " " + description)]
        return fragments

    def editing(self):
        control = self.app.layout.current_control
        return bool(getattr(control, "buffer", None) and not control.buffer.read_only())

    def current_actions(self):
        if self.screen == "home" or not self.action_groups:
            return list(self.buttons.values())
        return next((group for group in self.action_groups if any(
            self.app.layout.has_focus(button) for button in group)), [])

    def update_captions(self):
        for label, button in self.buttons.items():
            group = (list(self.buttons.values()) if self.screen == "home" else
                     next((g for g in self.action_groups if button in g), []))
            number = (group or list(self.buttons.values())).index(button) + 1
            prefix = f"{number}  " if self.screen != "home" else ""
            button.text = prefix + self.t(label)
        self.help_button.text = self.t("Close help")
        from prompt_toolkit.document import Document
        self.help_area.buffer.set_document(Document(self.help_text()), bypass_readonly=True)

    def toggle_language(self):
        self.language = "en" if self.language == "zh-CN" else "zh-CN"
        self.update_captions()
        if self.refresh_text:
            self.refresh_text()
        try:
            save_language(self.registry, self.language)
        except OSError:
            self.notice = "Language changed; could not save the preference."
        self.app.invalidate()

    def help_text(self):
        index = 2 if self.language == "zh-CN" else 1
        return "\n".join(key + " " * (18 - get_cwidth(key)) + row[index]
                         for row in HELP for key in [row[0]])

    def toggle_help(self):
        if self.help_open:
            self.help_open = False
            self.app.layout.focus(self.help_focus)
        else:
            self.help_focus = self.app.layout.current_window
            self.help_open = True
            self.app.layout.focus(self.help_area)
        self.app.layout.update_parents_relations()
        self.app.invalidate()

    def visible_body(self):
        if not hasattr(self, "app"):
            return self.body
        size = self.app.output.get_size()
        if size.columns < 60 or size.rows < 18:
            return self.label(
                "Resize the terminal to at least 60 columns by 18 rows.\nCtrl+Q exits; your form stays intact while resizing."
            )
        return self.body

    def content_width(self):
        return min(100, self.app.output.get_size().columns) - 6

    def quit(self):
        if self.busy:
            self.close_when_done = True
            self.notice = "The current operation will finish before the interface closes."
            self.app.invalidate()
        else:
            self.app.exit()

    def show(self, title, content, actions, *, focus=None, back=None, screen="view",
             action_groups=()):
        self.title, self.screen, self.notice = title, screen, ""
        self.submit = None
        self.refresh_text = None
        self.back = back or self.menu
        self.buttons = {
            label: Button(label, handler=lambda fn=handler: self.invoke(fn), width=23,
                          left_symbol="", right_symbol="")
            for label, handler in actions
        }
        buttons = list(self.buttons.values())
        self.action_groups = []
        self.group_focus = []

        def action_rows():
            count = max(1, min(3, (self.content_width() - 2) // 25))
            return HSplit(
                [
                    VSplit(buttons[i : i + count], padding=2, align=HorizontalAlign.CENTER)
                    for i in range(0, len(buttons), count)
                ]
            )

        if action_groups:
            groups = [[self.buttons[label] for label in labels] for _, labels in action_groups]
            self.action_groups = groups
            self.group_focus = [group[0] for group in self.action_groups]
            def column_width():
                count = max(1, min(3, len(groups), (self.content_width() + 2) // 27))
                width = min(32, (self.content_width() - 2 * (count - 1)) // count)
                if screen == "home":
                    # Match the parity of the visible lines: Latin copy uses odd
                    # cell widths, Chinese copy even widths. This avoids half-cell
                    # offsets between the title and each description line.
                    width -= (width - (1 if self.language == "en" else 0)) % 2
                return width

            if screen == "home":
                frames = [self.home_card(groups[0][0], "Start Codex\nSessions and models", column_width, 1),
                          self.home_card(groups[1][0], "Add/connect providers\nStart/stop services", column_width, 2)]
            else:
                frames = [self.action_section(title, group, column_width, index + 1)
                          for index, ((title, _), group) in enumerate(zip(action_groups, groups))]

            def grouped_rows():
                count = max(1, min(3, (self.content_width() + 2) // 27))
                return HSplit(
                    [VSplit(frames[i:i + count], padding=2, align=HorizontalAlign.CENTER)
                     for i in range(0, len(frames), count)],
                    padding=1,
                )

            if screen == "home":
                self.body = HSplit([
                    content, Window(height=1),
                    HSplit([DynamicContainer(grouped_rows)], align=VerticalAlign.CENTER),
                ])
            else:
                self.body = ScrollablePane(
                    HSplit([content, Window(height=1), DynamicContainer(grouped_rows)],
                           align=VerticalAlign.CENTER), show_scrollbar=False,
                )
        else:
            def content_height():
                height = to_container(content).preferred_height(self.content_width() - 2, 10000).preferred + 2
                return Dimension(min=3, preferred=max(3, height), max=max(3, height))

            self.body = HSplit(
                [
                    self.frame(content, title="Settings" if screen == "form" else "Details",
                          height=content_height),
                    Window(height=1),
                    self.frame(DynamicContainer(action_rows), title="Actions"),
                ],
                align=VerticalAlign.CENTER,
            )
        self.update_captions()
        if focus is not None or buttons:
            target = focus if focus is not None else buttons[0]
            if self.help_open:
                self.help_focus = target
                self.app.layout.focus(self.help_area)
            else:
                self.app.layout.focus(target)
        self.app.layout.update_parents_relations()
        self.app.invalidate()

    def action_section(self, title, buttons, width, number):
        def heading():
            active = any(self.app.layout.has_focus(button) for button in buttons)
            return [("class:tab.active" if active else "class:tab", f" {number}  {self.t(title)} ")]
        for button in buttons:
            button.window.align = WindowAlign.LEFT
            button.control.text = lambda button=button: [("[SetCursorPosition]", ""), ("class:button.text", button.text)]
        return HSplit([
            self.label(heading), Window(char="─", height=1, style="class:separator"),
            HSplit(buttons, width=Dimension(min=23)),
        ], width=width, height=len(buttons) + 2)

    def home_card(self, button, description, width, number):
        def focused():
            return self.app.layout.has_focus(button)

        button.width = 21
        button.window.width = 21
        button.window.style = "class:title"
        # Let the window center the unpadded title exactly once. Button's own
        # padding otherwise adds a second rounding offset to this card's axis.
        button.control.text = lambda: [("[SetCursorPosition]", ""), ("class:button.text", button.text)]
        # Keep each card's slot fixed; expand its border by one character on
        # every side when focused, without moving the neighbouring card.
        frame = self.frame(HSplit([
            Window(height=lambda: 2 if focused() else 1), button,
            self.label(description, align=WindowAlign.CENTER), Window(height=lambda: 2 if focused() else 1),
        ], width=Dimension(min=21)), title=str(number), height=lambda: 9 if focused() else 7,
            width=lambda: width() - (0 if focused() else 2))
        return HSplit([
            Window(height=lambda: 0 if focused() else 1),
            VSplit([Window(width=lambda: 0 if focused() else 1), frame,
                    Window(width=lambda: 0 if focused() else 1)]),
            Window(height=lambda: 0 if focused() else 1),
        ], height=9, width=width,
            style=lambda: "class:card.focused" if focused() else "")

    def invoke(self, action):
        """Keep recoverable local errors inside the manager, including stale registry entries."""
        try:
            action()
        except (ValueError, OSError, RuntimeError) as error:
            self.text("Request failed", str(error), screen="error")

    def text(self, title, text, actions=(), *, back=None, screen="view"):
        def rendered():
            return clean(text() if callable(text) else self.t(text))
        area = TextArea(text=rendered(), read_only=True, scrollbar=True)
        self.show(
            title, area, list(actions) + [("Back", back or self.menu)], back=back, screen=screen
        )
        def refresh():
            position = area.buffer.cursor_position
            from prompt_toolkit.document import Document
            value = rendered()
            area.buffer.set_document(Document(value, min(position, len(value))), bypass_readonly=True)
        self.refresh_text = refresh

    def message(self, template, **values):
        return lambda: self.t(template, **values)

    def form(self, title, fields, submit, *, values=None, back=None, label="Continue", intro="", actions=()):
        values = values or {}
        self.fields = {}
        widgets = [self.label(intro, style="class:subtitle"), Window(height=1)] if intro else []
        for spec in fields:
            value = values.get(spec.key, spec.value)
            if spec.choices:
                widget = RadioList([(key, lambda text=text: self.t(text)) for key, text in spec.choices],
                                   default=value, select_on_focus=True)
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
                        "class:focused" if self.app.layout.has_focus(widget) else "class:title",
                        self.t(spec.label),
                    )
                ]

            widgets.append(self.frame(widget, title=title_text))
            if spec.help:
                widgets.append(self.label(spec.help, style="class:subtitle"))
            widgets.append(Window(height=1))

        def accept(handler=submit):
            data = {
                key: (field.current_value if isinstance(field, RadioList) else field.text.strip())
                for key, field in self.fields.items()
            }
            try:
                handler(data)
            except (ValueError, OSError, RuntimeError) as error:
                self.notice = clean(error)
                self.app.invalidate()

        self.show(
            title,
            ScrollablePane(HSplit(widgets)),
            [(label, accept)] + [(name, lambda fn=fn: accept(fn)) for name, fn in actions]
            + [("Back", back or self.menu)],
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
        if self.help_open:
            target = self.help_button if self.app.layout.has_focus(self.help_area) else self.help_area
            self.app.layout.focus(target)
            return
        layout = self.app.layout
        if self.action_groups:
            for index, group in enumerate(self.action_groups):
                for widget in group:
                    if layout.has_focus(widget):
                        self.focus_group((index + step) % len(self.action_groups))
                        return
        windows = [window for window in layout.get_focusable_windows()
                   if window not in (self.help_area.window, self.help_button.window)]
        if windows:
            current = (
                windows.index(layout.current_window) if layout.current_window in windows else -1
            )
            layout.focus(windows[(current + step) % len(windows)])

    def focus_group(self, index):
        for current, group in enumerate(self.action_groups):
            for button in group:
                if self.app.layout.has_focus(button):
                    self.group_focus[current] = button
        self.app.layout.focus(self.group_focus[index])

    def move_action(self, step):
        for group in self.action_groups:
            for index, widget in enumerate(group):
                if self.app.layout.has_focus(widget):
                    self.app.layout.focus(group[(index + step) % len(group)])
                    return

    def choose(self, title, choices, actions, *, back=None, description=""):
        if not choices:
            self.text(title, description or "No entries found.", back=back)
            return
        listing = RadioList(choices, select_on_focus=True)
        content = HSplit(
            [self.label(description, style="class:subtitle"), Window(height=1), self.frame(listing)]
        )
        wrapped = [(label, lambda fn=fn: fn(listing.current_value)) for label, fn in actions]
        self.show(title, content, wrapped + [("Back", back or self.menu)], focus=listing, back=back)

    def work(self, title, function, success, *, retry=None, failure=None, back=None):
        """Keep the renderer responsive while bounded native operations run in a worker."""
        if self.busy:
            return
        self.busy, self.stage, self.completed = True, "", []
        self.show(
            title,
            self.label(lambda: self.progress_text()),
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
        lines = [self.t("Completed steps"), ""]
        lines.extend("  " + self.t("[done]") + " " + self.t(STAGES.get(s, s)) for s in self.completed)
        lines += [
            "",
            "  " + self.t("[working]") + " " + self.t(STAGES.get(self.stage, self.stage or "Contacting the backend")),
            "",
            self.t("The interface remains available while this operation completes."),
        ]
        return "\n".join(lines)

    def load_routes(self):
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
            return False
        names = list(self.routes)
        if self.selected not in self.routes:
            self.selected = names[0] if names else None
        return True

    def overview(self):
        return DynamicContainer(self.overview_content)

    def overview_content(self):
        def shorten(value, columns):
            text = clean(value).replace("\n", " ").replace("\t", " ")
            if get_cwidth(text) > columns:
                while text and get_cwidth(text) > columns - 1:
                    text = text[:-1]
                text += "…"
            return text

        available = max(3, self.app.output.get_size().rows - 15)
        if not self.routes:
            message = ("No Codex added yet.\nChoose Manage backends to get started."
                       if available > 3 else "No Codex added. Choose Manage backends.")
            return self.frame(self.label(message, align=WindowAlign.CENTER),
                         title="Configured backends", height=min(available, 4), style="class:overview-panel")

        count = len(self.routes)
        enabled = sum(b.enabled for b in self.routes.values())
        summary = [
            ("class:title", self.t("{count} backends", count=count)),
            ("class:overview.field", " · "),
            ("class:overview.enabled", self.t("Bridge: {count} enabled", count=enabled)),
        ]
        if enabled < count:
            summary += [("class:overview.field", " · "),
                        ("class:overview.disabled", self.t("{count} disabled", count=count - enabled))]
        columns = max(1, min(3, count, (self.content_width() + 2) // 26))
        for rows in (2, 1):
            visible = min(count, rows * columns)
            height = 5 * ((visible + columns - 1) // columns) + 1 + (visible < count)
            if height <= available:
                break
        else:
            return self.frame(self.label(summary, align=WindowAlign.CENTER),
                         title="Configured backends", height=3, style="class:overview-panel")

        width = min(32, (self.content_width() - 2 * (columns - 1)) // columns)
        entries = []
        for backend in list(self.routes.values())[:visible]:
            status = self.t("Enabled" if backend.enabled else "Disabled")
            field_width = max(get_cwidth(self.t(key)) for key in ("Provider", "Bridge")) + 2

            def prefix(key):
                value = self.t(key)
                return " " + value + " " * (field_width - get_cwidth(value))
            status_style = "class:overview.enabled" if backend.enabled else "class:overview.disabled"
            entries.append(self.frame(HSplit([
                self.label([("class:overview.field", prefix("Provider")),
                       ("class:overview.value", shorten(backend.model_provider, width - field_width - 5))]),
                self.label([("class:overview.field", prefix("Bridge")), (status_style, status)]),
            ], width=Dimension(min=20)), title=lambda name=backend.name: shorten(name, width - 8),
                width=width, height=4, style="class:overview-panel"))
        content = [self.label(summary, align=WindowAlign.CENTER), Window(height=1),
                   HSplit([VSplit(entries[i:i + columns], padding=2, align=HorizontalAlign.CENTER)
                           for i in range(0, len(entries), columns)], padding=1)]
        if visible < count:
            content.append(self.label(self.t("+ {count} more configured", count=count - visible), align=WindowAlign.CENTER,
                                 style="class:overview.field"))
        return HSplit(content, height=height)

    def home(self):
        self.menu = self.home
        if not self.load_routes():
            return
        self.show("Codex manager", self.overview(), [
            ("Use backend", self.use_backend), ("Manage backends", self.manage_backends),
        ], back=self.quit, screen="home", action_groups=[
            ("Sessions & models", ["Use backend"]),
            ("Setup & services", ["Manage backends"]),
        ])

    def choose_backend(self, action, *, back=None):
        if not self.load_routes():
            return
        if not self.routes:
            self.text("No Codex added", "Add a Codex configuration before using a backend.",
                      [("Manage backends", self.manage_backends)], back=self.home)
            return
        if len(self.routes) == 1:
            self.selected = next(iter(self.routes))
            action()
            return
        listing = RadioList([(name, clean(f"{name} / {b.model_provider}"))
                             for name, b in self.routes.items()],
                            default=self.selected, select_on_focus=True)

        def accept():
            listing._handle_enter()
            self.selected = listing.current_value
            action()

        @listing.control.key_bindings.add("enter")
        def enter(_event):
            self.invoke(accept)

        self.show("Choose Codex", self.frame(listing, title="Name / Provider"),
                  [("Continue", accept), ("Back", back or self.menu)],
                  focus=listing, back=back, screen="backend-picker")

    def use_backend(self):
        self.choose_backend(self.backend_menu, back=self.home)

    def backend_menu(self):
        target = self.bridge.backend(self.selected)
        self.menu = self.backend_menu
        navigation = ([("Switch backend", self.use_backend)] if len(self.routes) > 1 else [])
        navigation += [("Home", self.home)]
        self.show("Use / " + target.name,
                  self.label(lambda: clean(f"Codex: {target.name}\n{self.t('Provider')}: {target.model_provider}")), [
                      ("Open Codex", self.open_codex), ("Account status", lambda: self.codex_status(target.home)),
                      ("Sessions", self.sessions),
                      ("Backend models", self.models), ("Model overview", self.all_models),
                  ] + navigation, back=self.home, screen="use", action_groups=[
                      ("Work", ["Open Codex", "Account status", "Sessions", "Backend models", "Model overview"]),
                      ("Navigate", [label for label, _ in navigation]),
                  ])

    def manage_backends(self):
        self.menu = self.manage_backends
        if not self.load_routes():
            return

        def selected(fn):
            return lambda: self.choose_backend(fn)

        setup = [
            ("Set up this Codex", self.setup_codex),
            ("Current Codex", lambda: self.codex_status(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))),
            ("New provider", self.new_provider),
            ("Attach existing", self.attach),
        ]
        management = []
        if self.routes:
            setup += [
                ("Connect bridge", selected(self.connect)),
                ("Install launcher", selected(self.install_launcher)),
            ]
            management += [
                ("Daemon status", selected(lambda: self.daemon("status"))),
                ("Start daemon", selected(lambda: self.daemon("start"))),
                ("Stop daemon", selected(self.confirm_stop)),
                ("Remove daemon", selected(self.remove_daemon)),
            ]
        tools = [("More tools", self.more_tools), ("Refresh", self.manage_backends),
                 ("Home", self.home), ("Quit", self.quit)]
        groups = [("Setup", setup), ("Daemon", management), ("Tools", tools)]
        groups = [(title, actions) for title, actions in groups if actions]
        self.show(
            "Manage backends", self.overview(), setup + management + tools,
            back=self.home, screen="manager",
            action_groups=[(title, [label for label, _ in actions]) for title, actions in groups],
        )

    def setup_codex(self):
        specs = [
            Field("home", "Codex directory", os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                  help="The default is your current Codex. Keep it to enable discovery in new sessions."),
            Field("name", "Name in the manager", "codex", help="Existing routes are reused."),
            Field("provider", "Provider", "", choices=(
                ("", "Use the provider already configured in this directory"),
                ("openai", "OpenAI (use this directory's login)")),
                  help=lambda: self.t(
                      "ChatGPT: browser or device code login, using plan access.\n"
                      "API key: usage billed to your OpenAI Platform account.\n"
                      "Enterprise automation can also use a Codex access token.\n"
                      "Setup reuses this directory's login; sign in with Codex first."
                      if self.fields["provider"].current_value == "openai" else
                      "For standard Codex without a provider setting, select OpenAI.")),
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
                          self.message("The model responded, the bridge tools are available, and its guide is enabled.\n\n"
                          "New Codex sessions using this directory will discover the bridge automatically.\n"
                          "Existing sessions may need to reconnect to refresh their tools.\n\n{directory}",
                          directory=directory),
                          [("Open Codex", self.open_codex), ("Manage backends", self.manage_backends)],
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
                    if (("model_provider" in message and "explicit" in message)
                            or "home does not contain config.toml" in message):
                        message = ("This directory has no provider selection. Return to setup and "
                                   "choose OpenAI for standard Codex. For another service, "
                                   "use New provider in the manager.")
                    actions = []
                self.text("Setup incomplete", message, actions + [("Open native Codex", lambda: self.open_native_codex(directory, back=self.setup_codex)),
                                      ("Retry setup", self.setup_codex)],
                          screen="error")

            self.work("Setting up this Codex", operation, ready, failure=failed)

        def native(data):
            self.setup_draft = data.copy()
            self.open_native_codex(Path(data["home"]).expanduser(), back=self.setup_codex)

        def inspect(data):
            self.setup_draft = data.copy()
            self.codex_status(Path(data["home"]).expanduser(), back=self.setup_codex)

        self.form("Set up this Codex", specs, install, values=self.setup_draft, label="Set up",
                  actions=[("Inspect Codex", inspect), ("Open native Codex", native)],
                  intro="New to Codex? Open native Codex to finish its own setup, then return here.\n"
                        "Bridge setup verifies a model response and installs discovery for new sessions.")

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
            def details():
                message = getattr(error, "detail", None) or str(error)
                if retry:
                    message += "\n\n" + self.t(
                        "If the connection settings are wrong, correct {path} before retrying. The existing home is retained.",
                        path=route.home / "config.toml")
                return message

            self.text(
                "Provider setup incomplete",
                details,
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
            self.message("{name} is verified and enabled.\n\nHome       {home}\nLauncher   {launcher}\n\n"
                         "The temporary verification session has been deleted.",
                         name=result["backend"], home=result["home"], launcher=result["launcher"]),
            [
                ("Open sessions", self.sessions),
                ("Open Codex", self.open_codex),
                ("Providers", self.home),
            ],
            screen="success",
        )

    def codex_status(self, home, *, back=None):
        from .diagnostics import inspect_codex

        back = back or self.menu
        labels = {"apiKey": "OpenAI API key", "chatgpt": "ChatGPT account",
                  "amazonBedrock": "Amazon Bedrock", "gcp": "Google Cloud",
                  "provider_managed": "Managed by provider configuration",
                  "sign_in_required": "Sign-in required", "unknown": "Not reported"}

        def display(result):
            def content():
                lines = [str(home), ""]
                if not result["connected"]:
                    lines.append(self.t("No running Codex backend was found. Authentication and model selection have not been checked.\nOpen native Codex to set up or sign in, then return to bridge setup."))
                else:
                    lines += [self.t("Provider: {provider}", provider=result["provider"] or self.t("Codex default")),
                              self.t("Model: {model}", model=result["model"] or self.t("Codex default")),
                              self.t("Authentication: {auth}", auth=self.t(labels[result["authentication"]])),
                              self.t("Codex account: {account}", account=self.t(labels[result["account"]])),
                              self.t("Plan: {plan}", plan=result["plan"] or self.t("Not reported")), "",
                              self.t("Read from the running Codex backend. This does not verify model access or remaining credits."),
                              self.t("This account response does not identify browser, device-code or access-token sign-in.")]
                return wrap_text("\n".join(lines), max(10, self.content_width() - 4))
            self.text("Codex account and model", content, actions, back=back)

        actions = [("Open native Codex", lambda: self.open_native_codex(home, back=lambda: self.codex_status(home, back=back))),
                   ("Refresh", lambda: self.codex_status(home, back=back)),
                   ("Bridge setup", lambda: self.setup_for_home(home))]
        self.work("Inspecting Codex", lambda _p: inspect_codex(home), display, back=back,
                  failure=lambda error: self.text("Cannot inspect Codex", str(error), actions, back=back))

    def setup_for_home(self, home):
        self.setup_draft = {"home": str(home)}
        self.setup_codex()

    def open_native_codex(self, home, *, back):
        if not home.is_absolute():
            raise ValueError("Choose an absolute Codex directory.")
        if home.exists() and not home.is_dir():
            raise ValueError("Choose a Codex directory, not a file.")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        def native():
            # Codex requires an explicitly supplied CODEX_HOME to exist. Leave
            # all login, model selection and configuration to its own interface.
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            return subprocess.run(["codex"], env=env, check=False).returncode
        self.run_native(native, back=back)

    def open_codex(self, args=()):
        from .providers import run_provider

        name = self.selected
        def native_exec(_file, argv, env):
            return subprocess.run(argv, env=env, check=False).returncode

        self.run_native(lambda: run_provider(name, args, registry_path=self.registry,
                                            exec_runner=native_exec), back=self.home)

    def run_native(self, function, *, back):
        if self.busy:
            return
        self.busy = True

        async def run():
            error = None
            try:
                code = await run_in_terminal(function, in_executor=True)
            except (RuntimeError, ValueError, OSError) as failure:
                error = failure
            finally:
                self.busy = False
            if self.close_when_done:
                self.app.exit()
            elif error is not None:
                self.text("Could not open Codex", str(error), back=back)
            elif isinstance(code, dict):
                self.text("Daemon result", "\n".join(f"{key}: {clean(value)}" for key, value in code.items()), back=back)
            else:
                back()
                if code:
                    self.notice = self.message("Codex exited with status {code}.", code=code)

        self.worker = self.app.create_background_task(run())

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
            self.message("{name}\n{home}\n\nConnect the bridge to verify and enable this provider.",
                         name=backend.name, home=backend.home),
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
            self.message("{home}\n\nStopping this daemon can interrupt its active sessions.", home=target.home),
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
                def content():
                    text = self.t(details)
                    if result["kept_files"]:
                        text += "\n\n" + self.t("Kept modified or unrelated files:") + "\n" + "\n".join(result["kept_files"])
                    return text
                self.text("Daemon removed", content, [("Manage backends", self.manage_backends)], screen="success")

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
        ], submit, label="Remove daemon", intro=self.message("{name}\n{home}\n\n"
                  "This stops the selected daemon, including any active tasks, and removes its registration.\n"
                  "Other daemons and the bridge application remain available.", name=target.name, home=target.home))

    def more_tools(self):
        actions = [("Create daemon", self.create_daemon), ("Choose registry", self.choose_registry),
                   ("Diagnostics", self.diagnostics), ("MCP connection", self.mcp_connection),
                   ("Uninstall bridge", self.confirm_uninstall)]
        if self.routes:
            actions += [("Verify model ID", lambda: self.choose_backend(self.verify_model_id,
                                                                      back=self.more_tools)),
                        ("Codex arguments", lambda: self.choose_backend(self.codex_arguments,
                                                                       back=self.more_tools))]
        self.text("More tools", self.message("Additional setup, native Codex and application management.\n\nCurrent registry: {registry}",
                               registry=self.registry), actions)

    def diagnostics(self):
        from .diagnostics import diagnose

        def display(result):
            def content():
                lines = ["Codex Daemon Bridge " + result["version"], ""]
                for check in result["checks"]:
                    status, name, detail = (diagnostic_text(self.language, check[key])
                                            for key in ("status", "name", "detail"))
                    lines.append(f"[{status}] {name}: {detail}")
                    if check["advice"]:
                        lines.append("  " + diagnostic_text(self.language, check["advice"]))
                return "\n".join(lines)
            self.text("Installation diagnostics", content,
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
        self.text("MCP connection", self.message("Use Set up this Codex or Connect bridge for automatic registration.\n\n"
                  "For another MCP client, use this stdio server command:\n\n{command}", command=command),
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
        update = Checkbox(lambda: self.t("Update the installed bridge skill"), checked=draft.get("update_skill", False))
        unattended = Checkbox(
            lambda: self.t("Allow unattended delegation (four session write tools)"), checked=draft.get("unattended", False)
        )
        self.fields = {"update_skill": update, "unattended": unattended}
        content = HSplit(
            [
                self.label("Verify model access and connect MCP. A stopped daemon will be started."),
                Window(height=1),
                update,
                unattended,
                Window(height=1),
                self.label(
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
                    self.message("{name} is verified and enabled.\n"
                    "New Codex sessions using this directory can discover the bridge automatically.", name=target.name),
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

    def models(self, cursor=None, *, back=None):
        self.work(
            "Loading models",
            lambda _p: self.bridge.list_models(self.selected, cursor, 30),
            lambda result: self.model_list(result, cursor=cursor, back=back),
            retry=lambda: self.models(cursor, back=back),
            back=back,
        )

    def model_list(self, result, *, cursor=None, back=None):
        models = result["models"]
        def page():
            self.models(cursor, back=back)

        def details(index):
            model = models[index]
            efforts = [r["reasoningEffort"] for r in model.get("supportedReasoningEfforts") or []]
            def text():
                return self.t(
                    "{model}\n\n{description}\n\nReasoning: {efforts}\n\nListed by the daemon; access still needs a real inference check.",
                    model=model["model"], description=model.get("description") or "",
                    efforts=", ".join(efforts) or self.t("Provider default"))
            self.text(
                "Model details",
                text,
                [
                    ("Verify model", lambda: self.verify_model(model, back=page)),
                    ("New session", lambda: self.new_session(model, back=page)),
                ],
                back=page,
            )

        actions = [("Details", details)]
        if result.get("next_cursor"):
            actions.append(("Next page", lambda _i: self.models(result["next_cursor"], back=back)))
        if not models:
            navigation = [("Next page", lambda: self.models(result["next_cursor"], back=back))] if result.get("next_cursor") else []
            self.text("Models / " + self.selected, "No models on this page.", navigation, back=back)
            return
        self.choose(
            "Models / " + self.selected,
            [(i, clean(m["model"])) for i, m in enumerate(models)],
            actions,
            back=back,
            description="Available model IDs come from this daemon's current catalog.",
        )

    def all_models(self):
        def display(result):
            def content():
                lines = [self.t("Model catalogs only; model access has not been verified."), ""]
                for provider in result["providers"]:
                    lines.append(provider["backend"])
                    if provider.get("reachable"):
                        models = provider.get("models", [])
                        lines.extend("  " + model["model"] for model in models)
                        if not models:
                            lines.append("  " + self.t("No models returned."))
                        if provider.get("next_cursor"):
                            lines.append("  " + self.t("Showing {count} models; more are available.", count=len(models)))
                    else:
                        lines.append("  " + provider.get("error", self.t("Unavailable")))
                    lines.append("")
                return "\n".join(lines)
            actions = [("Refresh", self.all_models)]
            if result["providers"]:
                actions.insert(0, ("Browse backend", lambda: self.choose_backend(
                    lambda: self.models(back=self.all_models), back=self.all_models)))
            self.text("Model overview", content, actions)

        self.work(
            "Discovering provider models", lambda _p: self.bridge.list_provider_models(), display
        )

    def verify_model(self, model, *, back=None):
        from .probe import check_model

        target = self.bridge.backend(self.selected)
        back = back or self.models
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
                    back=back,
                ),
                back=back,
            )

        self.form(
            "Verify model access",
            [Field("effort", "Reasoning effort", default_effort, choices=choices)],
            verify,
            back=back,
            label="Run verification",
            intro="This sends one short model request.",
        )

    def sessions(self, cursor=None):
        def page():
            self.sessions(cursor)

        def display(result):
            sessions = result["sessions"]
            actions = [
                ("Open session", lambda thread: self.session(thread, back=page)),
                ("New session", lambda _id: self.new_session(back=page)),
            ]
            if result.get("next_cursor"):
                actions.append(("Next page", lambda _id: self.sessions(result["next_cursor"])))
            if not sessions:
                navigation = [("New session", lambda: self.new_session(back=page))]
                if result.get("next_cursor"):
                    navigation.append(("Next page", lambda: self.sessions(result["next_cursor"])))
                self.text(
                    "Sessions / " + self.selected,
                    "No sessions on this page." if cursor or result.get("next_cursor") else "No sessions yet.",
                    navigation,
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

    def new_session(self, model=None, *, back=None):
        back = back or self.sessions
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
                back=back,
            )

        self.form(
            "New session / " + self.selected, specs, create, back=back, label="Start task"
        )

    def accepted(self, result):
        self.text(
            "Task accepted",
            self.message("The request was accepted. Open the session to follow its result.\n\n{thread}",
                         thread=result["thread_id"]),
            [("Open session", lambda: self.session(result["thread_id"]))],
            back=self.sessions,
        )

    def session(self, thread_id, cursor=None, *, back=None):
        back = back or self.sessions
        self.work(
            "Reading session",
            lambda _p: self.bridge.read_session(self.selected, thread_id, 3, 16000, cursor),
            lambda result: self.session_view(result, cursor=cursor, back=back),
            back=back,
        )

    def session_view(self, result, *, cursor=None, back=None):
        session = result["session"]
        thread_id = session["id"]
        back = back or self.sessions
        def page():
            self.session(thread_id, cursor, back=back)

        def content():
            lines = [
                session.get("name") or thread_id,
                self.t("State: {state}", state=session.get("status", {}).get("type", "unknown")),
                "",
            ]
            assignment = session.get("delegation")
            if assignment:
                parent = assignment.get("parent")
                source = assignment.get("fork_source")
                lines += [self.t("Assignment: {id}", id=assignment["delegation_id"]),
                          self.t("Type: {type} / Depth: {depth}", type=assignment["session_type"], depth=assignment["depth"])]
                if parent:
                    lines.append(self.t("Parent: {id} ({home})", id=parent["thread_id"], home=parent["home"]))
                if source and source != parent:
                    lines.append(self.t("History source: {id} ({home})", id=source["thread_id"], home=source["home"]))
                lines += [self.t("Task: {task}", task=assignment["task_requirements"]),
                          self.t("Acceptance criteria: {criteria}", criteria=assignment["acceptance_criteria"]), ""]
            for turn in reversed(result["turns"]):
                lines += [self.t("Turn {id} / {status}", id=turn["id"], status=turn["status"])]
                if turn.get("error"):
                    lines.append(turn["error"])
                for message in turn["messages"]:
                    lines += [message["type"], message["text"], ""]
            return "\n".join(lines)

        active = next((t for t in result["turns"] if t["status"] == "inProgress"), None)
        actions = [("Refresh", page)]
        if active:
            actions.append(
                ("Add instructions", lambda: self.message_session(thread_id, "steer", active["id"], back=page))
            )
        elif session.get("status", {}).get("type") != "active":
            actions.append(("Continue", lambda: self.message_session(thread_id, "continue", back=page)))
        actions += [
            ("Fork session", lambda: self.message_session(thread_id, "fork", back=page)),
            (
                "Read a turn",
                lambda: self.choose(
                    "Choose a turn",
                    [(t["id"], f"{t['id']} / {t['status']}") for t in result["turns"]],
                    [("Read messages", lambda turn: self.messages(thread_id, turn, back=page))],
                    back=page,
                ),
            ),
        ]
        if result.get("next_cursor"):
            actions.append(("Older turns", lambda: self.session(thread_id, result["next_cursor"], back=back)))
        self.text("Session / " + self.selected, content, actions, back=back)

    def messages(self, thread, turn, cursor=None, *, back=None):
        back = back or (lambda: self.session(thread))
        def display(result):
            def content():
                lines = [m["type"] + (self.t(" (continued)") if m.get("text_offset") else "")
                         + "\n" + m["text"] + ("\n" + self.t("[Continues on the next page]") if m.get("truncated") else "")
                         for m in result["messages"]]
                return "\n\n".join(lines) or self.t("No visible messages.")
            actions = (
                [("More messages", lambda: self.messages(thread, turn, result["next_cursor"], back=back))]
                if result.get("next_cursor")
                else []
            )
            self.text(
                "Turn messages",
                content,
                actions,
                back=back,
            )

        self.work(
            "Reading messages",
            lambda _p: self.bridge.read_messages(
                self.selected, thread, turn, cursor, text_limit=16000
            ),
            display,
            back=back,
        )

    def message_session(self, thread, mode, turn=None, *, back=None):
        back = back or (lambda: self.session(thread))
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
                "Sending instructions", operation, self.accepted, back=back
            )

        self.form(
            mode.title() + " session",
            fields,
            send,
            back=back,
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
