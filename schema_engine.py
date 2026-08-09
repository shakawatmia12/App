"""Dynamic schema parser and JSON config persistence engine.

Reads a top-level `SCHEMA` dict from a target .py file using `ast`
(never `exec`/`eval` of the whole file), extracts the pip package list
and dynamic UI field definitions, and persists per-script user settings
to a single JSON file (default: /sdcard/config.json).
"""
import ast
import json
import os

CONFIG_PATH = "/sdcard/config.json"

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

    with open(script_path, "r", encoding="utf-8") as f:
        source = f.read()

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


def load_all_config(config_path=CONFIG_PATH):
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_config_for_script(script_path, config_path=CONFIG_PATH):
    """Return the previously saved settings dict for one specific script."""
    return load_all_config(config_path).get(script_path, {})


def save_config_for_script(script_path, values, config_path=CONFIG_PATH):
    """Persist `values` under `script_path` inside the shared config JSON.

    The file keeps one entry per script path so multiple wrapped scripts
    can share the same config.json without clobbering each other.
    """
    all_config = load_all_config(config_path)
    all_config[script_path] = values

    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(all_config, f, indent=2, ensure_ascii=False)

    return config_path


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
