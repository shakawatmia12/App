"""Dynamic schema parser, dependency detector, and JSON config utilities.

Reads a top-level `SCHEMA` dict from a target .py file using `ast`
(never `exec`/`eval` of the whole file), extracts the pip package list
and dynamic UI field definitions, detects third-party imports the script
actually uses, and provides small platform-agnostic helpers for the
per-script JSON config files main.py manages on Android.
"""
import ast
import json
import os
import re
import sys

SUPPORTED_FIELD_TYPES = {"number", "text", "boolean", "select"}


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
        "description": "No SCHEMA dict found in this script — running with no options.",
        "packages": [],
        "fields": [],
    }


def _normalize_schema(schema, script_path):
    normalized = {
        "name": schema.get("name") or os.path.basename(script_path),
        "description": schema.get("description", ""),
        "packages": [],
        "fields": [],
    }

    packages = schema.get("packages", [])
    if isinstance(packages, list):
        normalized["packages"] = [str(p).strip() for p in packages if str(p).strip()]

    fields = schema.get("fields", [])
    if isinstance(fields, list):
        for raw_field in fields:
            field = _normalize_field(raw_field)
            if field:
                normalized["fields"].append(field)

    return normalized


def _normalize_field(raw_field):
    if not isinstance(raw_field, dict):
        return None

    key = raw_field.get("key")
    ftype = raw_field.get("type", "text")
    if not key or ftype not in SUPPORTED_FIELD_TYPES:
        return None

    field = {
        "key": str(key),
        "type": ftype,
        "label": raw_field.get("label", str(key)),
        "default": raw_field.get("default", _type_default(ftype)),
        "required": bool(raw_field.get("required", False)),
    }

    if ftype == "select":
        options = raw_field.get("options", [])
        field["options"] = [str(o) for o in options] if isinstance(options, list) else []
        if field["default"] not in field["options"] and field["options"]:
            field["default"] = field["options"][0]

    return field


def _type_default(ftype):
    return {"number": 0, "text": "", "boolean": False, "select": ""}.get(ftype, "")


def get_packages(schema):
    return list(schema.get("packages", []))


def get_fields(schema):
    return list(schema.get("fields", []))


def cast_field_value(field, raw_value):
    """Coerce a raw widget value to the type declared by its field schema."""
    ftype = field["type"]

    if ftype == "number":
        try:
            text = str(raw_value)
            return float(text) if "." in text else int(text)
        except (TypeError, ValueError):
            return field.get("default", 0)

    if ftype == "boolean":
        return bool(raw_value)

    return str(raw_value)


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
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
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


def _extract_prompt_text(call_node):
    """Best-effort extraction of an input() call's prompt string, so the
    auto-generated field has a useful label instead of a generic one."""
    if not call_node.args:
        return ""

    arg = call_node.args[0]
    try:
        return str(ast.literal_eval(arg)).strip()
    except (ValueError, TypeError):
        pass

    if isinstance(arg, ast.JoinedStr):  # f-string with only literal pieces
        parts = [
            value.value for value in arg.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        return "".join(parts).strip()

    return ""


_MENU_LINE_RE = re.compile(r'^\s*(\d+)\s*[.)\-:]\s*(.+?)\s*$')
_SIMPLE_PRINT_RE = re.compile(r'^print\(\s*f?[\'"](.*)[\'"]\s*\)\s*$')


def _collect_menu_options(source_lines, input_lineno, max_lookback=20):
    """Look for a plain `print("N. option")`-per-line menu immediately
    above an input() call, and return (display_options, raw_values) if
    one is recognized, else (None, None).

    Deliberately simple regex matching on raw source text rather than
    full parsing: this only catches menus written as an unbroken run of
    literal one-string-per-line print() calls right before the input()
    that reads the choice (very common in hand-written CLI menus), and
    safely finds nothing for anything more dynamic (a for-loop building
    the menu, f-strings pulling from a list, etc.) rather than guessing.
    """
    menu_texts = []
    idx = input_lineno - 2  # 0-indexed line immediately above the input() line
    checked = 0
    while idx >= 0 and checked < max_lookback:
        line = source_lines[idx].strip()
        if not line:
            break
        match = _SIMPLE_PRINT_RE.match(line)
        if not match:
            break
        menu_texts.insert(0, match.group(1))
        idx -= 1
        checked += 1

    options, raw_values = [], []
    for text in menu_texts:
        m = _MENU_LINE_RE.match(text)
        if not m:
            return None, None
        raw_values.append(m.group(1))
        options.append(f"{m.group(1)}) {m.group(2)}")

    if len(options) >= 2:
        return options, raw_values
    return None, None


def detect_inputs(script_path):
    """Best-effort scan for input() calls, in source order, used to build
    a settings form automatically when a script has no SCHEMA of its own.

    This is a heuristic, not a real interpreter: it finds every input()
    call anywhere in the file -- including ones inside if/elif branches
    or loops -- and turns each into one text field, in source order. It
    cannot know which branch actually executes at runtime, so a script
    whose questions depend on an earlier menu choice may end up with
    extra/mismatched fields. It works well for scripts that just ask a
    flat, unconditional sequence of questions.
    """
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=script_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    source_lines = source.splitlines()

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "input"
    ]
    calls.sort(key=lambda n: (n.lineno, n.col_offset))

    fields = []
    for i, call in enumerate(calls, start=1):
        prompt = _extract_prompt_text(call)
        field = {
            "key": f"input_{i}",
            "type": "text",
            "label": prompt or f"Input #{i}",
            "default": "",
            "required": False,
        }

        options, raw_values = _collect_menu_options(source_lines, call.lineno)
        if options:
            # A numbered print() menu was found right above this input() --
            # render it as a dropdown instead of a plain text box. The
            # displayed option is "N) description" for readability, but
            # the value actually fed to the script's stdin (see
            # main.py.run_script) is just the raw "N" the script expects.
            field["type"] = "select"
            field["options"] = options
            field["default"] = options[0]
            field["_option_values"] = raw_values
        elif re.search(r"\.txt|\bfile\b", field["label"], re.IGNORECASE):
            # Prompt text mentions "file" or a .txt example -- the script
            # almost certainly wants a path to read from, which the user
            # can't usefully type by hand (they don't know Termux's
            # filesystem). Render a Browse button instead of a text box;
            # see main.py._pick_attachment for how the picked file's
            # content gets transferred to a path Termux can read.
            field["type"] = "file"

        fields.append(field)
    return fields


# ---- Per-script config helpers (platform-agnostic) ---------------------

def sanitize_name(name):
    """Turn a script filename into a safe token for a config filename."""
    stem = os.path.splitext(name or "script")[0]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return cleaned or "script"


def config_filename_for(script_name):
    return f"{sanitize_name(script_name)}_config.json"


def load_json_file(path):
    """Read a JSON dict from `path`, returning {} for anything that isn't
    a valid, present JSON object (missing file, empty file, bad JSON)."""
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
