"""Thin client for the local WhatsApp bridge. monitor.py and the router use only these two functions."""
import os
import requests

BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:3001")
HEADERS = {"x-token": os.environ.get("BRIDGE_TOKEN", "")}


def send_text(text: str) -> None:
    r = requests.post(f"{BRIDGE}/send", json={"text": text}, headers=HEADERS, timeout=30)
    r.raise_for_status()


def send_file(path: str, caption: str = "") -> None:
    r = requests.post(f"{BRIDGE}/send-file", json={"path": os.path.abspath(path), "caption": caption},
                      headers=HEADERS, timeout=120)
    r.raise_for_status()