"""
WhatsApp monitor — multi-user version.
check_updates(user) handles one user dict (chat_id, email).
The scheduler job in wa_bot.py calls run_all_users() which loops sequentially.
"""
import logging
from datetime import datetime, timezone

from lms_client import LMSAuthExpired
from state import load_state, save_state
from timeutils import to_local
import wa_transport as wa
import users as usr

log = logging.getLogger("sathi.wa_monitor")
REMINDER_WINDOWS = [(3, "3 day"), (1, "1 day")]


def course_label(course):
    return f"{course.get('code')} - {course.get('name')}" if course else "Unknown course"


def check_updates(user: dict):
    """
    Run one monitor cycle for a single active user.
    user dict must have: chat_id, email, status.
    Raises nothing — all exceptions are caught internally.
    """
    chat_id = user["chat_id"]
    lock = usr.get_lock(chat_id)
    with lock:
        _run_for_user(chat_id)


def _run_for_user(chat_id: str):
    udir = usr.user_dir(chat_id)
    lms = usr.get_client(chat_id)
    if lms is None:
        # Reload from token file
        from lms_client import LMSClient
        tf = usr.token_file(chat_id)
        import os
        if not os.path.exists(tf):
            log.warning("No token file for %s…, skipping", chat_id[-6:])
            return
        lms = LMSClient(token_file=tf)
        usr.set_client(chat_id, lms)

    state = load_state(udir)
    first_run = not state["seeded"]
    alerts = []

    try:
        courses = {c["id"]: c for c in lms.get_courses()}

        resources = lms.get_resources()
        seen = set(state["resources"])
        for r in resources:
            if r["id"] in seen or first_run:
                continue
            c = courses.get(r["course_id"])
            week = f"Week {r['week_number']}" if r.get("week_number") is not None else "Unscheduled"
            alerts.append(
                f"New lecture uploaded!\n\nCourse: {course_label(c)}\n{week}\nTitle: {r['title']}\n\n"
                f"Reply: download {c['code'] if c else ''} {r['title'][:25]}")

        assignments = lms.get_assignments()
        seen = set(state["assignments"])
        for a in assignments:
            if a["id"] in seen or first_run:
                continue
            c = courses.get(a["course_id"])
            alerts.append(
                f"New assignment posted!\n\nCourse: {course_label(c)}\nTitle: {a['title']}\n"
                f"Due: {to_local(a['due_date'])}\nMax marks: {a.get('max_marks', 'n/a')}")

        records = lms.get_attendance_records_detailed()
        seen = set(state["attendance"])
        for r in records:
            if r["id"] in seen or first_run:
                continue
            c = courses.get(r["course_id"])
            alerts.append(f"Attendance marked: {r['status'].upper()}\n"
                          f"Course: {course_label(c)}\nDate: {r.get('date')}")

        now = datetime.now(timezone.utc)
        sent = set(state["reminders_sent"])
        for a in assignments:
            due = datetime.fromisoformat(a["due_date"].replace("Z", "+00:00"))
            hours_left = (due - now).total_seconds() / 3600
            for days, label in REMINDER_WINDOWS:
                key = f"{a['id']}:{days}day"
                if key in sent or not (0 <= hours_left <= days * 24):
                    continue
                c = courses.get(a["course_id"])
                alerts.append(f"Deadline reminder - {label} left\n\nCourse: {course_label(c)}\n"
                              f"Title: {a['title']}\nDue: {to_local(a['due_date'])}")
                sent.add(key)

        for text in alerts:
            wa.send_text(chat_id, text)

        state["resources"] = [r["id"] for r in resources]
        state["assignments"] = [a["id"] for a in assignments]
        state["attendance"] = [r["id"] for r in records]
        state["reminders_sent"] = list(sent)
        if first_run:
            state["seeded"] = True
            log.info("Seeded baseline for %s…: %d resources, %d assignments, %d attendance",
                     chat_id[-6:], len(resources), len(assignments), len(records))
        save_state(state, udir)
        if alerts:
            log.info("Sent %d alerts to %s…", len(alerts), chat_id[-6:])

    except LMSAuthExpired:
        log.warning("Session expired for %s…", chat_id[-6:])
        usr.upsert_user(chat_id, status="expired")
        usr.remove_client(chat_id)
        try:
            wa.send_text(chat_id, "Your LMS session has expired. Send `login` to sign in again.")
        except Exception:
            pass
    except Exception:
        log.exception("Monitor run failed for %s… — will retry next cycle", chat_id[-6:])


def run_all_users():
    """Called by the scheduler. Loops over all active users sequentially."""
    for user in usr.all_active_users():
        try:
            check_updates(user)
        except Exception:
            log.exception("Unexpected error for user %s…", user.get("chat_id", "?")[-6:])
