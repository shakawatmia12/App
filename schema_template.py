"""
Universal Schema Template
--------------------------
SCHEMA is entirely optional. Any plain .py script using ordinary input()
calls (numbered menus, plain questions, whatever) already works with the
wrapper app with zero changes -- it runs your script for real inside
Termux and shows buttons/an answer box for each prompt live, as your
script actually asks for it. There's no SCHEMA "fields" list to author
anymore; the app no longer tries to guess your script's questions ahead
of time by reading its source.

The only thing SCHEMA is still useful for is naming your script and
hinting extra pip packages beyond what's auto-detected from your own
`import` statements (needed only for something not directly imported,
e.g. a package needed by a sub-dependency).

Copy the dict below to the top of your script if you want that; it must
contain only literal values (str/int/float/bool/list/dict) -- the
wrapper reads it with `ast.literal_eval`, never by executing your file.
"""

SCHEMA = {
    "name": "My Script",
    "description": "One-line description of what this script does.",

    # Installed via: pkg install python -y && pip install <packages>
    # (merged with whatever this file's own `import` statements detect).
    "packages": ["requests", "bs4", "colorama"],
}
