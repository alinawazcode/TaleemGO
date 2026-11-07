"""
users.py - Multi-user registry for Saathi WhatsApp bot.

Responsibilities:
- SQLite users table (chat_id, email, status, created_at, last_seen)
- Per-user directory: data/users/<sha256(chat_id)[:16]>/
- Encrypted token file: tokens.enc
- Per-user monitor state: monitor_state.json
- In-memory cache of LMSClient instances (one per active user)
- Per-user threading.Lock
"""

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT_DIR / ".env")

DB_PATH = str(DATA_DIR / "users.db")
USERS_DIR = DATA_DIR / "users"
USERS_DIR.mkdir(parents=True, exist_ok=True)

MAX_USERS = int(os.environ.get("MAX_USERS", "20"))

log = logging.getLogger("sathi.users")

# --- DB setup ---

_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    TEXT PRIMARY KEY,
                email      TEXT,
                status     TEXT NOT NULL DEFAULT 'awaiting_email',
                attempt    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                last_seen  TEXT
            )
        """)
        c.commit()


init_db()


# --- path helpers ---

def _slug(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode()).hexdigest()[:16]


def user_dir(chat_id: str) -> str:
    d = USERS_DIR / _slug(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def token_file(chat_id: str) -> str:
    return os.path.join(user_dir(chat_id), "tokens.enc")


# --- DB helpers ---

def get_user(chat_id: str):
    with _db_lock, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(chat_id: str, **kwargs):
    kwargs["last_seen"] = datetime.now(timezone.utc).isoformat()
    with _db_lock, _conn() as c:
        existing = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            c.execute(f"UPDATE users SET {sets} WHERE chat_id=?",
                      list(kwargs.values()) + [chat_id])
        else:
            kwargs["chat_id"] = chat_id
            kwargs.setdefault("created_at", kwargs["last_seen"])
            cols = ", ".join(kwargs.keys())
            qs = ", ".join("?" * len(kwargs))
            c.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", list(kwargs.values()))
        c.commit()


def delete_user(chat_id: str):
    with _db_lock, _conn() as c:
        c.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        c.commit()


def count_users() -> int:
    with _db_lock, _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def all_active_users():
    with _db_lock, _conn() as c:
        rows = c.execute("SELECT * FROM users WHERE status='active'").fetchall()
    return [dict(r) for r in rows]


# --- LMSClient cache ---

_client_cache: dict = {}
_client_locks: dict = {}
_cache_lock = threading.Lock()


def get_lock(chat_id: str) -> threading.Lock:
    with _cache_lock:
        if chat_id not in _client_locks:
            _client_locks[chat_id] = threading.Lock()
        return _client_locks[chat_id]


def get_client(chat_id: str):
    """Return cached LMSClient for an active user, or None if not cached yet."""
    with _cache_lock:
        return _client_cache.get(chat_id)


def set_client(chat_id: str, client):
    with _cache_lock:
        _client_cache[chat_id] = client


def remove_client(chat_id: str):
    with _cache_lock:
        _client_cache.pop(chat_id, None)
        _client_locks.pop(chat_id, None)


def purge_user_data(chat_id: str):
    """Delete DB row, in-memory client, and all files for this user."""
    import shutil
    delete_user(chat_id)
    remove_client(chat_id)
    d = USERS_DIR / _slug(chat_id)
    if d.exists():
        shutil.rmtree(d)
    log.info("Purged all data for chat %s…", chat_id[-6:])

