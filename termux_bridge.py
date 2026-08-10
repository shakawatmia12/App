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

# The FIFO/answers-queue/feeder-pid trio for an interactive run MUST live
# under Termux's own internal storage, never under /sdcard: /sdcard is a
# FUSE-backed virtual filesystem (for Android's scoped-storage
# enforcement), and FUSE generally doesn't support real named pipes --
# `mkfifo` there can silently behave like a plain file instead of
# erroring. A plain file has no "block until a writer shows up" blocking
# read behaviour a FIFO has: reading it past its (empty) end just returns
# EOF immediately, which is exactly what made a script's very first
# input() see an instant empty read before the user had answered
# anything. Termux's own home directory is on the app's private internal
# storage (a real filesystem, full POSIX support) -- confirmed as the
# fix. Nothing here is ever read by OUR OWN process directly (unlike
# RUN_LOG/INSTALL_LOG, which our app polls straight off /sdcard), so
# there's no accessibility downside to keeping it entirely on Termux's
# side.
RUNTIME_DIR = f"{TERMUX_HOME}/.script_wrapper_run"

# Primary IPC channel: a plain loopback TCP socket main.py listens on.
# Two apps on the same Android device share the same network namespace,
# so a socket bound to 127.0.0.1 by our app is reachable from Termux's
# process exactly like any other loopback client/server pair -- this
# sidesteps the whole FIFO-on-a-real-filesystem question entirely, since
# a TCP connection has no dependency on what filesystem anything lives
# on. A blocking recv() on a connected socket only ever returns empty
# when the peer actually closes the connection, never spuriously the way
# a FIFO backed by an unsuitable filesystem could -- see the wrapper
# script built by _build_wrapper_source below for how a script's actual
# input()/print() calls are routed through it, with the RUNTIME_DIR FIFO
# kept as an automatic fallback if the socket can't be reached.
SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 9988

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
    """Per-script paths for the FIFO/answers-queue/feeder-pid/wrapper
    quartet used by an interactive run -- named after the script so two
    different scripts picked one after another don't collide."""
    import schema_engine

    stem = schema_engine.sanitize_name(filename)
    return {
        "fifo": f"{RUNTIME_DIR}/_run_{stem}.fifo",
        "answers": f"{RUNTIME_DIR}/_run_{stem}_answers.txt",
        "feed_pid": f"{RUNTIME_DIR}/_run_{stem}_feed.pid",
        "wrapper": f"{RUNTIME_DIR}/_run_{stem}_wrapper.py",
    }


# The actual script never runs directly -- it's exec'd (via runpy) from
# inside this small wrapper, which monkey-patches input()/stdout first.
# That's what makes the "never hand the script a blank/EOF answer"
# guarantee (layer 3) independent of which transport ends up being used:
# _read_answer_line() below refuses to return anything for an empty
# read, on either the socket or the FIFO fallback, and just keeps
# waiting instead -- a human never submits a genuinely blank answer, so
# an empty read only ever means "the channel isn't ready yet", not a
# real answer.
#
# Placeholders (__TOKEN__) are substituted with repr()'d Python literals
# by _build_wrapper_source, never raw string interpolation -- so a path
# or argument containing a quote/backslash can't corrupt the generated
# source.
_WRAPPER_SOURCE_TEMPLATE = r'''
import sys
import socket
import builtins
import time
import runpy
import json
import traceback

SCRIPT_PATH = __SCRIPT_PATH_REPR__
ARGS = __ARGS_REPR__
HOST = __HOST_REPR__
PORT = __PORT__
FALLBACK_FIFO = __FALLBACK_FIFO_REPR__

sys.argv = [SCRIPT_PATH] + list(ARGS)

# PYTHONUNBUFFERED=1 and `python -u` are already set at the shell level
# (see build_interactive_run_command's run_inner) -- that's what actually
# unbuffers this whole process's stdio from the moment it starts, which a
# reconfigure() call after the fact cannot retroactively fix if it didn't
# take. This is just defense-in-depth for the narrow case where something
# (a library the target script imports, a broken environment) resets
# stdout's buffering mode after startup -- cheap, and never harmful.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError, OSError):
    pass

_channel = ("none", None)

# main.py's socket server starts in a background thread during its own
# __init__ -- there is a real window, right after this wrapper process
# spawns, where that thread hasn't reached accept() yet (app just cold
# started, or was busy on the main thread at that exact moment). A single
# connect() attempt racing that window would spuriously fall back to the
# FIFO channel every time on a slow-starting device, even though the
# socket path would have worked fine a fraction of a second later. Retry
# a handful of times with a short delay before giving up on the socket.
_CONNECT_RETRIES = 10
_CONNECT_RETRY_DELAY = 0.3


def _connect():
    global _channel
    for attempt in range(_CONNECT_RETRIES):
        try:
            sock = socket.create_connection((HOST, PORT), timeout=5)
            _channel = ("socket", sock.makefile(
                "rw", encoding="utf-8", newline="\n", errors="replace"
            ))
            return
        except OSError:
            if attempt < _CONNECT_RETRIES - 1:
                time.sleep(_CONNECT_RETRY_DELAY)
    try:
        # errors="replace": a stray non-UTF-8 byte from the target script
        # (a mis-encoded print(), binary-ish output) must never raise and
        # kill this whole relay -- better to show a replacement character
        # than crash the bridge and lose the rest of the run's output.
        _channel = ("fifo", open(FALLBACK_FIFO, "r", encoding="utf-8", errors="replace"))
    except OSError:
        _channel = ("none", None)


_connect()


def _read_answer_line():
    kind, chan = _channel
    if chan is None:
        while True:
            time.sleep(1)
    while True:
        try:
            line = chan.readline()
        except OSError:
            time.sleep(0.2)
            continue
        if line == "":
            # Never trust a single empty read as a real answer -- a
            # human never submits nothing, so treat it as "not ready
            # yet" and keep waiting instead of handing the caller "".
            time.sleep(0.1)
            continue
        return line.rstrip("\n")


def _notify_prompt(prompt=""):
    """Tell main.py a blocking read has actually started -- this is what
    makes _on_socket_prompt fire the step UI immediately instead of
    waiting out the quiet-tick timer. Shared by _bridged_input AND
    _BridgedStdin below: a script that reads sys.stdin directly instead
    of calling input() still needs this exact same live signal, or the
    app would have no way to know it's time to show buttons at all --
    blocking forever on a real answer is pointless if the UI never
    appears to let a human provide one.
    """
    if _channel[0] == "socket":
        try:
            _channel[1].write("PROMPT:" + str(prompt).replace("\n", " ") + "\n")
            _channel[1].flush()
        except OSError:
            pass


def _bridged_input(prompt=""):
    if prompt:
        sys.__stdout__.write(str(prompt))
        sys.__stdout__.flush()
    _notify_prompt(prompt)
    return _read_answer_line()


builtins.input = _bridged_input


class _BridgedStdout:
    def __init__(self, real):
        self._real = real

    def write(self, text):
        self._real.write(text)
        if text and _channel[0] == "socket":
            try:
                _channel[1].write("OUT:" + text.replace("\n", "\x02") + "\n")
                _channel[1].flush()
            except OSError:
                pass

    def flush(self):
        self._real.flush()

    def isatty(self):
        return False


class _BridgedStdin:
    """Overriding builtins.input alone is NOT enough: a script that reads
    the domain/count/whatever straight off sys.stdin (sys.stdin.readline(),
    sys.stdin.read(), or a bare `for line in sys.stdin:`) instead of
    calling input() bypasses that patch completely and hits Termux's real
    stdin fd -- which RUN_COMMAND does not connect to a live terminal, so
    it behaves like /dev/null: any read on it returns "" (EOF) instantly.
    That is indistinguishable, from the target script's own point of view,
    from a human submitting a blank answer -- exactly the "Invalid Domain
    Selected!"-before-any-button-exists failure this class exists to rule
    out. Routing every stdin read through the SAME _read_answer_line()
    blocking shield as _bridged_input means it no longer matters which of
    the two ways a script chooses to ask -- both are guaranteed to block
    for a genuine answer instead of ever seeing a blank/EOF read.
    """

    def readline(self, *_a, **_k):
        _notify_prompt()
        return _read_answer_line() + "\n"

    def read(self, size=-1):
        _notify_prompt()
        line = _read_answer_line() + "\n"
        if size is not None and size >= 0:
            return line[:size]
        return line

    def __iter__(self):
        return self

    def __next__(self):
        return self.readline()

    def isatty(self):
        # Deliberately True, not a passthrough to the real fd: several
        # "[1] Option" style menu libraries (questionary/InquirerPy/
        # simple-term-menu and similar) check sys.stdin.isatty() BEFORE
        # ever calling read()/readline(), and silently take a
        # non-interactive fallback (often an immediate default/"invalid
        # selection") when it's False -- bypassing every blocking
        # guarantee in this class entirely, since they never reach it.
        # Termux's real RUN_COMMAND stdin fd is not a tty, so reporting
        # the honest answer here reproduces exactly that failure. This
        # class already guarantees a real, blocking, human-driven read
        # once a script does call in -- claiming tty support is accurate
        # to that guarantee, not a lie to route around it.
        return True

    def fileno(self):
        try:
            return sys.__stdin__.fileno()
        except (AttributeError, OSError, ValueError):
            return -1

    def flush(self):
        pass


sys.stdout = _BridgedStdout(sys.stdout)
sys.stdin = _BridgedStdin()


def _report_fatal_error(exc):
    """A script's own SystemExit (menu-driven CLIs routinely call
    sys.exit() on bad input or a normal quit -- see multi3.py) is NOT a
    crash and must never trigger this; only a genuinely UNCAUGHT
    exception (a missing dependency's ImportError, any other runtime
    error) reaches here. Two things happen: the human-readable traceback
    goes through sys.stdout (the _BridgedStdout instance above), so it
    reaches the terminal box over the SAME channel real output does --
    fixing a real gap where an uncaught exception's default traceback
    printed straight to the real stderr, which the shell's `2>&1 | tee`
    still captured into the log file, but which never went out over the
    socket and so was invisible for the rest of a run once the socket
    channel had already taken over display (see main.py's
    self._socket_active guard). Second, a compact JSON summary goes out
    as its own ERR: message, giving the UI a structured signal to mark
    the run as failed without having to text-sniff the traceback.
    """
    tb_text = traceback.format_exc()
    try:
        sys.stdout.write("\n[FATAL] Script crashed -- " + type(exc).__name__ + ": " + str(exc) + "\n")
        sys.stdout.write(tb_text)
        sys.stdout.flush()
    except OSError:
        pass
    if _channel[0] == "socket":
        payload = json.dumps({"type": type(exc).__name__, "message": str(exc)})
        try:
            _channel[1].write("ERR:" + payload + "\n")
            _channel[1].flush()
        except OSError:
            pass


try:
    runpy.run_path(SCRIPT_PATH, run_name="__main__")
except SystemExit as exc:
    # sys.exit(0)/sys.exit()/sys.exit(<int>) -- a script's normal,
    # intentional way to quit (a menu-driven CLI calling this after
    # "Invalid selection", or just finishing) -- not a failure, nothing
    # to report. sys.exit("some message") is different: Python's own
    # default behaviour for a STRING exit code is to print that string
    # to stderr, which previously landed only in the tee'd log (never
    # over the socket -- the same visibility gap _report_fatal_error
    # fixes below for uncaught exceptions) and could go missing entirely
    # once the socket channel had already taken over display. Forward it
    # as plain output, not a [FATAL] banner -- the script chose this exit
    # deliberately, so it isn't a crash, just a message worth showing.
    if isinstance(exc.code, str) and exc.code:
        try:
            sys.stdout.write(exc.code if exc.code.endswith("\n") else exc.code + "\n")
            sys.stdout.flush()
        except OSError:
            pass
except BaseException as exc:
    _report_fatal_error(exc)
finally:
    if _channel[0] == "socket":
        try:
            _channel[1].write("DONE:\n")
            _channel[1].flush()
        except OSError:
            pass
'''


def _build_wrapper_source(script_path, fallback_fifo, extra_args=None):
    src = _WRAPPER_SOURCE_TEMPLATE
    src = src.replace("__SCRIPT_PATH_REPR__", repr(script_path))
    src = src.replace("__ARGS_REPR__", repr(list(extra_args or [])))
    src = src.replace("__HOST_REPR__", repr(SOCKET_HOST))
    src = src.replace("__PORT__", str(SOCKET_PORT))
    src = src.replace("__FALLBACK_FIFO_REPR__", repr(fallback_fifo))
    return src


def build_interactive_run_command(content, filename, extra_args=None, preset_answers=None,
                                   attachments=None, log_path=None):
    """Run `content` inside Termux via a small wrapper (see
    _build_wrapper_source) that monkey-patches input()/stdout before
    exec'ing it, so answers can be fed to it ONE AT A TIME while it's
    actually running, in response to what it actually prints -- not
    pre-computed once, up front, from a static guess about what it will
    ask.

    The wrapper's own primary channel is a loopback TCP socket main.py
    listens on (see SOCKET_HOST/SOCKET_PORT) -- a connected socket has no
    dependency on any filesystem's quirks, so a blocking read on it only
    ever returns empty when the peer truly closes it. If that connection
    can't be made (app not running, port unreachable, anything), the
    wrapper falls back to opening a FIFO directly itself.

    That FIFO still needs a separate relay ("feeder") process holding its
    write end open, for the same reason as before: a FIFO delivers EOF to
    its reader the instant its write end has NO open writers left, even
    for a moment -- so a sequence of independent short-lived `> fifo`
    writers (open, write one line, close) would hand the script an
    EOFError the moment the FIRST one closes, well before a second answer
    ever arrives. The feeder keeps the FIFO's write end open for the
    script's *entire* run by holding it open itself (`exec 3>fifo`) and
    relaying lines appended to a plain "answers" file into it via
    `tail -f`; appending to a plain file from any later, independent
    RUN_COMMAND has no such EOF hazard.

    preset_answers: an optional list of answers known up front (loaded
    from a saved preset -- see preset_db.py, entirely local to the app,
    no Termux involvement in storing/loading it) is pre-written
    into the answers file for the FIFO-fallback case (main.py separately
    auto-answers from the same preset over the socket when that's the
    active channel), so a run goes hands-free through however many of
    its prompts the preset covers, and only ever falls back to waiting
    for a live answer once/if it asks for more than the preset provided.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script_path = f"{SCRIPTS_DIR}/{filename}"
    target = log_path or RUN_LOG
    paths = _run_paths(filename)
    fifo, answers, feed_pid, wrapper_path = (
        paths["fifo"], paths["answers"], paths["feed_pid"], paths["wrapper"]
    )

    mkdir_dirs = {SCRIPTS_DIR, os.path.dirname(target), RUNTIME_DIR}
    write_parts = [f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(script_path)}"]

    wrapper_source = _build_wrapper_source(script_path, fifo, extra_args)
    encoded_wrapper = base64.b64encode(wrapper_source.encode("utf-8")).decode("ascii")
    write_parts.append(f"echo {shlex.quote(encoded_wrapper)} | base64 -d > {shlex.quote(wrapper_path)}")

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
        # Explicit zero-byte truncate -- no manual answer means the
        # answers file (and therefore the FIFO) must stay completely
        # empty until the user actually taps something. `printf '' >`
        # writes literally nothing (not even a bare newline), unlike
        # e.g. `echo "" >` which writes a single "\n" that Python's
        # input() would read as an immediate blank answer the moment
        # the script's first prompt is reached.
        seed_answers = f"printf '' > {shlex.quote(answers)}"

    mkdir = f"mkdir -p {' '.join(shlex.quote(d) for d in mkdir_dirs)}"
    write_all = " && ".join(write_parts)

    # `exec 3>fifo` opens the FIFO's write end and holds it on fd 3 for
    # the life of this subshell. That fd only stays open as long as the
    # subshell itself keeps running something -- if `tail -f` were the
    # very last command and it ever exited on its own (killed, a
    # transient error, Android reclaiming the process under memory
    # pressure -- anything), the subshell would have nothing left to do
    # and would exit too, closing fd 3 and delivering EOF to the
    # script's next input() immediately, indistinguishable from the
    # script actually seeing a blank answer. Wrapping it in `while true;
    # do ... ; done` means the subshell always has something to run next,
    # so fd 3 -- and the write end it holds -- can only ever close when
    # this whole subshell is explicitly killed (see build_stop_command),
    # never on its own.
    #
    # `rm`/`mkfifo`/truncating `answers` run BEFORE anything is
    # backgrounded, and the backgrounding itself is wrapped in its own
    # `( ... )` group -- a bare trailing `&` on a full `&&`-chain like
    # `rm -f X && mkfifo X && : > Y & echo ...` backgrounds the ENTIRE
    # chain (rm/mkfifo/truncate included), not just the intended
    # long-lived feeder loop. That raced mkfifo/truncation against the
    # very next `&&`-chained step (starting the actual script with
    # `< fifo`), which could try to open a FIFO that doesn't exist yet
    # or read from an answers file that hasn't been truncated yet --
    # exactly the kind of timing bug that produces unpredictable
    # first-input behaviour. Confirmed by inspecting the generated
    # command directly.
    setup = (
        f"rm -f {shlex.quote(fifo)} && mkfifo {shlex.quote(fifo)} && "
        f"{seed_answers} && "
        f"( ( exec 3>{shlex.quote(fifo)}; "
        f"while true; do tail -n +1 -f {shlex.quote(answers)} >&3; sleep 1; done ) & "
        f"echo $! > {shlex.quote(feed_pid)} )"
    )

    # -u / PYTHONUNBUFFERED: CPython block-buffers stdout once it isn't a
    # real TTY (true here regardless, since everything goes through
    # `tee`), so prompts can sit invisible for a long time without this.
    # Runs the WRAPPER, not the raw script directly -- no `< fifo`
    # redirect on this process's own stdin at all anymore, since the
    # wrapper acquires its answers explicitly (socket or an open() on
    # the FIFO path itself), completely decoupled from whatever this
    # shell happens to hand it as fd 0.
    run_inner = f"PYTHONUNBUFFERED=1 python -u {shlex.quote(wrapper_path)}"
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
    """Kill whatever Run Script last started, escalating to SIGKILL if it's
    still alive half a second after the initial (default SIGTERM) kill --
    a script blocked deep in a call that ignores or defers SIGTERM (some
    C extensions, or a script that traps it) would otherwise linger
    running in the background indefinitely after the user has already
    been told it stopped."""
    paths = _run_paths(filename)
    pid_file = shlex.quote(RUN_PID_FILE)
    return (
        f"if [ -f {pid_file} ]; then "
        f"PID=$(cat {pid_file}); "
        f"if kill -0 $PID 2>/dev/null; then "
        f"kill $PID 2>/dev/null; sleep 0.5; "
        f"kill -0 $PID 2>/dev/null && kill -9 $PID 2>/dev/null; "
        f"echo 'Stop signal sent to the running script.'; "
        f"else echo 'The script had already finished.'; fi; "
        f"else echo 'No running script is being tracked.'; fi; "
        f"kill $(cat {shlex.quote(paths['feed_pid'])}) 2>/dev/null; "
        f"rm -f {shlex.quote(paths['fifo'])}"
    )


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
