"""Termux Script Management Wrapper - GUI Dashboard & Terminal Console UI."""
import os

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

import schema_engine
import termux_bridge

try:
    from android.permissions import request_permissions, Permission  # noqa: F401
    ON_ANDROID = True
except ImportError:
    ON_ANDROID = False

try:
    from plyer import filechooser
except ImportError:
    filechooser = None


KV = """
<RootWidget>:
    orientation: "vertical"
    padding: dp(10)
    spacing: dp(8)

    BoxLayout:
        size_hint_y: None
        height: dp(40)
        spacing: dp(8)

        Button:
            text: "Setup Termux (one-time)"
            on_release: root.setup_termux()

        Label:
            text: "Do this once before your first run"
            font_size: "12sp"
            color: 0.7, 0.7, 0.7, 1

    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)

        Button:
            text: "Select Script (.py)"
            size_hint_x: None
            width: dp(200)
            on_release: root.pick_script()

        Label:
            id: script_label
            text: root.script_name or "No script selected"
            shorten: True
            shorten_from: "left"

    ScrollView:
        size_hint_y: 0.35

        GridLayout:
            id: form_grid
            cols: 2
            spacing: dp(6)
            padding: dp(6)
            size_hint_y: None
            height: self.minimum_height
            row_force_default: False

    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)

        Button:
            text: "Save Config"
            on_release: root.save_config()

        Button:
            text: "Install Packages"
            on_release: root.install_packages()

        Button:
            text: "Run Script"
            on_release: root.run_script()

    BoxLayout:
        size_hint_y: None
        height: dp(36)
        spacing: dp(8)

        Label:
            text: "Terminal Output"
            bold: True
            halign: "left"
            valign: "middle"
            text_size: self.size

        Button:
            text: "Copy Output"
            size_hint_x: None
            width: dp(140)
            on_release: root.copy_output()

    ScrollView:
        id: output_scroll
        size_hint_y: 0.45
        do_scroll_x: False

        Label:
            id: output_label
            text: root.output_text
            size_hint_y: None
            height: self.texture_size[1]
            text_size: self.width, None
            halign: "left"
            valign: "top"
            color: 0, 1, 0, 1
            padding: dp(6), dp(6)
"""


class RootWidget(BoxLayout):
    script_path = StringProperty("")
    script_name = StringProperty("")
    output_text = StringProperty("Output will appear here after you run a script.\n")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.schema = schema_engine.default_schema("")
        self.field_widgets = {}
        self._poll_event = None

    # ---- One-time Termux setup -------------------------------------------
    def setup_termux(self):
        """Copy the setup command and bring Termux to the front.

        Termux refuses RUN_COMMAND intents from other apps until
        `allow-external-apps=true` is set inside its own private
        ~/.termux/termux.properties. No app can write that file for the
        user (that's Termux's whole point), so the best we can offer is:
        copy the one-liner, open Termux, and let the user paste + Enter.
        """
        Clipboard.copy(termux_bridge.SETUP_COMMAND)
        self._append_output(
            "[setup] Command copied to clipboard.\n"
            "[setup] Termux is opening -- long-press to Paste, then press Enter.\n"
            "[setup] You only need to do this once.\n"
        )
        try:
            termux_bridge.open_termux()
        except RuntimeError as exc:
            self._append_output(f"[error] {exc}\n")

    # ---- Script selection -------------------------------------------------
    def pick_script(self):
        # Deliberately no `filters=` here: Android's native file picker
        # (SAF) filters by MIME type, and ".py" has no reliable MIME
        # mapping on most devices -- passing a filter made every .py file
        # disappear from the picker. We show all files instead and
        # validate the extension/content after the user picks one.
        if filechooser is not None:
            filechooser.open_file(
                on_selection=self._on_file_selected,
                path="/sdcard",
            )
        else:
            self._show_message("File chooser is unavailable on this platform.")

    def _on_file_selected(self, selection):
        if not selection:
            return
        # plyer's callback can fire off the main thread; hop back onto it.
        Clock.schedule_once(lambda dt: self._load_script(selection[0]))

    def _load_script(self, path):
        if not path.lower().endswith(".py"):
            self._show_message(f"'{os.path.basename(path)}' is not a .py file. Pick a Python script.")
            return

        try:
            self.schema = schema_engine.load_schema_from_file(path)
        except schema_engine.SchemaError as exc:
            self._show_message(str(exc))
            return

        self.script_path = path
        self.script_name = os.path.basename(path)
        self._build_form()
        self._append_output(f"[schema] Loaded '{self.schema.get('name')}' "
                             f"({len(schema_engine.get_fields(self.schema))} fields, "
                             f"{len(schema_engine.get_packages(self.schema))} packages)\n")

    # ---- Dynamic form -------------------------------------------------
    def _build_form(self):
        grid = self.ids.form_grid
        grid.clear_widgets()
        self.field_widgets = {}

        saved_values = schema_engine.load_config_for_script(self.script_path)
        fields = schema_engine.get_fields(self.schema)

        if not fields:
            grid.add_widget(Label(text="No configurable options", size_hint_y=None, height=44))
            grid.add_widget(Label(text="", size_hint_y=None, height=44))
            return

        for field in fields:
            grid.add_widget(Label(text=field["label"], size_hint_y=None, height=44))
            value = saved_values.get(field["key"], field["default"])
            widget = self._make_field_widget(field, value)
            self.field_widgets[field["key"]] = (field, widget)
            grid.add_widget(widget)

    def _make_field_widget(self, field, value):
        ftype = field["type"]

        if ftype == "boolean":
            return CheckBox(active=bool(value), size_hint_y=None, height=44)

        if ftype == "select":
            options = field.get("options", [])
            return Spinner(
                text=str(value) if value else (options[0] if options else ""),
                values=options,
                size_hint_y=None,
                height=44,
            )

        return TextInput(
            text=str(value),
            multiline=False,
            input_filter="float" if ftype == "number" else None,
            size_hint_y=None,
            height=44,
        )

    def _collect_form_values(self):
        values = {}
        for key, (field, widget) in self.field_widgets.items():
            if field["type"] == "boolean":
                raw = widget.active
            else:
                raw = widget.text
            values[key] = schema_engine.cast_field_value(field, raw)
        return values

    # ---- Actions -------------------------------------------------
    def save_config(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        values = self._collect_form_values()
        path = schema_engine.save_config_for_script(self.script_path, values)
        self._append_output(f"[config] Saved settings to {path}\n")

    def install_packages(self):
        packages = schema_engine.get_packages(self.schema)
        self._append_output(
            f"[install] Requesting install of: {', '.join(packages) or '(none declared)'}\n"
        )
        self._run_bridge_action(lambda: termux_bridge.install_packages(packages))
        self._start_polling(termux_bridge.read_install_log)

    def run_script(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        self.save_config()
        self._append_output(f"[run] Launching {self.script_name} in Termux...\n")
        self._run_bridge_action(lambda: termux_bridge.run_script(self.script_path))
        self._start_polling(termux_bridge.read_run_log)

    def _run_bridge_action(self, action):
        try:
            action()
        except RuntimeError as exc:
            self._append_output(f"[error] {exc}\n")

    def copy_output(self):
        Clipboard.copy(self.output_text)
        self._append_output("[info] Output copied to clipboard.\n")

    # ---- Output polling -------------------------------------------------
    def _start_polling(self, reader):
        if self._poll_event:
            self._poll_event.cancel()

        def poll(_dt):
            content = reader()
            if content and content != self.output_text:
                self.output_text = content

        self._poll_event = Clock.schedule_interval(poll, 2)

    def _append_output(self, text):
        self.output_text += text

    def _show_message(self, message):
        Popup(
            title="Notice",
            content=Label(text=message),
            size_hint=(0.8, 0.3),
        ).open()


class ScriptWrapperApp(App):
    def build(self):
        if ON_ANDROID:
            request_permissions([
                Permission.READ_EXTERNAL_STORAGE,
                Permission.WRITE_EXTERNAL_STORAGE,
            ])
        Builder.load_string(KV)
        return RootWidget()


if __name__ == "__main__":
    ScriptWrapperApp().run()
