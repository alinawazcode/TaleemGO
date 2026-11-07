"""
WhatsApp command router — multi-user version.
Run with:  uvicorn app.wa_bot:api --port 8000

Incoming JSON: { "chat_id": "...", "text": "...", "is_self": true/false }
Conversation states: awaiting_email → awaiting_password → active | expired
"""
import logging
import os
import re
import sys
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from courses import course_acronym, format_course_list, resolve_course  # noqa: E402
from lms_client import LMSClient, LMSAuthExpired, LMSError             # noqa: E402
from timeutils import to_local                                          # noqa: E402
import wa_transport as wa                                               # noqa: E402
import users as usr                                                     # noqa: E402
from wa_monitor import run_all_users                                    # noqa: E402

log = logging.getLogger("taleemgo.wa")
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
MAX_USERS = int(os.environ.get("MAX_USERS", "5"))
ATTENDANCE_WARN = float(os.environ.get("ATTENDANCE_WARN", "75"))

ACTIVATION_PHRASES = [
    p.strip().lower()
    for p in os.environ.get("ACTIVATION_PHRASES", "taleemgo ai,taleem go ai,taleemgo").split(",")
    if p.strip()
]

HELP = (
    "TaleemGo AI commands:\n"
    "courses\nassignments\nupcoming\n"
    "resources <course>\nattendance [course]\nclos <course>\n"
    "download <course> [week N | title]\n"
    "logout  — disconnect your account\n"
    "login   — re-link after session expiry"
)

WELCOME = (
    "👋 Welcome to TaleemGo AI — your LMS assistant on WhatsApp!\n\n"
    "To get started, please send me your *LMS email address*.\n\n"
    "Send your LMS email to begin."
)

WEEK_RE = re.compile(r"^(?:week|wk|w|lecture|lec|l)\s*(\d+)$", re.IGNORECASE)

ASSIGNMENT_EMOJI = {
    "submitted": "✅",
    "pending": "⏳",
    "overdue": "❌",
    "logged_no_file": "📝",
}

api = FastAPI()


@api.on_event("startup")
def _start_monitor():
    from apscheduler.schedulers.background import BackgroundScheduler
    minutes = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "30"))
    sched = BackgroundScheduler()
    sched.add_job(run_all_users, "interval", minutes=minutes,
                  next_run_time=datetime.now() + timedelta(seconds=60))
    sched.add_job(usr.cleanup_stale_signups, "interval", minutes=2,
                  next_run_time=datetime.now() + timedelta(seconds=30))
    sched.start()
    api.state.sched = sched
    log.info("Monitor scheduled every %d min; stale cleanup every 2 min", minutes)


# ---- helpers ----

def _reply(chat_id: str, text: str):
    try:
        wa.send_text(chat_id, text)
    except Exception:
        log.exception("Failed to send reply to %s…", chat_id[-6:])


def _clean_text(s: str) -> str:
    """Normalize text for activation phrase matching: lowercase, strip punctuation, collapse whitespace."""
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", s or "").lower().split())


def contains_activation_phrase(text: str, phrases=None) -> bool:
    if phrases is None:
        phrases = ACTIVATION_PHRASES
    clean_msg = _clean_text(text)
    for p in phrases:
        clean_p = _clean_text(p)
        if clean_p and clean_p in clean_msg:
            return True
    return False


# ---- command handlers ----

def cmd_courses(lms, _=None):
    courses = lms.get_courses()
    if not courses:
        return "No courses found."
    return format_course_list(courses)


def cmd_assignments(lms, _=None):
    rows = lms.get_assignments_with_status()
    if not rows:
        return "No assignments."
    lines = []
    for a in rows:
        st = a.get("status", "pending")
        emoji = ASSIGNMENT_EMOJI.get(st, "⏳")
        lines.append(f"{emoji} [{st.capitalize()}] {to_local(a['due_date'])}  {a['title']}")
    return "\n".join(lines)


def cmd_upcoming(lms, _=None):
    rows = [a for a in lms.get_assignments_with_status() if a.get("status") == "pending"]
    if not rows:
        return "Nothing pending."
    lines = []
    for a in rows:
        lines.append(f"⏳ {to_local(a['due_date'])}  {a['title']}")
    return "\n".join(lines)


def _format_att_stats(pres: int, tot: int, pct, absent: int = None) -> str:
    if tot == 0 or pct is None:
        status_str = "0/0 present, N/A"
    else:
        icon = "⚠️" if pct < ATTENDANCE_WARN else "✅"
        status_str = f"{pres}/{tot} present, {pct}% {icon}"
    if absent is not None:
        status_str += f", absent: {absent}"
    return status_str


def _show_course_attendance(chat_id: str, lms, course: dict, att: dict = None):
    if att is None:
        att = lms.get_attendance()
    c_att = att.get("courses", {}).get(course["id"], {})
    pres = c_att.get("present", 0)
    tot = c_att.get("total", 0)
    pct = c_att.get("percent")
    absent = c_att.get("absent", 0)
    acr = course_acronym(course.get("name", ""))
    label = f"{acr} ({course['code']})" if acr else course["code"]
    stats = _format_att_stats(pres, tot, pct, absent=absent)
    _reply(chat_id, f"{label}: {stats}")


def _handle_attendance_cmd(chat_id: str, lms, arg: str):
    att = lms.get_attendance()
    courses = lms.get_courses()
    if not arg:
        o = att.get("overall", {})
        pres = o.get("present", 0)
        tot = o.get("total", 0)
        pct = o.get("percent")
        overall_line = f"Overall attendance: {_format_att_stats(pres, tot, pct)}"
        lines = [overall_line]
        for c in courses:
            c_att = att.get("courses", {}).get(c["id"])
            if c_att:
                acr = course_acronym(c.get("name", ""))
                label = f"{acr} ({c['code']})" if acr else c["code"]
                stats = _format_att_stats(c_att.get("present", 0), c_att.get("total", 0), c_att.get("percent"))
                lines.append(f"{label}: {stats}")
        _reply(chat_id, "\n".join(lines))
        return

    matches = resolve_course(lms, arg)
    if not matches:
        _reply(chat_id, "I couldn't find that course. Your courses:\n" + format_course_list(courses))
    elif len(matches) == 1:
        _show_course_attendance(chat_id, lms, matches[0], att)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "attendance", "extra": ""})
        _reply(chat_id, f"Which course?\n{options}\n\nReply with a number. Reply \"cancel\" to stop.")


def _show_course_resources(chat_id: str, lms, course: dict):
    res = [r for r in lms.get_resources() if r["course_id"] == course["id"]]
    if not res:
        _reply(chat_id, f"No resources uploaded yet for {course['code']} - {course['name']}.")
        return

    weeks_map = {}
    for r in res:
        wn = r.get("week_number")
        weeks_map.setdefault(wn, []).append(r)

    sorted_weeks = sorted([w for w in weeks_map.keys() if w is not None])
    if None in weeks_map:
        sorted_weeks.append(None)

    lines = [f"{course['code']} - {course['name']} Resources:"]
    file_idx = 1
    for w in sorted_weeks:
        header = f"\nWeek {w}:" if w is not None else "\nUnscheduled:"
        lines.append(header)
        for r in weeks_map[w]:
            lines.append(f"  {file_idx}. {r['title']}")
            file_idx += 1

    _reply(chat_id, "\n".join(lines))


def _handle_resources_cmd(chat_id: str, lms, arg: str):
    courses = lms.get_courses()
    if not arg:
        _reply(chat_id, "Usage: resources <course>\n\nYour courses:\n" + format_course_list(courses))
        return
    matches = resolve_course(lms, arg)
    if not matches:
        _reply(chat_id, "I couldn't find that course. Your courses:\n" + format_course_list(courses))
    elif len(matches) == 1:
        _show_course_resources(chat_id, lms, matches[0])
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "resources", "extra": ""})
        _reply(chat_id, f"Which course?\n{options}\n\nReply with a number. Reply \"cancel\" to stop.")


def _show_course_clos(chat_id: str, lms, course: dict):
    clos = lms.get_clos(course["id"])
    if not clos:
        _reply(chat_id, f"No CLOs found for {course['code']} - {course['name']}.")
        return
    lines = [f"{course['code']} - {course['name']} CLOs:"]
    for x in clos:
        num = x.get("clo_number", "")
        desc = x.get("description", "")
        bt = x.get("bt_level")
        bt_str = f" [BT Level {bt}]" if bt else ""
        lines.append(f"CLO {num}: {desc}{bt_str}")
    _reply(chat_id, "\n".join(lines))


def _handle_clos_cmd(chat_id: str, lms, arg: str):
    courses = lms.get_courses()
    if not arg:
        _reply(chat_id, "Usage: clos <course>\n\nYour courses:\n" + format_course_list(courses))
        return
    matches = resolve_course(lms, arg)
    if not matches:
        _reply(chat_id, "I couldn't find that course. Your courses:\n" + format_course_list(courses))
    elif len(matches) == 1:
        _show_course_clos(chat_id, lms, matches[0])
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "clos", "extra": ""})
        _reply(chat_id, f"Which course?\n{options}\n\nReply with a number. Reply \"cancel\" to stop.")


def _send_files_choice(chat_id: str, lms, files: list, indices: list = None):
    if indices is None:
        to_send = files[:10]  # maximum 10 files per request
    else:
        to_send = [files[i - 1] for i in indices if 1 <= i <= len(files)][:10]

    if not to_send:
        return

    folder = usr.user_downloads_dir(chat_id)
    usr.cleanup_user_downloads(chat_id)

    if len(to_send) == 1 and indices is not None and len(indices) == 1:
        r = to_send[0]
        try:
            path = lms.download_resource(r["id"], folder=folder)
            wa.send_file(chat_id, path, r.get("title", ""))
        except LMSError as e:
            _reply(chat_id, str(e))
        except Exception:
            log.exception("Failed to download resource %s for %s…", r.get("id"), chat_id[-6:])
            _reply(chat_id, f"Could not download {r.get('title', 'file')}. Please try again later.")
        return

    total = len(to_send)
    sent_count = 0
    failed_names = []

    for r in to_send:
        try:
            path = lms.download_resource(r["id"], folder=folder)
            wa.send_file(chat_id, path, r.get("title", ""))
            sent_count += 1
        except Exception as e:
            failed_names.append(r.get("title", "Unknown file"))
            log.warning("Failed to download/send file %s: %s", r.get("id"), str(e))

    if not failed_names:
        _reply(chat_id, f"Sent {sent_count} of {total} files.")
    else:
        failed_str = ", ".join(failed_names)
        _reply(chat_id, f"Sent {sent_count} of {total} files ({len(failed_names)} failed: {failed_str}).")


def _handle_download_for_course(chat_id: str, lms, course: dict, remainder: str):
    all_res = [r for r in lms.get_resources() if r["course_id"] == course["id"]]
    acr = course_acronym(course.get("name", "")) or course["code"]

    if not remainder:
        if not all_res:
            _reply(chat_id, f"No files found in {course['code']} - {course['name']}.")
            return
        weeks_map = {}
        for r in all_res:
            wn = r.get("week_number")
            weeks_map.setdefault(wn, []).append(r)
        sorted_weeks = sorted([w for w in weeks_map.keys() if w is not None])
        if None in weeks_map:
            sorted_weeks.append(None)

        lines = [f"Files in {acr} ({course['code']}):"]
        flat_files = []
        file_idx = 1
        for w in sorted_weeks:
            header = f"\nWeek {w}:" if w is not None else "\nUnscheduled:"
            lines.append(header)
            for r in weeks_map[w]:
                lines.append(f"  {file_idx}. {r['title']}")
                flat_files.append(r)
                file_idx += 1

        lines.append('\nReply with a number (e.g. 2), several numbers (1,3), or "all". Reply "cancel" to stop.')
        usr.set_pending(chat_id, {"type": "files", "files": flat_files, "course": course})
        _reply(chat_id, "\n".join(lines))
        return

    m_week = WEEK_RE.match(remainder)
    if m_week:
        week_num = int(m_week.group(1))
        week_files = [r for r in all_res if r.get("week_number") == week_num]
        if not week_files:
            avail = sorted({r["week_number"] for r in all_res if r.get("week_number") is not None})
            if avail:
                weeks_str = ", ".join(str(w) for w in avail)
                _reply(chat_id, f"No files for week {week_num} in {acr}. Weeks with files: {weeks_str}.")
            else:
                _reply(chat_id, f"No files for week {week_num} in {acr}.")
            return
        elif len(week_files) == 1:
            _send_files_choice(chat_id, lms, week_files, indices=[1])
            return
        else:
            lines = [f"Week {week_num} of {acr} has {len(week_files)} files:"]
            for i, r in enumerate(week_files, 1):
                lines.append(f"{i}. {r['title']}")
            lines.append('\nReply with a number (e.g. 2), several numbers (1,3), or "all". Reply "cancel" to stop.')
            usr.set_pending(chat_id, {"type": "files", "files": week_files, "course": course})
            _reply(chat_id, "\n".join(lines))
            return
    else:
        q_words = remainder.lower()
        hits = [r for r in all_res if q_words in r["title"].lower()]
        if not hits:
            _reply(chat_id, "No matching file.")
            return
        elif len(hits) == 1:
            _send_files_choice(chat_id, lms, hits, indices=[1])
            return
        else:
            lines = [f"Found {len(hits)} files matching \"{remainder}\":"]
            for i, r in enumerate(hits, 1):
                lines.append(f"{i}. {r['title']}")
            lines.append('\nReply with a number (e.g. 2), several numbers (1,3), or "all". Reply "cancel" to stop.')
            usr.set_pending(chat_id, {"type": "files", "files": hits, "course": course})
            _reply(chat_id, "\n".join(lines))
            return


def _handle_download_cmd(chat_id: str, lms, arg: str):
    if not arg:
        _reply(chat_id, "Usage: download <course> [week N | title]")
        return

    words = arg.split()
    best_matches = []
    best_idx = 0
    for i in range(len(words), 0, -1):
        prefix = " ".join(words[:i])
        hits = resolve_course(lms, prefix)
        if hits:
            best_matches = hits
            best_idx = i
            break

    if not best_matches:
        courses = lms.get_courses()
        _reply(chat_id, "I couldn't find that course. Your courses:\n" + format_course_list(courses))
        return

    remainder = " ".join(words[best_idx:]).strip()

    if len(best_matches) == 1:
        _handle_download_for_course(chat_id, lms, best_matches[0], remainder)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(best_matches, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": best_matches,
            "command": "download",
            "extra": remainder,
        })
        _reply(chat_id, f"Which course?\n{options}\n\nReply with a number. Reply \"cancel\" to stop.")


def _execute_course_command(chat_id: str, lms, cmd: str, course: dict, extra: str = ""):
    if cmd == "attendance":
        _show_course_attendance(chat_id, lms, course)
    elif cmd == "resources":
        _show_course_resources(chat_id, lms, course)
    elif cmd == "clos":
        _show_course_clos(chat_id, lms, course)
    elif cmd == "download":
        _handle_download_for_course(chat_id, lms, course, extra)


def _handle_pending(chat_id: str, lms, pending: dict, text: str) -> bool:
    t = text.strip()
    tl = t.lower()

    if pending.get("type") == "files":
        if tl == "cancel":
            usr.clear_pending(chat_id)
            _reply(chat_id, "Download cancelled.")
            return True

        files = pending.get("files", [])
        if tl == "all":
            usr.clear_pending(chat_id)
            _send_files_choice(chat_id, lms, files, indices=None)
            return True

        tokens = [x for x in re.split(r"[\s,]+", t) if x]
        if tokens and all(x.isdigit() for x in tokens):
            nums = [int(x) for x in tokens]
            if any(n < 1 or n > len(files) for n in nums):
                _reply(chat_id, f"Please pick a number between 1 and {len(files)}.")
                return True
            usr.clear_pending(chat_id)
            _send_files_choice(chat_id, lms, files, indices=nums)
            return True

        usr.clear_pending(chat_id)
        return False

    elif pending.get("type") == "course":
        if tl == "cancel":
            usr.clear_pending(chat_id)
            _reply(chat_id, "Cancelled.")
            return True

        courses = pending.get("courses", [])
        if t.isdigit():
            idx = int(t)
            if 1 <= idx <= len(courses):
                usr.clear_pending(chat_id)
                chosen = courses[idx - 1]
                cmd = pending.get("command")
                extra = pending.get("extra", "")
                _execute_course_command(chat_id, lms, cmd, chosen, extra)
                return True
            else:
                _reply(chat_id, f"Please pick a number between 1 and {len(courses)}.")
                return True

        usr.clear_pending(chat_id)
        return False

    return False


def _handle_cmd_users(chat_id: str):
    summary = usr.get_users_summary()
    count = len(summary)
    lines = [f"Users: {count}/{MAX_USERS}"]
    for i, u in enumerate(summary, 1):
        email_str = u.get("email") or "(no email)"
        lines.append(f"{i}. {email_str} ({u.get('status', 'unknown')})")
    _reply(chat_id, "\n".join(lines))


# ---- conversation state machine ----

def handle(chat_id: str, text: str, is_self: bool = False):
    """Main dispatcher. Everything sync; called via run_in_threadpool."""
    lock = usr.get_lock(chat_id)
    with lock:
        _handle_locked(chat_id, text, is_self)


def _handle_locked(chat_id: str, text: str, is_self: bool = False):
    user = usr.get_user(chat_id)

    # Unknown user handling (BEFORE any other logic, including login, logout, help)
    if user is None:
        if not is_self:
            if not contains_activation_phrase(text):
                # Do nothing at all: no reply, no DB row, no directory, no log of text
                return
            if not usr.try_register(chat_id, is_admin=False):
                _reply(chat_id, f"TaleemGo AI is full right now ({MAX_USERS}/{MAX_USERS} users). Please try again later.")
                return
            _reply(chat_id, WELCOME)
            return
        else:
            # Self-chat skips activation phrase
            if not usr.try_register(chat_id, is_admin=True):
                _reply(chat_id, f"TaleemGo AI is full right now ({MAX_USERS}/{MAX_USERS} users). Please try again later.")
                return
            _reply(chat_id, WELCOME)
            return

    # Known user
    if is_self and not user.get("is_admin"):
        usr.upsert_user(chat_id, is_admin=1)
        user["is_admin"] = 1

    status = user["status"]

    # When awaiting_password, treat the entire message as password — never treat as command
    if status == "awaiting_password":
        _handle_awaiting_password(chat_id, user, text)
        return

    text_stripped = text.strip()
    word = text_stripped.split()[0].lower().lstrip("/") if text_stripped else ""

    if word == "logout":
        _do_logout(chat_id)
        return
    if word == "login":
        _do_login_reset(chat_id)
        return
    if word == "help":
        _reply(chat_id, HELP)
        return
    if word == "users":
        if user.get("is_admin"):
            _handle_cmd_users(chat_id)
        return

    if status == "awaiting_email":
        _handle_awaiting_email(chat_id, text)
    elif status == "active":
        _handle_active(chat_id, text, user)
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
    tf = usr.token_file(chat_id)
    try:
        lms = LMSClient.login_once(email=email, password=text, token_file=tf)
    except requests.HTTPError as e:
        status_code = getattr(e.response, "status_code", None)
        if status_code in (400, 401):
            attempt = user.get("attempt", 0) + 1
            log.info("Login attempt %d failed for %s…", attempt, chat_id[-6:])
            if attempt >= 3:
                usr.upsert_user(chat_id, status="awaiting_email", attempt=0, email=None)
                _reply(chat_id, "Too many failed attempts. Please send your LMS email to try again.")
            else:
                usr.upsert_user(chat_id, attempt=attempt)
                _reply(chat_id, f"Login failed. Check your email and password ({attempt}/3 attempts).")
        else:
            log.warning("LMS HTTP error %s for %s…", status_code, chat_id[-6:])
            _reply(chat_id, "LMS is unreachable right now, please try again in a few minutes.")
        return
    except Exception:
        log.exception("LMS connection failed for %s…", chat_id[-6:])
        _reply(chat_id, "LMS is unreachable right now, please try again in a few minutes.")
        return

    usr.set_client(chat_id, lms)
    usr.upsert_user(chat_id, status="active", attempt=0)

    # Silent baseline seed — run monitor once so old data doesn't flood alerts
    udir = usr.user_dir(chat_id)
    from wa_monitor import _run_for_user
    try:
        _run_for_user(chat_id)
    except Exception:
        log.exception("Baseline seed failed for %s…", chat_id[-6:])

    _reply(chat_id, f"✅ Connected as {email}\n\n{HELP}")


def _handle_active(chat_id: str, text: str, user: dict):
    lms = usr.get_client(chat_id)
    if lms is None:
        tf = usr.token_file(chat_id)
        if not os.path.exists(tf):
            usr.upsert_user(chat_id, status="awaiting_email")
            _reply(chat_id, "Session not found. Please send your LMS email to sign in again.")
            return
        lms = LMSClient(token_file=tf)
        usr.set_client(chat_id, lms)

    # Check if there is an in-memory pending selection
    pending = usr.get_pending(chat_id)
    if pending is not None:
        if _handle_pending(chat_id, lms, pending, text):
            return

    text_stripped = text.strip()
    word, _, arg = text_stripped.partition(" ")
    word = word.lower().lstrip("/")
    arg = arg.strip()

    try:
        if word in ("download", "dl"):
            _handle_download_cmd(chat_id, lms, arg)
        elif word == "attendance":
            _handle_attendance_cmd(chat_id, lms, arg)
        elif word == "resources":
            _handle_resources_cmd(chat_id, lms, arg)
        elif word == "clos":
            _handle_clos_cmd(chat_id, lms, arg)
        elif word == "courses":
            _reply(chat_id, cmd_courses(lms, arg))
        elif word == "assignments":
            _reply(chat_id, cmd_assignments(lms, arg))
        elif word == "upcoming":
            _reply(chat_id, cmd_upcoming(lms, arg))
        else:
            _reply(chat_id, HELP)

    except LMSAuthExpired:
        usr.upsert_user(chat_id, status="expired")
        usr.remove_client(chat_id)
        _reply(chat_id, "Your LMS session expired. Send `login` to sign in again.")
    except Exception:
        log.exception("Command failed for %s…", chat_id[-6:])
        _reply(chat_id, "Something went wrong talking to the LMS. Try again.")


def _do_logout(chat_id: str):
    usr.purge_user_data(chat_id)
    _reply(chat_id, 'Logged out and all your data deleted. Send "Hi TaleemGo AI" to start again.')


def _do_login_reset(chat_id: str):
    user = usr.get_user(chat_id)
    if user:
        usr.upsert_user(chat_id, status="awaiting_email", attempt=0)
        usr.remove_client(chat_id)
        _reply(chat_id, "Please send your LMS email to sign in.")
    else:
        if not usr.try_register(chat_id, is_admin=False):
            _reply(chat_id, f"TaleemGo AI is full right now ({MAX_USERS}/{MAX_USERS} users). Please try again later.")
            return
        _reply(chat_id, "Please send your LMS email to sign in.")


# ---- FastAPI endpoint ----

@api.post("/incoming")
async def incoming(request: Request, x_token: str = Header(default="")):
    if x_token != TOKEN:
        raise HTTPException(401)
    body = await request.json()
    chat_id = body.get("chat_id", "").strip()
    text = body.get("text", "").strip()
    is_self = bool(body.get("is_self", False))
    if not chat_id:
        raise HTTPException(400, "chat_id required")
    await run_in_threadpool(handle, chat_id, text, is_self)
    return {"ok": True}
