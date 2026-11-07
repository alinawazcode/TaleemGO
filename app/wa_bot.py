"""
WhatsApp command router — multi-user version.
Run with:  uvicorn app.wa_bot:api --port 8000

Incoming JSON: { "chat_id": "...", "text": "..." }
Conversation states: awaiting_email → awaiting_password → active | expired
"""
import logging
import os
import re
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from lms_client import LMSClient, LMSAuthExpired   # noqa: E402
from timeutils import to_local                      # noqa: E402
import wa_transport as wa                           # noqa: E402
import users as usr                                 # noqa: E402
from wa_monitor import run_all_users                # noqa: E402

log = logging.getLogger("sathi.wa")
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
MAX_USERS = int(os.environ.get("MAX_USERS", "20"))

HELP = (
    "Sathi commands:\n"
    "courses\nassignments\nupcoming\n"
    "resources <course>\nattendance [course]\nclos <course>\n"
    "download <course> <part of title>\n"
    "logout  — disconnect your account\n"
    "login   — re-link after session expiry"
)

WELCOME = (
    "👋 Welcome to Sathi — your LMS assistant on WhatsApp!\n\n"
    "To get started, please send me your *LMS email address*."
)

api = FastAPI()


@api.on_event("startup")
def _start_monitor():
    import threading
    from apscheduler.schedulers.background import BackgroundScheduler
    minutes = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "30"))
    sched = BackgroundScheduler()
    sched.add_job(run_all_users, "interval", minutes=minutes,
                  next_run_time=datetime.now() + timedelta(seconds=60))
    sched.start()
    api.state.sched = sched
    log.info("Monitor scheduled every %d min", minutes)


# ---- helpers ----

def _reply(chat_id: str, text: str):
    try:
        wa.send_text(chat_id, text)
    except Exception:
        log.exception("Failed to send reply to %s…", chat_id[-6:])


def find_course(lms, query: str):
    q = query.strip().lower()
    for c in lms.get_courses():
        if q and (q in c["code"].lower() or q in c["name"].lower()):
            return c
    return None


# ---- command handlers (each takes lms client + arg string) ----

def cmd_courses(lms, _):
    return "\n".join(f"{c['code']} - {c['name']}" for c in lms.get_courses())


def cmd_assignments(lms, _):
    rows = lms.get_assignments_with_status()
    return "\n".join(f"[{a['status']}] {to_local(a['due_date'])}  {a['title']}" for a in rows) or "No assignments."


def cmd_upcoming(lms, _):
    rows = [a for a in lms.get_assignments_with_status() if a["status"] == "pending"]
    return "\n".join(f"{to_local(a['due_date'])}  {a['title']}" for a in rows) or "Nothing pending."


def cmd_resources(lms, arg):
    c = find_course(lms, arg)
    if not c:
        return "Course not found. Try: resources AI"
    res = [r for r in lms.get_resources() if r["course_id"] == c["id"]]
    res.sort(key=lambda r: (r.get("week_number") is None, r.get("week_number") or 0))
    lines = [f"Week {r['week_number'] if r.get('week_number') is not None else '-'}: {r['title']}" for r in res]
    return f"{c['code']} {c['name']}\n" + "\n".join(lines)


def cmd_attendance(lms, arg):
    att = lms.get_attendance()
    if not arg:
        return f"Overall attendance: {att['overall']}"
    c = find_course(lms, arg)
    return f"{c['code']}: {att['courses'][c['id']]}" if c else "Course not found."


def cmd_clos(lms, arg):
    c = find_course(lms, arg)
    if not c:
        return "Course not found."
    return "\n".join(str(x) for x in lms.get_clos(c["id"]))


def cmd_download(lms, chat_id, arg):
    parts = arg.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: download <course> <part of title>"
    c = find_course(lms, parts[0])
    if not c:
        return "Course not found."
    hits = [r for r in lms.get_resources()
            if r["course_id"] == c["id"] and parts[1].lower() in r["title"].lower()]
    if not hits:
        return "No matching file."
    if len(hits) > 1:
        return "Several matches, be more specific:\n" + "\n".join(r["title"] for r in hits[:10])
    r = hits[0]
    path = lms.download_resource(r["id"])
    wa.send_file(chat_id, path, r["title"])
    return None


ROUTES = {
    "courses": cmd_courses,
    "assignments": cmd_assignments,
    "upcoming": cmd_upcoming,
    "resources": cmd_resources,
    "attendance": cmd_attendance,
    "clos": cmd_clos,
}


# ---- conversation state machine ----

def handle(chat_id: str, text: str):
    """Main dispatcher. Everything sync; called via run_in_threadpool."""
    lock = usr.get_lock(chat_id)
    with lock:
        _handle_locked(chat_id, text)


def _handle_locked(chat_id: str, text: str):
    text = text.strip()
    word = text.split()[0].lower().lstrip("/") if text else ""

    # Always-available commands
    if word == "logout":
        _do_logout(chat_id)
        return
    if word == "login":
        _do_login_reset(chat_id)
        return

    user = usr.get_user(chat_id)

    # Unknown user — start registration
    if user is None:
        if usr.count_users() >= MAX_USERS:
            _reply(chat_id, "Sorry, this bot is currently full. Please try again later.")
            return
        usr.upsert_user(chat_id, status="awaiting_email")
        _reply(chat_id, WELCOME)
        return

    status = user["status"]

    if word == "help":
        _reply(chat_id, HELP)
        return

    if status == "awaiting_email":
        _handle_awaiting_email(chat_id, text)
    elif status == "awaiting_password":
        _handle_awaiting_password(chat_id, user, text)
    elif status == "active":
        _handle_active(chat_id, text)
    elif status == "expired":
        _reply(chat_id, "Your session has expired. Send `login` to sign in again.")
    else:
        _reply(chat_id, WELCOME)


def _handle_awaiting_email(chat_id: str, text: str):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text):
        _reply(chat_id, "That doesn't look like an email address. Please send your LMS email.")
        return
    usr.upsert_user(chat_id, email=text, status="awaiting_password", attempt=0)
    _reply(chat_id,
           f"Got it! Now please send your LMS password.\n\n"
           f"⚠️ Use your LMS password only. I never store it — I only use it once to sign in.")


def _handle_awaiting_password(chat_id: str, user: dict, text: str):
    # IMPORTANT: never log text here
    email = user["email"]
    attempt = user.get("attempt", 0) + 1
    tf = usr.token_file(chat_id)
    try:
        lms = LMSClient.login_once(email=email, password=text, token_file=tf)
    except Exception:
        log.info("Login attempt %d failed for %s…", attempt, chat_id[-6:])
        if attempt >= 3:
            usr.upsert_user(chat_id, status="awaiting_email", attempt=0, email=None)
            _reply(chat_id, "Too many failed attempts. Please send your LMS email to try again.")
        else:
            usr.upsert_user(chat_id, attempt=attempt)
            _reply(chat_id, f"Login failed. Check your email and password ({attempt}/3 attempts).")
        return

    usr.set_client(chat_id, lms)
    usr.upsert_user(chat_id, status="active", attempt=0)

    # Silent baseline seed — run monitor once so old data doesn't flood alerts
    udir = usr.user_dir(chat_id)
    from wa_monitor import _run_for_user
    try:
        _run_for_user(chat_id)   # first run sets seeded=True, sends nothing
    except Exception:
        log.exception("Baseline seed failed for %s…", chat_id[-6:])

    _reply(chat_id, f"✅ Connected as {email}\n\n{HELP}")


def _handle_active(chat_id: str, text: str):
    lms = usr.get_client(chat_id)
    if lms is None:
        # Reload from token file
        tf = usr.token_file(chat_id)
        import os
        if not os.path.exists(tf):
            usr.upsert_user(chat_id, status="awaiting_email")
            _reply(chat_id, "Session not found. Please send your LMS email to sign in again.")
            return
        lms = LMSClient(token_file=tf)
        usr.set_client(chat_id, lms)

    word, _, arg = text.partition(" ")
    word = word.lower().lstrip("/")

    try:
        if word == "download":
            reply = cmd_download(lms, chat_id, arg)
        elif word in ROUTES:
            reply = ROUTES[word](lms, arg)
        else:
            reply = HELP

        if reply:
            _reply(chat_id, reply)

    except LMSAuthExpired:
        usr.upsert_user(chat_id, status="expired")
        usr.remove_client(chat_id)
        _reply(chat_id, "Your LMS session expired. Send `login` to sign in again.")
    except Exception:
        log.exception("Command failed for %s…", chat_id[-6:])
        _reply(chat_id, "Something went wrong talking to the LMS. Try again.")


def _do_logout(chat_id: str):
    usr.purge_user_data(chat_id)
    _reply(chat_id, "✅ Logged out. All your data has been deleted. Send any message to start again.")


def _do_login_reset(chat_id: str):
    user = usr.get_user(chat_id)
    if user:
        # Keep email for convenience, reset status
        usr.upsert_user(chat_id, status="awaiting_email", attempt=0)
        usr.remove_client(chat_id)
    else:
        if usr.count_users() >= MAX_USERS:
            _reply(chat_id, "Sorry, this bot is currently full.")
            return
        usr.upsert_user(chat_id, status="awaiting_email")
    _reply(chat_id, "Please send your LMS email to sign in.")


# ---- FastAPI endpoint ----

@api.post("/incoming")
async def incoming(request: Request, x_token: str = Header(default="")):
    if x_token != TOKEN:
        raise HTTPException(401)
    body = await request.json()
    chat_id = body.get("chat_id", "").strip()
    text = body.get("text", "").strip()
    if not chat_id:
        raise HTTPException(400, "chat_id required")
    await run_in_threadpool(handle, chat_id, text)
    return {"ok": True}
