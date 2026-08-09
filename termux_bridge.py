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
  real time. Every command is wrapped with `... 2>&1 | tee <logfile>` and
  the app polls that logfile from Python (best-effort -- see read_log()).
* The user must enable `allow-external-apps=true` in
  `~/.termux/termux.properties` inside Termux itself, or Termux will
  silently refuse the RUN_COMMAND intent.
* Confirmed on a real device: Termux cannot read/write files our own app
  publishes into a MediaStore collection via androidstorage4kivy's
  copy_to_shared() ("Permission denied" trying to `bash` a script placed
  there), even though Termux has its own broad legacy storage access
  everywhere else. So every script/config Termux needs to read or write
  is transferred as base64-encoded CONTENT embedded directly in the shell
  command -- Termux decodes and writes it into a plain path it owns
  itself (under LOG_DIR), never touching anything our app inserted via
  MediaStore. Only reading files *for our own process* (schema parsing)
  goes through copy_from_shared(), which stays entirely on our side.
"""
import base64
import json
import os
import shlex

TERMUX_PACKAGE = "com.termux"
TERMUX_SERVICE = "com.termux.app.RunCommandService"
TERMUX_ACTION = "com.termux.RUN_COMMAND"
TERMUX_BASH = "/data/data/com.termux/files/usr/bin/bash"
TERMUX_HOME = "/data/data/com.termux/files/home"

# Termux writes and reads everything here with its own full storage access.
# This directory does NOT need to exist beforehand from our side -- every
# command below `mkdir -p`s it itself.
LOG_DIR = "/sdcard/termux_wrapper"
SCRIPTS_DIR = f"{LOG_DIR}/scripts"
CONFIGS_DIR = f"{LOG_DIR}/configs"
INSTALL_LOG = f"{LOG_DIR}/install_output.log"
RUN_LOG = f"{LOG_DIR}/run_output.log"

# Termux enforces `allow-external-apps=true` in its own private
# ~/.termux/termux.properties before it will honor a RUN_COMMAND intent
# from any other app. No app -- including this one -- can write into
# Termux's private storage to flip that flag for the user (that's the
# whole point of the setting: it's a deliberate, user-only opt-in gate).
SETUP_COMMAND = (
    "mkdir -p ~/.termux && "
    "echo 'allow-external-apps=true' >> ~/.termux/termux.properties && "
    "termux-reload-settings && termux-setup-storage && "
    "echo 'Termux is ready for Script Wrapper.'"
)


def build_install_command(packages, log_path=None):
    """Return the shell command that installs python + pip packages."""
    pkg_list = " ".join(shlex.quote(p) for p in packages if p)

    if pkg_list:
        body = f"pkg install python -y && pip install {pkg_list}"
    else:
        body = "echo 'No packages declared in SCHEMA[\"packages\"].'"

    target = log_path or INSTALL_LOG
    mkdir = f"mkdir -p {shlex.quote(os.path.dirname(target))}"
    return f"{mkdir} && ({body}) 2>&1 | tee {shlex.quote(target)}"


def build_run_command_from_content(content, filename, extra_args=None, log_path=None):
    """Have Termux write the script's content to a path it owns, then run
    it -- see the module docstring for why we hand Termux content instead
    of a path we published via MediaStore.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script_path = f"{SCRIPTS_DIR}/{filename}"
    target = log_path or RUN_LOG
    args = " ".join(shlex.quote(str(a)) for a in (extra_args or []))

    mkdir = f"mkdir -p {shlex.quote(SCRIPTS_DIR)} {shlex.quote(os.path.dirname(target))}"
    write_script = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(script_path)}"
    # Redirect stdin from /dev/null: RUN_COMMAND runs headless with nobody
    # able to type into it, so a script that calls input() would otherwise
    # hang forever with zero output -- indistinguishable from "still
    # running a slow network call". This turns that into an immediate,
    # visible EOFError in the log instead, which is at least diagnosable.
    run = f"python {shlex.quote(script_path)} {args} < /dev/null".strip()
    return f"{mkdir} && {write_script} && ({run}) 2>&1 | tee {shlex.quote(target)}"


def config_path_for(script_name):
    import schema_engine

    return f"{CONFIGS_DIR}/{schema_engine.config_filename_for(script_name)}"


def build_save_config_command(script_name, values):
    """Have Termux write the config file under its own CONFIGS_DIR.

    Values are base64-encoded so arbitrary JSON (quotes, newlines)
    survives shell-command quoting unscathed.
    """
    config_path = config_path_for(script_name)
    payload = json.dumps(values, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    mkdir = f"mkdir -p {shlex.quote(CONFIGS_DIR)}"
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


# Termux's four RUN_COMMAND_SESSION_ACTION values (from its own source):
#   0 = keep current session, don't open Termux's activity
#   1 = keep current session, open Termux's activity
#   2 = switch to the new session, don't open Termux's activity
#   3 = switch to the new session, open Termux's activity
# We were defaulting to "0" -- Termux would run the command in a new
# session but never bring itself to the foreground or switch to it, so
# every successful run looked identical to nothing happening at all.
SESSION_ACTION_SWITCH_AND_OPEN = "3"


def send_termux_command(shell_command, session_action=SESSION_ACTION_SWITCH_AND_OPEN, background=False):
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

    activity = PythonActivity.mActivity

    # Wrap every string extra in an explicit java.lang.String instead of
    # handing pyjnius a plain Python str: Intent.putExtra() has ~15
    # overloads (String, CharSequence, boolean, int, String[], ...), and
    # jnius' automatic type coercion has already misfired twice elsewhere
    # in this project (RUN_COMMAND_ARGUMENTS, a ContentResolver.query()
    # call) picking an unintended overload. Termux itself reported
    # "Mandatory extra missing" for RUN_COMMAND_PATH on a real device even
    # though it was being set -- consistent with the String value landing
    # in the intent as the wrong extra type, so getStringExtra() finds
    # nothing. Explicit String() construction removes the ambiguity.
    JString = autoclass("java.lang.String")

    intent = Intent()
    intent.setClassName(TERMUX_PACKAGE, TERMUX_SERVICE)
    intent.setAction(TERMUX_ACTION)
    intent.putExtra("com.termux.RUN_COMMAND_PATH", JString(TERMUX_BASH))
    intent.putExtra("com.termux.RUN_COMMAND_WORKDIR", JString(TERMUX_HOME))
    intent.putExtra("com.termux.RUN_COMMAND_BACKGROUND", bool(background))
    intent.putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", JString(str(session_action)))
    # Confirmed on device: passing a plain Python list here actually
    # works fine (pyjnius auto-converts it to the String[] this putExtra
    # overload expects) -- every earlier failure got past this line just
    # fine. _to_java_string_array() as the unconditional path instead
    # broke with "Cannot convert list to jnius.JavaClass", so it's kept
    # only as a fallback, not used first.
    try:
        intent.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", ["-c", shell_command])
    except Exception:
        intent.putExtra(
            "com.termux.RUN_COMMAND_ARGUMENTS",
            _to_java_string_array(["-c", shell_command]),
        )

    # Plain framework Activity methods instead of androidx.core's
    # ContextCompat.startForegroundService(): buildozer/p4a's default
    # Gradle setup doesn't bundle androidx.core, so that class straight up
    # doesn't exist in the compiled APK (ClassNotFoundException at
    # runtime, confirmed on device). startForegroundService() has existed
    # on the plain Activity/Context class since API 26, no AndroidX needed.
    VERSION = autoclass("android.os.Build$VERSION")
    if VERSION.SDK_INT >= 26:
        activity.startForegroundService(intent)
    else:
        activity.startService(intent)


def install_packages(packages, log_path=None):
    """Auto Package Install Action: pkg install python -y && pip install <packages>."""
    command = build_install_command(packages, log_path=log_path)
    send_termux_command(command)
    return command


def run_script_from_content(content, filename, extra_args=None, log_path=None):
    """Run Script Action: Termux writes `content` to its own SCRIPTS_DIR
    and runs it with `python`."""
    command = build_run_command_from_content(content, filename, extra_args, log_path=log_path)
    send_termux_command(command)
    return command


def save_config(script_name, values):
    """Save Config Action: delegate the actual file write to Termux."""
    command = build_save_config_command(script_name, values)
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


def is_termux_installed():
    """Check the package is actually present before firing a command at
    it -- lets us give a clear "Termux isn't installed" message instead
    of a command that silently goes nowhere."""
    try:
        from jnius import autoclass
    except ImportError:
        return False

    try:
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        activity.getPackageManager().getPackageInfo(TERMUX_PACKAGE, 0)
        return True
    except Exception:
        return False


def read_log(log_path):
    """Direct read of a plain path Termux wrote to.

    The APK's manifest carries android:requestLegacyExternalStorage="true"
    (patched in by p4a_hook.py at build time), which restores full plain
    filesystem access on Android 10/11 regardless of targetSdkVersion. On
    Android 12+ that flag is ignored, so this can still fail silently
    (returns "") there without MANAGE_EXTERNAL_STORAGE -- callers should
    treat empty as "unknown/check Termux directly", not "nothing happened
    yet".
    """
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
