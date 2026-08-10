"""Termux Script Management Wrapper - GUI Dashboard & Terminal Console UI."""
import json
import os
import shlex
import socket
import threading
import time

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
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

import preset_db
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

        Button:
            text: "Keep App Awake"
            on_release: root.request_battery_optimization_exemption()

        Label:
            text: "Do all three once before your first run"
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
        height: dp(44)
        spacing: dp(8)

        Label:
            text: "CLI Args:"
            size_hint_x: None
            width: dp(70)

        TextInput:
            id: extra_args_input
            multiline: False
            hint_text: "optional, e.g. 1 2 -- only if the script reads sys.argv"

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
            text: "Build Preset"
            on_release: root.open_preset_wizard()

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
        # Every step (prompt text + any detected menu options) the live
        # step UI has shown this run, in order -- see _show_step_ui.
        # Persisted as this script's schema once the run finishes (see
        # _finish_run) so the Preset Wizard can replay the same steps
        # later without running the script again.
        self._recorded_steps = []
        # Preset Wizard scratch state (see open_preset_wizard) -- only
        # populated while a wizard dialog sequence is actually open.
        self._wizard_steps = []
        self._wizard_answers = []
        self._manual_steps = []
        self._manual_schema = []
        # Set only by _offer_live_or_manual_build's "Run Live" choice --
        # tells _finish_run/stop_script to offer saving this run's
        # answers as a preset once it ends, instead of the normal
        # "tap Save Config yourself" flow.
        self._preset_build_mode = False
        # Preset answers still waiting to be auto-fed once the socket
        # channel confirms a prompt actually happened (see
        # _on_socket_prompt) -- the FIFO-fallback path pre-seeds the
        # same values a different way (preset_answers in
        # run_script_interactive), so this queue is only consulted when
        # a live socket connection is what's actually driving the run.
        self._preset_queue = []
        # Live IPC (primary channel -- see termux_bridge's wrapper
        # script and module docstring): a connected client's write-file
        # object, guarded by a lock since the accept/read loop runs on a
        # background thread while answers get sent from the main
        # (Kivy/UI) thread.
        self._socket_conn = None
        self._socket_active = False
        self._socket_lock = threading.Lock()
        # Lazily created by _acquire_wakelock on the first run -- a real
        # android.os.PowerManager.WakeLock object once on Android, stays
        # None (and every wakelock call is a no-op) off-Android.
        self._wakelock = None

        if ON_ANDROID and SharedStorage is not None and Chooser is not None:
            self.shared_storage = SharedStorage()
            self.chooser = Chooser(self._on_chooser_selection)
        else:
            self.shared_storage = None
            self.chooser = None

        self._start_socket_server()

    # ---- Live socket IPC (primary interactive channel) -------------------
    def _start_socket_server(self):
        """Listen on 127.0.0.1 for the wrapper script Termux runs (see
        termux_bridge._build_wrapper_source) to connect to. Two apps on
        the same Android device share one network namespace, so a
        loopback socket is reachable across the app boundary exactly
        like any other client/server pair -- no dependency on which
        filesystem anything lives on, unlike a FIFO. Runs for the whole
        app lifetime; each script run is just a fresh connection accepted
        in turn, since only one script runs at a time.

        Failing to bind (port in use, sockets unavailable for some
        reason) is non-fatal -- the wrapper's own fallback to a FIFO
        under Termux's home directory still works without this, just
        without instant prompt notifications (falls back to the
        polling/quiet-detection heuristic instead).
        """
        BIND_RETRIES = 3
        BIND_RETRY_DELAY = 0.5
        server = None
        last_exc = None
        for attempt in range(BIND_RETRIES):
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # SO_REUSEADDR: lets a fast app restart re-bind port 9988
                # immediately instead of failing with "Address already in
                # use" while the previous socket lingers in the OS's
                # TIME_WAIT/CLOSE_WAIT state.
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((termux_bridge.SOCKET_HOST, termux_bridge.SOCKET_PORT))
                server.listen(1)
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                try:
                    server.close()
                except (OSError, AttributeError):
                    pass
                server = None
                if attempt < BIND_RETRIES - 1:
                    time.sleep(BIND_RETRY_DELAY)
        if server is None:
            self._socket_server = None
            Clock.schedule_once(lambda dt: self._append_output(
                f"[Debug] Local socket server unavailable ({last_exc}) -- "
                "interactive runs will use the FIFO fallback only.\n"
            ))
            return
        self._socket_server = server
        threading.Thread(target=self._socket_accept_loop, daemon=True).start()

    def _socket_accept_loop(self):
        while True:
            try:
                conn, _addr = self._socket_server.accept()
            except OSError:
                return
            # errors="replace": a stray non-UTF-8 byte on either side must
            # never blow up this accept/read loop with a UnicodeDecodeError
            # -- that would silently kill live updates for the rest of the
            # run (falling back to slow log-polling) over a single bad
            # byte in one line of a script's output.
            rfile = conn.makefile("r", encoding="utf-8", newline="\n", errors="replace")
            wfile = conn.makefile("w", encoding="utf-8", newline="\n", errors="replace")
            with self._socket_lock:
                self._socket_conn = wfile
            self._socket_read_loop(rfile)
            with self._socket_lock:
                if self._socket_conn is wfile:
                    self._socket_conn = None

    def _socket_read_loop(self, rfile):
        """Runs on the background accept thread for the life of one
        connection -- every UI-facing action it triggers is marshalled
        onto the main thread via Clock.schedule_once, since Kivy widgets/
        properties are not thread-safe to touch directly."""
        while True:
            try:
                line = rfile.readline()
            except OSError:
                return
            if line == "":
                return
            line = line.rstrip("\n")
            if line.startswith("PROMPT:"):
                text = line[len("PROMPT:"):]
                Clock.schedule_once(lambda dt, t=text: self._on_socket_prompt(t))
            elif line.startswith("OUT:"):
                text = line[len("OUT:"):].replace("\x02", "\n")
                Clock.schedule_once(lambda dt, t=text: self._on_socket_output(t))
            elif line.startswith("ERR:"):
                payload = line[len("ERR:"):]
                Clock.schedule_once(lambda dt, p=payload: self._on_socket_error(p))
            elif line.startswith("DONE:"):
                Clock.schedule_once(lambda dt: self._on_socket_done())

    def _on_socket_output(self, text):
        """A print() from the running script, forwarded live -- same
        text the tee'd log file will also end up with, so this is purely
        a faster/more direct path to the same content; _start_polling's
        poll() skips its own append once self._socket_active is set, to
        avoid showing everything twice."""
        if not self._script_running:
            return
        self._socket_active = True
        clean = schema_engine.strip_ansi(text)
        self._append_output(clean if clean.endswith("\n") else clean + "\n")
        self._pending_chunk += text

    def _on_socket_prompt(self, prompt_text):
        """The wrapper's input() call has actually been reached -- this
        is an exact signal, not a guess from silence, so the step UI (or
        a preset auto-answer) fires immediately instead of waiting out
        the quiet-tick timer."""
        if not self._script_running:
            return
        self._socket_active = True
        if self._preset_queue:
            value = self._preset_queue.pop(0)
            self._append_output(f"[preset] Auto-answering: {value!r}\n")
            self._submit_answer(value)
            return
        chunk = self._pending_chunk if self._pending_chunk.strip() else prompt_text
        self._show_step_ui(chunk)

    def _on_socket_error(self, payload):
        """The wrapper's _report_fatal_error sent this after the target
        script raised something it never caught itself (missing
        dependency, any other runtime error) -- distinct from the script
        calling sys.exit() on its own, which the wrapper deliberately
        does NOT report here since that's routine control flow for a
        menu-driven CLI, not a failure. The human-readable traceback was
        already sent as normal OUT: text right before this and is already
        visible above in the terminal box; this just gives the status bar
        a clear, structured "this run failed" signal instead of leaving
        it to _classify_output's best-effort text sniffing.
        """
        if not self._script_running:
            return
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            data = {"type": "Error", "message": payload}
        self._set_status(
            f"Script crashed: {data.get('type', 'Error')} -- see Terminal Output below.",
            "error",
        )

    def _on_socket_done(self):
        self._finish_run()

    def _close_socket_conn(self):
        """Proactively drop the current connection instead of waiting for
        the OS to notice the Termux-side wrapper process died (Stop kills
        it, or it exits normally) -- otherwise a stale write-file object
        could briefly look connected (_socket_conn is not None) right as
        a NEW run starts, before the accept loop has had a chance to
        notice the old peer is gone and clear it itself."""
        with self._socket_lock:
            wfile = self._socket_conn
            self._socket_conn = None
        if wfile is not None:
            try:
                wfile.close()
            except OSError:
                pass
        self._socket_active = False

    def _send_via_socket(self, value):
        with self._socket_lock:
            wfile = self._socket_conn
        if wfile is None:
            return False
        try:
            wfile.write(value + "\n")
            wfile.flush()
            return True
        except OSError:
            with self._socket_lock:
                if self._socket_conn is wfile:
                    self._socket_conn = None
            return False

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
            # Some ROMs/Android versions don't ship an activity for the
            # direct MANAGE_APP_ALL_FILES_ACCESS_PERMISSION shortcut
            # (ActivityNotFoundException) -- this is an expected,
            # harmless fallback path on those devices, not a crash, so
            # the visible line stays calm; the raw exception still goes
            # to [Debug] in case it's ever something else.
            self._append_output(
                "[setup] That direct shortcut isn't available on this device -- "
                "opening this app's settings page instead. Look for 'Files and "
                "media' / 'All files access' there and enable it.\n"
                f"[Debug] {type(exc).__name__}: {exc}\n"
            )
            self._open_app_settings()

    def request_battery_optimization_exemption(self):
        """Ask the OS to stop applying battery-optimization throttling to
        THIS app -- a PARTIAL_WAKE_LOCK (see _acquire_wakelock) keeps the
        CPU from deep-sleeping mid-run, but it does nothing against an
        OEM task killer (Samsung's power management included) deciding to
        kill a recently-backgrounded app outright to save battery. This
        system dialog is the actual, user-grantable fix for that -- one
        tap, one time, same pattern as request_storage_access().
        """
        if not ON_ANDROID:
            self._show_message("Battery optimization exemption only applies on Android.")
            return
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            activity = PythonActivity.mActivity
            package_name = activity.getPackageName()

            intent = Intent("android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS")
            intent.setData(Uri.parse(f"package:{package_name}"))
            activity.startActivity(intent)
            self._append_output(
                "[setup] Opening battery optimization exemption request -- allow "
                "it so Android/your phone's power manager doesn't kill this app "
                "while a script is running in the background. One-time only.\n"
            )
        except Exception as exc:
            # Some ROMs don't ship this exact action (or Play Protect
            # policy variants reject it) -- same calm-fallback pattern as
            # request_storage_access(): send the user to the app's own
            # battery settings page instead of crashing/looking broken.
            self._append_output(
                "[setup] That direct shortcut isn't available on this device -- "
                "opening this app's settings page instead. Look for 'Battery' "
                "there and set it to 'Unrestricted'.\n"
                f"[Debug] {type(exc).__name__}: {exc}\n"
            )
            self._open_app_settings()

    def _acquire_wakelock(self):
        """Hold a PARTIAL_WAKE_LOCK for the duration of a script run --
        keeps the CPU running (screen can still turn off) so a run isn't
        silently paused mid-way if the screen times out while the user
        isn't actively tapping anything. Independent of, and weaker than,
        the battery-optimization exemption above (that's about the OS not
        killing the whole process; this is about the CPU not sleeping
        while it's still alive) -- both matter, neither substitutes for
        the other.
        """
        if not ON_ANDROID:
            return
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Context = autoclass("android.content.Context")
            PowerManager = autoclass("android.os.PowerManager")

            activity = PythonActivity.mActivity
            power_service = activity.getSystemService(Context.POWER_SERVICE)
            if self._wakelock is None:
                self._wakelock = power_service.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "ScriptWrapper::RunLock"
                )
            if not self._wakelock.isHeld():
                self._wakelock.acquire()
        except Exception as exc:
            self._append_output(f"[Debug] WakeLock unavailable ({type(exc).__name__}: {exc}) -- continuing without it.\n")

    def _release_wakelock(self):
        if self._wakelock is None:
            return
        try:
            if self._wakelock.isHeld():
                self._wakelock.release()
        except Exception:
            pass

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
        self._recorded_steps = []
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
        """This script's saved presets, from the app's own local sqlite
        database (preset_db.py) -- entirely local to this app's private
        storage, no Termux round-trip and no /sdcard scoped-storage
        quirks to fail silently against."""
        return preset_db.load_presets(self.script_name)

    def on_preset_selected(self, value):
        self.selected_preset = value

    # ---- Actions -------------------------------------------------
    def save_config(self):
        """Persist the answers given during the last/current interactive
        run as a named, reusable preset. Purely local (see preset_db.py)
        -- no Termux involved in saving a preset, only in the run that
        produced the answers being saved."""
        if not self.script_path:
            self._show_message("Select a script first.")
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
            preset_db.save_preset(self.script_name, name, answers)
            self._presets[name] = answers
            if name not in self.preset_names:
                self.preset_names = self.preset_names + [name]
            self.selected_preset = name
            self._append_output(f"[config] Preset '{name}' saved ({len(answers)} answer(s)).\n")
            self._set_status(f"Preset '{name}' saved.", "success")

        ok_btn.bind(on_release=_do_save)
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        popup.open()

    # ---- Preset Wizard: build a preset with zero script execution ------
    def open_preset_wizard(self):
        """Phase 1 of the Preset Wizard: build a named preset. Two paths
        depending on whether a schema exists yet for this script (see
        preset_db.save_schema):

        - A schema already exists (this script has been run
          interactively at least once, or a preset was already built
          manually before -- see _open_manual_step_builder) -- replay
          those exact steps as buttons/text boxes (_show_wizard_step),
          zero execution needed.
        - No schema yet -- offer a choice (_offer_live_or_manual_build)
          instead of guessing or forcing typed-only entry: run the
          script live through Termux (the same sandboxed run_script()
          this app already uses for Run Script -- real detected menu
          buttons, tap them exactly like a normal run, then the answers
          get offered as a preset once it's done), or type the steps in
          by hand without running anything at all.
        """
        if not self.script_path:
            self._show_message("Select a script first.")
            return
        steps = preset_db.load_schema(self.script_name)
        if not steps:
            self._offer_live_or_manual_build()
            return
        self._wizard_steps = steps
        self._wizard_answers = []
        self._show_wizard_step(0)

    def _offer_live_or_manual_build(self):
        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(10))
        content.add_widget(self._step_label(
            "No recorded steps yet for this script. Build this preset by "
            "actually running it once live through Termux -- the normal "
            "safe, sandboxed Run Script, with real menu buttons to tap -- "
            "or type the steps in by hand without running anything."
        ))
        btn_col = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(140), spacing=dp(8))
        live_btn = Button(text="Run Live via Termux (Recommended)")
        manual_btn = Button(text="Type Steps Manually")
        cancel_btn = Button(text="Cancel")
        btn_col.add_widget(live_btn)
        btn_col.add_widget(manual_btn)
        btn_col.add_widget(cancel_btn)
        content.add_widget(btn_col)
        popup = Popup(title="Build Preset", content=content, size_hint=(0.9, 0.55))

        def _go_live(*_a):
            popup.dismiss()
            if not self._check_termux_ready():
                return
            # This run must be answered live by the user tapping/typing,
            # not auto-fed from whatever preset happened to be selected
            # last -- the whole point is to observe (and record) real
            # prompts as they're actually answered.
            self.selected_preset = self.MANUAL_LABEL
            self._preset_build_mode = True
            self.run_script()

        def _go_manual(*_a):
            popup.dismiss()
            self._open_manual_step_builder()

        live_btn.bind(on_release=_go_live)
        manual_btn.bind(on_release=_go_manual)
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        popup.open()

    def _maybe_prompt_preset_save(self):
        """Called from both _finish_run and stop_script once a run
        started via 'Build Preset -> Run Live' (see
        _offer_live_or_manual_build) ends, one way or another. Reuses
        Save Config's own name-prompt popup -- a preset built this way
        isn't structurally different from one saved after a plain manual
        run, just triggered from a different button."""
        if not self._preset_build_mode:
            return
        self._preset_build_mode = False
        if self._collected_answers:
            self._prompt_preset_name()
        else:
            self._show_message(
                "No prompts were answered during that run -- nothing to "
                "save as a preset yet."
            )

    def _open_manual_step_builder(self):
        """No prior run means no auto-detected schema to replay -- build
        the preset from scratch instead, one step at a time, as either:

        - a Menu Step: define option names + the raw values the script
          expects for them (you already know your own script's menu),
          then TAP the one you want -- same tap-to-pick feel as the
          real-run wizard's buttons, without ever running the script.
        - a Text Step: type the answer value directly, for anything that
          isn't a menu (a count, a proxy string, etc).

        Also saves a matching schema alongside the preset (menu steps
        keep their full option list, text steps have options=None), so
        building a SECOND preset for this same never-run script goes
        through the normal replay wizard next time -- menu steps show
        as real tap-able buttons there too, not just here.
        """
        self._manual_steps = []
        self._manual_schema = []

        outer = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        popup = Popup(title="Build Preset -- No Run Yet", content=outer, size_hint=(0.92, 0.85))

        hint = Label(
            text=(
                "No recorded run for this script yet. Add each step in "
                "the exact order the script will ask for it -- a Menu "
                "Step if it's a numbered choice (define the options once, "
                "then tap the one you want), or a Text Step for anything "
                "else."
            ),
            size_hint_y=None, height=dp(74), halign="left", valign="top",
        )
        hint.bind(width=lambda inst, v: setattr(inst, "text_size", (v, None)))

        steps_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        steps_box.bind(minimum_height=steps_box.setter("height"))
        scroll = ScrollView(size_hint_y=1)
        scroll.add_widget(steps_box)

        def _refresh_list():
            steps_box.clear_widgets()
            for i, (value, entry) in enumerate(zip(self._manual_steps, self._manual_schema)):
                options = entry.get("options")
                if options:
                    raw_values = entry.get("raw_values") or []
                    label = options[raw_values.index(value)] if value in raw_values else value
                    text = f"Step {i + 1} (menu): {label} -> {value!r}"
                else:
                    text = f"Step {i + 1}: {value!r}"
                steps_box.add_widget(Label(
                    text=text, size_hint_y=None, height=dp(28),
                    halign="left", valign="middle",
                ))

        def _add_text_step(value):
            self._manual_steps.append(value)
            self._manual_schema.append(
                {"prompt": f"Step {len(self._manual_steps)}", "options": None, "raw_values": None}
            )
            _refresh_list()

        def _add_menu_step(options, raw_values, chosen_value):
            self._manual_steps.append(chosen_value)
            self._manual_schema.append({
                "prompt": f"Step {len(self._manual_steps)}",
                "options": options,
                "raw_values": raw_values,
            })
            _refresh_list()

        answer_input = TextInput(
            hint_text="Answer for the next step, e.g. 1", multiline=False,
            size_hint_y=None, height=dp(44),
        )

        def _add_step(*_a):
            value = answer_input.text.strip()
            if not value:
                self._show_message("Type a value before adding the step.")
                return
            _add_text_step(value)
            answer_input.text = ""

        add_btn = Button(text="Add Text Step", size_hint_x=None, width=dp(120))
        add_btn.bind(on_release=_add_step)

        menu_btn = Button(text="Add Menu Step", size_hint_y=None, height=dp(44))
        menu_btn.bind(on_release=lambda *_a: self._open_menu_step_definer(_add_menu_step))

        input_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        input_row.add_widget(answer_input)
        input_row.add_widget(add_btn)

        btn_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        cancel_btn = Button(text="Cancel")
        finish_btn = Button(text="Finish & Save")
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(finish_btn)

        def _finish(*_a):
            if not self._manual_steps:
                self._show_message("Add at least one step first.")
                return
            popup.dismiss()
            answers = list(self._manual_steps)
            schema = list(self._manual_schema)
            preset_db.save_schema(self.script_name, schema)
            self._wizard_answers = answers
            self._finish_wizard()

        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        finish_btn.bind(on_release=_finish)

        outer.add_widget(hint)
        outer.add_widget(scroll)
        outer.add_widget(input_row)
        outer.add_widget(menu_btn)
        outer.add_widget(btn_row)

        _refresh_list()
        popup.open()

    def _open_menu_step_definer(self, on_done):
        """Nested dialog opened by _open_manual_step_builder's 'Add Menu
        Step': define a menu's options (a display name plus the raw
        value the script actually expects for it, e.g. "Domain1" / "1"),
        then TAP the option you want as this preset's answer for the
        step -- exactly like tapping a real run's detected menu buttons,
        just built from what you already know about your own script
        instead of from a live run's output.

        `on_done(options, raw_values, chosen_value)` fires once a tap
        picks one -- the full option list is kept (not just the tapped
        one), so replaying this schema later for another preset offers
        every option as a real button too, not just this one choice.
        """
        pending_options = []
        pending_raw_values = []

        outer = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        popup = Popup(title="Define Menu Options", content=outer, size_hint=(0.92, 0.85))

        hint = Label(
            text=(
                "Add each option's name and the value the script expects "
                "for it (e.g. Domain1 / 1), one at a time. Once you've "
                "added them, tap the option you want for THIS preset."
            ),
            size_hint_y=None, height=dp(60), halign="left", valign="top",
        )
        hint.bind(width=lambda inst, v: setattr(inst, "text_size", (v, None)))

        options_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        options_box.bind(minimum_height=options_box.setter("height"))
        scroll = ScrollView(size_hint_y=1)
        scroll.add_widget(options_box)

        def _finalize(raw_value):
            popup.dismiss()
            on_done(list(pending_options), list(pending_raw_values), raw_value)

        def _refresh_options():
            options_box.clear_widgets()
            for label, raw in zip(pending_options, pending_raw_values):
                btn = Button(text=f"{label}  ({raw})", size_hint_y=None, height=dp(40))
                btn.bind(on_release=lambda *_a, v=raw: _finalize(v))
                options_box.add_widget(btn)

        label_input = TextInput(hint_text="Option name, e.g. Domain1", multiline=False,
                                 size_hint_y=None, height=dp(44))
        value_input = TextInput(hint_text="Value, e.g. 1", multiline=False,
                                 size_hint_y=None, height=dp(44))

        def _add_option(*_a):
            label = label_input.text.strip()
            value = value_input.text.strip()
            if not label or not value:
                self._show_message("Enter both an option name and its value.")
                return
            pending_options.append(label)
            pending_raw_values.append(value)
            label_input.text = ""
            value_input.text = ""
            _refresh_options()

        add_opt_btn = Button(text="Add Option", size_hint_y=None, height=dp(44))
        add_opt_btn.bind(on_release=_add_option)

        input_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        input_row.add_widget(label_input)
        input_row.add_widget(value_input)

        cancel_btn = Button(text="Cancel", size_hint_y=None, height=dp(44))
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())

        outer.add_widget(hint)
        outer.add_widget(scroll)
        outer.add_widget(input_row)
        outer.add_widget(add_opt_btn)
        outer.add_widget(cancel_btn)

        popup.open()

    def _show_wizard_step(self, index):
        if index >= len(self._wizard_steps):
            self._finish_wizard()
            return
        step = self._wizard_steps[index]
        prompt = step.get("prompt") or f"Step {index + 1}"
        options = step.get("options")
        raw_values = step.get("raw_values")

        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        content.add_widget(self._step_label(f"Step {index + 1}/{len(self._wizard_steps)}: {prompt}"))
        popup = Popup(title="Build Preset", content=content, size_hint=(0.9, 0.6))

        def _advance(value):
            self._wizard_answers.append(value)
            popup.dismiss()
            self._show_wizard_step(index + 1)

        if options:
            for opt, raw in zip(options, raw_values):
                btn = Button(text=opt, size_hint_y=None, height=dp(40))
                btn.bind(on_release=lambda *_a, v=raw: _advance(v))
                content.add_widget(btn)
        else:
            answer_input = TextInput(multiline=False, size_hint_y=None, height=dp(44))
            content.add_widget(answer_input)

            def _submit_text(*_a):
                value = answer_input.text.strip()
                if not value:
                    self._show_message("Enter a value before continuing.")
                    return
                _advance(value)

            next_btn = Button(text="Next", size_hint_y=None, height=dp(44))
            next_btn.bind(on_release=_submit_text)
            content.add_widget(next_btn)

        cancel_btn = Button(text="Cancel Wizard", size_hint_y=None, height=dp(36))
        cancel_btn.bind(on_release=lambda *_a: popup.dismiss())
        content.add_widget(cancel_btn)
        popup.open()

    def _finish_wizard(self):
        answers = list(self._wizard_answers)
        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        content.add_widget(Label(
            text=f"All {len(answers)} step(s) answered. Save this as a preset named:",
            size_hint_y=None, height=dp(30),
        ))
        name_input = TextInput(multiline=False, size_hint_y=None, height=dp(44))
        content.add_widget(name_input)
        btn_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        cancel_btn = Button(text="Cancel")
        ok_btn = Button(text="Save")
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(ok_btn)
        content.add_widget(btn_row)
        popup = Popup(title="Save Preset", content=content, size_hint=(0.85, 0.35))

        def _do_save(*_a):
            name = name_input.text.strip()
            if not name:
                self._show_message("Enter a preset name.")
                return
            popup.dismiss()
            preset_db.save_preset(self.script_name, name, answers)
            self._presets[name] = answers
            if name not in self.preset_names:
                self.preset_names = self.preset_names + [name]
            self.selected_preset = name
            self._append_output(
                f"[wizard] Preset '{name}' built from {len(answers)} step(s) -- "
                "no script execution was needed.\n"
            )
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
        self._close_socket_conn()
        self._release_wakelock()
        self._show_finished_notice("Script Stopped")
        if self._poll_event:
            self._poll_event.cancel()
        if self._recorded_steps:
            preset_db.save_schema(self.script_name, self._recorded_steps)
        self._set_status("Stop signal sent.", "warn")
        self._maybe_prompt_preset_save()

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

        extra_args_raw = self.ids.extra_args_input.text.strip() if hasattr(self, "ids") else ""
        extra_args = []
        if extra_args_raw:
            try:
                extra_args = shlex.split(extra_args_raw)
            except ValueError as exc:
                # Unbalanced quotes etc. -- refuse to launch with a
                # half-parsed argument list rather than guess at what the
                # user meant.
                self._show_message(f"Could not parse CLI Args: {exc}")
                self._preset_build_mode = False
                return
            self._append_output(
                f"[run] Passing CLI arguments to sys.argv: {extra_args!r} -- the "
                "script only sees these if it actually reads sys.argv itself; "
                "plain input()/sys.stdin calls are unaffected and still work "
                "through the live step UI below.\n"
            )

        preset_answers = None
        if self.selected_preset and self.selected_preset != self.MANUAL_LABEL:
            preset_answers = list(self._presets.get(self.selected_preset, []))
            self._append_output(
                f"[run] Using preset '{self.selected_preset}' -- {len(preset_answers)} "
                "answer(s) will be sent automatically. If the script asks for more "
                "than that, this app will prompt you for the rest live.\n"
            )

        self._collected_answers = []
        self._recorded_steps = []
        self._pending_chunk = ""
        self._shown_chunk_len = 0
        self._quiet_ticks = 0
        self._awaiting_input = False
        # Drop any leftover connection from a PREVIOUS run before this one
        # starts -- without this, a stale wfile object from a script that
        # exited abnormally (killed, crashed) and hadn't been noticed as
        # gone yet could let an early write briefly appear "sent" over a
        # dead socket before failing, instead of cleanly falling back to
        # the FIFO path for a fresh connection's first prompt.
        self._close_socket_conn()
        # Consumed by _on_socket_prompt as each PROMPT: notification
        # arrives over the socket -- the FIFO-fallback path instead gets
        # the same values pre-seeded into the answers file up front (see
        # preset_answers below), since it has no way to be told "a
        # prompt just happened" and has to fall back to quiet-detection.
        self._preset_queue = list(preset_answers or [])
        self._clear_step_panel()

        self._append_output(f"[run] Launching {self.script_name} in Termux...\n")
        self._set_status("Sending Run Script command to Termux...", "info")
        if not self._run_bridge_action(
            lambda: termux_bridge.run_script_interactive(
                content, filename, extra_args=extra_args, preset_answers=preset_answers,
            )
        ):
            return
        self._script_running = True
        self.run_button_text = "Stop Script"
        self._acquire_wakelock()
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
        # Concrete evidence for next time instead of another guess: the
        # exact (ANSI-stripped) text this button set was built from, and
        # the exact raw values bound to each button -- if a script's own
        # validation ever disagrees with what gets sent, this shows
        # precisely what the app saw and decided, not just what it
        # looked like on screen.
        self._append_output(
            f"[debug] Prompt chunk: {schema_engine.strip_ansi(chunk)!r}\n"
            f"[debug] Detected options: {options!r} raw_values: {raw_values!r}\n"
        )
        # Record this step for the Preset Wizard (see preset_db.save_schema,
        # called from _finish_run once the whole run is done) -- evidence
        # of what the script actually asked, not a guess.
        self._recorded_steps.append({
            "prompt": prompt or "",
            "options": options,
            "raw_values": raw_values,
        })

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
        if not value.strip():
            # Only the manual Send button can reach this with an empty
            # value (option/chip taps always pass a real one) -- refuse
            # to feed a blank line into the script's stdin at all rather
            # than send one and clear the step UI as if it had answered
            # something.
            self._show_message("Enter a value before sending.")
            return
        self._collected_answers.append(value)
        # repr(), not the bare value -- reveals any invisible leading/
        # trailing whitespace or stray newline that plain text would hide,
        # so a report of "the script rejected my answer" carries actual
        # evidence of exactly what was sent.
        self._append_output(f"[you] {value!r}\n")
        # Primary channel: write straight to the connected wrapper's
        # socket if one is live for this run -- effectively instant, no
        # RUN_COMMAND round-trip needed. Only falls back to the
        # FIFO-relay RUN_COMMAND path (send_answer) when no socket
        # connection exists (the wrapper's own connect() failed and it
        # dropped to its FIFO fallback instead).
        if not self._send_via_socket(value):
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
                # Once the socket channel is confirmed live for this run,
                # _on_socket_output already appends every print() live --
                # appending the same text again here from the tee'd log
                # diff would just show everything twice.
                if visible_part.strip() and not self._socket_active:
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
                    self._finish_run()
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
            # its next prompt and show buttons/an answer box for it. Not
            # consulted once the socket channel is live -- there,
            # _on_socket_prompt gets an exact signal instead of a guess.
            if self._awaiting_input or self._socket_active:
                return
            self._quiet_ticks += 1
            if self._quiet_ticks == 2 and self._pending_chunk.strip():
                self._show_step_ui(self._pending_chunk)

        self._poll_event = Clock.schedule_interval(poll, 2)

    def _finish_run(self):
        """Shared by both completion signals: termux_bridge.DONE_MARKER
        showing up in the polled log, and a "DONE:" message over the live
        socket (see _on_socket_done) -- whichever arrives first. Guarded
        so a second signal arriving shortly after the first is a no-op
        instead of double-logging/resetting state that's already reset.
        """
        if not self._script_running:
            return
        self._script_running = False
        self.run_button_text = "Run Script"
        self._awaiting_input = False
        self._close_socket_conn()
        self._release_wakelock()
        self._show_finished_notice("Script Finished")
        if self._poll_event:
            self._poll_event.cancel()
        # Persist whatever steps this run's live step UI actually showed
        # (see _show_step_ui) as this script's schema -- evidence from a
        # real run, not a guess -- so Build Preset can replay them later
        # with zero execution. A run driven entirely by an auto-answered
        # preset records nothing new here, which is fine: the schema it
        # would produce is already saved from whatever earlier run first
        # supplied that preset.
        if self._recorded_steps:
            preset_db.save_schema(self.script_name, self._recorded_steps)
        note = (
            f" {len(self._collected_answers)} answer(s) were given this run -- "
            "tap Save Config to store them as a reusable preset."
            if self._collected_answers else ""
        )
        self._append_output(f"[run] Script process has exited.{note}\n")
        self._maybe_prompt_preset_save()

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
        self.root_widget = RootWidget()
        return self.root_widget

    def on_stop(self):
        """Close the listening socket cleanly when the app itself is
        killed/backgrounded-and-reclaimed -- without this, the OS could
        take longer to release port 9988 on the next launch, and
        SO_REUSEADDR is a mitigation for that race, not a substitute for
        actually closing the socket when we're done with it."""
        widget = getattr(self, "root_widget", None)
        server = getattr(widget, "_socket_server", None) if widget is not None else None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if widget is not None:
            widget._release_wakelock()


if __name__ == "__main__":
    ScriptWrapperApp().run()
