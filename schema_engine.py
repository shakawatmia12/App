"""SCHEMA parser, dependency detector, live-output menu parser, and JSON
config utilities.

Reads an OPTIONAL top-level `SCHEMA` dict from a target .py file using
`ast` (never `exec`/`eval` of the whole file) for its `name`/`description`/
`packages` -- packages get merged with auto-detected imports for Install
Packages. Everything about a script's *interactive* behaviour (its
input() prompts, menus, argparse flags, whatever) is no longer guessed by
statically reading its source at all: main.py now runs the script for
real inside Termux and reacts to what it actually prints, live, via
parse_menu_options()/last_prompt_line() below. That earlier approach --
AST-walking the script hunting for input()/argparse/sys.argv usage and
guessing at menu structure from print() call shapes -- covered a lot of
cases but was fundamentally chasing an unbounded number of ways a script
can be written; a script's real runtime behaviour is only ever fully
knowable by actually running it.
"""
import ast
import json
import os
import re
import sys

# Matches both forms an ANSI colour escape can take in a .py file: the
# literal backslash-digit source text (\033[1;92m, \x1b[1;92m) as it
# appears before Python ever evaluates the string literal, and the real
# ESC control byte in case a file already contains raw bytes.
_ANSI_SOURCE_RE = re.compile(r"\\033\[[0-9;]*m|\\x1b\[[0-9;]*m|\x1b\[[0-9;]*m", re.IGNORECASE)


def _read_source_clean(script_path):
    """Read a script and strip ANSI colour escapes immediately -- only
    used by detect_imports below, where it's harmless/unnecessary but
    cheap; kept for consistency with how source is always read here."""
    with open(script_path, "r", encoding="utf-8") as f:
        source = f.read()
    return _ANSI_SOURCE_RE.sub("", source)


class SchemaError(Exception):
    """Raised when a target script's SCHEMA cannot be read or is invalid."""


def _find_schema_node(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCHEMA":
                    return node.value
    return None


def load_schema_from_file(script_path):
    """Parse `script_path` and return a normalized schema dict.

    If the script has no SCHEMA dict, a safe empty default schema is
    returned instead of raising, so any plain .py file can still be run.
    A SCHEMA here is optional and only ever used for `packages` (a hint
    list merged with auto-detected imports) plus `name`/`description` --
    there is no `fields` concept anymore (see module docstring).
    """
    if not script_path or not os.path.isfile(script_path):
        raise SchemaError(f"Script not found: {script_path}")

    try:
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaError(f"Could not read script: {exc}") from exc

    try:
        tree = ast.parse(source, filename=script_path)
    except SyntaxError as exc:
        raise SchemaError(f"Could not parse script: {exc}") from exc

    node = _find_schema_node(tree)
    if node is None:
        return default_schema(script_path)

    try:
        schema = ast.literal_eval(node)
    except (ValueError, SyntaxError) as exc:
        raise SchemaError(
            f"SCHEMA must contain only literal values (str/int/float/bool/list/dict): {exc}"
        ) from exc

    if not isinstance(schema, dict):
        raise SchemaError("SCHEMA must be a dict")

    return _normalize_schema(schema, script_path)


def default_schema(script_path):
    return {
        "name": os.path.basename(script_path) if script_path else "Untitled",
        "description": "No SCHEMA dict found in this script.",
        "packages": [],
    }


def _normalize_schema(schema, script_path):
    normalized = {
        "name": schema.get("name") or os.path.basename(script_path),
        "description": schema.get("description", ""),
        "packages": [],
    }

    packages = schema.get("packages", [])
    if isinstance(packages, list):
        normalized["packages"] = [str(p).strip() for p in packages if str(p).strip()]

    return normalized


def get_packages(schema):
    return list(schema.get("packages", []))


# ---- Dependency auto-detection ---------------------------------------

def _stdlib_module_names():
    try:
        return set(sys.stdlib_module_names)  # Python 3.10+
    except AttributeError:
        return set(sys.builtin_module_names) | {
            "os", "sys", "time", "json", "re", "math", "random", "subprocess",
            "threading", "collections", "itertools", "functools", "typing",
            "pathlib", "shutil", "logging", "argparse", "datetime", "string",
            "io", "socket", "http", "urllib", "sqlite3", "csv", "hashlib",
            "base64", "struct", "copy", "enum", "abc", "asyncio", "queue",
            "traceback", "unittest", "xml", "email", "glob", "tempfile",
            "zipfile", "gzip", "pickle", "ast", "shlex", "platform", "uuid",
            "warnings", "contextlib", "dataclasses", "statistics", "operator",
            "textwrap", "configparser", "secrets", "signal", "select", "ctypes",
        }


def detect_imports(script_path):
    """Best-effort scan of a script's top-level `import x` / `from x import
    y` statements via ast, returning third-party module names (stdlib
    filtered out). Used to auto-suggest packages for "Install Packages"
    without requiring every script to hand-list them in SCHEMA.
    """
    try:
        source = _read_source_clean(script_path)
        tree = ast.parse(source, filename=script_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])

    stdlib = _stdlib_module_names()
    return sorted(m for m in modules if m and m not in stdlib)


# ---- Live terminal-output parsing (interactive run) ---------------------
#
# The app now runs a script for real inside Termux and watches its actual
# stdout, instead of statically guessing from source code. By the time
# output reaches here it has ALREADY been rendered -- any ANSI colour,
# f-string, colorama, or string-concatenation trick a script's source
# used to build a menu is irrelevant, because we're reading the literal
# characters Termux's terminal would have shown a human. This is why the
# whole AST-based menu-detection machinery this module used to carry
# (fighting ANSI codes, curly-brace colour variables, for-loops built
# from a list, argparse declarations, ...) is gone: none of that
# complexity exists on this side at all anymore.

_LIVE_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LIVE_MENU_LINE_RE = re.compile(r'^\s*[\[\(]?\s*(\d{1,2})\s*[\]\)]?\s*[.\-:]?\s*(.+?)\s*$')

_NUMBER_PROMPT_HINT_RE = re.compile(
    r"thread|count|concurrent|how many|number of|limit|delay|timeout|retries|retry|workers?",
    re.IGNORECASE,
)
_PROXY_PROMPT_HINT_RE = re.compile(r"proxy", re.IGNORECASE)


def strip_ansi(text):
    return _LIVE_ANSI_RE.sub("", text) if text else text


def parse_menu_options(text):
    """Scan a chunk of already-rendered terminal output for numbered or
    bracketed menu lines -- "[1] Gmail.com", "1. Gmail.com", "1) Gmail.com"
    -- so the app can turn whatever a running script just printed into
    tap-able buttons, with zero knowledge of how the script's source
    produced that text. Returns (display_options, raw_values), each
    display option formatted "N) label" for the button/dropdown text
    while raw_values holds the bare "N" actually fed back to the
    script's stdin -- or (None, None) if fewer than two numbered lines
    are found.
    """
    options, raw_values, seen = [], [], set()
    for raw_line in text.splitlines():
        line = strip_ansi(raw_line).strip()
        if not line:
            continue
        m = _LIVE_MENU_LINE_RE.match(line)
        if not m:
            continue
        num, label = m.group(1), m.group(2).strip()
        if not label or num in seen:
            continue
        seen.add(num)
        raw_values.append(num)
        options.append(f"{num}) {label}")
    if len(options) >= 2:
        return options, raw_values
    return None, None


def last_prompt_line(text):
    """The last non-empty, ANSI-stripped line of a chunk of output --
    usually the actual prompt text a script is currently waiting on
    (e.g. "Enter Threads: "). Used to decide whether to offer number
    chips (looks like it's asking for a count) or a paste button
    (mentions "proxy") alongside the plain answer box."""
    for raw_line in reversed(text.splitlines()):
        line = strip_ansi(raw_line).strip()
        if line:
            return line
    return ""


def prompt_wants_number_chips(prompt_text):
    return bool(_NUMBER_PROMPT_HINT_RE.search(prompt_text or ""))


def prompt_wants_paste_button(prompt_text):
    return bool(_PROXY_PROMPT_HINT_RE.search(prompt_text or ""))


# ---- Per-script config/preset helpers (platform-agnostic) ---------------

def sanitize_name(name):
    """Turn a script filename into a safe token for a config filename."""
    stem = os.path.splitext(name or "script")[0]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return cleaned or "script"


def load_json_file(path):
    """Read a JSON value from `path`, returning {} for anything that isn't
    present/valid JSON (missing file, empty file, bad JSON)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return json.loads(content) if content else {}
    except (OSError, json.JSONDecodeError):
        return {}


def dump_json(values):
    return json.dumps(values, indent=2, ensure_ascii=False)
