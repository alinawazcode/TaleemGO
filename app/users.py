"""
users.py - Multi-user registry for TaleemGo WhatsApp bot.

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
from datetime import datetime, timezone, timedelta
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

log = logging.getLogger("taleemgo.users")

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
                chat_id          TEXT PRIMARY KEY,
                email            TEXT,
                status           TEXT NOT NULL DEFAULT 'awaiting_email',
                attempt          INTEGER NOT NULL DEFAULT 0,
                is_admin         INTEGER NOT NULL DEFAULT 0,
                lang             TEXT NOT NULL DEFAULT 'en',
                llm_locked_until TEXT,
                created_at       TEXT,
                last_seen        TEXT
            )
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "is_admin" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if "lang" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
        if "llm_locked_until" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN llm_locked_until TEXT")

        c.execute("""
            CREATE TABLE IF NOT EXISTS i18n_cache (
                lang TEXT,
                key  TEXT,
                text TEXT,
                PRIMARY KEY (lang, key)
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
    """Delete DB row, in-memory client and lock, pending choice, history, strikes, and all files for this user."""
    import shutil
    delete_user(chat_id)
    clear_pending(chat_id)
    clear_pending_action(chat_id)
    clear_strikes(chat_id)
    clear_hourly_calls(chat_id)
    try:
        try:
            from app.llm.intent import clear_user_history
        except ImportError:
            from llm.intent import clear_user_history
        clear_user_history(chat_id)
    except Exception:
        pass
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


# --- In-memory pending actions (10 min TTL) ---

_pending_actions: dict = {}
_pending_action_lock = threading.Lock()


def set_pending_action(chat_id: str, action: dict, ttl: int = 600):
    """Store unfinished action awaiting a course query in memory with TTL."""
    with _pending_action_lock:
        act = dict(action)
        act["ts"] = time.time()
        act["expires_at"] = time.time() + ttl
        _pending_actions[chat_id] = act


def get_pending_action(chat_id: str):
    """Retrieve stored unfinished action if not expired."""
    with _pending_action_lock:
        act = _pending_actions.get(chat_id)
        if not act:
            return None
        if time.time() > act.get("expires_at", 0):
            _pending_actions.pop(chat_id, None)
            return None
        return act


def clear_pending_action(chat_id: str):
    """Clear stored pending action."""
    with _pending_action_lock:
        _pending_actions.pop(chat_id, None)


# --- Strike tracking & Lockout ---

_user_strikes: dict = {}
_strikes_lock = threading.Lock()

_user_hourly_calls: dict = {}
_hourly_lock = threading.Lock()


def clear_strikes(chat_id: str):
    with _strikes_lock:
        _user_strikes.pop(chat_id, None)


def clear_hourly_calls(chat_id: str):
    with _hourly_lock:
        _user_hourly_calls.pop(chat_id, None)


def record_strike(chat_id: str, strikes: int = 1, is_admin: bool = False) -> bool:
    """
    Record off-topic strike(s).
    If strikes reach threshold within LLM_STRIKE_WINDOW_MIN, initiate lockout for LLM_LOCK_HOURS.
    Returns True if a lockout just began, False otherwise.
    """
    admin_exempt = int(os.environ.get("LLM_ADMIN_EXEMPT", "1"))
    if is_admin and admin_exempt:
        return False

    strike_threshold = int(os.environ.get("LLM_STRIKES", "2"))
    window_sec = int(os.environ.get("LLM_STRIKE_WINDOW_MIN", "10")) * 60
    lock_hours = int(os.environ.get("LLM_LOCK_HOURS", "2"))

    now = time.time()
    with _strikes_lock:
        timestamps = _user_strikes.get(chat_id, [])
        timestamps = [ts for ts in timestamps if now - ts < window_sec]
        for _ in range(strikes):
            timestamps.append(now)

        if len(timestamps) >= strike_threshold:
            _user_strikes.pop(chat_id, None)
            locked_until = datetime.now(timezone.utc) + timedelta(hours=lock_hours)
            upsert_user(chat_id, llm_locked_until=locked_until.isoformat())
            log.warning("User %s locked out until %s", chat_id[-6:], locked_until.isoformat())
            return True
        else:
            _user_strikes[chat_id] = timestamps
            return False


def is_llm_locked(chat_id: str, is_admin: bool = False) -> bool:
    """Check if user is currently locked out from natural language chat."""
    admin_exempt = int(os.environ.get("LLM_ADMIN_EXEMPT", "1"))
    if is_admin and admin_exempt:
        return False

    user = get_user(chat_id)
    if not user:
        return False

    locked_str = user.get("llm_locked_until")
    if not locked_str:
        return False

    try:
        locked_until = datetime.fromisoformat(locked_str)
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < locked_until:
            return True
        else:
            # Expired: clear lockout and strikes
            upsert_user(chat_id, llm_locked_until=None)
            clear_strikes(chat_id)
            return False
    except Exception:
        upsert_user(chat_id, llm_locked_until=None)
        return False


def count_locked_users() -> int:
    """Return count of users currently under active lockout."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with _db_lock, _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM users WHERE llm_locked_until IS NOT NULL AND llm_locked_until > ?", (now_iso,)).fetchone()
        return row[0] if row else 0


def check_and_record_llm_limit(chat_id: str) -> bool:
    """
    Check if user exceeded LLM_USER_LIMIT_PER_HOUR (default 20).
    Returns True if allowed (and records the call), False if limit reached.
    """
    limit = int(os.environ.get("LLM_USER_LIMIT_PER_HOUR", "20"))
    now = time.time()
    with _hourly_lock:
        calls = _user_hourly_calls.get(chat_id, [])
        calls = [ts for ts in calls if now - ts < 3600]
        if len(calls) >= limit:
            _user_hourly_calls[chat_id] = calls
            return False
        calls.append(now)
        _user_hourly_calls[chat_id] = calls
        return True
