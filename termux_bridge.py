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
  real time, and there is no live pipe/pty shared between our app's
  process and whatever Termux runs -- they are two entirely separate
  Android apps. Every command is wrapped with `... 2>&1 | tee <logfile>`
  and the app polls that logfile from Python (best-effort -- see
  read_log()). Interactive step-by-step input (see
  build_interactive_run_command below) works around the lack of a live
  channel using a named pipe (FIFO) plus a small relay process, fed one
  answer at a time by separate, independent RUN_COMMAND dispatches.
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
RUN_PID_FILE = f"{SCRIPTS_DIR}/_last_run.pid"

# Printed once a script's process has actually exited, so polling the log
# can tell "finished" apart from "just quiet for a moment" -- see
# build_interactive_run_command.
DONE_MARKER = "___WRAPPER_SCRIPT_DONE___"

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


def _run_paths(filename):
    """Per-script paths for the FIFO/answers-queue/feeder-pid trio used by
    an interactive run -- named after the script so two different scripts
    picked one after another don't collide on a leftover FIFO."""
    import schema_engine

    stem = schema_engine.sanitize_name(filename)
    return {
        "fifo": f"{SCRIPTS_DIR}/_run_{stem}.fifo",
        "answers": f"{SCRIPTS_DIR}/_run_{stem}_answers.txt",
        "feed_pid": f"{SCRIPTS_DIR}/_run_{stem}_feed.pid",
    }


def build_interactive_run_command(content, filename, extra_args=None, preset_answers=None,
                                   attachments=None, log_path=None):
    """Run `content` inside Termux with its stdin wired to a named pipe
    (FIFO) instead of a plain file, so answers can be fed to it ONE AT A
    TIME while it's actually running, in response to what it actually
    prints -- not pre-computed once, up front, from a static guess about
    what it will ask.

    Why a FIFO plus a separate relay ("feeder") process, instead of just
    repeatedly doing `echo answer > fifo` from each new RUN_COMMAND: a
    FIFO delivers EOF to its reader the instant its write end has NO open
    writers left, even for a moment -- so a sequence of independent
    short-lived `> fifo` writers (open, write one line, close) would hand
    the script an EOFError the moment the FIRST one closes, well before a
    second answer ever arrives. The feeder keeps the FIFO's write end
    open for the script's *entire* run by holding it open itself
    (`exec 3>fifo`) and relaying lines appended to a plain "answers" file
    into it via `tail -f`; appending to a plain file from any later,
    independent RUN_COMMAND has no such EOF hazard.

    preset_answers: an optional list of answers known up front (loaded
    from a saved preset -- see build_save_preset_command) is pre-written
    into the answers file before the script starts, so it runs hands-free
    through however many of its prompts the preset covers, and only ever
    falls back to waiting for a live answer (send_answer()) once/if it
    asks for more than the preset provided.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script_path = f"{SCRIPTS_DIR}/{filename}"
    target = log_path or RUN_LOG
    args = " ".join(shlex.quote(str(a)) for a in (extra_args or []))
    paths = _run_paths(filename)
    fifo, answers, feed_pid = paths["fifo"], paths["answers"], paths["feed_pid"]

    mkdir_dirs = {SCRIPTS_DIR, os.path.dirname(target)}
    write_parts = [f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(script_path)}"]

    if attachments:
        for path, text in attachments.items():
            mkdir_dirs.add(os.path.dirname(path))
            encoded_att = base64.b64encode(str(text).encode("utf-8", "replace")).decode("ascii")
            write_parts.append(f"echo {shlex.quote(encoded_att)} | base64 -d > {shlex.quote(path)}")

    if preset_answers:
        preset_payload = "\n".join(str(v) for v in preset_answers) + "\n"
        encoded_preset = base64.b64encode(preset_payload.encode("utf-8")).decode("ascii")
        seed_answers = f"echo {shlex.quote(encoded_preset)} | base64 -d > {shlex.quote(answers)}"
    else:
        seed_answers = f": > {shlex.quote(answers)}"

    mkdir = f"mkdir -p {' '.join(shlex.quote(d) for d in mkdir_dirs)}"
    write_all = " && ".join(write_parts)

    # `exec 3>fifo` opens the FIFO's write end and keeps it held on fd 3
    # for as long as this backgrounded subshell lives; `tail -f` never
    # exits on its own, so that write end stays open for the script's
    # whole run, and every line later appended to `answers` (see
    # build_send_answer_command) gets relayed straight into the FIFO.
    setup = (
        f"rm -f {shlex.quote(fifo)} && mkfifo {shlex.quote(fifo)} && "
        f"{seed_answers} && "
        f"( exec 3>{shlex.quote(fifo)}; tail -n +1 -f {shlex.quote(answers)} >&3 ) & "
        f"echo $! > {shlex.quote(feed_pid)}"
    )

    # -u / PYTHONUNBUFFERED: CPython block-buffers stdout once it isn't a
    # real TTY (true here regardless, since everything goes through
    # `tee`), so prompts can sit invisible for a long time without this.
    run_inner = (
        f"PYTHONUNBUFFERED=1 python -u {shlex.quote(script_path)} {args} < {shlex.quote(fifo)}"
    ).strip()
    run = f"{run_inner} & echo $! > {shlex.quote(RUN_PID_FILE)}; wait $(cat {shlex.quote(RUN_PID_FILE)})"
    cleanup = f"kill $(cat {shlex.quote(feed_pid)}) 2>/dev/null; rm -f {shlex.quote(fifo)}"

    return (
        f"{mkdir} && {write_all} && {setup} && "
        f"(({run}) 2>&1; echo {shlex.quote(DONE_MARKER)}) | tee {shlex.quote(target)}; {cleanup}"
    )


def build_send_answer_command(filename, value):
    """Append one answer line to the running script's answers queue (see
    build_interactive_run_command) -- the feeder relays it into the
    script's stdin FIFO the moment it appears. Base64-encoded so an
    arbitrary typed/pasted value (quotes, newlines) survives shell
    quoting unscathed."""
    paths = _run_paths(filename)
    payload = str(value) + "\n"
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"echo {shlex.quote(encoded)} | base64 -d >> {shlex.quote(paths['answers'])}"


def send_answer(filename, value):
    """Feed one answer to whatever script is currently running (see
    run_script_interactive). Sent quietly in the background, like
    save_config()/stop_script() -- there's no separate output to watch
    for beyond the main run log, which keeps updating on its own."""
    command = build_send_answer_command(filename, value)
    send_termux_command(command, background=True)
    return command


def build_stop_command(filename):
    paths = _run_paths(filename)
    return (
        f"if [ -f {shlex.quote(RUN_PID_FILE)} ]; then "
        f"kill $(cat {shlex.quote(RUN_PID_FILE)}) 2>/dev/null "
        f"&& echo 'Stop signal sent to the running script.' "
        f"|| echo 'The script had already finished.'; "
        f"else echo 'No running script is being tracked.'; fi; "
        f"kill $(cat {shlex.quote(paths['feed_pid'])}) 2>/dev/null; "
        f"rm -f {shlex.quote(paths['fifo'])}"
    )


def presets_path_for(script_name):
    import schema_engine

    return f"{CONFIGS_DIR}/{schema_engine.sanitize_name(script_name)}_presets.json"


def build_save_preset_command(script_name, preset_name, answers):
    """Have Termux merge one named preset (an ordered list of answers)
    into that script's presets JSON file, keeping any other presets
    already saved there. The merge itself runs as a tiny Python snippet
    *inside Termux* (base64-transferred the same way a script itself is)
    rather than trying to hand-roll a shell one-liner that reads-modifies-
    writes JSON -- Termux always has Python available (it's this whole
    app's baseline requirement), and repr() safely escapes the embedded
    strings/lists as valid Python source, so there's no shell-quoting
    hazard from whatever characters are in the answers themselves.
    """
    path = presets_path_for(script_name)
    snippet = (
        "import json\n"
        f"path = {path!r}\n"
        f"name = {preset_name!r}\n"
        f"answers = {list(answers)!r}\n"
        "try:\n"
        "    with open(path, 'r', encoding='utf-8') as f:\n"
        "        data = json.load(f)\n"
        "    if not isinstance(data, dict):\n"
        "        data = {}\n"
        "except Exception:\n"
        "    data = {}\n"
        "data[name] = answers\n"
        "with open(path, 'w', encoding='utf-8') as f:\n"
        "    json.dump(data, f, indent=2, ensure_ascii=False)\n"
        "print('Preset saved: ' + name)\n"
    )
    encoded_snippet = base64.b64encode(snippet.encode("utf-8")).decode("ascii")
    snippet_path = f"{CONFIGS_DIR}/_save_preset.py"
    mkdir = f"mkdir -p {shlex.quote(CONFIGS_DIR)}"
    write = f"echo {shlex.quote(encoded_snippet)} | base64 -d > {shlex.quote(snippet_path)}"
    run = f"python {shlex.quote(snippet_path)}"
    return f"{mkdir} && {write} && {run}"


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


def run_script_interactive(content, filename, extra_args=None, preset_answers=None,
                            attachments=None, log_path=None):
    """Run Script Action: Termux writes `content` to its own SCRIPTS_DIR
    and runs it with `python`, stdin wired to a FIFO for live step-by-step
    answers -- see build_interactive_run_command."""
    command = build_interactive_run_command(
        content, filename, extra_args=extra_args, preset_answers=preset_answers,
        attachments=attachments, log_path=log_path,
    )
    send_termux_command(command)
    return command


def stop_script(filename):
    """Best-effort kill of whatever Run Script last started (see the PID
    file written in build_interactive_run_command), plus its feeder
    process and FIFO. Sent quietly in the background -- there's no
    separate output to watch for."""
    command = build_stop_command(filename)
    send_termux_command(command, background=True)
    return command


def save_preset(script_name, preset_name, answers):
    """Save Config Action: persist the given answer sequence under a name
    Termux can look up again later (see build_save_preset_command)."""
    command = build_save_preset_command(script_name, preset_name, answers)
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
