"""Thin client for the local WhatsApp bridge — multi-user version."""
import inspect
import logging
import os
import time
import threading
import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:3001")
_TOKEN = os.environ.get("BRIDGE_TOKEN", "")

log = logging.getLogger("taleemgo.wa_transport")

_send_lock = threading.Lock()
_last_send = 0.0
MIN_INTERVAL = 1.5          # seconds between sends (anti-spam)


def _headers():
    return {"x-token": _TOKEN}


def _throttle():
    global _last_send
    with _send_lock:
        wait = MIN_INTERVAL - (time.time() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.time()


def _get_caller_name(default_name: str) -> str:
    try:
        stack = inspect.stack()
        if len(stack) > 2:
            return stack[2].function
        elif len(stack) > 1:
            return stack[1].function
    except Exception:
        pass
    return default_name


def send_text(chat_id: str, text: str) -> None:
    if text is None or not str(text).strip():
        caller = _get_caller_name("send_text")
        log.info("[SEND] empty text suppressed caller=%s", caller)
        return
    _throttle()
    for attempt in (1, 2):
        try:
            r = requests.post(
                f"{BRIDGE}/send",
                json={"chat_id": chat_id, "text": text},
                headers=_headers(),
                timeout=30,
            )
            if r.status_code == 503 and attempt == 1:
                time.sleep(3)
                continue
            if not r.ok:
                err_msg = ""
                try:
                    err_msg = r.json().get("error", r.text[:200])
                except Exception:
                    err_msg = r.text[:200]
                log.error("Bridge error sending to %s…: status=%s, err=%s", chat_id[-6:], r.status_code, err_msg)
            r.raise_for_status()
            return
        except requests.HTTPError:
            if attempt == 2:
                raise


def send_file(chat_id: str, path: str, caption: str = "") -> None:
    if not caption or not str(caption).strip():
        caption = os.path.basename(path)
    _throttle()
    for attempt in (1, 2):
        try:
            r = requests.post(
                f"{BRIDGE}/send-file",
                json={"chat_id": chat_id, "path": os.path.abspath(path), "caption": caption},
                headers=_headers(),
                timeout=120,
            )
            if r.status_code == 503 and attempt == 1:
                time.sleep(3)
                continue
            if not r.ok:
                err_msg = ""
                try:
                    err_msg = r.json().get("error", r.text[:200])
                except Exception:
                    err_msg = r.text[:200]
                log.error("Bridge error sending file to %s…: status=%s, err=%s", chat_id[-6:], r.status_code, err_msg)
            r.raise_for_status()
            return
        except requests.HTTPError:
            if attempt == 2:
                raise
