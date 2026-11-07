import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Legacy path kept for the Telegram monitor (app/monitor.py)
STATE_FILE = str(DATA_DIR / "monitor_state.json")

DEFAULT_STATE = {
    "seeded": False,
    "resources": [],
    "assignments": [],
    "attendance": [],
    "reminders_sent": [],
}


def load_state(user_dir=None):
    path = os.path.join(user_dir, "monitor_state.json") if user_dir else STATE_FILE
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {**DEFAULT_STATE, **data}


def save_state(state, user_dir=None):
    path = os.path.join(user_dir, "monitor_state.json") if user_dir else STATE_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)
