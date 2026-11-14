"""
WhatsApp monitor — multi-user version.
check_updates(user) handles one user dict (chat_id, email).
The scheduler job in wa_bot.py calls run_all_users() which loops sequentially.
"""
import logging
import os
from datetime import datetime, timezone

import requests
from lms_client import LMSAuthExpired
from state import load_state, save_state
from timeutils import to_local
from courses import course_acronym
import wa_transport as wa
import users as usr

log = logging.getLogger("taleemgo.wa_monitor")
REMINDER_WINDOWS = [(3, "3 day"), (1, "1 day")]


def course_label(course):
    if not course:
        return "Unknown course"
    acr = course_acronym(course.get("name", ""))
    prefix = f"{acr} - " if acr else ""
    return f"{prefix}{course.get('code')} - {course.get('name')}"


def course_grade_label(course):
    if not course:
        return "Unknown course"
    acr = course_acronym(course.get("name", ""))
    return f"{acr} ({course.get('code')})" if acr else str(course.get("code", ""))


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
    user = usr.get_user(chat_id)
    if not user or user.get("status") != "active":
        return

    udir = usr.user_dir(chat_id)
    try:
        lms = usr.get_client(chat_id)
        if lms is None:
            from lms_client import LMSClient
            tf = usr.token_file(chat_id)
            if not os.path.exists(tf):
                log.warning("No token file for %s…, skipping", chat_id[-6:])
                return
            lms = LMSClient(token_file=tf)
            usr.set_client(chat_id, lms)

        state = load_state(udir)
        first_run = not state.get("seeded", False)
        courses = {c["id"]: c for c in lms.get_courses()}

        resources = lms.get_resources()
        seen_res = set(state.get("resources", []))
        for r in resources:
            rid = r["id"]
            if rid in seen_res or first_run:
                continue
            c = courses.get(r.get("course_id"))
            acr = course_acronym(c.get("name", "")) if c else ""
            c_tag = acr or (c["code"] if c else "")
            if r.get("week_number") is not None:
                hint = f"Reply: download {c_tag} week {r['week_number']}".strip()
            else:
                hint = f"Reply: download {c_tag} {r['title'][:25]}".strip()
            week = f"Week {r['week_number']}" if r.get("week_number") is not None else "Unscheduled"
            text = (
                f"New lecture uploaded!\n\nCourse: {course_label(c)}\n{week}\nTitle: {r['title']}\n\n"
                f"{hint}"
            )
            wa.send_text(chat_id, text)
            seen_res.add(rid)
            state["resources"] = list(seen_res)
            save_state(state, udir)

        assignments = lms.get_assignments()
        seen_asgn = set(state.get("assignments", []))
        for a in assignments:
            aid = a["id"]
            if aid in seen_asgn or first_run:
                continue
            c = courses.get(a.get("course_id"))
            text = (
                f"New assignment posted!\n\nCourse: {course_label(c)}\nTitle: {a['title']}\n"
                f"Due: {to_local(a['due_date'])}\nMax marks: {a.get('max_marks', 'n/a')}"
            )
            wa.send_text(chat_id, text)
            seen_asgn.add(aid)
            state["assignments"] = list(seen_asgn)
            save_state(state, udir)

        records = lms.get_attendance_records_detailed()
        seen_att = set(state.get("attendance", []))
        for r in records:
            rid = r["id"]
            if rid in seen_att or first_run:
                continue
            c = courses.get(r.get("course_id"))
            text = (
                f"Attendance marked: {r['status'].upper()}\n"
                f"Course: {course_label(c)}\nDate: {r.get('date')}"
            )
            wa.send_text(chat_id, text)
            seen_att.add(rid)
            state["attendance"] = list(seen_att)
            save_state(state, udir)

        now = datetime.now(timezone.utc)
        sent = set(state.get("reminders_sent", []))
        for a in assignments:
            due = datetime.fromisoformat(a["due_date"].replace("Z", "+00:00"))
            hours_left = (due - now).total_seconds() / 3600
            for days, label in REMINDER_WINDOWS:
                key = f"{a['id']}:{days}day"
                if key in sent or not (0 <= hours_left <= days * 24):
                    continue
                c = courses.get(a.get("course_id"))
                text = (
                    f"Deadline reminder - {label} left\n\nCourse: {course_label(c)}\n"
                    f"Title: {a['title']}\nDue: {to_local(a['due_date'])}"
                )
                wa.send_text(chat_id, text)
                sent.add(key)
                state["reminders_sent"] = list(sent)
                save_state(state, udir)

        # Grade alerts
        grades = lms.get_grades()
        seen_grades = dict(state.get("grades", {}))
        grades_seeded = bool(state.get("grades_seeded", False))

        if not grades_seeded:
            for g_item in grades:
                grade_val = g_item.get("grade")
                if grade_val is None:
                    continue
                aid_str = str(g_item["id"])
                g_disp = f"{int(grade_val)}" if isinstance(grade_val, (int, float)) and grade_val == int(grade_val) else f"{grade_val:g}"
                mm = g_item.get("max_marks")
                if mm is not None:
                    mm_disp = f"{int(mm)}" if isinstance(mm, (int, float)) and mm == int(mm) else f"{mm:g}"
                    curr_str = f"{g_disp}/{mm_disp}"
                else:
                    curr_str = g_disp
                seen_grades[aid_str] = curr_str

            state["grades"] = seen_grades
            state["grades_seeded"] = True
            save_state(state, udir)
        else:
            for g_item in grades:
                grade_val = g_item.get("grade")
                if grade_val is None:
                    continue
                aid_str = str(g_item["id"])
                g_disp = f"{int(grade_val)}" if isinstance(grade_val, (int, float)) and grade_val == int(grade_val) else f"{grade_val:g}"
                mm = g_item.get("max_marks")
                if mm is not None:
                    mm_disp = f"{int(mm)}" if isinstance(mm, (int, float)) and mm == int(mm) else f"{mm:g}"
                    curr_str = f"{g_disp}/{mm_disp}"
                else:
                    curr_str = g_disp

                if aid_str not in seen_grades:
                    c = courses.get(g_item.get("course_id"))
                    text = f"New grade posted\nCourse: {course_grade_label(c)}\n{g_item['title']}: {curr_str}"
                    fb = (g_item.get("feedback") or "").strip()
                    if fb:
                        text += f"\nFeedback: {fb[:150]}"
                    wa.send_text(chat_id, text)
                    seen_grades[aid_str] = curr_str
                    state["grades"] = seen_grades
                    save_state(state, udir)
                else:
                    prev_str = seen_grades[aid_str]
                    if prev_str != curr_str:
                        c = courses.get(g_item.get("course_id"))
                        text = f"Updated: Grade posted\nCourse: {course_grade_label(c)}\n{g_item['title']}: {curr_str}"
                        fb = (g_item.get("feedback") or "").strip()
                        if fb:
                            text += f"\nFeedback: {fb[:150]}"
                        wa.send_text(chat_id, text)
                        seen_grades[aid_str] = curr_str
                        state["grades"] = seen_grades
                        save_state(state, udir)

        state["resources"] = list(seen_res.union(r["id"] for r in resources))
        state["assignments"] = list(seen_asgn.union(a["id"] for a in assignments))
        state["attendance"] = list(seen_att.union(r["id"] for r in records))
        state["reminders_sent"] = list(sent)
        state["grades"] = seen_grades
        state["grades_seeded"] = True
        if first_run:
            state["seeded"] = True
            log.info("Seeded baseline for %s…: %d resources, %d assignments, %d attendance, %d grades",
                     chat_id[-6:], len(resources), len(assignments), len(records), len(seen_grades))
        save_state(state, udir)

    except LMSAuthExpired:
        log.warning("Session expired for %s…", chat_id[-6:])
        prev_status = user.get("status") if user else None
        usr.upsert_user(chat_id, status="expired")
        usr.remove_client(chat_id)
        usr.delete_token_file(chat_id)
        if prev_status == "active":
            try:
                wa.send_text(chat_id, "Your LMS session has expired. Send `login` to sign in again.")
            except Exception:
                pass
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        log.warning("LMS network error for %s…: %s — skipping until next cycle", chat_id[-6:], e)
    except Exception:
        log.exception("Monitor run failed for %s… — will retry next cycle", chat_id[-6:])


def run_all_users():
    """Called by the scheduler. Loops over all active users sequentially."""
    usr.cleanup_stale_signups()
    for user in usr.all_active_users():
        u = usr.get_user(user.get("chat_id", ""))
        if not u or u.get("status") != "active":
            continue
        try:
            check_updates(u)
        except Exception:
            log.exception("Unexpected error for user %s…", user.get("chat_id", "?")[-6:])
