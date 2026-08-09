"""Termux Script Management Wrapper - GUI Dashboard & Terminal Console UI."""
import os

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
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
#      - reading a picked file for OUR OWN purposes (import detection)
#        uses copy_from_shared() into our private cache.
#      - anything TERMUX needs to read or write (the script to run, its
#        saved presets, the one-time setup command) is transferred as
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

        Label:
            text: root.script_name or "No script selected"
            shorten: True
            shorten_from: "left"

    BoxLayout:
        size_hint_y: None
        height: dp(40)
        canvas.before:
            Color:
                rgba: root.status_color
            Rectangle:
                pos: self.pos
                size: self.size

        Label:
            id: status_label
            text: root.status_text
            bold: True
            shorten: True
            shorten_from: "right"
            text_size: self.width, None

    BoxLayout:
        size_hint_y: None
        height: dp(44)
        spacing: dp(8)

        Label:
            text: "Preset:"
            size_hint_x: None
            width: dp(60)

        Spinner:
            id: preset_spinner
            text: root.selected_preset
            values: root.preset_names
            on_text: root.on_preset_selected(self.text)

    ScrollView:
        size_hint_y: None
        height: dp(220)
        canvas.before:
            Color:
                rgba: 0.12, 0.12, 0.12, 1
            Rectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            id: step_panel
            orientation: "vertical"
            spacing: dp(6)
            padding: dp(6)
            size_hint_y: None
            height: self.minimum_height

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
            text: root.run_button_text
            on_release: root.on_run_stop_pressed()

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

    BoxLayout:
        size_hint_y: None
        height: dp(40)
        spacing: dp(6)

        Button:
            text: "Copy Success Result"
            on_release: root.copy_success_result()

        Button:
            text: "Copy Fail Result"
            on_release: root.copy_fail_result()

    TextInput:
        id: output_label
        text: root.output_text
        readonly: True
        multiline: True
        background_color: 0, 0, 0, 1
        foreground_color: 0, 1, 0, 1
        cursor_color: 0, 1, 0, 1
        padding: dp(6), dp(6)
"""


class RootWidget(BoxLayout):
    MANUAL_LABEL = "Manual (no preset)"

    script_path = StringProperty("")
    script_name = StringProperty("")
    output_text = StringProperty("Output will appear here after you run a script.\n")
    status_text = StringProperty("Ready.")
    status_color = ListProperty([0.25, 0.25, 0.25, 1])
    run_button_text = StringProperty("Run Script")
    preset_names = ListProperty(["Manual (no preset)"])
    selected_preset = StringProperty("Manual (no preset)")

    STATUS_COLORS = {
        "info": [0.16, 0.32, 0.5, 1],
        "success": [0.13, 0.45, 0.2, 1],
        "error": [0.55, 0.14, 0.14, 1],
        "warn": [0.55, 0.4, 0.08, 1],
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.declared_packages = []
        self._presets = {}
        self._poll_event = None
        self._readable_script_path = ""
        self._current_filename = ""
        self._script_running = False
        self._last_result_content = ""
        self._last_result_kind = None
        # Interactive-run state: what's been printed since the last
        # answer, how many quiet polling ticks that's been sitting for,
        # whether the step UI is currently up, and every answer given so
        # far this run (so Save Config can turn them into a preset).
        self._pending_chunk = ""
        self._shown_chunk_len = 0
        self._quiet_ticks = 0
        self._awaiting_input = False
        self._collected_answers = []

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
        no app can flip it for itself.
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
        self._show_message("No file picker available on this device.")

    def _on_chooser_selection(self, shared_file_list):
        if not shared_file_list:
            return
        Clock.schedule_once(lambda dt: self._handle_picked_file(shared_file_list[0]))

    def _handle_picked_file(self, shared_file):
        """Read a SAF-picked file into our own private cache via
        androidstorage4kivy. That private copy is used for everything OUR
        OWN process needs to do with it (import detection for Install
        Packages) AND is what gets read fresh and embedded as content
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

        self._apply_loaded_script(private_path, filename)

    def _on_file_selected(self, selection):
        if not selection:
            return
        path = selection[0]
        # plyer's callback can fire off the main thread; hop back onto it.
        Clock.schedule_once(lambda dt: self._load_script_from_plain_path(path))

    def _load_script_from_plain_path(self, path):
        """Used by the plyer fallback picker: reads the same plain path
        directly, no shared-storage copy. Works when the device doesn't
        enforce scoped storage, or when 'Grant Storage Access'
        (MANAGE_EXTERNAL_STORAGE) has been granted.
        """
        if not path.lower().endswith(".py"):
            self._show_message(f"'{os.path.basename(path)}' is not a .py file. Pick a Python script.")
            return
        self._apply_loaded_script(path, os.path.basename(path))

    def _apply_loaded_script(self, readable_path, display_name):
        self.script_path = readable_path
        self.script_name = display_name
        self._readable_script_path = readable_path
        self._script_running = False
        self.run_button_text = "Run Script"
        self._collected_answers = []
        self._pending_chunk = ""
        self._shown_chunk_len = 0
        self._quiet_ticks = 0
        self._awaiting_input = False
        self._clear_step_panel()

        try:
            schema = schema_engine.load_schema_from_file(readable_path)
            self.declared_packages = schema_engine.get_packages(schema)
        except schema_engine.SchemaError:
            self.declared_packages = []

        self._presets = self._load_presets()
        self.preset_names = [self.MANUAL_LABEL] + sorted(self._presets.keys())
        self.selected_preset = self.MANUAL_LABEL

        self._append_output(
            f"[script] Loaded '{self.script_name}'. Run Script executes it live "
            "inside Termux -- if/when it asks a question, this app watches its "
            "actual output and shows buttons or an answer box for it right here, "
            "as it happens.\n"
        )
        if self._presets:
            self._append_output(
                f"[config] Found {len(self._presets)} saved preset(s) for this "
                "script -- pick one from the Preset dropdown to auto-run without "
                "answering by hand (it'll still ask you for anything beyond what "
                "the preset covers).\n"
            )

    def _load_presets(self):
        """Best-effort read of this script's saved presets.

        Termux owns this file (plain path under termux_bridge.CONFIGS_DIR,
        written by save_config()'s embedded snippet) -- our own process
        reading it directly can fail silently on Android 10+ scoped
        storage, in which case this just returns {} and only "Manual"
        shows up in the dropdown.
        """
        if not self.script_name:
            return {}
        path = termux_bridge.presets_path_for(self.script_name)
        data = schema_engine.load_json_file(path)
        return data if isinstance(data, dict) else {}

    def on_preset_selected(self, value):
        self.selected_preset = value

    # ---- Actions -------------------------------------------------
    def save_config(self):
        """Persist the answers given during the last/current interactive
        run as a named, reusable preset."""
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        if not self._check_termux_ready():
            return
        if not self._collected_answers:
            self._show_message(
                "No answers recorded yet -- run the script and answer at least "
                "one prompt first, then Save Config."
            )
            return
        self._prompt_preset_name()

    def _prompt_preset_name(self):
        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        content.add_widget(Label(
            text=f"Save this run's {len(self._collected_answers)} answer(s) as a preset named:",
            size_hint_y=None, height=dp(30),
        ))
        default_name = self.selected_preset if self.selected_preset != self.MANUAL_LABEL else ""
        name_input = TextInput(text=default_name, multiline=False, size_hint_y=None, height=dp(44))
        content.add_widget(name_input)
        btn_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        cancel_btn = Button(text="Cancel")
        ok_btn = Button(text="Save")
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(ok_btn)
        content.add_widget(btn_row)
        popup = Popup(title="Save Config", content=content, size_hint=(0.85, 0.35))

        def _do_save(*_a):
            name = name_input.text.strip()
            if not name:
                self._show_message("Enter a preset name.")
                return
            popup.dismiss()
            answers = list(self._collected_answers)
            self._append_output(f"[config] Saving preset '{name}' ({len(answers)} answer(s)) via Termux...\n")
            self._set_status("Sending Save Config to Termux...", "info")
            if self._run_bridge_action(lambda: termux_bridge.save_preset(self.script_name, name, answers)):
                self._presets[name] = answers
                if name not in self.preset_names:
                    self.preset_names = self.preset_names + [name]
                self.selected_preset = name
                self._set_status(f"Preset '{name}' saved.", "success")

        ok_btn.bind(on_release=_do_save)
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        popup.open()

    def install_packages(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        if not self._check_termux_ready():
            return

        detected = []
        if self._readable_script_path:
            detected = schema_engine.detect_imports(self._readable_script_path)
        packages = sorted(set(self.declared_packages) | set(detected))

        note = ""
        new_from_detection = sorted(set(detected) - set(self.declared_packages))
        if new_from_detection:
            note = f" (auto-detected from imports: {', '.join(new_from_detection)})"
        self._append_output(
            f"[install] Requesting install of: {', '.join(packages) or '(none found)'}{note}\n"
        )

        self._set_status("Sending Install Packages command to Termux...", "info")
        if not self._run_bridge_action(lambda: termux_bridge.install_packages(packages)):
            return
        self._set_status("Sent -- waiting for Termux output...", "info")
        self._start_polling(termux_bridge.read_install_log)

    def on_run_stop_pressed(self):
        if self._script_running:
            self.stop_script()
        else:
            self.run_script()

    def stop_script(self):
        """Best-effort: send a kill for whatever Run Script last started
        (and its FIFO feeder). Safe to press even if the script already
        finished on its own -- termux_bridge.build_stop_command() just
        reports that instead.
        """
        self._append_output("[run] Sending stop signal to Termux...\n")
        filename = self._current_filename or os.path.basename(self._readable_script_path or "")
        self._run_bridge_action(lambda: termux_bridge.stop_script(filename))
        self._script_running = False
        self.run_button_text = "Run Script"
        self._awaiting_input = False
        self._show_finished_notice("Script Stopped")
        if self._poll_event:
            self._poll_event.cancel()
        self._set_status("Stop signal sent.", "warn")

    def run_script(self):
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        if not self._check_termux_ready():
            return

        try:
            with open(self._readable_script_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            msg = self._friendly_error(exc)
            self._append_output(f"{msg}\n")
            self._set_status(msg, "error")
            return

        filename = os.path.basename(self._readable_script_path)
        self._current_filename = filename

        preset_answers = None
        if self.selected_preset and self.selected_preset != self.MANUAL_LABEL:
            preset_answers = list(self._presets.get(self.selected_preset, []))
            self._append_output(
                f"[run] Using preset '{self.selected_preset}' -- {len(preset_answers)} "
                "answer(s) will be sent automatically. If the script asks for more "
                "than that, this app will prompt you for the rest live.\n"
            )

        self._collected_answers = []
        self._pending_chunk = ""
        self._shown_chunk_len = 0
        self._quiet_ticks = 0
        self._awaiting_input = False
        self._clear_step_panel()

        self._append_output(f"[run] Launching {self.script_name} in Termux...\n")
        self._set_status("Sending Run Script command to Termux...", "info")
        if not self._run_bridge_action(
            lambda: termux_bridge.run_script_interactive(
                content, filename, preset_answers=preset_answers,
            )
        ):
            return
        self._script_running = True
        self.run_button_text = "Stop Script"
        self._set_status("Sent -- waiting for Termux output...", "info")
        self._start_polling(termux_bridge.read_run_log, silence_timeout_s=30, interactive=True)

    # ---- Interactive step UI --------------------------------------------
    def _clear_step_panel(self):
        panel = self.ids.get("step_panel") if hasattr(self, "ids") else None
        if panel is not None:
            panel.clear_widgets()

    def _show_step_ui(self, chunk):
        """Build buttons/inputs from whatever the script printed since the
        last answer (see schema_engine.parse_menu_options/last_prompt_line)
        -- these read the script's REAL rendered output, so it doesn't
        matter how the script's source built that text (colour codes,
        f-strings, a loop, argparse, anything)."""
        self._awaiting_input = True
        # Remember exactly how much of _pending_chunk this UI was built
        # from -- if the script prints its NEXT prompt before the user
        # taps an answer to THIS one (a fast retry-validation loop, e.g.
        # "Invalid selection, try again"), that new text keeps
        # accumulating past this point. _submit_answer must only consume
        # the part shown here, not wipe out that already-arrived next
        # prompt too -- losing it meant the step UI could go blank
        # forever with nothing left to detect the next prompt from.
        self._shown_chunk_len = len(chunk)
        prompt = schema_engine.last_prompt_line(chunk)
        options, raw_values = schema_engine.parse_menu_options(chunk)

        panel = self.ids.step_panel
        panel.clear_widgets()

        if prompt:
            panel.add_widget(self._step_label(f"Waiting: {prompt}"))

        if options:
            # A real menu was detected in the script's own output --
            # tapping one of these IS the answer, so the generic
            # manual text box/Send row (and the chips, which only ever
            # make sense for a free-typed number/proxy value) stay
            # hidden entirely instead of sitting there unused beneath
            # the buttons.
            for opt, raw in zip(options, raw_values):
                btn = Button(text=opt, size_hint_y=None, height=dp(40))
                btn.bind(on_release=lambda *_a, v=raw: self._submit_answer(v))
                panel.add_widget(btn)
            return

        answer_input = TextInput(multiline=False, size_hint_y=None, height=dp(40))

        quick_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        if schema_engine.prompt_wants_number_chips(prompt):
            for chip in ("1", "5", "10", "25"):
                cbtn = Button(text=chip, size_hint_x=None, width=dp(48))
                cbtn.bind(on_release=lambda *_a, v=chip: self._submit_answer(v))
                quick_row.add_widget(cbtn)
        if schema_engine.prompt_wants_paste_button(prompt):
            paste_btn = Button(text="Paste", size_hint_x=None, width=dp(70))
            paste_btn.bind(on_release=lambda *_a: setattr(answer_input, "text", Clipboard.paste()))
            quick_row.add_widget(paste_btn)
        if quick_row.children:
            panel.add_widget(quick_row)

        send_btn = Button(text="Send", size_hint_x=None, width=dp(70))
        send_btn.bind(on_release=lambda *_a: self._submit_answer(answer_input.text))
        input_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        input_row.add_widget(answer_input)
        input_row.add_widget(send_btn)
        panel.add_widget(input_row)

    def _step_label(self, text):
        label = Label(text=text, size_hint_y=None, height=dp(30), halign="left",
                       valign="middle", color=(1, 0.85, 0.3, 1))

        def _sync_text_size(instance, value):
            instance.text_size = (value, None)

        def _sync_height(instance, value):
            instance.height = max(dp(24), value[1])

        label.bind(width=_sync_text_size, texture_size=_sync_height)
        return label

    def _submit_answer(self, value):
        value = str(value)
        self._collected_answers.append(value)
        self._append_output(f"[you] {value}\n")
        self._run_bridge_action(lambda: termux_bridge.send_answer(self._current_filename, value))
        self._awaiting_input = False
        # Keep anything that arrived AFTER the snapshot the current step
        # UI was built from (see _show_step_ui) instead of discarding the
        # whole buffer -- that leftover is the start of the script's NEXT
        # prompt, already in hand.
        self._pending_chunk = self._pending_chunk[self._shown_chunk_len:]
        self._shown_chunk_len = 0
        self._quiet_ticks = 0
        self._clear_step_panel()

    def _show_finished_notice(self, text):
        panel = self.ids.get("step_panel") if hasattr(self, "ids") else None
        if panel is None:
            return
        panel.clear_widgets()
        panel.add_widget(self._step_label(text))

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
            msg = self._friendly_error(exc)
            self._append_output(f"{msg}\n")
            # The friendly message above deliberately hides the raw
            # exception text behind a canned, actionable sentence -- but
            # that means real diagnosis of *why* Termux rejected a given
            # command (a SecurityException with a specific permission
            # name, vs. a plain timing issue) has no evidence to go on.
            # Logging the raw text too costs nothing for a normal user
            # (it's just more lines in a log box they can ignore) and
            # turns the next report of this into something fixable
            # instead of another guess.
            detail = str(exc).strip()
            if detail:
                self._append_output(f"[Debug] {type(exc).__name__}: {detail}\n")
            self._set_status(msg, "error")
            return False
        return True

    def _set_status(self, text, kind="info"):
        self.status_text = text
        self.status_color = self.STATUS_COLORS.get(kind, self.STATUS_COLORS["info"])

    def copy_output(self):
        Clipboard.copy(self.output_text)
        self._append_output("[info] Output copied to clipboard.\n")

    def copy_success_result(self):
        """Copy just the last successful Termux result (Install Packages
        or Run Script output classified as "success"), not the whole
        mixed log -- see _last_result_content in _start_polling.poll()."""
        if self._last_result_kind == "success" and self._last_result_content:
            Clipboard.copy(self._last_result_content)
            self._append_output("[info] Copied the last successful result to clipboard.\n")
        else:
            self._show_message("No successful result to copy yet.")

    def copy_fail_result(self):
        """Copy just the last failed/error Termux result."""
        if self._last_result_kind in ("error", "warn") and self._last_result_content:
            Clipboard.copy(self._last_result_content)
            self._append_output("[info] Copied the last failed result to clipboard.\n")
        else:
            self._show_message("No failed result to copy yet.")

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
    def _start_polling(self, reader, silence_timeout_s=16, interactive=False):
        """Poll `reader` every 2s for fresh output.

        `reader` reads a plain path Termux wrote to directly (see
        termux_bridge.read_log's docstring) -- this can come back empty on
        Android 10+ scoped storage even once Termux has genuinely finished,
        since our own process may simply be unable to see the file.

        When `interactive` is set (Run Script, not Install Packages): a
        stretch with no new output is treated as "the script is probably
        blocked on its next prompt" and triggers the step UI (see
        _show_step_ui) from whatever's been printed since the last
        answer, instead of just showing a generic "no output" warning.
        Termux appends termux_bridge.DONE_MARKER once the script's
        process has actually exited, which is how this tells "finished"
        apart from "quiet for a moment".
        """
        if self._poll_event:
            self._poll_event.cancel()

        state = {"ticks": 0, "got_output": False, "hinted": False, "last_content": ""}
        max_ticks = max(1, silence_timeout_s // 2)

        def finish_run():
            self._script_running = False
            self.run_button_text = "Run Script"
            self._awaiting_input = False
            self._show_finished_notice("Script Finished")
            if self._poll_event:
                self._poll_event.cancel()
            note = (
                f" {len(self._collected_answers)} answer(s) were given this run -- "
                "tap Save Config to store them as a reusable preset."
                if self._collected_answers else ""
            )
            self._append_output(f"[run] Script process has exited.{note}\n")

        def poll(_dt):
            content = reader()
            if content and content != state["last_content"]:
                # Termux's `tee` truncates and rewrites the log file fresh
                # each run, so `content` usually grows on each poll while
                # the command is still running. Append only the new
                # suffix instead of replacing self.output_text wholesale.
                if content.startswith(state["last_content"]):
                    new_part = content[len(state["last_content"]):]
                else:
                    new_part = content
                state["last_content"] = content
                state["got_output"] = True

                done = interactive and termux_bridge.DONE_MARKER in new_part
                visible_part = new_part.replace(termux_bridge.DONE_MARKER, "") if done else new_part
                if visible_part.strip():
                    # The terminal box should show the same text a human
                    # would see in a real terminal, not raw escape bytes
                    # like "\x1b[1;92m" -- parse_menu_options/
                    # last_prompt_line already strip ANSI themselves when
                    # reading _pending_chunk for menu detection, so the
                    # raw (uncleaned) text is still what gets accumulated
                    # there; only the *displayed* copy needs cleaning.
                    clean_part = schema_engine.strip_ansi(visible_part)
                    self._append_output(clean_part if clean_part.endswith("\n") else clean_part + "\n")
                    if interactive:
                        self._pending_chunk += visible_part
                        self._quiet_ticks = 0

                status_text, kind = self._classify_output(content)
                self._set_status(status_text, kind)
                # Track the raw Termux result separately from output_text
                # (which also has our own [schema]/[run]/etc lines mixed
                # in) so Copy Success/Fail Result can grab just the
                # actual result instead of the whole noisy log.
                self._last_result_content = content
                self._last_result_kind = kind

                if done:
                    finish_run()
                return

            if not interactive:
                if state["got_output"] or state["hinted"]:
                    return
                state["ticks"] += 1
                if state["ticks"] >= max_ticks:
                    state["hinted"] = True
                    msg = f"No output yet after {silence_timeout_s}s -- still checking automatically."
                    self._set_status(msg, "warn")
                    self._append_output(
                        f"[Termux] {msg} This box updates itself the moment Termux writes "
                        "anything, so if your script makes network calls it may simply "
                        "still be running -- no need to tap anything again. If this "
                        "never changes after a couple of minutes, confirm 'Setup Termux' "
                        "was pasted + Enter inside Termux (not just tapped).\n"
                    )
                return

            # Interactive mode: no new output this tick. Once it's been
            # quiet for a couple of ticks (~4s) and there's something
            # pending from before, treat that as the script blocking on
            # its next prompt and show buttons/an answer box for it.
            if self._awaiting_input:
                return
            self._quiet_ticks += 1
            if self._quiet_ticks == 2 and self._pending_chunk.strip():
                self._show_step_ui(self._pending_chunk)

        self._poll_event = Clock.schedule_interval(poll, 2)

    def _classify_output(self, content):
        """Cheap heuristic to turn raw tee'd output into a short status
        line -- doesn't try to be exhaustive, just catches the common
        pip-install and Python-traceback cases so the status banner says
        something more useful than "output received" most of the time.
        """
        lower = content.lower()
        if "traceback (most recent call last)" in lower or "command not found" in lower:
            return "Finished with errors -- see Terminal Output below.", "error"
        if "successfully installed" in lower:
            return "Packages installed successfully.", "success"
        if "error" in lower or "failed" in lower:
            return "Finished, but output mentions an error -- check below.", "warn"
        return "Termux output received -- see Terminal Output below.", "success"

    def _append_output(self, text):
        self.output_text += text
        widget = self.ids.get("output_label") if hasattr(self, "ids") else None
        if widget is not None:
            # Auto-scroll the terminal box to the newest line, like a real
            # console tailing output, instead of leaving the view wherever
            # it happened to be scrolled before.
            Clock.schedule_once(
                lambda dt: setattr(widget, "cursor", widget.get_cursor_from_index(len(widget.text)))
            )

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
