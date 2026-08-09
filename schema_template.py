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
    """Read this script's own saved settings back from /sdcard/config.json.

    The wrapper app writes {"<absolute_script_path>": {...values...}} into
    that file before launching the script, so a wrapped script can call
    this at startup to pick up whatever the user configured in the UI.
    """
    import json
    import os
    import sys

    config_path = "/sdcard/config.json"
    script_path = os.path.abspath(sys.argv[0])

    if not os.path.isfile(config_path):
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        all_config = json.load(f)

    return all_config.get(script_path, {})


if __name__ == "__main__":
    cfg = load_saved_config()
    print("Loaded config:", cfg)
