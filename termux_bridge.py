"""Termux Intent & Execution Bridge.

Talks to Termux 0.119.0's RunCommandService via the documented
`com.termux.permission.RUN_COMMAND` intent (Termux:API / RUN_COMMAND spec).

Design notes
------------
* We always target Termux with an *explicit* intent
  (`setClassName("com.termux", "com.termux.app.RunCommandService")`).
  Explicit component intents are exempt from Android 11+ package-visibility
  filtering, so no `<queries>` manifest entry is required.
* Termux's RUN_COMMAND intent does not stream stdout back to the caller in
  real time. To give the wrapper app a "Terminal Output Stream View" without
  a custom Java BroadcastReceiver, every command is wrapped with
  `... 2>&1 | tee <logfile>` and the app polls that logfile from Python.
* The user must enable `allow-external-apps=true` in
  `~/.termux/termux.properties` inside Termux itself, or Termux will
  silently refuse the RUN_COMMAND intent. See build_instructions.md-style
  notes at the end of buildozer.spec / project README for the full checklist.
"""
import base64
import hashlib
import json
import os
import shlex

TERMUX_PACKAGE = "com.termux"
TERMUX_SERVICE = "com.termux.app.RunCommandService"
TERMUX_ACTION = "com.termux.RUN_COMMAND"
TERMUX_BASH = "/data/data/com.termux/files/usr/bin/bash"
TERMUX_HOME = "/data/data/com.termux/files/home"

# Termux writes everything here with its own full storage access -- this
# directory does NOT need to exist beforehand from our side, since our own
# app can't reliably create/write shared-storage paths under Android 10+
# scoped storage. Every command below `mkdir -p`s it itself.
LOG_DIR = "/sdcard/termux_wrapper"
INSTALL_LOG = f"{LOG_DIR}/install_output.log"
RUN_LOG = f"{LOG_DIR}/run_output.log"

# Termux enforces `allow-external-apps=true` in its own private
# ~/.termux/termux.properties before it will honor a RUN_COMMAND intent
# from any other app. No app -- including this one -- can write into
# Termux's private storage to flip that flag for the user (that's the
# whole point of the setting: it's a deliberate, user-only opt-in gate).
# The best we can do is make the one-time paste as short as possible.
SETUP_COMMAND = (
    "mkdir -p ~/.termux && "
    "(grep -q allow-external-apps ~/.termux/termux.properties 2>/dev/null || "
    "echo 'allow-external-apps=true' >> ~/.termux/termux.properties) && "
    "termux-reload-settings && termux-setup-storage && "
    "echo 'Termux is ready for Script Wrapper.'"
)


def build_install_command(packages, log_path=None):
    """Return the shell command that installs python + pip packages.

    `log_path` should be a real path our own app can also read back (see
    main.py's _prepare_shared_log) -- Termux's plain-filesystem write to
    the default LOG_DIR works fine for Termux itself, but our own process
    can't read that path back under Android 10+ scoped storage.
    """
    pkg_list = " ".join(shlex.quote(p) for p in packages if p)

    if pkg_list:
        body = f"pkg install python -y && pip install {pkg_list}"
    else:
        body = "echo 'No packages declared in SCHEMA[\"packages\"].'"

    target = log_path or INSTALL_LOG
    mkdir = f"mkdir -p {shlex.quote(os.path.dirname(target))}"
    return f"{mkdir} && ({body}) 2>&1 | tee {shlex.quote(target)}"


def build_run_command(script_path, extra_args=None, log_path=None):
    """Return the shell command that runs the selected script in Termux."""
    if not script_path:
        raise ValueError("script_path is required")

    quoted_script = shlex.quote(script_path)
    args = " ".join(shlex.quote(str(a)) for a in (extra_args or []))
    body = f"python {quoted_script} {args}".strip()

    target = log_path or RUN_LOG
    mkdir = f"mkdir -p {shlex.quote(os.path.dirname(target))}"
    return f"{mkdir} && ({body}) 2>&1 | tee {shlex.quote(target)}"


def _config_filename_for(script_path):
    digest = hashlib.md5(script_path.encode("utf-8")).hexdigest()[:12]
    return f"config_{digest}.json"


def config_path_for(script_path):
    """Per-script config file path (avoids read-modify-write of one shared
    JSON file, which would need something in Termux capable of parsing
    JSON just to merge keys -- plain `base64 -d > file` needs nothing but
    coreutils, which Termux always has even before `pkg install python`)."""
    return f"{LOG_DIR}/{_config_filename_for(script_path)}"


def build_save_config_command(script_path, values):
    """Have Termux itself write the config file.

    Our own app's process is scoped-storage-restricted on Android 10+ and
    can't reliably write arbitrary shared-storage paths, but Termux
    already has full storage access via `termux-setup-storage`. Values
    are base64-encoded so arbitrary JSON (quotes, newlines) survives
    shell-command quoting unscathed.
    """
    config_path = config_path_for(script_path)
    payload = json.dumps(values, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    mkdir = f"mkdir -p {shlex.quote(LOG_DIR)}"
    write = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(config_path)}"
    return f"{mkdir} && {write} && echo 'Config saved to {config_path}'"


def _to_java_string_array(items):
    """Fallback manual String[] builder if jnius' list-autoconvert fails."""
    from jnius import autoclass, cast

    jstring = autoclass("java.lang.String")
    jarray = autoclass("java.lang.reflect.Array")
    arr = jarray.newInstance(jstring, len(items))
    for i, item in enumerate(items):
        jarray.set(arr, i, jstring(item))
    return cast("[Ljava.lang.String;", arr)


def send_termux_command(shell_command, session_action="0", background=False):
    """Fire the RUN_COMMAND intent at Termux. Android-only (needs pyjnius)."""
    try:
        from jnius import autoclass
    except ImportError as exc:
        raise RuntimeError(
            "termux_bridge can only send commands when running inside the "
            "Android app (pyjnius/jnius is unavailable on this platform)."
        ) from exc

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Intent = autoclass("android.content.Intent")
    ContextCompat = autoclass("androidx.core.content.ContextCompat")

    activity = PythonActivity.mActivity

    intent = Intent()
    intent.setClassName(TERMUX_PACKAGE, TERMUX_SERVICE)
    intent.setAction(TERMUX_ACTION)
    intent.putExtra("com.termux.RUN_COMMAND_PATH", TERMUX_BASH)
    intent.putExtra("com.termux.RUN_COMMAND_WORKDIR", TERMUX_HOME)
    intent.putExtra("com.termux.RUN_COMMAND_BACKGROUND", bool(background))
    intent.putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", str(session_action))

    try:
        intent.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", ["-c", shell_command])
    except Exception:
        intent.putExtra(
            "com.termux.RUN_COMMAND_ARGUMENTS",
            _to_java_string_array(["-c", shell_command]),
        )

    ContextCompat.startForegroundService(activity, intent)


def install_packages(packages, log_path=None):
    """Auto Package Install Action: pkg install python -y && pip install <packages>."""
    command = build_install_command(packages, log_path=log_path)
    send_termux_command(command)
    return command


def run_script(script_path, extra_args=None, log_path=None):
    """Run Script Action: python /sdcard/selected_script.py in a Termux session."""
    command = build_run_command(script_path, extra_args, log_path=log_path)
    send_termux_command(command)
    return command


def save_config(script_path, values):
    """Save Config Action: delegate the actual file write to Termux."""
    command = build_save_config_command(script_path, values)
    send_termux_command(command, background=True)
    return command


def open_termux():
    """Bring Termux to the foreground (Android-only, needs pyjnius)."""
    try:
        from jnius import autoclass
    except ImportError as exc:
        raise RuntimeError(
            "open_termux can only run inside the Android app."
        ) from exc

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    activity = PythonActivity.mActivity

    intent = activity.getPackageManager().getLaunchIntentForPackage(TERMUX_PACKAGE)
    if intent is None:
        raise RuntimeError("Termux does not appear to be installed.")

    activity.startActivity(intent)


def read_log(log_path):
    if not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def read_install_log():
    return read_log(INSTALL_LOG)


def read_run_log():
    return read_log(RUN_LOG)
