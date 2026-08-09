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

# Matches both forms an ANSI colour escape can take in a .py file: the
# literal backslash-digit source text (\033[1;92m, \x1b[1;92m) as it
# appears before Python ever evaluates the string literal, and the real
# ESC control byte in case a file already contains raw bytes.
_ANSI_SOURCE_RE = re.compile(r"\\033\[[0-9;]*m|\\x1b\[[0-9;]*m|\x1b\[[0-9;]*m", re.IGNORECASE)


def _read_source_clean(script_path):
    """Read a script and strip ANSI colour escapes immediately, before
    anything else touches it, so every detector below -- and the AST it
    parses -- only ever sees the plain text a colourised menu actually
    presents to the user, never raw escape bytes."""
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


def _literal_value(node):
    """ast.literal_eval that returns None instead of raising for anything
    that isn't a literal (a variable, function call, etc.) -- used
    throughout the detectors below where a non-literal argument should
    just be skipped, not crash the whole scan."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text):
    """Strip ANSI escape/colour codes (e.g. "\\033[1;92m" ... "\\033[0m",
    however many parameters or final letter they use) so a script that
    colourizes its menus doesn't hide the actual option text from the
    detectors below or leak escape bytes into the UI."""
    return _ANSI_RE.sub("", text) if text else text


def _extract_string_arg_node(node, strict=False):
    """Resolve a single AST node to its string value, or None if it can't
    be resolved statically. In `strict` mode (used when the exact text
    matters, e.g. reading a menu option's number/label), an f-string with
    any `{expr}` placeholder is rejected outright rather than silently
    dropping the placeholder -- a script computing a menu line at runtime
    is the for-loop detector's job, not this one's. Non-strict mode (used
    for prompt/help labels, where approximate is fine) keeps whatever
    literal text surrounds the placeholders."""
    value = _literal_value(node)
    if isinstance(value, str):
        return _strip_ansi(value.strip())

    if isinstance(node, ast.JoinedStr):
        has_placeholder = any(isinstance(p, ast.FormattedValue) for p in node.values)
        if strict and has_placeholder:
            return None
        parts = [
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        return _strip_ansi("".join(parts).strip())

    return None


def _extract_string_arg(call_node, index=0):
    """Best-effort extraction of a call's Nth positional string argument
    (a prompt, help text, etc.), so an auto-generated field can have a
    useful label instead of a generic one."""
    if len(call_node.args) <= index:
        return ""
    return _extract_string_arg_node(call_node.args[index]) or ""


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


# Matches "1. text", "1) text", "1 - text", "1: text", and bracketed
# styles like "[1] text" or "(1) text" -- covers every numbered-menu
# convention seen in practice, including colourised ones once ANSI codes
# are stripped out first.
_MENU_LINE_RE = re.compile(r'^\s*[\[\(]?\s*(\d+)\s*[\]\)\.\-:]?\s*(.+?)\s*$')


def _extract_print_text(stmt):
    """If `stmt` is a bare `print(...)` expression statement with only
    statically-resolvable string arguments, return its joined, ANSI-
    stripped text; otherwise None. A print() with a dynamic argument
    (an f-string referencing a loop variable, a computed value, etc.)
    also returns None -- that's a signal to the caller that whatever
    menu-scan is in progress should stop here, not guess."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == "print"):
        return None
    if not stmt.value.args:
        return None

    parts = []
    for arg in stmt.value.args:
        text = _extract_string_arg_node(arg, strict=True)
        if text is None:
            return None
        parts.append(text)
    return _strip_ansi(" ".join(parts))


def _collect_print_menu(block, call_stmt_idx):
    """Scan backward from block[call_stmt_idx] through the contiguous run
    of plain `print(...)` statements immediately above it (comments and
    blank lines aren't statements, so they're already transparent to
    this) for numbered/bracketed menu options such as:
        print("\\033[1;92m[1] Gmail.com\\033[0m")
        print("\\033[1;92m[2] Yahoo.com\\033[0m")
        domain = input("Select Domain : ")
    and return (display_options, raw_values), or (None, None) if fewer
    than two option-shaped lines are found. Stops the instant it hits any
    statement that isn't a resolvable bare print() -- including another
    input()-bearing statement, which marks a different, already-answered
    prompt's territory, so two colourised menus back-to-back each match
    their own input() instead of one bleeding into the other.
    """
    texts = []
    for prev in reversed(block[:call_stmt_idx]):
        text = _extract_print_text(prev)
        if text is None:
            break
        texts.insert(0, text)

    options, raw_values = [], []
    for text in texts:
        for line in text.split("\n"):
            m = _MENU_LINE_RE.match(line.strip())
            if m:
                raw_values.append(m.group(1))
                options.append(f"{m.group(1)}) {m.group(2)}")
            # else: a non-numbered line (header/label) -- ignore, don't abort

    if len(options) >= 2:
        return options, raw_values
    return None, None


_RAW_INPUT_CALL_RE = re.compile(r'\binput\s*\(')
_RAW_NUMBER_TOKEN_RE = re.compile(r'(?<!\d)(\d{1,2})(?!\d)')
# Short f-string colour placeholders scripts commonly alias colour codes
# to, e.g. f"{C}[1] Gmail.com{W}" (C=Cyan, G=Green, W=White, M=Magenta,
# R=Red/Reset, Y=Yellow, B=Blue...). Only meaningful here on raw,
# unevaluated source text -- the AST paths either resolve a real f-string
# placeholder properly or reject it outright, so they never see this
# literal "{C}" text in the first place.
_CURLY_VAR_RE = re.compile(r'\{[A-Za-z0-9_]+\}')
# Catches a numbering prefix left at the very start of an already-built
# label -- e.g. if the raw line itself repeated the number/bracket
# ("[1][1] Gmail.com") -- so the final dropdown text never doubles up
# the "1) " this module already adds itself.
_LEADING_NUM_PREFIX_RE = re.compile(r'^[\[\(]?\d{1,2}[\]\)]?\s*[.\-:]?\s*')


def _raw_line_menu_item(line):
    """Very permissive, shape-agnostic extraction: does this single
    source line contain something that looks like ONE numbered/bracketed
    menu option, no matter how it's actually built -- a plain print()
    literal, colour applied via string concatenation or an f-string with
    a variable/Fore.GREEN-style placeholder, "[1]", "1.", "1)", or a
    prefixed style like "[G][1]"? Returns (number, label) or (None,
    None).

    Only the number has to be right, since that's the raw value actually
    fed back to the script's stdin (see run_script in main.py) -- a
    messy label left over from stripped call/quote syntax around it is
    cosmetic, not functional, so this deliberately does not try to be a
    real parser.
    """
    text = line.strip()
    text = re.sub(r'^print\s*\(', ' ', text)
    text = re.sub(r'\)\s*$', ' ', text)
    text = re.sub(r'''f?["']''', ' ', text)
    text = _CURLY_VAR_RE.sub(' ', text)

    match = _RAW_NUMBER_TOKEN_RE.search(text)
    if not match:
        return None, None

    num = match.group(1)
    label = text[:match.start()] + text[match.end():]
    label = re.sub(r'[\[\]()+,]+', ' ', label)
    label = re.sub(r'\s+', ' ', label).strip(' .:-')
    label = _LEADING_NUM_PREFIX_RE.sub('', label).strip()
    if not label:
        return None, None
    return num, label


def _raw_regex_menu_scan(source_lines, input_lineno, lookback=25):
    """Last-resort, structure-agnostic fallback for menus the structural
    AST scan can't cleanly resolve -- colour applied via a variable or
    Fore.GREEN-style call the strict AST extractor won't guess the value
    of, a print() built from concatenation, an unusual call shape, etc.
    Scans up to `lookback` raw lines immediately above the input() call
    (ANSI escapes already stripped up front by _read_source_clean) for
    anything that looks like a numbered option, tolerating blank lines,
    headers, comments, and non-print statements freely mixed in between
    -- only consulted when the AST scan found nothing, as a safety net,
    not the primary path.

    Stops the instant it reaches a different input() call -- without
    that boundary this would walk straight past a separate,
    already-answered prompt's menu and misattribute it to the current
    one, since raw text has no notion of "which statement is this line
    part of" the way the AST scan does.
    """
    start = max(0, input_lineno - 1 - lookback)
    collected = []
    seen = set()
    for idx in range(input_lineno - 2, start - 1, -1):
        raw_line = source_lines[idx]
        if _RAW_INPUT_CALL_RE.search(raw_line):
            break
        num, label = _raw_line_menu_item(raw_line)
        if num and label and num not in seen:
            seen.add(num)
            collected.insert(0, (num, label))
    if len(collected) < 2:
        return None, None
    raw_values = [n for n, _ in collected]
    options = [f"{n}) {l}" for n, l in collected]
    return options, raw_values


def _resolve_literal_lists(tree):
    """Map every name assigned a literal list/tuple of scalars anywhere in
    the module to its value, e.g. `domains = ["gmail.com", "yahoo.com"]`.
    Used to resolve a for-loop like `for i, item in enumerate(domains):`
    back to the actual option text even though the loop itself never
    spells the options out."""
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            literal = _literal_value(node.value)
            if isinstance(literal, (list, tuple)) and literal:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = list(literal)
    return values


def _resolve_iterable(node, literal_lists):
    """Resolve a for-loop's iterable to a concrete list of scalar values,
    either from an inline literal (`for x in ["a", "b"]`) or a
    previously-collected `name = [...]` assignment. None if it can't be
    resolved statically (e.g. built from a function call or API result)."""
    literal = _literal_value(node)
    if isinstance(literal, (list, tuple)) and literal:
        return list(literal)
    if isinstance(node, ast.Name):
        return literal_lists.get(node.id)
    return None


def _menu_from_for_loop(for_node, literal_lists):
    """Recognize the two common ways scripts print a numbered/listed menu
    from a list in a loop, rather than one print() literal per option:
        for i, item in enumerate(DOMAINS): print(f"{i+1}. {item}")
        for item in DOMAINS: print(item)
    and return (options, raw_values) built from the list's actual
    contents, or (None, None) if this loop doesn't look like a menu
    printer (e.g. it never prints its loop variable at all)."""
    target = for_node.target
    iter_node = for_node.iter
    item_names = []
    items = None
    numbered = False

    if (isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name)
            and iter_node.func.id == "enumerate" and iter_node.args):
        items = _resolve_iterable(iter_node.args[0], literal_lists)
        numbered = True
        if isinstance(target, ast.Tuple) and len(target.elts) == 2:
            item_names = [elt.id for elt in target.elts if isinstance(elt, ast.Name)]
    else:
        items = _resolve_iterable(iter_node, literal_lists)
        if isinstance(target, ast.Name):
            item_names = [target.id]

    if not items or len(items) < 2 or item_names is None or not item_names:
        return None, None
    if not all(isinstance(v, (str, int, float)) for v in items):
        return None, None

    prints_an_item = any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "print"
        and any(
            isinstance(sub, ast.Name) and sub.id in item_names
            for arg in call.args for sub in ast.walk(arg)
        )
        for stmt in for_node.body for call in ast.walk(stmt)
    )
    if not prints_an_item:
        return None, None

    if numbered:
        options = [f"{i + 1}) {v}" for i, v in enumerate(items)]
        raw_values = [str(i + 1) for i in range(len(items))]
    else:
        options = [str(v) for v in items]
        raw_values = list(options)
    return options, raw_values


def _direct_child_blocks(stmt):
    """One level of nested statement-lists belonging to `stmt` (its
    body/orelse/finalbody, and each except-handler's body for a Try)."""
    blocks = []
    for field_name in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field_name, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            blocks.append(block)
    if isinstance(stmt, ast.Try):
        for handler in stmt.handlers:
            if handler.body:
                blocks.append(handler.body)
    return blocks


def _contains_call(stmt, call):
    return any(node is call for node in ast.walk(stmt))


def _contains_input_call(stmt):
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input"
        for node in ast.walk(stmt)
    )


def _find_menu_for_call(block, call, literal_lists):
    """Depth-first: find the innermost block of statements that directly
    contains `call` (descending into if/for/while/try/function bodies so
    a menu right next to the input(), not one merely earlier in an
    enclosing function, wins), then look backward through that same
    block for either a for-loop that builds a numbered/listed menu, or a
    run of plain print(...) statements with numbered/bracketed option
    lines -- covers both `for i, x in enumerate(LIST): print(...)` and
    literal `print("[1] ...")` per line above the input().

    Stops looking the instant it hits another input()-bearing statement
    while scanning backward -- that boundary means whatever's beyond it
    belongs to a *different*, already-answered prompt, not this one, so
    two menus back-to-back in the same block each get matched to their
    own input() rather than both grabbing whichever menu is found first.
    """
    for i, stmt in enumerate(block):
        if not _contains_call(stmt, call):
            continue
        for nested in _direct_child_blocks(stmt):
            if any(_contains_call(s, call) for s in nested):
                options, raw_values = _find_menu_for_call(nested, call, literal_lists)
                if options:
                    return options, raw_values
        for prev in reversed(block[:i]):
            if isinstance(prev, ast.For):
                options, raw_values = _menu_from_for_loop(prev, literal_lists)
                if options:
                    return options, raw_values
                break
            if _contains_input_call(prev):
                break
        return _collect_print_menu(block, i)
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
        source = _read_source_clean(script_path)
        tree = ast.parse(source, filename=script_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    source_lines = source.splitlines()
    literal_lists = _resolve_literal_lists(tree)

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

        # Hybrid detection, most-structured first: an AST scan understands
        # the code's actual shape (for-loops over a list, or a run of
        # plain print() statements) and wins whenever it can resolve
        # something; a permissive raw-text regex scan of the few lines
        # right above the input() is the fallback for whatever shape the
        # AST scan couldn't cleanly pin down (string concatenation, an
        # f-string with runtime pieces the strict extractor won't guess
        # at, etc.) -- so an unfamiliar menu style still has a decent
        # chance of being caught instead of falling through to a blank
        # text box.
        options, raw_values = _find_menu_for_call(tree.body, call, literal_lists)
        if not options:
            options, raw_values = _raw_regex_menu_scan(source_lines, call.lineno)
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
        source = _read_source_clean(script_path)
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
        source = _read_source_clean(script_path)
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
