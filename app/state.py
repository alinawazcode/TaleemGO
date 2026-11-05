import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = str(DATA_DIR / "monitor_state.json")

DEFAULT_STATE = {
    "seeded": False,       # True after the first run has recorded a baseline
    "resources": [],       # resource ids already seen
    "assignments": [],     # assignment ids already seen
    "attendance": [],      # attendance_records ids already seen
    "reminders_sent": [],  # "<assignment_id>:<3day|1day>" keys already sent
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {**DEFAULT_STATE, **data}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)