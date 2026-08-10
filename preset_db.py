"""Local SQLite storage for per-script step schemas and answer presets.

Phase 1 of the Preset Wizard needs to build/edit a named preset without
ever running the target script or sending anything to Termux. Storing
presets (and the step "schema" the wizard walks through) in a plain
local sqlite3 database under the app's own private storage
(App.user_data_dir) makes that trivially true: no storage permission,
no /sdcard scoped-storage quirks, no RUN_COMMAND round-trip -- just a
local file only this app ever touches.

The "schema" table holds evidence, not a guess: main.py's live step UI
(_show_step_ui) already detects each prompt's text and any menu options
straight from the script's REAL rendered output every time it runs
interactively (see schema_engine.parse_menu_options/last_prompt_line).
save_schema() just persists that same detected sequence so the wizard
can replay it later with zero execution, instead of the app trying to
independently guess what a script might ask.
"""
import json
import os
import sqlite3
import threading
import time

_DB_PATH = None
_LOCK = threading.Lock()


def _default_db_path():
    try:
        from kivy.app import App
        app = App.get_running_app()
        if app is not None:
            return os.path.join(app.user_data_dir, "presets.db")
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.db")


def _connect():
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = _default_db_path()
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS script_schemas ("
        "script_name TEXT PRIMARY KEY, "
        "steps_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS presets ("
        "script_name TEXT NOT NULL, "
        "preset_name TEXT NOT NULL, "
        "answers_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (script_name, preset_name))"
    )
    return conn


def save_schema(script_name, steps):
    """Persist the ordered list of {"prompt", "options", "raw_values"}
    step dicts recorded from a real interactive run."""
    if not script_name or not steps:
        return
    payload = json.dumps(steps, ensure_ascii=False)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO script_schemas (script_name, steps_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(script_name) DO UPDATE SET "
                "steps_json=excluded.steps_json, updated_at=excluded.updated_at",
                (script_name, payload, str(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


def load_schema(script_name):
    if not script_name:
        return []
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT steps_json FROM script_schemas WHERE script_name = ?",
                (script_name,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return []
    try:
        steps = json.loads(row[0])
    except (ValueError, TypeError):
        return []
    return steps if isinstance(steps, list) else []


def save_preset(script_name, preset_name, answers):
    if not script_name or not preset_name:
        return
    payload = json.dumps(list(answers), ensure_ascii=False)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO presets (script_name, preset_name, answers_json, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(script_name, preset_name) DO UPDATE SET "
                "answers_json=excluded.answers_json, created_at=excluded.created_at",
                (script_name, preset_name, payload, str(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


def load_presets(script_name):
    if not script_name:
        return {}
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT preset_name, answers_json FROM presets WHERE script_name = ?",
                (script_name,),
            ).fetchall()
        finally:
            conn.close()
    result = {}
    for name, payload in rows:
        try:
            answers = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if isinstance(answers, list):
            result[name] = answers
    return result
