"""
Universal Schema Template
--------------------------
Copy the SCHEMA dict below to the top of any Python script you want to run
through the Termux Script Management Wrapper.

The wrapper reads this dict with `ast.literal_eval` (schema_engine.py), so
it must contain only literal values: str, int, float, bool, list, dict.
No function calls, no f-strings with expressions, no imports inside it.
"""

SCHEMA = {
    "name": "My Script",
    "description": "One-line description of what this script does.",

    # Installed via: pkg install python -y && pip install <packages>
    "packages": ["requests", "bs4", "colorama"],

    # UI fields rendered dynamically in the wrapper app.
    # Supported types: "text", "number", "boolean", "select"
    "fields": [
        {
            "key": "target_url",
            "type": "text",
            "label": "Target URL",
            "default": "https://example.com",
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
    """Read this script's own saved settings back from Termux's shared dir.

    The wrapper app's "Save Config" delegates the actual write to Termux
    (its own process is scoped-storage-restricted on Android 10+ and can't
    reliably write shared paths itself), landing at
    /sdcard/termux_wrapper/config_<12-char md5 of the script's own run
    path>.json -- one small file per script rather than one shared file,
    so Termux can just overwrite it with `base64 -d > file`, no JSON
    read-modify-write required on that side. Must match
    termux_bridge.config_path_for() exactly.
    """
    import hashlib
    import json
    import os
    import sys

    script_path = os.path.abspath(sys.argv[0])
    digest = hashlib.md5(script_path.encode("utf-8")).hexdigest()[:12]
    config_path = f"/sdcard/termux_wrapper/config_{digest}.json"

    if not os.path.isfile(config_path):
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    cfg = load_saved_config()
    print("Loaded config:", cfg)
