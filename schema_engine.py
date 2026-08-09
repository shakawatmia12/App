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


def _literal_value(node):
    """ast.literal_eval that returns None instead of raising for anything
    that isn't a literal (a variable, function call, etc.) -- used
    throughout the detectors below where a non-literal argument should
    just be skipped, not crash the whole scan."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _extract_string_arg(call_node, index=0):
    """Best-effort extraction of a call's Nth positional string argument
    (a prompt, help text, etc.), so an auto-generated field can have a
    useful label instead of a generic one."""
    if len(call_node.args) <= index:
        return ""

    arg = call_node.args[index]
    value = _literal_value(arg)
    if isinstance(value, str):
        return value.strip()

    if isinstance(arg, ast.JoinedStr):  # f-string with only literal pieces
        parts = [
            part.value for part in arg.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        return "".join(parts).strip()

    return ""


_FILE_HINT_RE = re.compile(r"\.txt\b|\bfile\b", re.IGNORECASE)


def _maybe_mark_as_file(field):
    """Prompt/help text mentioning "file" or a .txt example means the
    script wants a path to read from, which the user can't usefully
    hand-type (they don't know Termux's filesystem). Render a Browse
    button instead of a text box -- see main.py._pick_attachment for how
    the picked file's content gets transferred to a path Termux can read.
    """
    if field["type"] == "text" and _FILE_HINT_RE.search(field["label"]):
        field["type"] = "file"
    return field


_MENU_LINE_RE = re.compile(r'^\s*(\d+)\s*[.)\-:]\s*(.+?)\s*$')
_SIMPLE_PRINT_RE = re.compile(r'^print\(\s*f?[\'"](.*)[\'"]\s*\)\s*$')
_COMMENT_RE = re.compile(r'^\s*#')


def _collect_menu_options(source_lines, input_lineno, max_lookback=25):
    """Look for a plain `print("N. option")`-per-line menu above an
    input() call, and return (display_options, raw_values) if one is
    recognized, else (None, None).

    Deliberately simple regex matching on raw source text rather than
    full parsing. Blank lines and comments between the menu and the
    input() (very common for readability) are skipped rather than
    treated as "no menu here"; a single print() with embedded "\\n"
    joining several options is also split apart. A non-numbered print()
    line mixed into the block (a header like print("Choose a domain:")
    right before the numbered options) is treated as a label and
    ignored rather than aborting the whole match -- only a line that
    isn't a print()/blank/comment at all stops the scan, so a for-loop
    building the menu dynamically, or unrelated code above the prompt,
    still safely yields no menu instead of a guess.
    """
    menu_texts = []
    idx = input_lineno - 2  # 0-indexed line immediately above the input() line
    checked = 0
    while idx >= 0 and checked < max_lookback:
        checked += 1
        line = source_lines[idx].strip()
        if not line or _COMMENT_RE.match(line):
            idx -= 1
            continue
        match = _SIMPLE_PRINT_RE.match(line)
        if not match:
            break
        for part in reversed(match.group(1).split("\\n")):
            menu_texts.insert(0, part)
        idx -= 1

    options, raw_values = [], []
    for text in menu_texts:
        m = _MENU_LINE_RE.match(text.strip())
        if m:
            raw_values.append(m.group(1))
            options.append(f"{m.group(1)}) {m.group(2)}")
        # else: a non-numbered line (header/label) -- ignore, don't abort

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
        prompt = _extract_string_arg(call)
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
        else:
            field = _maybe_mark_as_file(field)

        fields.append(field)
    return fields


# ---- argparse-based scripts --------------------------------------------

def _find_argparse_parser_names(tree):
    """Variable names assigned from argparse.ArgumentParser(...) anywhere
    in the module (covers both `import argparse; argparse.ArgumentParser()`
    and `from argparse import ArgumentParser; ArgumentParser()`)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call_func = node.value.func
            is_parser_call = (
                (isinstance(call_func, ast.Attribute) and call_func.attr == "ArgumentParser")
                or (isinstance(call_func, ast.Name) and call_func.id == "ArgumentParser")
            )
            if is_parser_call:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def detect_argparse_fields(script_path):
    """Scan for parser.add_argument(...) calls on any variable created via
    argparse.ArgumentParser(), and turn each declared argument into a
    settings field -- the most common fully-static way Python CLI scripts
    declare their own inputs, so a script already written with argparse
    needs zero changes to work here.
    """
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=script_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    parser_names = _find_argparse_parser_names(tree)
    if not parser_names:
        return []

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in parser_names
    ]
    calls.sort(key=lambda n: (n.lineno, n.col_offset))

    fields = []
    for i, call in enumerate(calls, start=1):
        flag_names = [
            arg.value for arg in call.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if not flag_names:
            continue  # a dynamically-built flag name -- nothing static to read

        kwargs = {}
        for kw in call.keywords:
            if kw.arg is None:  # **something -- can't resolve statically
                continue
            if kw.arg == "type" and isinstance(kw.value, ast.Name):
                kwargs["type"] = kw.value.id  # "int" / "float" / "str"
            else:
                kwargs[kw.arg] = _literal_value(kw.value)

        primary_flag = max(flag_names, key=len)  # prefer the long form, e.g. --threads
        is_positional = not primary_flag.startswith("-")
        key = re.sub(r"[^A-Za-z0-9_]+", "_", primary_flag.lstrip("-")).strip("_") or f"arg_{i}"

        ftype = "text"
        options = None
        if kwargs.get("action") in ("store_true", "store_false"):
            ftype = "boolean"
        elif isinstance(kwargs.get("choices"), (list, tuple)):
            ftype = "select"
            options = [str(c) for c in kwargs["choices"]]
        elif kwargs.get("type") in ("int", "float"):
            ftype = "number"

        default = kwargs.get("default")
        if default is None:
            default = _type_default(ftype)
        if ftype == "select" and options and default not in options:
            default = options[0]

        field = {
            "key": key,
            "type": ftype,
            "label": str(kwargs.get("help") or primary_flag),
            "default": default,
            "required": bool(kwargs.get("required", False)),
            "_argparse_flag": primary_flag,
            "_argparse_positional": is_positional,
            "_argparse_is_flag_only": ftype == "boolean",
        }
        if options:
            field["options"] = options
        if ftype == "text":
            field = _maybe_mark_as_file(field)

        fields.append(field)

    return fields


# ---- sys.argv-based scripts ---------------------------------------------

def detect_sys_argv_fields(script_path):
    """Scan for sys.argv[N] subscripts and build one positional text field
    per distinct index referenced (argv[0], the script's own path, is
    skipped). Covers scripts that read command-line args by hand instead
    of using argparse.
    """
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=script_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    indexes = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "argv"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys"
        ):
            continue

        slice_node = node.slice
        # Python <3.9 wraps a plain index in ast.Index; unwrap it so
        # _literal_value sees the actual constant either way.
        if hasattr(slice_node, "value") and not isinstance(slice_node, (ast.Constant, ast.Slice)):
            slice_node = slice_node.value
        idx = _literal_value(slice_node)
        if isinstance(idx, int) and idx > 0:
            indexes.add(idx)

    if not indexes:
        return []

    fields = []
    for idx in sorted(indexes):
        field = {
            "key": f"argv_{idx}",
            "type": "text",
            "label": f"Command-line argument #{idx}",
            "default": "",
            "required": False,
            "_argv_index": idx,
        }
        fields.append(field)
    return fields


# ---- Unified auto-detection entry point ---------------------------------

def auto_detect_fields(script_path):
    """Try, in priority order: argparse declarations, sys.argv[N] usage,
    then input() prompts. Returns (fields, mode) -- mode is "argparse",
    "argv", "input", or None if the script has no SCHEMA and nothing
    detectable either.

    The mode tells the caller (main.py) how to feed answers back to the
    script: argparse/argv fields become command-line arguments (see
    build_cli_args), input fields get piped in as stdin, in source order.
    This covers the three common fully-static ways a Python CLI script
    takes input; a script that pulls its options from somewhere dynamic
    (an API call, a database, computed at runtime) has nothing static for
    any tool to read without actually executing it, and gets no fields
    here rather than an incorrect guess.
    """
    fields = detect_argparse_fields(script_path)
    if fields:
        return fields, "argparse"

    fields = detect_sys_argv_fields(script_path)
    if fields:
        return fields, "argv"

    fields = detect_inputs(script_path)
    if fields:
        return fields, "input"

    return [], None


def build_cli_args(fields, values):
    """Build the argv list to pass to the script from argparse/sys.argv
    -detected fields (see auto_detect_fields) and the user's answers.
    """
    positional = {}
    flags = []

    for field in fields:
        key = field["key"]
        value = values.get(key, field.get("default", ""))

        if "_argv_index" in field:
            positional[field["_argv_index"]] = str(value)
            continue

        flag = field.get("_argparse_flag")
        if not flag:
            continue
        if field.get("_argparse_is_flag_only"):
            if value:
                flags.append(flag)
        elif field.get("_argparse_positional"):
            if str(value):
                flags.append(str(value))
        elif str(value):
            flags.append(flag)
            flags.append(str(value))

    if positional:
        highest = max(positional)
        ordered = [positional.get(i, "") for i in range(1, highest + 1)]
        return ordered + flags
    return flags


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
