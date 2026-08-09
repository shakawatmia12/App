"""Termux Script Management Wrapper - GUI Dashboard & Terminal Console UI."""
import os

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
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

# NOTE on the back-and-forth in this file's history:
# 1) Browsing /sdcard directly with Kivy's own FileChooserListView
#    (os.listdir) returns a completely EMPTY listing on Android 10+ for
#    any app targeting API 29+ -- scoped storage blocks raw directory
#    enumeration outright, storage permission granted or not.
# 2) plyer's SAF-backed picker (ACTION_GET_CONTENT) avoids that and can
#    resolve a real filesystem path -- but *reading* that resolved path
#    with plain open() still raises PermissionError on Android 10+,
#    confirmed on a real device, because our own process is still
#    scoped-storage-restricted regardless of which path string it holds.
# 3) androidstorage4kivy's Chooser hands back the original SAF content
#    reference (not a resolved path), and copy_from_shared() reads it via
#    ContentResolver under the picker's own permission grant, bypassing
#    scoped storage entirely -- but only for OUR OWN process. It is NOT a
#    way to hand files to Termux: publishing a file into a MediaStore
#    collection via copy_to_shared() and pointing Termux at that path
#    fails with "Permission denied" (confirmed on device) -- Termux
#    can't read entries our app owns in MediaStore, even though it has
#    full plain-filesystem access everywhere else (its own home dir, and
#    any plain path under /sdcard it creates itself). So:
#      - reading a picked file for OUR OWN purposes (schema parsing,
#        import detection) uses copy_from_shared() into our private cache.
#      - anything TERMUX needs to read or write (the script to run, its
#        saved config, the one-time setup command) is transferred as
#        base64-encoded CONTENT embedded in the shell command itself, so
#        Termux writes it into a plain path under its own control
#        (termux_bridge.LOG_DIR and friends) and never has to touch
#        anything our app inserted into MediaStore.
try:
    from androidstorage4kivy import Chooser, SharedStorage
except ImportError:
    Chooser = None
    SharedStorage = None

# Kept as a secondary picker for platforms/devices where the above isn't
# available (e.g. pre-scoped-storage Android): plyer resolves SAF picks to
# a real path directly, which works fine when nothing blocks reading it.
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
            text: "Grant Storage Access"
            on_release: root.request_storage_access()

        Button:
            text: "Setup Termux"
            on_release: root.setup_termux()

        Label:
            text: "Do both once before your first run"
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

        Button:
            id: script_label
            text: (root.script_name or "No script selected") + (" (tap for options)" if root.script_name else "")
            shorten: True
            shorten_from: "left"
            on_release: root.show_script_options()

    BoxLayout:
        size_hint_y: None
        height: dp(40)
        spacing: dp(8)

        TextInput:
            id: manual_path_input
            hint_text: "Or type the full path, e.g. /storage/emulated/0/Download/multi3.py"
            multiline: False

        Button:
            text: "Load Path"
            size_hint_x: None
            width: dp(110)
            on_release: root.load_manual_path(manual_path_input.text)

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
        self._readable_script_path = ""

        if ON_ANDROID and SharedStorage is not None and Chooser is not None:
            self.shared_storage = SharedStorage()
            self.chooser = Chooser(self._on_chooser_selection)
        else:
            self.shared_storage = None
            self.chooser = None

    # ---- One-time Termux setup -------------------------------------------
    def setup_termux(self):
        """Copy the setup command and bring Termux to the front.

        Termux refuses RUN_COMMAND intents from other apps until
        `allow-external-apps=true` is set inside its own private
        ~/.termux/termux.properties. No app can write that file for the
        user (that's Termux's whole point), so the best we can offer is:
        copy the command, open Termux, and let the user paste + Enter.
        (An earlier version tried publishing this as a script file via
        copy_to_shared() and pasting a short `bash "<path>"` instead, to
        dodge a paste-corruption glitch seen on one device -- but Termux
        can't read files published that way at all, confirmed separately,
        so that approach is out. The plain command is self-contained: it
        only ever touches Termux's own home directory.)
        """
        Clipboard.copy(termux_bridge.SETUP_COMMAND)
        self._append_output(
            "[setup] Command copied to clipboard.\n"
            "[setup] Termux is opening -- long-press to Paste, then press Enter.\n"
            "[setup] If the pasted line looks garbled/broken (stray characters "
            "at the start), clear it and type it by hand instead.\n"
            "[setup] You only need to do this once.\n"
        )
        try:
            termux_bridge.open_termux()
        except Exception as exc:
            self._append_output(f"{self._friendly_error(exc)}\n")

    # ---- Storage access (Android 11+ scoped storage) ----------------------
    def request_storage_access(self):
        """Guide the user to the one-tap "All files access" toggle.

        On Android 11+, plain WRITE/READ_EXTERNAL_STORAGE no longer allows
        opening arbitrary paths like a script picked from anywhere in
        shared storage. That requires the MANAGE_EXTERNAL_STORAGE special
        permission, which can only be granted through a Settings screen --
        no app can flip it for itself. Most flows no longer need this
        (they go through androidstorage4kivy/SAF instead), but the manual
        "Load Path" fallback still benefits from it on Android 11+.
        """
        if not ON_ANDROID:
            self._show_message("Storage access request only applies on Android.")
            return
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            activity = PythonActivity.mActivity
            package_name = activity.getPackageName()

            # Use the raw action string instead of the Settings.* constant:
            # pyjnius reflection doesn't reliably resolve that field on
            # every device/ROM even when the OS itself supports it.
            intent = Intent("android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION")
            intent.setData(Uri.parse(f"package:{package_name}"))
            activity.startActivity(intent)
            self._append_output(
                "[setup] Opening storage settings -- toggle 'Allow access to "
                "manage all files' on, then come back. One-time only.\n"
            )
        except Exception as exc:
            self._append_output(
                f"[warn] Direct toggle unavailable ({exc}).\n"
                "[warn] Opening this app's settings page instead -- look for "
                "'Files and media' / 'All files access' there.\n"
            )
            self._open_app_settings()

    def _open_app_settings(self):
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            activity = PythonActivity.mActivity
            package_name = activity.getPackageName()

            intent = Intent("android.settings.APPLICATION_DETAILS_SETTINGS")
            intent.setData(Uri.parse(f"package:{package_name}"))
            activity.startActivity(intent)
        except Exception as exc:
            self._append_output(f"[error] Could not open app settings either: {exc}\n")

    # ---- Script selection -------------------------------------------------
    def pick_script(self):
        if self.chooser is not None:
            self.chooser.choose_content("*/*")
            return
        if filechooser is not None:
            try:
                filechooser.open_file(on_selection=self._on_file_selected, path="/sdcard")
            except Exception as exc:
                self._append_output(f"{self._friendly_error(exc)}\n")
            return
        self._show_message("No file picker available -- use 'Load Path' below instead.")

    def _on_chooser_selection(self, shared_file_list):
        if not shared_file_list:
            return
        Clock.schedule_once(lambda dt: self._handle_picked_file(shared_file_list[0]))

    def _handle_picked_file(self, shared_file):
        """Read a SAF-picked file into our own private cache via
        androidstorage4kivy. That private copy is used for everything OUR
        OWN process needs to do with it (schema parsing, import
        detection) AND is what gets read fresh and embedded as content
        when Termux needs to run it -- see the module-level NOTE above the
        imports for why we don't hand Termux a path instead.
        """
        try:
            private_path = self.shared_storage.copy_from_shared(shared_file)
        except Exception as exc:
            self._append_output(f"{self._friendly_error(exc)}\n")
            return
        if not private_path:
            self._append_output("[File Error] Could not read the picked file (no data returned).\n")
            return

        filename = os.path.basename(private_path)
        if not filename.lower().endswith(".py"):
            self._show_message(f"'{filename}' is not a .py file. Pick a Python script.")
            return

        try:
            schema = schema_engine.load_schema_from_file(private_path)
        except schema_engine.SchemaError as exc:
            self._show_message(self._friendly_error(exc))
            return

        self._apply_loaded_script(schema, private_path, filename)

    def load_manual_path(self, path):
        path = path.strip()
        if not path:
            self._show_message("Type a full file path first.")
            return
        self._load_script_from_plain_path(path)

    def _on_file_selected(self, selection):
        if not selection:
            return
        # plyer's callback can fire off the main thread; hop back onto it.
        Clock.schedule_once(lambda dt: self._load_script_from_plain_path(selection[0]))

    def _load_script_from_plain_path(self, path):
        """Used by the manual path box and the plyer fallback picker: reads
        the same plain path directly, no shared-storage copy. Works when
        the device doesn't enforce scoped storage, or when 'Grant Storage
        Access' (MANAGE_EXTERNAL_STORAGE) has been granted.
        """
        if not path.lower().endswith(".py"):
            self._show_message(f"'{os.path.basename(path)}' is not a .py file. Pick a Python script.")
            return

        try:
            schema = schema_engine.load_schema_from_file(path)
        except schema_engine.SchemaError as exc:
            self._show_message(self._friendly_error(exc))
            return

        self._apply_loaded_script(schema, path, os.path.basename(path))

    def _apply_loaded_script(self, schema, readable_path, display_name):
        self.schema = schema
        self.script_path = readable_path
        self.script_name = display_name
        self._readable_script_path = readable_path

        saved_values = self._load_saved_config()
        self._build_form(saved_values)

        self._append_output(f"[schema] Loaded '{self.schema.get('name')}' "
                             f"({len(schema_engine.get_fields(self.schema))} fields, "
                             f"{len(schema_engine.get_packages(self.schema))} packages)\n")
        if saved_values:
            self._append_output(f"[config] Restored previously saved settings for '{self.script_name}'.\n")

    # ---- Per-script config: load, reset -----------------------------
    def _load_saved_config(self):
        """Best-effort read of this script's previously saved settings.

        Termux owns this file (plain path under termux_bridge.CONFIGS_DIR,
        written by save_config()'s embedded-content command) -- our own
        process reading it directly can fail silently on Android 10+
        scoped storage, in which case this just returns {} and the form
        starts from schema defaults. Save Config itself is unaffected
        either way: it doesn't depend on this read succeeding.
        """
        if not self.script_name:
            return {}
        config_path = termux_bridge.config_path_for(self.script_name)
        return schema_engine.load_json_file(config_path)

    def show_script_options(self):
        if not self.script_path:
            return

        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        content.add_widget(Label(text=self.script_name))

        button_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        reset_btn = Button(text="Reset/Delete Saved Settings")
        cancel_btn = Button(text="Cancel")
        button_row.add_widget(reset_btn)
        button_row.add_widget(cancel_btn)
        content.add_widget(button_row)

        popup = Popup(title="Script Options", content=content, size_hint=(0.85, 0.35))

        def do_reset(_instance):
            popup.dismiss()
            self.reset_config()

        reset_btn.bind(on_release=do_reset)
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        popup.open()

    def reset_config(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return

        config_path = termux_bridge.config_path_for(self.script_name)
        try:
            if os.path.isfile(config_path):
                os.remove(config_path)
        except OSError:
            pass  # scoped storage may block this from our side; harmless

        self._build_form({})
        self._append_output(
            f"[config] Reset form to defaults for '{self.script_name}' "
            f"(the saved file at {config_path} will be overwritten next Save Config).\n"
        )

    # ---- Dynamic form -------------------------------------------------
    def _build_form(self, saved_values=None):
        saved_values = saved_values or {}
        grid = self.ids.form_grid
        grid.clear_widgets()
        self.field_widgets = {}

        fields = schema_engine.get_fields(self.schema)

        if not fields:
            grid.add_widget(Label(text="No configurable options", size_hint_y=None, height=44))
            grid.add_widget(Label(text="", size_hint_y=None, height=44))
            return

        for field in fields:
            label_text = field["label"] + (" *" if field.get("required") else "")
            grid.add_widget(Label(text=label_text, size_hint_y=None, height=44))
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

    def _missing_required_fields(self):
        missing = []
        for _key, (field, widget) in self.field_widgets.items():
            if not field.get("required"):
                continue
            if field["type"] == "boolean":
                continue  # a checkbox has no "empty" state
            if not str(widget.text).strip():
                missing.append(field["label"])
        return missing

    # ---- Actions -------------------------------------------------
    def save_config(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return False

        missing = self._missing_required_fields()
        if missing:
            self._append_output(
                f"[Config Error] Required setting field empty: {', '.join(missing)}. "
                f"Please configure before running.\n"
            )
            return False

        values = self._collect_form_values()
        config_path = termux_bridge.config_path_for(self.script_name)
        self._append_output(f"[config] Saving settings to {config_path} via Termux...\n")
        self._run_bridge_action(lambda: termux_bridge.save_config(self.script_name, values))
        return True

    def install_packages(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        if not self._check_termux_ready():
            return

        declared = schema_engine.get_packages(self.schema)
        detected = []
        if self._readable_script_path:
            detected = schema_engine.detect_imports(self._readable_script_path)
        packages = sorted(set(declared) | set(detected))

        note = ""
        new_from_detection = sorted(set(detected) - set(declared))
        if new_from_detection:
            note = f" (auto-detected from imports: {', '.join(new_from_detection)})"
        self._append_output(
            f"[install] Requesting install of: {', '.join(packages) or '(none found)'}{note}\n"
        )

        self._run_bridge_action(lambda: termux_bridge.install_packages(packages))
        self._start_polling(termux_bridge.read_install_log)

    def run_script(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        if not self._check_termux_ready():
            return
        if not self.save_config():
            return

        try:
            with open(self._readable_script_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            self._append_output(f"{self._friendly_error(exc)}\n")
            return

        filename = os.path.basename(self._readable_script_path)
        self._append_output(f"[run] Launching {self.script_name} in Termux...\n")
        self._run_bridge_action(
            lambda: termux_bridge.run_script_from_content(content, filename)
        )
        self._start_polling(termux_bridge.read_run_log)

    def _check_termux_ready(self):
        """Verify Termux is actually installed before firing a command at
        it. Doesn't (can't) verify 'allow-external-apps=true' is set --
        Termux gives no API for that -- but this rules out one concrete
        failure mode with a clear message instead of a command that just
        goes nowhere.
        """
        if not ON_ANDROID:
            return True
        if termux_bridge.is_termux_installed():
            return True
        self._append_output(
            "[Termux Error] Termux is not installed on this device. Install "
            "it from F-Droid or GitHub Releases (not Play Store).\n"
        )
        return False

    def _run_bridge_action(self, action):
        # Broad except is deliberate: pyjnius/Android calls can raise all
        # sorts of exception types (JavaException, AttributeError from a
        # reflection miss, etc.), not just RuntimeError. An earlier
        # version only caught RuntimeError here, so any other exception
        # type from a Termux call went uncaught and crashed the whole app
        # instead of just showing an error line.
        try:
            action()
        except Exception as exc:
            self._append_output(f"{self._friendly_error(exc)}\n")

    def copy_output(self):
        Clipboard.copy(self.output_text)
        self._append_output("[info] Output copied to clipboard.\n")

    # ---- Human-friendly diagnostics -----------------------------------
    def _friendly_error(self, exc):
        """Translate a raw exception into the short, actionable messages
        asked for, instead of a backend traceback/repr the user can't act
        on. Falls back to the raw message, prefixed, for anything unknown
        -- so unexpected problems are still visible enough to report back.
        """
        text = str(exc)
        lowered = text.lower()

        if isinstance(exc, schema_engine.SchemaError) or isinstance(exc, (PermissionError, FileNotFoundError)) \
                or "permission denied" in lowered or "errno 13" in lowered or "enoent" in lowered:
            return "[File Error] Selected path does not exist or storage permission denied."

        if "activitynotfoundexception" in lowered or "run_command" in lowered or "runcommandservice" in lowered:
            return (
                "[Termux Error] Termux didn't accept the command. Make sure "
                "'Setup Termux' has been run (allow-external-apps) and Termux "
                "itself has storage access (run 'termux-setup-storage' in Termux)."
            )

        return f"[Error] {text}"

    # ---- Output polling -------------------------------------------------
    def _start_polling(self, reader, silence_timeout_s=16):
        """Poll `reader` every 2s for fresh output.

        `reader` reads a plain path Termux wrote to directly (see
        termux_bridge.read_log's docstring) -- this can come back empty on
        Android 10+ scoped storage even once Termux has genuinely finished,
        since our own process may simply be unable to see the file. If
        nothing at all comes back within `silence_timeout_s`, surface the
        most likely reason (Termux never accepted the command) while being
        clear that checking Termux directly is always the reliable option.
        """
        if self._poll_event:
            self._poll_event.cancel()

        state = {"ticks": 0, "got_output": False, "hinted": False}
        max_ticks = max(1, silence_timeout_s // 2)

        def poll(_dt):
            content = reader()
            if content and content != self.output_text:
                self.output_text = content
                state["got_output"] = True
                return

            if state["got_output"] or state["hinted"]:
                return

            state["ticks"] += 1
            if state["ticks"] >= max_ticks:
                state["hinted"] = True
                self._append_output(
                    f"[Termux Error] No output visible here after {silence_timeout_s}s. "
                    "Either Termux hasn't accepted the command (confirm 'Setup Termux' was "
                    "pasted + Enter inside Termux, not just tapped), or this device's storage "
                    "permissions won't let this app read Termux's output file even though "
                    "Termux is working fine -- open Termux directly to check either way.\n"
                )

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
