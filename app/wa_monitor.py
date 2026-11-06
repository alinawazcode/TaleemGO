"""WhatsApp version of the monitor. Same logic as monitor.py, but sync and sending via wa_transport.
First run only records a baseline and sends nothing.
State is saved only if every alert was delivered, so a bridge outage never loses alerts.
"""
import logging
from datetime import datetime, timezone

from state import load_state, save_state
from timeutils import to_local
import wa_transport as wa

log = logging.getLogger("sathi.wa_monitor")
REMINDER_WINDOWS = [(3, "3 day"), (1, "1 day")]


def course_label(course):
    return f"{course.get('code')} - {course.get('name')}" if course else "Unknown course"


def check_updates(lms):
    state = load_state()
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

        for text in alerts:          # any failure raises -> state not saved -> retried next cycle
            wa.send_text(text)

        state["resources"] = [r["id"] for r in resources]
        state["assignments"] = [a["id"] for a in assignments]
        state["attendance"] = [r["id"] for r in records]
        state["reminders_sent"] = list(sent)
        if first_run:
            state["seeded"] = True
            log.info("Seeded baseline: %d resources, %d assignments, %d attendance",
                     len(resources), len(assignments), len(records))
        save_state(state)
        if alerts:
            log.info("Sent %d alerts", len(alerts))
    except Exception:
        log.exception("Monitor run failed - will retry next cycle")