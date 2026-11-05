"""
monitor.py - periodic job that finds new resources/assignments/attendance
and sends Telegram alerts. Registered as a job_queue job in bot.py.

First run after a fresh install only records a baseline - it does NOT
send 56 "new lecture" messages for files that already existed.
"""

import logging
from datetime import datetime, timezone

from state import load_state, save_state
from timeutils import to_local

log = logging.getLogger("sathi.monitor")

REMINDER_WINDOWS = [(3, "3 day"), (1, "1 day")]  # (days before due, label)


def course_label(course):
    if not course:
        return "Unknown course"
    return f"{course.get('code')} - {course.get('name')}"


async def check_updates(context):
    lms = context.bot_data["lms"]
    chat_id = context.bot_data["authorized_id"]
    state = load_state()
    first_run = not state["seeded"]

    try:
        courses = {c["id"]: c for c in lms.get_courses()}

        # ---------- new resources ----------
        resources = lms.get_resources()
        seen = set(state["resources"])
        new_items = [r for r in resources if r["id"] not in seen]
        if not first_run:
            for r in new_items:
                c = courses.get(r["course_id"])
                week = (f"Week {r['week_number']}"
                        if r.get("week_number") is not None else "Unscheduled")
                await context.bot.send_message(
                    chat_id,
                    f"New lecture uploaded!\n\n"
                    f"Course: {course_label(c)}\n{week}\nTitle: {r['title']}\n\n"
                    f"/download {c['code'] if c else ''} {r['title'][:25]}"
                )
        state["resources"] = [r["id"] for r in resources]

        # ---------- new assignments ----------
        assignments = lms.get_assignments()
        seen = set(state["assignments"])
        new_items = [a for a in assignments if a["id"] not in seen]
        if not first_run:
            for a in new_items:
                c = courses.get(a["course_id"])
                await context.bot.send_message(
                    chat_id,
                    f"New assignment posted!\n\n"
                    f"Course: {course_label(c)}\nTitle: {a['title']}\n"
                    f"Due: {to_local(a['due_date'])}\nMax marks: {a.get('max_marks', 'n/a')}"
                )
        state["assignments"] = [a["id"] for a in assignments]

        # ---------- new attendance records ----------
        records = lms.get_attendance_records_detailed()
        seen = set(state["attendance"])
        new_items = [r for r in records if r["id"] not in seen]
        if not first_run:
            for r in new_items:
                c = courses.get(r["course_id"])
                await context.bot.send_message(
                    chat_id,
                    f"Attendance marked: {r['status'].upper()}\n"
                    f"Course: {course_label(c)}\nDate: {r.get('date')}"
                )
        state["attendance"] = [r["id"] for r in records]

        # ---------- deadline reminders (run every time, not just first run) ----------
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
                await context.bot.send_message(
                    chat_id,
                    f"Deadline reminder - {label} left\n\n"
                    f"Course: {course_label(c)}\nTitle: {a['title']}\n"
                    f"Due: {to_local(a['due_date'])}"
                )
                sent.add(key)
        state["reminders_sent"] = list(sent)

        if first_run:
            log.info("Monitor seeded baseline: %d resources, %d assignments, %d attendance",
                      len(resources), len(assignments), len(records))
            state["seeded"] = True

        save_state(state)

    except Exception:
        log.exception("Monitor run failed - will retry next cycle")