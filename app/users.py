"""
users.py - Multi-user registry for Saathi WhatsApp bot.

Responsibilities:
- SQLite users table (chat_id, email, status, attempt, is_admin, created_at, last_seen)
- Atomic registration with 5-user cap and owner slot reservation
- Per-user directory: data/users/<sha256(chat_id)[:16]>/
- Encrypted token file: tokens.enc
- Per-user monitor state: monitor_state.json
- Per-user downloads folder: data/users/<sha256(chat_id)[:16]>/downloads/
- In-memory cache of LMSClient instances (one per active user)
- Per-user threading.Lock
- Per-user in-memory pending selection (week/files choice, course choice)
"""

import hashlib
import logging
import os
import sqlite3
import threading
import time
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

MAX_USERS = int(os.environ.get("MAX_USERS", "5"))

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
                is_admin   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                last_seen  TEXT
            )
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "is_admin" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
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


def user_downloads_dir(chat_id: str) -> str:
    d = USERS_DIR / _slug(chat_id) / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def cleanup_user_downloads(chat_id: str, max_age_seconds: int = 24 * 3600):
    d = USERS_DIR / _slug(chat_id) / "downloads"
    if not d.exists():
        return
    now = time.time()
    for f in d.iterdir():
        if f.is_file():
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink(missing_ok=True)
            except Exception:
                pass


# --- DB helpers ---

def get_user(chat_id: str):
    with _db_lock, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(chat_id: str, **kwargs):
    kwargs.setdefault("last_seen", datetime.now(timezone.utc).isoformat())
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


def try_register(chat_id: str, is_admin: bool = False) -> bool:
    """
    Attempt atomic registration within a single SQLite transaction (BEGIN IMMEDIATE).
    Caps total users at MAX_USERS (default 5).
    Reserves one slot for the owner if no admin is registered yet.
    Returns True if successfully registered (or already registered), False if full.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT is_admin FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            if existing:
                if is_admin and not existing["is_admin"]:
                    conn.execute("UPDATE users SET is_admin=1, last_seen=? WHERE chat_id=?", (now, chat_id))
                    conn.commit()
                else:
                    conn.execute("UPDATE users SET last_seen=? WHERE chat_id=?", (now, chat_id))
                    conn.commit()
                return True

            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]

            if not is_admin:
                reserved = 1 if admin_count == 0 else 0
                if count + reserved >= MAX_USERS:
                    conn.rollback()
                    return False
            else:
                if count >= MAX_USERS:
                    conn.rollback()
                    return False

            admin_val = 1 if is_admin else 0
            conn.execute(
                "INSERT INTO users (chat_id, status, attempt, is_admin, created_at, last_seen) VALUES (?, 'awaiting_email', 0, ?, ?, ?)",
                (chat_id, admin_val, now, now)
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def cleanup_stale_signups(timeout_seconds: int = 15 * 60) -> int:
    """Delete abandoned sign-ups older than timeout_seconds."""
    now = datetime.now(timezone.utc)
    stale_chats = []
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT chat_id, created_at, last_seen FROM users WHERE status IN ('awaiting_email', 'awaiting_password')"
        ).fetchall()
        for r in rows:
            ts_str = r["last_seen"] or r["created_at"]
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if (now - ts).total_seconds() > timeout_seconds:
                        stale_chats.append(r["chat_id"])
                except Exception:
                    pass
    for cid in stale_chats:
        purge_user_data(cid)
        log.info("Cleaned up stale sign-up for %s…", cid[-6:])
    return len(stale_chats)


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


def get_users_summary() -> list[dict]:
    with _db_lock, _conn() as c:
        rows = c.execute("SELECT chat_id, email, status, is_admin, created_at, last_seen FROM users ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


# --- LMSClient cache & Locks ---

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
    """Remove LMSClient instance from cache. Retains lock to prevent race conditions."""
    with _cache_lock:
        _client_cache.pop(chat_id, None)


def purge_user_data(chat_id: str):
    """Delete DB row, in-memory client and lock, pending choice, and all files for this user."""
    import shutil
    delete_user(chat_id)
    clear_pending(chat_id)
    with _cache_lock:
        _client_cache.pop(chat_id, None)
        _client_locks.pop(chat_id, None)
    d = USERS_DIR / _slug(chat_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    log.info("Purged all data for chat %s…", chat_id[-6:])


# --- In-memory pending choices (10 min TTL) ---

_pending_choices: dict = {}
_pending_lock = threading.Lock()


def set_pending(chat_id: str, data: dict):
    with _pending_lock:
        data["expires_at"] = time.time() + data.get("ttl", 600)
        _pending_choices[chat_id] = data


def get_pending(chat_id: str):
    with _pending_lock:
        data = _pending_choices.get(chat_id)
        if not data:
            return None
        if time.time() > data.get("expires_at", 0):
            _pending_choices.pop(chat_id, None)
            return None
        return data


def clear_pending(chat_id: str):
    with _pending_lock:
        _pending_choices.pop(chat_id, None)
