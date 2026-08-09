"""
Universal Schema Template
--------------------------
Copy the SCHEMA dict below to the top of any Python script you want to run
through the Termux Script Management Wrapper.

The wrapper reads this dict with `ast.literal_eval` (schema_engine.py), so
it must contain only literal values: str, int, float, bool, list, dict.
No function calls, no f-strings with expressions, no imports inside it.

You normally don't need to list every dependency in "packages" -- the
wrapper also scans this file's own `import` statements and auto-detects
third-party packages when you tap "Install Packages". Declare packages
here only for things not directly imported (e.g. a package needed by a
sub-dependency) or to be explicit.
"""

SCHEMA = {
    "name": "My Script",
    "description": "One-line description of what this script does.",

    # Installed via: pkg install python -y && pip install <packages>
    # (merged with whatever this file's own `import` statements detect).
    "packages": ["requests", "bs4", "colorama"],

    # UI fields rendered dynamically in the wrapper app.
    # Supported types: "text", "number", "boolean", "select"
    # "required": True marks a field that must be non-empty before Save
    # Config / Run Script will proceed (checkboxes can't be "required",
    # they're always either True or False).
    "fields": [
        {
            "key": "target_url",
            "type": "text",
            "label": "Target URL",
            "default": "https://example.com",
            "required": True,
        },
        {
            "key": "timeout",
            "type": "number",
            "label": "Timeout (seconds)",
            "default": 30,
        },
        {
            "key": "verbose",
            "type": "boolean",
            "label": "Verbose output",
            "default": False,
        },
        {
            "key": "mode",
            "type": "select",
            "label": "Run mode",
            "options": ["safe", "fast", "stealth"],
            "default": "safe",
        },
    ],
}


def load_saved_config():
    """Read this script's own saved settings back from shared storage.

    The wrapper app's "Save Config" delegates the actual write to Termux
    (its own process is scoped-storage-restricted on Android 10+ and
    can't reliably write shared paths itself), landing at
    /storage/emulated/0/Documents/<APP_TITLE>/configs/<sanitized script
    name>_config.json. Must match main.py's APP_TITLE and
    schema_engine.sanitize_name()/config_filename_for() exactly.
    """
    import json
    import os
    import re
    import sys

    app_title = "Script Wrapper"  # must match buildozer.spec's [app] title

    def sanitize_name(name):
        stem = os.path.splitext(name or "script")[0]
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
        return cleaned or "script"

    script_name = sanitize_name(os.path.basename(sys.argv[0]))
    config_path = f"/storage/emulated/0/Documents/{app_title}/configs/{script_name}_config.json"

    if not os.path.isfile(config_path):
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return json.loads(content) if content else {}


if __name__ == "__main__":
    cfg = load_saved_config()
    print("Loaded config:", cfg)
