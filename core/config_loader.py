import os
import json
from datetime import datetime

# Relative to the working directory, so run `python main.py` from the project root.
# main.py and this file then read and write the same daily_state.json.
STATE_FILE = "daily_state.json"


def _resolved_state_path():
    return os.path.abspath(STATE_FILE)


def get_today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _default_state(today):
    return {
        "status": "NOT_LOADED",
        "date": today,
        "jenis_penugasan": "",
        "tasks_criteria": []
    }


def load_daily_state():
    """Read today's setup from disk.

    Returns a NOT_LOADED state when the file is missing, unreadable or dated another day.
    """
    today = get_today_str()
    path = _resolved_state_path()

    if not os.path.exists(STATE_FILE):
        print(f"[INFO] No daily setup found at {path}")
        return _default_state(today)

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not read {path}: {e}. Daily setup reset.")
        return _default_state(today)

    if state.get("date") != today:
        print(f"[INFO] Daily setup is from {state.get('date')}, not today. Reset.")
        return _default_state(today)

    print(f"[INFO] Daily setup loaded: {len(state.get('tasks_criteria', []))} tasks ({path})")
    return state


def save_daily_state(jenis_penugasan, tasks_criteria):
    """Write today's setup to disk and return it."""
    state = {
        "date": get_today_str(),
        "status": "LOADED",
        "jenis_penugasan": jenis_penugasan,
        "tasks_criteria": tasks_criteria
    }

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"[ OK ] Daily setup saved: {len(tasks_criteria)} tasks ({_resolved_state_path()})")
    return state
