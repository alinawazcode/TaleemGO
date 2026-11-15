"""
WhatsApp command router — multi-user version.
Run with:  uvicorn app.wa_bot:api --port 8000

Incoming JSON: { "chat_id": "...", "text": "...", "is_self": true/false }
Conversation states: awaiting_email → awaiting_password → active | expired
"""
import inspect
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from courses import _edit_distance_1, clean_course_query, course_acronym, format_course_list, resolve_course  # noqa: E402
from lms_client import LMSClient, LMSAuthExpired, LMSError, summarize_grades  # noqa: E402
import timeutils                                                        # noqa: E402
from timeutils import to_local, get_local_now, get_local_date_and_time  # noqa: E402
from calendar_gen import generate_calendar_ics                          # noqa: E402
from planner import calculate_attendance_plan, calculate_miss_what_if, get_default_target  # noqa: E402
import wa_transport as wa                                               # noqa: E402
import users as usr                                                     # noqa: E402
from wa_monitor import run_all_users                                    # noqa: E402

try:
    from app.doc_text import extract_document_text
    from app.study import generate_summary, generate_quiz, format_quiz_question, StudyBusyError
except ImportError:
    from doc_text import extract_document_text
    from study import generate_summary, generate_quiz, format_quiz_question, StudyBusyError

try:
    from app.llm.capacity import (
        can_run_rules_first,
        generate_capacity_report,
        get_rules_first_stats,
        intent_cache,
        is_exhibition_mode,
        record_intent_handled,
    )
except ImportError:
    try:
        from llm.capacity import (
            can_run_rules_first,
            generate_capacity_report,
            get_rules_first_stats,
            intent_cache,
            is_exhibition_mode,
            record_intent_handled,
        )
    except ImportError:
        can_run_rules_first = lambda t, r, l: False
        generate_capacity_report = lambda: ""
        get_rules_first_stats = lambda: {}
        intent_cache = None
        is_exhibition_mode = lambda: False
        record_intent_handled = lambda is_rule_handled: None

try:
    from app.llm import stt
    stt_transcribe = stt.transcribe
except ImportError:
    try:
        import llm.stt as stt
        stt_transcribe = stt.transcribe
    except ImportError:
        stt = None
        stt_transcribe = None

try:
    from app.llm.lang_data import (
        ACTIVATION_PHRASES_DEFAULT,
        KEYWORDS,
        fold_arabic_script,
        matches_jailbreak_prefilter,
        sanitize_user_message,
    )
    from app.llm.i18n import t
    from app.llm.intent import add_user_history, get_user_history
    from app.llm.providers import (
        PROVIDER_CONFIGS,
        get_key_for_instance,
        get_provider_key_instances,
        get_provider_keys,
        is_valid_api_key,
        registry,
        run_live_check,
    )
    from app.llm.router import get_configured_providers, is_llm_enabled, route_intent
    from app.llm.rules import detect_language, match_rules
except ImportError:
    from llm.lang_data import (
        ACTIVATION_PHRASES_DEFAULT,
        KEYWORDS,
        fold_arabic_script,
        matches_jailbreak_prefilter,
        sanitize_user_message,
    )
    from llm.i18n import t
    from llm.intent import add_user_history, get_user_history
    from llm.providers import (
        PROVIDER_CONFIGS,
        get_key_for_instance,
        get_provider_key_instances,
        get_provider_keys,
        is_valid_api_key,
        registry,
        run_live_check,
    )
    from llm.router import get_configured_providers, is_llm_enabled, route_intent
    from llm.rules import detect_language, match_rules

log = logging.getLogger("taleemgo.wa")
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
MAX_USERS = int(os.environ.get("MAX_USERS", "5"))
ATTENDANCE_WARN = float(os.environ.get("ATTENDANCE_WARN", "75"))
VOICE_MAX_SECONDS = int(os.environ.get("VOICE_MAX_SECONDS", "30"))
VOICE_MAX_BYTES = int(os.environ.get("VOICE_MAX_BYTES", "2000000"))

ACTIVATION_PHRASES = [
    p.strip().lower()
    for p in os.environ.get("ACTIVATION_PHRASES", "taleemgo ai,taleem go ai,taleemgo").split(",")
    if p.strip()
]

HELP = (
    "TaleemGo AI commands:\n"
    "courses\nassignments\nupcoming\n"
    "grades [course]\n"
    "resources <course>\nattendance [course]\nclos <course>\n"
    "download <course> [week N | title]\n"
    "plan [course] [target% | miss N]\n"
    "digest [HH:MM | on | off | now]\n"
    "calendar\n"
    "summarize <course> [week N | title | latest]\n"
    "quiz <course> [week N | title | latest] [n]\n"
    "logout  — disconnect your account\n"
    "login   — re-link after session expiry"
)

WELCOME = (
    "👋 Welcome to TaleemGo AI — your LMS assistant on WhatsApp!\n\n"
    "To get started, please send me your *LMS email address*.\n\n"
    "Send your LMS email to begin.\n\n"
    "I use AI services to understand your messages. Never send your password except when I ask for it at login.\n\n"
    "Voice notes are converted to text by an AI service.\n"
    "Summaries and quizzes send the text of the lecture file to an AI service."
)

WEEK_RE = re.compile(r"^(?:week|wk|w|lecture|lec|l|hafta|hafte|hafto|هفتو|هفتي|ہفتہ|ہفتے)\s*(\d+)$", re.IGNORECASE)
WEEK_SEARCH_RE = re.compile(r"\b(?:week|wk|w|lecture|lec|l|hafta|hafte|hafto|هفتو|هفتي|ہفتہ|ہفتے)\s*(\d+)\b", re.IGNORECASE)

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
    sched.add_job(check_daily_digests, "interval", minutes=1,
                  next_run_time=datetime.now() + timedelta(seconds=15))
    sched.add_job(usr.cleanup_stale_signups, "interval", minutes=2,
                  next_run_time=datetime.now() + timedelta(seconds=30))
    sched.start()
    api.state.sched = sched
    log.info("Monitor scheduled every %d min; daily digest every 1 min; stale cleanup every 2 min", minutes)

    if is_exhibition_mode():
        report = generate_capacity_report()
        log.info("\n" + report)

    def _startup_llm_check():
        if not is_llm_enabled():
            return
        time.sleep(1.0)
        try:
            for prov in get_configured_providers():
                cfg = PROVIDER_CONFIGS.get(prov)
                if not cfg:
                    continue
                instances = get_provider_key_instances(prov)
                for inst in instances:
                    raw_key = get_key_for_instance(inst)
                    if not is_valid_api_key(raw_key, provider_name=prov):
                        continue
                    status, model_id, latency = run_live_check(inst)
                    if status == "ok":
                        log.info("[LLM] %s OK model=%s %dms", inst, model_id, int(latency))
                    else:
                        log.warning("[LLM] %s FAILED %s model=%s", inst, status, model_id)
        except Exception as e:
            log.warning("Startup LLM live check error: %s", type(e).__name__)

    threading.Thread(target=_startup_llm_check, daemon=True).start()


# ---- helpers ----

def _reply(chat_id: str, text: str):
    if text is None or not str(text).strip():
        caller = inspect.stack()[1].function if len(inspect.stack()) > 1 else "_reply"
        log.info("[SEND] empty text suppressed caller=%s", caller)
        return
    try:
        if len(text) <= 3500:
            wa.send_text(chat_id, text)
            return
        chunks = []
        cur = []
        cur_len = 0
        for line in text.split("\n"):
            if cur_len + len(line) + 1 > 3500 and cur:
                chunks.append("\n".join(cur))
                cur = [line]
                cur_len = len(line)
            else:
                cur.append(line)
                cur_len += len(line) + 1
        if cur:
            chunks.append("\n".join(cur))
        for ch in chunks:
            wa.send_text(chat_id, ch)
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

    # Also check Arabic/Sindhi script folded phrases from lang_data
    try:
        folded_msg = fold_arabic_script(text.lower())
        for ap in ACTIVATION_PHRASES_DEFAULT:
            folded_ap = fold_arabic_script(ap.lower())
            if folded_ap and folded_ap in folded_msg:
                return True
    except Exception:
        pass

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
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "attendance"})
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
        usr.set_pending_action(chat_id, {"intent": "attendance"})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
    elif len(matches) == 1:
        _show_course_attendance(chat_id, lms, matches[0], att)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "attendance", "extra": ""})
        _reply(chat_id, t("which_course", lang, options=options))


def _show_course_plan(chat_id: str, lms, course: dict, target: Optional[float] = None, miss: Optional[int] = None, extra: str = "", lang: str = "en"):
    if target is None:
        target = get_default_target()
    if extra:
        if extra.startswith("miss="):
            try:
                miss = int(extra.split("=")[1])
            except Exception:
                pass
        elif extra.startswith("target="):
            try:
                target = float(extra.split("=")[1])
            except Exception:
                pass
        elif extra.isdigit():
            val = int(extra)
            if 1 <= val <= 99:
                target = val

    att = lms.get_attendance()
    c_att = att.get("courses", {}).get(course["id"], {})
    pres = c_att.get("present", 0)
    tot = c_att.get("total", 0)
    acr = course_acronym(course.get("name", ""))
    c_label = f"{acr} ({course['code']})" if acr else course["code"]

    if miss is not None:
        if tot <= 0:
            _reply(chat_id, t("plan_no_records", lang, course=c_label))
            return
        res = calculate_miss_what_if(pres, tot, miss)
        pct_disp = f"{res['new_percent']:.1f}" if res['new_percent'] is not None else "0.0"
        _reply(chat_id, t("plan_what_if", lang, course=c_label, miss=miss, pct=pct_disp, pres=pres, new_total=res['new_total']))
        return

    plan_res = calculate_attendance_plan(pres, tot, target)
    tgt_disp = int(target) if target == int(target) else target
    lines = [t("plan_heading", lang, target=tgt_disp)]
    if plan_res["status"] == "no_records":
        lines.append(t("plan_no_records", lang, course=c_label))
    elif plan_res["status"] == "safe":
        lines.append(t("plan_safe", lang, course=c_label, pres=pres, tot=tot, pct=f"{plan_res['percent']:.1f}", can_miss=plan_res["can_miss"], target=tgt_disp))
    else:
        lines.append(t("plan_need", lang, course=c_label, pres=pres, tot=tot, pct=f"{plan_res['percent']:.1f}", must_attend=plan_res["must_attend"], target=tgt_disp))
    lines.append(t("plan_assumption", lang))
    _reply(chat_id, "\n".join(lines))


def _handle_plan_cmd(chat_id: str, lms, arg: str):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    def_target = get_default_target()

    arg_clean = arg.strip() if arg else ""

    # Check for miss N
    miss_match = re.search(r"\bmiss\s*(\d+)\b", arg_clean, re.IGNORECASE)
    miss_num = int(miss_match.group(1)) if miss_match else None
    if miss_match:
        course_part = re.sub(r"\bmiss\s*\d+\b", "", arg_clean, flags=re.IGNORECASE).strip()
    else:
        course_part = arg_clean

    target = def_target
    tgt_match = re.search(r"\b(\d{1,3})\s*%?\b", course_part)
    if tgt_match and miss_num is None:
        val = int(tgt_match.group(1))
        is_standalone = re.search(r"(?:^|\s)" + tgt_match.group(1) + r"%?(?:$|\s)", course_part)
        if is_standalone:
            if not (1 <= val <= 99):
                _reply(chat_id, t("plan_invalid_target", lang))
                return
            target = val
            course_part = re.sub(r"(?:^|\s)" + tgt_match.group(1) + r"%?(?:$|\s)", " ", course_part).strip()

    courses = lms.get_courses()

    if not course_part and miss_num is None:
        att = lms.get_attendance()
        lines = [t("plan_heading", lang, target=target)]
        for c in courses:
            c_att = att.get("courses", {}).get(c["id"], {})
            pres = c_att.get("present", 0)
            tot = c_att.get("total", 0)
            acr = course_acronym(c.get("name", ""))
            c_label = f"{acr} ({c['code']})" if acr else c["code"]
            res = calculate_attendance_plan(pres, tot, target)
            if res["status"] == "no_records":
                lines.append(t("plan_no_records", lang, course=c_label))
            elif res["status"] == "safe":
                lines.append(t("plan_safe", lang, course=c_label, pres=pres, tot=tot, pct=f"{res['percent']:.1f}", can_miss=res["can_miss"], target=target))
            else:
                lines.append(t("plan_need", lang, course=c_label, pres=pres, tot=tot, pct=f"{res['percent']:.1f}", must_attend=res["must_attend"], target=target))
        lines.append(t("plan_assumption", lang))
        _reply(chat_id, "\n".join(lines))
        return

    matches = resolve_course(lms, course_part) if course_part else courses
    if not matches:
        usr.set_pending_action(chat_id, {"intent": "plan", "target": target, "miss": miss_num})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return
    elif len(matches) == 1:
        _show_course_plan(chat_id, lms, matches[0], target=target, miss=miss_num, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        extra_str = f"miss={miss_num}" if miss_num is not None else f"target={target}"
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "plan", "extra": extra_str})
        _reply(chat_id, t("which_course", lang, options=options))


def _handle_plan_action(chat_id: str, lms, act: dict, lang: str):
    cq = act.get("course_query", "")
    target = act.get("target") or get_default_target()
    miss_num = act.get("miss")

    courses = lms.get_courses()
    if not cq:
        if miss_num is None:
            att = lms.get_attendance()
            lines = [t("plan_heading", lang, target=target)]
            for c in courses:
                c_att = att.get("courses", {}).get(c["id"], {})
                pres = c_att.get("present", 0)
                tot = c_att.get("total", 0)
                acr = course_acronym(c.get("name", ""))
                c_label = f"{acr} ({c['code']})" if acr else c["code"]
                res = calculate_attendance_plan(pres, tot, target)
                if res["status"] == "no_records":
                    lines.append(t("plan_no_records", lang, course=c_label))
                elif res["status"] == "safe":
                    lines.append(t("plan_safe", lang, course=c_label, pres=pres, tot=tot, pct=f"{res['percent']:.1f}", can_miss=res["can_miss"], target=target))
                else:
                    lines.append(t("plan_need", lang, course=c_label, pres=pres, tot=tot, pct=f"{res['percent']:.1f}", must_attend=res["must_attend"], target=target))
            lines.append(t("plan_assumption", lang))
            _reply(chat_id, "\n".join(lines))
            return
        else:
            usr.set_pending_action(chat_id, {"intent": "plan", "target": target, "miss": miss_num})
            _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
            return

    matches = resolve_course(lms, cq)
    if not matches:
        usr.set_pending_action(chat_id, {"intent": "plan", "target": target, "miss": miss_num})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return
    elif len(matches) == 1:
        _show_course_plan(chat_id, lms, matches[0], target=target, miss=miss_num, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        extra_str = f"miss={miss_num}" if miss_num is not None else f"target={target}"
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "plan", "extra": extra_str})
        _reply(chat_id, t("which_course", lang, options=options))


def _build_daily_digest_text(chat_id: str, lms, user: dict, now_dt: Optional[datetime] = None) -> str:
    lang = user.get("lang", "en") if user else "en"
    local_now = get_local_now(now_dt)
    warn = int(os.environ.get("ATTENDANCE_WARN", "75"))

    courses = {c["id"]: c for c in lms.get_courses()}

    # 1. Assignments
    asgns = lms.get_assignments()
    subs = lms.get_submissions()
    due_today = []
    due_tomorrow = []
    due_next_3 = []
    overdue_count = 0

    today_date = local_now.date()
    tomorrow_date = today_date + timedelta(days=1)
    day3_date = today_date + timedelta(days=3)

    for a in asgns:
        sub = subs.get(a["id"])
        if sub and (sub.get("submitted_at") or sub.get("file_name")):
            continue
        due_raw = a.get("due_date")
        if not due_raw:
            continue
        try:
            due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(local_now.tzinfo)
        except Exception:
            continue

        c = courses.get(a.get("course_id"))
        acr = course_acronym(c.get("name", "")) if c else ""
        c_code = acr or (c["code"] if c else "")
        item_str = f"• {c_code}: {a['title']} (Due {to_local(due_raw)})"

        if due_dt < local_now:
            overdue_count += 1
        elif due_dt.date() == today_date:
            due_today.append(item_str)
        elif due_dt.date() == tomorrow_date:
            due_tomorrow.append(item_str)
        elif due_dt.date() <= day3_date:
            due_next_3.append(item_str)

    asgn_lines = []
    if due_today:
        asgn_lines.append("📅 Due Today:\n" + "\n".join(due_today))
    if due_tomorrow:
        asgn_lines.append("⏳ Due Tomorrow:\n" + "\n".join(due_tomorrow))
    if due_next_3:
        asgn_lines.append("📌 Due in 2-3 Days:\n" + "\n".join(due_next_3))

    if not asgn_lines:
        if overdue_count > 0:
            asgn_lines.append(f"{t('digest_nothing_due', lang)} ({overdue_count} overdue)")
        else:
            asgn_lines.append(t("digest_nothing_due", lang))
    elif overdue_count > 0:
        asgn_lines.append(f"⚠️ {overdue_count} overdue assignment(s)")

    # 2. Attendance warnings
    att = lms.get_attendance()
    att_warnings = []
    for cid, c_data in att.get("courses", {}).items():
        pct = c_data.get("percent")
        if pct is not None and pct < warn:
            c = courses.get(cid)
            acr = course_acronym(c.get("name", "")) if c else ""
            c_label = f"{acr} ({c['code']})" if acr else (c["code"] if c else "Course")
            att_warnings.append(f"• {c_label}: {pct}% (threshold: {warn}%)")

    att_lines = []
    if att_warnings:
        att_lines.append(f"⚠️ Attendance Warning (< {warn}%):\n" + "\n".join(att_warnings))
    else:
        att_lines.append(t("digest_att_ok", lang, warn=warn))

    # 3. New lectures and grades from last 24h
    now_utc = local_now.astimezone(timezone.utc)
    new_updates = []

    # Monitor state reuse
    udir = usr.user_dir(chat_id)
    try:
        from state import load_state
        mon_state = load_state(udir)
    except Exception:
        mon_state = {}
    seen_res = set(mon_state.get("resources", []))
    seen_grades = dict(mon_state.get("grades", {}))

    try:
        resources = lms.get_resources()
        for r in resources:
            cat = r.get("created_at")
            is_recent = False
            if cat:
                try:
                    rdt = datetime.fromisoformat(cat.replace("Z", "+00:00"))
                    if 0 <= (now_utc - rdt).total_seconds() <= 86400:
                        is_recent = True
                except Exception:
                    pass
            elif r["id"] not in seen_res:
                is_recent = True

            if is_recent:
                c = courses.get(r.get("course_id"))
                acr = course_acronym(c.get("name", "")) if c else ""
                c_code = acr or (c["code"] if c else "")
                new_updates.append(f"• Lecture: {c_code} - {r.get('title')}")
    except Exception:
        pass

    try:
        grades = lms.get_grades()
        for g in grades:
            gat = g.get("graded_at")
            is_recent_grade = False
            if gat:
                try:
                    gdt = datetime.fromisoformat(gat.replace("Z", "+00:00"))
                    if 0 <= (now_utc - gdt).total_seconds() <= 86400:
                        is_recent_grade = True
                except Exception:
                    pass
            elif str(g.get("id")) not in seen_grades and g.get("grade") is not None:
                is_recent_grade = True

            if is_recent_grade and g.get("grade") is not None:
                c = courses.get(g.get("course_id"))
                acr = course_acronym(c.get("name", "")) if c else ""
                c_code = acr or (c["code"] if c else "")
                new_updates.append(f"• Grade: {c_code} - {g.get('title')}: {g.get('grade')}/{g.get('max_marks')}")
    except Exception:
        pass

    updates_lines = []
    if new_updates:
        updates_lines.append("🔔 New in the last 24 hours:\n" + "\n".join(new_updates))
    else:
        updates_lines.append(t("digest_no_updates", lang))

    sections = [
        t("digest_header", lang),
        "\n".join(asgn_lines),
        "\n".join(att_lines),
        "\n".join(updates_lines),
    ]
    return "\n\n".join(s for s in sections if s)


def _send_daily_digest(chat_id: str, lms, user: dict, is_manual: bool = False, now_dt: Optional[datetime] = None):
    text = _build_daily_digest_text(chat_id, lms, user, now_dt=now_dt)
    wa.send_text(chat_id, text)


def _handle_digest_cmd(chat_id: str, lms, arg: str):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    tokens = arg.strip().split() if arg else []

    if not tokens:
        is_on = bool(user.get("digest_enabled", 0))
        d_time = user.get("digest_time") or os.environ.get("DIGEST_DEFAULT_TIME", "07:30")
        status_msg = t("digest_status_on", lang, time=d_time) if is_on else t("digest_status_off", lang)
        _reply(chat_id, status_msg)
        _send_daily_digest(chat_id, lms, user, is_manual=True)
        return

    sub = tokens[0].lower()
    if sub == "on":
        time_arg = tokens[1] if len(tokens) > 1 else os.environ.get("DIGEST_DEFAULT_TIME", "07:30")
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_arg):
            _reply(chat_id, t("digest_invalid_time", lang))
            return
        usr.set_digest(chat_id, enabled=True, time_str=time_arg)
        _reply(chat_id, t("digest_enabled", lang, time=time_arg))
    elif sub == "off":
        usr.set_digest(chat_id, enabled=False)
        _reply(chat_id, t("digest_disabled", lang))
    else:
        _reply(chat_id, t("digest_status_off", lang))


def _handle_digest_action(chat_id: str, lms, act: dict, lang: str):
    user = usr.get_user(chat_id)
    enable = act.get("enable")
    time_str = act.get("time")

    if enable is True:
        if time_str:
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_str):
                _reply(chat_id, t("digest_invalid_time", lang))
                return
        else:
            time_str = user.get("digest_time") or os.environ.get("DIGEST_DEFAULT_TIME", "07:30")
        usr.set_digest(chat_id, enabled=True, time_str=time_str)
        _reply(chat_id, t("digest_enabled", lang, time=time_str))
    elif enable is False:
        usr.set_digest(chat_id, enabled=False)
        _reply(chat_id, t("digest_disabled", lang))
    else:
        _handle_digest_cmd(chat_id, lms, "")


def check_daily_digests(now_dt: Optional[datetime] = None):
    local_date, local_time = get_local_date_and_time(now_dt)
    due_users = usr.all_due_digest_users(local_date, local_time)
    for u in due_users:
        chat_id = u["chat_id"]
        if u.get("status") != "active":
            continue
        try:
            lms = usr.get_client(chat_id)
            if lms is None:
                tf = usr.token_file(chat_id)
                if not os.path.exists(tf):
                    continue
                lms = LMSClient(token_file=tf)
                usr.set_client(chat_id, lms)
            _send_daily_digest(chat_id, lms, u, is_manual=False, now_dt=now_dt)
            usr.record_digest_sent(chat_id, local_date)
        except LMSAuthExpired:
            log.warning("Session expired for %s… during digest; skipping", chat_id[-6:])
            usr.record_digest_sent(chat_id, local_date)
        except Exception:
            log.exception("Daily digest failed for %s…", chat_id[-6:])


def _handle_calendar_cmd(chat_id: str, lms, arg: str = "", now_dt=None):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    asgns = lms.get_assignments()
    subs = lms.get_submissions()

    local_now = timeutils.get_local_now(now_dt)

    pending = []
    for a in asgns:
        aid = a.get("id")
        sub = subs.get(aid)
        if sub and (sub.get("submitted_at") or sub.get("file_name")):
            continue
        due_raw = a.get("due_date")
        if not due_raw:
            continue
        try:
            due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(local_now.tzinfo)
        except Exception:
            continue
        if due_dt > local_now:
            pending.append(a)

    courses = {c["id"]: c for c in lms.get_courses()}
    ics_str = generate_calendar_ics(pending, courses)
    if not ics_str:
        _reply(chat_id, t("calendar_no_deadlines", lang))
        return

    tmp_path = Path(usr.user_downloads_dir(chat_id)) / "deadlines.ics"
    try:
        tmp_path.write_text(ics_str, encoding="utf-8")
        caption = t("calendar_caption", lang, count=len(pending))
        wa.send_file(chat_id, str(tmp_path), caption=caption)
    finally:
        tmp_path.unlink(missing_ok=True)


def _handle_calendar_action(chat_id: str, lms, act: dict, lang: str):
    _handle_calendar_cmd(chat_id, lms, "")


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
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "resources"})
        _reply(chat_id, "Usage: resources <course>\n\nYour courses:\n" + format_course_list(courses))
        return
    matches = resolve_course(lms, arg)
    if not matches:
        usr.set_pending_action(chat_id, {"intent": "resources"})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
    elif len(matches) == 1:
        _show_course_resources(chat_id, lms, matches[0])
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "resources", "extra": ""})
        _reply(chat_id, t("which_course", lang, options=options))


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
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "clos"})
        _reply(chat_id, "Usage: clos <course>\n\nYour courses:\n" + format_course_list(courses))
        return
    matches = resolve_course(lms, arg)
    if not matches:
        usr.set_pending_action(chat_id, {"intent": "clos"})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
    elif len(matches) == 1:
        _show_course_clos(chat_id, lms, matches[0])
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "clos", "extra": ""})
        _reply(chat_id, t("which_course", lang, options=options))


def _show_course_grades(chat_id: str, lms, course: dict):
    course_grades = lms.get_grades(course["id"])
    acr = course_acronym(course.get("name", ""))
    label = f"{acr} ({course['code']})" if acr else course["code"]
    lines = [f"{label} Grades:"]
    if not course_grades:
        lines.append("No grades posted yet")
    else:
        for it in course_grades:
            g = it.get("grade")
            mm = it.get("max_marks")
            if g is not None:
                g_str = f"{int(g)}" if isinstance(g, (int, float)) and g == int(g) else f"{g:g}"
                if mm is not None:
                    mm_str = f"{int(mm)}" if isinstance(mm, (int, float)) and mm == int(mm) else f"{mm:g}"
                    mark_str = f"{g_str}/{mm_str}"
                else:
                    mark_str = g_str
            else:
                mark_str = "Not graded yet"
            line = f"{it['title']}: {mark_str}"
            fb = (it.get("feedback") or "").strip()
            if fb:
                line += f"\n  Feedback: {fb[:150]}"
            lines.append(line)

        summary = summarize_grades(course_grades)
        if summary["graded"] == 0:
            lines.append("No grades posted yet")
        else:
            ob = summary["obtained"]
            mt = summary["max_total"]
            ob_str = f"{int(ob)}" if ob == int(ob) else f"{ob:g}"
            mt_str = f"{int(mt)}" if mt == int(mt) else f"{mt:g}"
            lines.append(f"Graded so far: {ob_str}/{mt_str} ({summary['graded']} of {summary['total']} graded)")

    _reply(chat_id, "\n".join(lines))


def _handle_grades_cmd(chat_id: str, lms, arg: str = ""):
    courses = lms.get_courses()
    if not courses:
        _reply(chat_id, "No courses found.")
        return

    arg = (arg or "").strip()
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "grades"})
        blocks = []
        for c in courses:
            acr = course_acronym(c.get("name", ""))
            label = f"{acr} ({c['code']})" if acr else c["code"]
            c_grades = lms.get_grades(c["id"])
            c_lines = [f"{label}:"]
            if not c_grades:
                c_lines.append("No grades posted yet")
            else:
                for it in c_grades:
                    g = it.get("grade")
                    mm = it.get("max_marks")
                    if g is not None:
                        g_str = f"{int(g)}" if isinstance(g, (int, float)) and g == int(g) else f"{g:g}"
                        if mm is not None:
                            mm_str = f"{int(mm)}" if isinstance(mm, (int, float)) and mm == int(mm) else f"{mm:g}"
                            mark_str = f"{g_str}/{mm_str}"
                        else:
                            mark_str = g_str
                    else:
                        mark_str = "Not graded yet"
                    c_lines.append(f"{it['title']}: {mark_str}")

                summary = summarize_grades(c_grades)
                if summary["graded"] == 0:
                    c_lines.append("No grades posted yet")
                else:
                    ob = summary["obtained"]
                    mt = summary["max_total"]
                    ob_str = f"{int(ob)}" if ob == int(ob) else f"{ob:g}"
                    mt_str = f"{int(mt)}" if mt == int(mt) else f"{mt:g}"
                    c_lines.append(f"Graded so far: {ob_str}/{mt_str} ({summary['graded']} of {summary['total']} graded)")
            blocks.append("\n".join(c_lines))
        chunks = []
        cur_chunk = []
        cur_len = 0
        for b in blocks:
            b_len = len(b) + (2 if cur_chunk else 0)
            if cur_chunk and (cur_len + b_len > 3500):
                chunks.append("\n\n".join(cur_chunk))
                cur_chunk = [b]
                cur_len = len(b)
            else:
                cur_chunk.append(b)
                cur_len += b_len
        if cur_chunk:
            chunks.append("\n\n".join(cur_chunk))
        for ch in chunks:
            _reply(chat_id, ch)
        return

    matches = resolve_course(lms, arg)
    if not matches:
        usr.set_pending_action(chat_id, {"intent": "grades"})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
    elif len(matches) == 1:
        _show_course_grades(chat_id, lms, matches[0])
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
        usr.set_pending(chat_id, {"type": "course", "courses": matches, "command": "grades", "extra": ""})
        _reply(chat_id, t("which_course", lang, options=options))


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
        user = usr.get_user(chat_id)
        lang = user.get("lang", "en") if user else "en"
        try:
            path = lms.download_resource(r["id"], folder=folder)
            wa.send_file(chat_id, path, r.get("title", ""))
        except LMSError as e:
            _reply(chat_id, str(e))
        except Exception:
            log.exception("Failed to download resource %s for %s…", r.get("id"), chat_id[-6:])
            _reply(chat_id, t("file_failed", lang, filename=r.get("title", "file")))
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


def _handle_download_for_course(chat_id: str, lms, course: dict, remainder: str = "", is_latest: bool = False, lang: str = "en"):
    all_res = [r for r in lms.get_resources() if r["course_id"] == course["id"]]
    acr = course_acronym(course.get("name", "")) or course["code"]
    course_label = f"{acr} ({course['code']})"

    m_week = WEEK_RE.match(remainder) if remainder else None

    # If is_latest and no explicit week, send newest directly
    if is_latest and not m_week:
        if not all_res:
            _reply(chat_id, t("no_files", lang, course=course_label))
            return

        # Newest resource: highest week_number (null as -1), tie-break newest created_at
        newest = sorted(
            all_res,
            key=lambda r: (
                r.get("week_number") if r.get("week_number") is not None else -1,
                r.get("created_at") or "",
            ),
            reverse=True,
        )[0]

        wk = newest.get("week_number")
        title = newest.get("title", "")
        if wk is not None:
            caption = t("latest_caption", lang, course=course_label, week=wk, title=title)
        else:
            caption = t("latest_caption_no_week", lang, course=course_label, title=title)

        folder = usr.user_downloads_dir(chat_id)
        usr.cleanup_user_downloads(chat_id)
        try:
            path = lms.download_resource(newest["id"], folder=folder)
            wa.send_file(chat_id, path, caption)
        except Exception:
            log.exception("Failed to send latest file %s for %s…", newest.get("id"), chat_id[-6:])
            _reply(chat_id, t("file_failed", lang, filename=title or "file"))
        return

    if not remainder:
        if not all_res:
            _reply(chat_id, t("no_files", lang, course=course_label))
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
        usr.set_pending_action(chat_id, {"intent": "download"})
        _reply(chat_id, "Usage: download <course> [week N | title]")
        return

    # Check if arg mentions latest
    arg_lower = arg.lower()
    is_latest = False
    latest_kws = KEYWORDS.get("latest", [])
    for kw in latest_kws:
        if re.search(r"\b" + re.escape(kw.lower()) + r"\b", arg_lower):
            is_latest = True
            break
        if len(kw) >= 5:
            for word in re.findall(r"\b[a-z]+\b", arg_lower):
                if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                    is_latest = True
                    break

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

    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"

    remainder = " ".join(words[best_idx:]).strip() if best_matches else ""

    if not best_matches:
        # Fallback: check if there is an explicit week and clean the rest of arg
        m_wk = WEEK_SEARCH_RE.search(arg)
        arg_to_clean = arg
        if m_wk:
            remainder = f"week {m_wk.group(1)}"
            arg_to_clean = (arg[:m_wk.start()] + " " + arg[m_wk.end():]).strip()
        c_clean = clean_course_query(arg_to_clean)
        if c_clean:
            hits = resolve_course(lms, c_clean)
            if hits:
                best_matches = hits

    if not best_matches:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": "download", "latest": is_latest})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    if len(best_matches) == 1:
        _handle_download_for_course(chat_id, lms, best_matches[0], remainder, is_latest=is_latest, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(best_matches, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": best_matches,
            "command": "download",
            "extra": remainder,
            "is_latest": is_latest,
        })
        _reply(chat_id, t("which_course", lang, options=options))


def _execute_summarize_resource(chat_id: str, lms, resource: dict, lang: str = "en"):
    folder = usr.user_downloads_dir(chat_id)
    usr.cleanup_user_downloads(chat_id)
    path = None
    try:
        path = lms.download_resource(resource["id"], folder=folder)
        file_size = os.path.getsize(path) if (path and os.path.exists(path)) else 0

        # Check SQLite summary cache keyed by (resource_id, file_size, lang)
        cached = usr.get_cached_summary(resource["id"], file_size, lang)
        if cached:
            _reply(chat_id, cached)
            return

        # Check rate limit before calling LLM
        if not usr.check_and_record_study_limit(chat_id):
            _reply(chat_id, t("study_rate_limit", lang))
            return

        # Extract text bounded by timeout and size caps
        ext_res = extract_document_text(path)
        # Immediately delete temp file right after extraction
        try:
            if path and os.path.exists(path):
                os.unlink(path)
                path = None
        except Exception:
            pass

        if ext_res.error_code:
            err_map = {
                "scanned": "study_file_scanned",
                "encrypted": "study_file_encrypted",
                "too_large": "study_file_too_large",
                "unsupported_type": "study_file_unsupported",
                "empty": "study_file_empty",
            }
            key = err_map.get(ext_res.error_code, "study_failed")
            _reply(chat_id, t(key, lang))
            return

        filename = resource.get("title", "Lecture")
        summary = generate_summary(
            ext_res.text,
            filename,
            lang=lang,
            pages_used=ext_res.pages_used,
            was_truncated=ext_res.was_truncated,
        )
        if summary:
            usr.set_cached_summary(resource["id"], file_size, lang, summary)
            _reply(chat_id, summary)
        else:
            _reply(chat_id, t("study_failed", lang))
    except StudyBusyError:
        _reply(chat_id, t("study_busy", lang))
    except LMSError as e:
        log.warning("LMS download failed: %s", type(e).__name__)
        _reply(chat_id, t("file_failed", lang, filename=resource.get("title", "file")))
    except Exception as e:
        log.warning("Study summarize failed: %s", type(e).__name__)
        _reply(chat_id, t("study_failed", lang))
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass


def _execute_quiz_resource(chat_id: str, lms, resource: dict, n: int = 5, lang: str = "en"):
    # Rate limit check before calling LLM
    if not usr.check_and_record_study_limit(chat_id):
        _reply(chat_id, t("study_rate_limit", lang))
        return

    folder = usr.user_downloads_dir(chat_id)
    usr.cleanup_user_downloads(chat_id)
    path = None
    try:
        path = lms.download_resource(resource["id"], folder=folder)
        ext_res = extract_document_text(path)
        try:
            if path and os.path.exists(path):
                os.unlink(path)
                path = None
        except Exception:
            pass

        if ext_res.error_code:
            err_map = {
                "scanned": "study_file_scanned",
                "encrypted": "study_file_encrypted",
                "too_large": "study_file_too_large",
                "unsupported_type": "study_file_unsupported",
                "empty": "study_file_empty",
            }
            key = err_map.get(ext_res.error_code, "study_failed")
            _reply(chat_id, t(key, lang))
            return

        filename = resource.get("title", "Lecture")
        questions = generate_quiz(ext_res.text, filename, n=n, lang=lang)
        if not questions:
            _reply(chat_id, t("study_failed", lang))
            return

        quiz_state = {
            "resource_id": resource["id"],
            "filename": filename,
            "questions": questions,
            "current_idx": 0,
            "score": 0,
            "lang": lang,
            "reminded": False,
            "ttl": 1800,
        }
        usr.set_active_quiz(chat_id, quiz_state)
        first_q = format_quiz_question(questions[0], 0, len(questions), is_first=True)
        _reply(chat_id, first_q)
    except StudyBusyError:
        _reply(chat_id, t("study_busy", lang))
    except LMSError as e:
        log.warning("LMS download failed: %s", type(e).__name__)
        _reply(chat_id, t("file_failed", lang, filename=resource.get("title", "file")))
    except Exception as e:
        log.warning("Study quiz failed: %s", type(e).__name__)
        _reply(chat_id, t("study_failed", lang))
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass


def _handle_active_quiz_reply(chat_id: str, quiz: dict, text: str, user: dict) -> bool:
    tl = text.strip().lower()
    lang = quiz.get("lang") or (user.get("lang", "en") if user else "en")

    if tl in ("stop", "cancel", "exit", "quit"):
        usr.clear_active_quiz(chat_id)
        _reply(chat_id, t("quiz_stopped", lang))
        return True

    ans_clean = tl.strip("()., ")
    choice_map = {
        "a": 0, "1": 0,
        "b": 1, "2": 1,
        "c": 2, "3": 2,
        "d": 3, "4": 3,
    }
    choice = choice_map.get(ans_clean)

    if choice is not None:
        cur_idx = quiz.get("current_idx", 0)
        questions = quiz.get("questions", [])
        if 0 <= cur_idx < len(questions):
            q = questions[cur_idx]
            correct_idx = q.get("answer", 0)
            letter = chr(65 + correct_idx)
            options = q.get("options", ["", "", "", ""])
            correct_opt = options[correct_idx] if 0 <= correct_idx < len(options) else ""
            why = q.get("why", "").strip()

            is_correct = (choice == correct_idx)
            if is_correct:
                quiz["score"] = quiz.get("score", 0) + 1
                feedback = t("quiz_correct", lang)
            else:
                feedback = t("quiz_incorrect", lang, letter=letter, option=correct_opt)

            if why:
                feedback += f"\n{why}"

            next_idx = cur_idx + 1
            if next_idx < len(questions):
                quiz["current_idx"] = next_idx
                quiz["reminded"] = False
                usr.set_active_quiz(chat_id, quiz)
                next_q_text = format_quiz_question(questions[next_idx], next_idx, len(questions), is_first=False)
                _reply(chat_id, f"{feedback}\n\n{next_q_text}")
                return True
            else:
                usr.clear_active_quiz(chat_id)
                score = quiz.get("score", 0)
                total = len(questions)
                pct = (score / total) * 100 if total > 0 else 0
                if pct >= 80:
                    encouragement = t("quiz_encouragement_high", lang)
                elif pct >= 50:
                    encouragement = t("quiz_encouragement_medium", lang)
                else:
                    encouragement = t("quiz_encouragement_low", lang)
                completed_msg = t("quiz_completed", lang, score=score, total=total)
                _reply(chat_id, f"{feedback}\n\n{completed_msg}\n{encouragement}")
                return True

    # Non-answer text: send reminder once and let message pass through to normal commands / NLP
    if not quiz.get("reminded"):
        quiz["reminded"] = True
        usr.set_active_quiz(chat_id, quiz)
        _reply(chat_id, t("quiz_reminder", lang))

    return False


def _handle_study_for_course(
    chat_id: str,
    lms,
    course: dict,
    remainder: str = "",
    is_latest: bool = False,
    is_quiz: bool = False,
    n: int = 5,
    lang: str = "en",
):
    all_res = [r for r in lms.get_resources() if r["course_id"] == course["id"]]
    acr = course_acronym(course.get("name", "")) or course["code"]
    course_label = f"{acr} ({course['code']})"
    cmd_name = "quiz" if is_quiz else "summarize"

    if not all_res:
        _reply(chat_id, t("no_files", lang, course=course_label))
        return

    m_week = WEEK_RE.match(remainder) if remainder else None

    # If is_latest and no explicit week, pick newest directly
    if is_latest and not m_week:
        newest = sorted(
            all_res,
            key=lambda r: (
                r.get("week_number") if r.get("week_number") is not None else -1,
                r.get("created_at") or "",
            ),
            reverse=True,
        )[0]
        if is_quiz:
            _execute_quiz_resource(chat_id, lms, newest, n=n, lang=lang)
        else:
            _execute_summarize_resource(chat_id, lms, newest, lang=lang)
        return

    if not remainder:
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

        lines.append('\nReply with a number (e.g. 1) to select a file. Reply "cancel" to stop.')
        usr.set_pending(chat_id, {
            "type": "files",
            "files": flat_files,
            "course": course,
            "action": cmd_name,
            "n": n,
        })
        _reply(chat_id, "\n".join(lines))
        return

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
            if is_quiz:
                _execute_quiz_resource(chat_id, lms, week_files[0], n=n, lang=lang)
            else:
                _execute_summarize_resource(chat_id, lms, week_files[0], lang=lang)
            return
        else:
            lines = [f"Week {week_num} of {acr} has {len(week_files)} files:"]
            for i, r in enumerate(week_files, 1):
                lines.append(f"{i}. {r['title']}")
            lines.append('\nReply with a number (e.g. 1) to select a file. Reply "cancel" to stop.')
            usr.set_pending(chat_id, {
                "type": "files",
                "files": week_files,
                "course": course,
                "action": cmd_name,
                "n": n,
            })
            _reply(chat_id, "\n".join(lines))
            return
    else:
        q_words = remainder.lower()
        hits = [r for r in all_res if q_words in r.get("title", "").lower()]
        if not hits:
            _reply(chat_id, "No matching file.")
            return
        elif len(hits) == 1:
            if is_quiz:
                _execute_quiz_resource(chat_id, lms, hits[0], n=n, lang=lang)
            else:
                _execute_summarize_resource(chat_id, lms, hits[0], lang=lang)
            return
        else:
            lines = [f"Found {len(hits)} files matching \"{remainder}\":"]
            for i, r in enumerate(hits, 1):
                lines.append(f"{i}. {r['title']}")
            lines.append('\nReply with a number (e.g. 1) to select a file. Reply "cancel" to stop.')
            usr.set_pending(chat_id, {
                "type": "files",
                "files": hits,
                "course": course,
                "action": cmd_name,
                "n": n,
            })
            _reply(chat_id, "\n".join(lines))
            return


def _handle_summarize_cmd(chat_id: str, lms, arg: str):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "summarize"})
        _reply(chat_id, "Usage: summarize <course> [week N | title | latest]")
        return

    arg_lower = arg.lower()
    is_latest = False
    latest_kws = KEYWORDS.get("latest", [])
    for kw in latest_kws:
        if re.search(r"\b" + re.escape(kw.lower()) + r"\b", arg_lower):
            is_latest = True
            break
        if len(kw) >= 5:
            for word in re.findall(r"\b[a-z]+\b", arg_lower):
                if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                    is_latest = True
                    break

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

    remainder = " ".join(words[best_idx:]).strip() if best_matches else ""

    if not best_matches:
        m_wk = WEEK_SEARCH_RE.search(arg)
        arg_to_clean = arg
        if m_wk:
            remainder = f"week {m_wk.group(1)}"
            arg_to_clean = (arg[:m_wk.start()] + " " + arg[m_wk.end():]).strip()
        c_clean = clean_course_query(arg_to_clean)
        if c_clean:
            hits = resolve_course(lms, c_clean)
            if hits:
                best_matches = hits

    if not best_matches:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": "summarize", "latest": is_latest})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    if len(best_matches) == 1:
        _handle_study_for_course(chat_id, lms, best_matches[0], remainder=remainder, is_latest=is_latest, is_quiz=False, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(best_matches, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": best_matches,
            "command": "summarize",
            "extra": remainder,
            "is_latest": is_latest,
        })
        _reply(chat_id, t("which_course", lang, options=options))


def _handle_quiz_cmd(chat_id: str, lms, arg: str):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if not arg:
        usr.set_pending_action(chat_id, {"intent": "quiz"})
        _reply(chat_id, "Usage: quiz <course> [week N | title | latest] [n]")
        return

    arg_lower = arg.lower()
    is_latest = False
    latest_kws = KEYWORDS.get("latest", [])
    for kw in latest_kws:
        if re.search(r"\b" + re.escape(kw.lower()) + r"\b", arg_lower):
            is_latest = True
            break
        if len(kw) >= 5:
            for word in re.findall(r"\b[a-z]+\b", arg_lower):
                if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                    is_latest = True
                    break

    # Parse optional n (3..10, default 5)
    n = 5
    tokens = arg.split()
    m_wk = WEEK_SEARCH_RE.search(arg)
    wk_val = int(m_wk.group(1)) if m_wk else None

    if tokens and tokens[-1].isdigit():
        cand_n = int(tokens[-1])
        if 3 <= cand_n <= 10 and (wk_val is None or cand_n != wk_val):
            n = cand_n
            tokens = tokens[:-1]
            arg = " ".join(tokens)

    words = tokens
    best_matches = []
    best_idx = 0
    for i in range(len(words), 0, -1):
        prefix = " ".join(words[:i])
        hits = resolve_course(lms, prefix)
        if hits:
            best_matches = hits
            best_idx = i
            break

    remainder = " ".join(words[best_idx:]).strip() if best_matches else ""

    if not best_matches:
        if m_wk:
            remainder = f"week {m_wk.group(1)}"
            arg_to_clean = (arg[:m_wk.start()] + " " + arg[m_wk.end():]).strip()
        else:
            arg_to_clean = arg
        c_clean = clean_course_query(arg_to_clean)
        if c_clean:
            hits = resolve_course(lms, c_clean)
            if hits:
                best_matches = hits

    if not best_matches:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": "quiz", "latest": is_latest, "n": n})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    if len(best_matches) == 1:
        _handle_study_for_course(chat_id, lms, best_matches[0], remainder=remainder, is_latest=is_latest, is_quiz=True, n=n, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(best_matches, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": best_matches,
            "command": "quiz",
            "extra": remainder,
            "is_latest": is_latest,
            "n": n,
        })
        _reply(chat_id, t("which_course", lang, options=options))


def _handle_study_action(chat_id: str, lms, act: dict, is_quiz: bool, lang: str):
    cq = act.get("course_query", "")
    week = act.get("week")
    tq = act.get("title_query", "")
    is_latest = bool(act.get("latest", False))
    n = act.get("n", 5)
    cmd_name = "quiz" if is_quiz else "summarize"

    if not cq:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": cmd_name, "latest": is_latest, "week": week, "title_query": tq, "n": n})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    hits = resolve_course(lms, cq)
    if not hits:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": cmd_name, "latest": is_latest, "week": week, "title_query": tq, "n": n})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    remainder = f"week {week}" if week is not None else (tq or "")

    if len(hits) == 1:
        _handle_study_for_course(chat_id, lms, hits[0], remainder=remainder, is_latest=is_latest, is_quiz=is_quiz, n=n, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(hits, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": hits,
            "command": cmd_name,
            "extra": remainder,
            "is_latest": is_latest,
            "n": n,
        })
        _reply(chat_id, t("which_course", lang, options=options))


def _execute_course_command(chat_id: str, lms, cmd: str, course: dict, extra: str = "", is_latest: bool = False, n: int = 5):
    user = usr.get_user(chat_id)
    lang = user.get("lang", "en") if user else "en"
    if cmd == "attendance":
        _show_course_attendance(chat_id, lms, course)
    elif cmd == "resources":
        _show_course_resources(chat_id, lms, course)
    elif cmd == "clos":
        _show_course_clos(chat_id, lms, course)
    elif cmd == "download":
        _handle_download_for_course(chat_id, lms, course, extra, is_latest=is_latest, lang=lang)
    elif cmd == "grades":
        _show_course_grades(chat_id, lms, course)
    elif cmd == "plan":
        _show_course_plan(chat_id, lms, course, extra=extra, lang=lang)
    elif cmd == "summarize":
        _handle_study_for_course(chat_id, lms, course, remainder=extra, is_latest=is_latest, is_quiz=False, lang=lang)
    elif cmd == "quiz":
        _handle_study_for_course(chat_id, lms, course, remainder=extra, is_latest=is_latest, is_quiz=True, n=n, lang=lang)


def _handle_pending(chat_id: str, lms, pending: dict, text: str) -> bool:
    t = text.strip()
    tl = t.lower()

    if pending.get("type") == "files":
        action = pending.get("action", "download")
        if tl == "cancel":
            usr.clear_pending(chat_id)
            if action in ("summarize", "quiz"):
                _reply(chat_id, "Cancelled.")
            else:
                _reply(chat_id, "Download cancelled.")
            return True

        files = pending.get("files", [])
        if tl == "all":
            if action in ("summarize", "quiz"):
                _reply(chat_id, "Please pick a single file by number. Summaries and quizzes work on one file at a time.")
                return True
            usr.clear_pending(chat_id)
            _send_files_choice(chat_id, lms, files, indices=None)
            return True

        tokens = [x for x in re.split(r"[\s,]+", t) if x]
        if tokens and all(x.isdigit() for x in tokens):
            nums = [int(x) for x in tokens]
            if any(n < 1 or n > len(files) for n in nums):
                _reply(chat_id, f"Please pick a number between 1 and {len(files)}.")
                return True
            if action in ("summarize", "quiz"):
                if len(nums) > 1:
                    _reply(chat_id, "Please pick a single file by number.")
                    return True
                usr.clear_pending(chat_id)
                user = usr.get_user(chat_id)
                lang = user.get("lang", "en") if user else "en"
                chosen_file = files[nums[0] - 1]
                if action == "quiz":
                    _execute_quiz_resource(chat_id, lms, chosen_file, n=pending.get("n", 5), lang=lang)
                else:
                    _execute_summarize_resource(chat_id, lms, chosen_file, lang=lang)
                return True

            usr.clear_pending(chat_id)
            _send_files_choice(chat_id, lms, files, indices=nums)
            return True

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
                is_latest = bool(pending.get("is_latest", False))
                n = pending.get("n", 5)
                _execute_course_command(chat_id, lms, cmd, chosen, extra, is_latest=is_latest, n=n)
                return True
            else:
                _reply(chat_id, f"Please pick a number between 1 and {len(courses)}.")
                return True

        return False

    elif pending.get("type") == "course_menu":
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
                acr = course_acronym(chosen.get("name", "")) or chosen["code"]
                course_label = f"{acr} ({chosen['code']})"
                user = usr.get_user(chat_id)
                lang = user.get("lang", "en") if user else "en"
                _reply(chat_id, t("course_menu", lang, course=course_label))
                return True
            else:
                _reply(chat_id, f"Please pick a number between 1 and {len(courses)}.")
                return True

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


def _handle_cmd_llm(chat_id: str):
    summary = registry.get_summary()
    stt_summary = stt.get_summary() if (stt and hasattr(stt, "get_summary")) else {}
    locked_count = usr.count_locked_users()
    rf_stats = get_rules_first_stats() if get_rules_first_stats else {}
    cache_stats = intent_cache.stats() if intent_cache else {}

    lines = ["TaleemGo AI LLM Status:"]
    for prov, data in summary.items():
        status = data["status"]
        model = data.get("model", "")
        calls = data["calls_today"]
        last_err = data["last_error"]
        line = f"- {prov}: {status} (model: {model}) | calls today: {calls} | last error: {last_err}"

        usage_parts = []
        if data.get("rpm_pct") is not None:
            usage_parts.append(f"RPM:{data['rpm_pct']}%")
        if data.get("tpm_pct") is not None:
            usage_parts.append(f"TPM:{data['tpm_pct']}%")
        if data.get("rpd_pct") is not None:
            usage_parts.append(f"RPD:{data['rpd_pct']}%")
        if usage_parts:
            line += f" | usage: {', '.join(usage_parts)}"
        if data.get("chains"):
            line += f" | chains: {','.join(data['chains'])}"
        lines.append(line)

    if stt_summary:
        lines.append("\nSTT Providers:")
        for prov, data in stt_summary.items():
            status = data["status"]
            model = data.get("model", "")
            calls = data.get("calls_today", 0)
            last_err = data.get("last_error", "none")
            lines.append(f"- {prov} (STT): {status} (model: {model}) | calls today: {calls} | last error: {last_err}")

    # Telemetry for capacity, rules-first, and cache
    if rf_stats:
        rules_pct = rf_stats.get("rule_share_pct", 0.0)
        lines.append(f"\nThroughput & Optimization:")
        lines.append(f"- Rules-first: {rf_stats.get('rules_handled', 0)}/{rf_stats.get('total', 0)} ({rules_pct}% rule-handled)")
    if cache_stats:
        lines.append(f"- Intent Cache: {cache_stats.get('hits', 0)} hits, {cache_stats.get('misses', 0)} misses, {cache_stats.get('size', 0)}/{cache_stats.get('capacity', 500)} entries")

    lines.append(f"\nLocked users: {locked_count} locked")
    _reply(chat_id, "\n".join(lines))


def _handle_cmd_llm_test(chat_id: str):
    providers = get_configured_providers()
    lines = ["TaleemGo AI LLM Test:"]
    for prov in providers:
        instances = get_provider_key_instances(prov)
        for inst in instances:
            status, model_id, latency = run_live_check(inst)
            if status == "ok":
                lines.append(f"- {inst}: ok ({model_id}, {int(latency)}ms)")
            else:
                lines.append(f"- {inst}: {status} ({model_id})")
    if stt and hasattr(stt, "run_live_check") and hasattr(stt, "get_configured_stt_providers"):
        for prov in stt.get_configured_stt_providers():
            status, model_id, latency = stt.run_live_check(prov)
            if status == "ok":
                lines.append(f"- {prov} (STT): ok ({model_id}, {int(latency)}ms)")
            else:
                lines.append(f"- {prov} (STT): {status} ({model_id})")
    _reply(chat_id, "\n".join(lines))


# ---- conversation state machine ----

def handle(chat_id: str, text: str, is_self: bool = False):
    """Main dispatcher. Everything sync; called via run_in_threadpool."""
    lock = usr.get_lock(chat_id)
    with lock:
        _handle_locked(chat_id, text, is_self)


def handle_audio(chat_id: str, audio_data: dict, is_self: bool = False):
    """Handle incoming voice/audio message. Called via run_in_threadpool."""
    lock = usr.get_lock(chat_id)
    with lock:
        _handle_audio_locked(chat_id, audio_data, is_self)


def handle_audio_error(chat_id: str, is_self: bool = False):
    """Handle bridge downloadMedia failure event. Called via run_in_threadpool."""
    lock = usr.get_lock(chat_id)
    with lock:
        _handle_audio_error_locked(chat_id, is_self)


def _handle_audio_error_locked(chat_id: str, is_self: bool = False):
    user = usr.get_user(chat_id)
    if user is None:
        return

    if is_self and not user.get("is_admin"):
        usr.upsert_user(chat_id, is_admin=1)
        user["is_admin"] = 1

    lang = user.get("lang", "en")
    status = user.get("status", "awaiting_email")

    if status in ("awaiting_email", "awaiting_password"):
        _reply(chat_id, t("voice_signup_rejected", lang))
        return

    if status != "active":
        return

    is_admin = bool(user.get("is_admin"))
    if usr.is_llm_locked(chat_id, is_admin=is_admin):
        return

    _reply(chat_id, t("voice_download_failed", lang))


def _handle_audio_locked(chat_id: str, audio_data: dict, is_self: bool = False):
    """
    Voice message gate logic (all checks before transcription):
    1. Unknown user → silently ignore (no reply, no audio sent anywhere)
    2. Sign-up states (awaiting_email, awaiting_password) → localised rejection, audio dropped
    3. Active but locked out → silently ignore
    4. Hourly rate limit → localised rejection
    5. Mime type check → localised rejection for non-audio
    6. Transcribe → voice_not_understood if empty
    7. Prepend "I heard:" and process transcript through normal text pipeline
    """
    user = usr.get_user(chat_id)

    # 1. Unknown user: silently ignore
    if user is None:
        return

    # Known user
    if is_self and not user.get("is_admin"):
        usr.upsert_user(chat_id, is_admin=1)
        user["is_admin"] = 1

    lang = user.get("lang", "en")
    status = user.get("status", "awaiting_email")

    # 2. Sign-up states: reject with localised message, do NOT transcribe
    if status in ("awaiting_email", "awaiting_password"):
        _reply(chat_id, t("voice_signup_rejected", lang))
        return

    # Only active users beyond this point
    if status != "active":
        return

    # 3. Lockout check
    is_admin = bool(user.get("is_admin"))
    if usr.is_llm_locked(chat_id, is_admin=is_admin):
        return

    # 4. Hourly rate limit
    if not usr.check_and_record_voice_limit(chat_id):
        _reply(chat_id, t("voice_rate_limit", lang))
        return

    # 5. Mime type check
    mime = (audio_data.get("mime") or "").lower()
    if not mime.startswith("audio/"):
        _reply(chat_id, t("voice_wrong_mime", lang))
        return

    # 6. Size/duration check (Python-side defense in depth; bridge already checked)
    seconds = float(audio_data.get("seconds") or 0.0)
    if seconds > VOICE_MAX_SECONDS:
        _reply(chat_id, t("voice_too_long", lang, max_seconds=VOICE_MAX_SECONDS))
        return

    data_b64 = audio_data.get("data_b64") or ""
    if not data_b64:
        _reply(chat_id, t("voice_not_understood", lang))
        return
    # Estimate raw bytes
    raw_bytes = len(data_b64) * 3 // 4
    if raw_bytes > VOICE_MAX_BYTES:
        _reply(chat_id, t("voice_too_large", lang))
        return

    # 7. Transcribe
    if stt_transcribe is None:
        _reply(chat_id, t("voice_not_understood", lang))
        return

    try:
        transcript = stt_transcribe(data_b64, mime, seconds, lang_hint=lang)
    except Exception:
        log.exception("[VOICE] STT raised exception for %s…", chat_id[-6:])
        transcript = None

    if not transcript or not transcript.strip():
        _reply(chat_id, t("voice_not_understood", lang))
        return

    transcript_trimmed = transcript.strip()[:200]

    # 8. Send "I heard:" prefix
    _reply(chat_id, t("voice_heard", lang, transcript=transcript_trimmed))

    # 9. Process transcript through normal text pipeline (same as typed message)
    _handle_active(chat_id, transcript.strip(), user)


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
            usr.remove_client(chat_id)
            usr.delete_token_file(chat_id)
            _reply(chat_id, WELCOME)
            return
        else:
            # Self-chat skips activation phrase
            if not usr.try_register(chat_id, is_admin=True):
                _reply(chat_id, f"TaleemGo AI is full right now ({MAX_USERS}/{MAX_USERS} users). Please try again later.")
                return
            usr.remove_client(chat_id)
            usr.delete_token_file(chat_id)
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
    if word == "llm":
        if user.get("is_admin"):
            parts = text_stripped.split()
            if len(parts) > 1 and parts[1].lower() == "test":
                _handle_cmd_llm_test(chat_id)
            else:
                _handle_cmd_llm(chat_id)
        return

    if status == "awaiting_email":
        _handle_awaiting_email(chat_id, text)
    elif status == "active":
        _handle_active(chat_id, text, user)
    elif status == "expired":
        usr.remove_client(chat_id)
        usr.delete_token_file(chat_id)
        _reply(chat_id, "Your session has expired. Send `login` to sign in again.")
    else:
        _reply(chat_id, WELCOME)


def _handle_awaiting_email(chat_id: str, text: str):
    usr.remove_client(chat_id)
    usr.delete_token_file(chat_id)
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
    lang = user.get("lang", "en") if user else "en"
    try:
        lms = LMSClient.login_once(email=email, password=text, token_file=tf)
    except LMSAuthExpired as e:
        log.warning("Login failed for %s…: %s", chat_id[-6:], e)
        _reply(chat_id, t("login_failed_password", lang))
        return
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
    except (requests.ConnectionError, requests.Timeout) as e:
        log.warning("LMS network error for %s…: %s", chat_id[-6:], e)
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


_user_help_sent: dict = {}
_help_lock = threading.Lock()


def _are_all_providers_failing() -> bool:
    """Returns True only when providers are configured with valid keys but all are failing/in cooldown."""
    providers = get_configured_providers()
    has_valid_key = False
    for prov in providers:
        cfg = PROVIDER_CONFIGS.get(prov)
        if cfg and is_valid_api_key(os.environ.get(cfg["env_key"], "")):
            has_valid_key = True
            if registry.is_available(prov):
                return False
    return has_valid_key


def _maybe_send_help_fallback(chat_id: str, lang: str, is_busy: bool = False):
    now = time.time()
    interval_sec = int(os.environ.get("LLM_HELP_REPLY_INTERVAL_MIN", "2")) * 60
    with _help_lock:
        last = _user_help_sent.get(chat_id, 0.0)
        if now - last >= interval_sec:
            _user_help_sent[chat_id] = now
            key = "busy" if is_busy else "not_understood"
            _reply(chat_id, t(key, lang, help=t("help", lang)))


def _dispatch_exact_cmd(chat_id: str, lms, word: str, arg: str):
    usr.clear_pending(chat_id)
    if word in ("download", "dl"):
        _handle_download_cmd(chat_id, lms, arg)
    elif word == "attendance":
        _handle_attendance_cmd(chat_id, lms, arg)
    elif word in ("grades", "grade"):
        _handle_grades_cmd(chat_id, lms, arg)
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
    elif word == "plan":
        _handle_plan_cmd(chat_id, lms, arg)
    elif word == "digest":
        _handle_digest_cmd(chat_id, lms, arg)
    elif word == "calendar":
        _handle_calendar_cmd(chat_id, lms, arg)
    elif word in ("summarize", "summary"):
        _handle_summarize_cmd(chat_id, lms, arg)
    elif word == "quiz":
        _handle_quiz_cmd(chat_id, lms, arg)


def _handle_choose_action(chat_id: str, lms, act: dict, lang: str):
    pending = usr.get_pending(chat_id)
    if not pending:
        return
    if act.get("cancel"):
        usr.clear_pending(chat_id)
        _reply(chat_id, t("cancel_ack", lang))
        return
    files = pending.get("files", [])
    action = pending.get("action", "download")
    if action in ("summarize", "quiz"):
        if act.get("all"):
            _reply(chat_id, "Please pick a single file by number. Summaries and quizzes work on one file at a time.")
            return
        nums = act.get("numbers")
        if nums:
            if any(n < 1 or n > len(files) for n in nums):
                _reply(chat_id, f"Please pick a number between 1 and {len(files)}.")
                return
            if len(nums) > 1:
                _reply(chat_id, "Please pick a single file by number.")
                return
            usr.clear_pending(chat_id)
            chosen_file = files[nums[0] - 1]
            if action == "quiz":
                _execute_quiz_resource(chat_id, lms, chosen_file, n=pending.get("n", 5), lang=lang)
            else:
                _execute_summarize_resource(chat_id, lms, chosen_file, lang=lang)
        return

    if act.get("all"):
        usr.clear_pending(chat_id)
        _send_files_choice(chat_id, lms, files, indices=None)
        return
    nums = act.get("numbers")
    if nums:
        if any(n < 1 or n > len(files) for n in nums):
            _reply(chat_id, f"Please pick a number between 1 and {len(files)}.")
            return
        usr.clear_pending(chat_id)
        _send_files_choice(chat_id, lms, files, indices=nums)


def _handle_download_action(chat_id: str, lms, act: dict, lang: str):
    cq = act.get("course_query", "")
    week = act.get("week")
    tq = act.get("title_query", "")
    is_latest = bool(act.get("latest", False))

    if not cq:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": "download", "latest": is_latest, "week": week, "title_query": tq})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    hits = resolve_course(lms, cq)
    if not hits:
        courses = lms.get_courses()
        usr.set_pending_action(chat_id, {"intent": "download", "latest": is_latest, "week": week, "title_query": tq})
        _reply(chat_id, t("course_not_found", lang, courses=format_course_list(courses)))
        return

    remainder = f"week {week}" if week is not None else (tq or "")

    if len(hits) == 1:
        _handle_download_for_course(chat_id, lms, hits[0], remainder, is_latest=is_latest, lang=lang)
    else:
        options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(hits, 1))
        usr.set_pending(chat_id, {
            "type": "course",
            "courses": hits,
            "command": "download",
            "extra": remainder,
            "is_latest": is_latest,
        })
        _reply(chat_id, t("which_course", lang, options=options))


def _execute_action(chat_id: str, lms, act: dict, lang: str):
    intent = act.get("intent")
    if intent == "courses":
        cq = act.get("course_query")
        if cq:
            matches = resolve_course(lms, cq)
            if len(matches) == 1:
                course = matches[0]
                acr = course_acronym(course.get("name", "")) or course["code"]
                course_label = f"{acr} ({course['code']})"
                _reply(chat_id, t("course_menu", lang, course=course_label))
                return
            elif len(matches) > 1:
                options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
                usr.set_pending(chat_id, {"type": "course_menu", "courses": matches})
                _reply(chat_id, t("which_course", lang, options=options))
                return
        _reply(chat_id, cmd_courses(lms))
    elif intent == "assignments":
        _reply(chat_id, cmd_assignments(lms))
    elif intent == "upcoming":
        _reply(chat_id, cmd_upcoming(lms))
    elif intent == "grades":
        cq = act.get("course_query", "")
        _handle_grades_cmd(chat_id, lms, cq)
    elif intent == "help":
        _reply(chat_id, t("help", lang))
    elif intent == "attendance":
        cq = act.get("course_query", "")
        _handle_attendance_cmd(chat_id, lms, cq)
    elif intent == "resources":
        cq = act.get("course_query", "")
        _handle_resources_cmd(chat_id, lms, cq)
    elif intent == "clos":
        cq = act.get("course_query", "")
        _handle_clos_cmd(chat_id, lms, cq)
    elif intent == "download":
        _handle_download_action(chat_id, lms, act, lang)
    elif intent == "choose":
        _handle_choose_action(chat_id, lms, act, lang)
    elif intent == "plan":
        _handle_plan_action(chat_id, lms, act, lang)
    elif intent == "digest":
        _handle_digest_action(chat_id, lms, act, lang)
    elif intent == "calendar":
        _handle_calendar_action(chat_id, lms, act, lang)
    elif intent == "summarize":
        _handle_study_action(chat_id, lms, act, is_quiz=False, lang=lang)
    elif intent == "quiz":
        _handle_study_action(chat_id, lms, act, is_quiz=True, lang=lang)


def _handle_natural_language(chat_id: str, text: str, user: dict, lms):
    is_admin = bool(user.get("is_admin"))

    # 1. Explicit check for user requesting to log out or delete data
    text_lower = text.lower()
    logout_triggers = [
        "delete my data", "delete data", "delete account", "remove my data",
        "logout", "log me out", "sign out", "account delete", "data delete",
        "ڈیٹا ڈیلیٹ", "اکاؤنٹ ڈیلیٹ", "لاگ آؤٹ", "لاگ آئوٽ", "ڊيٽا مٽايو", "کاتو ختم",
    ]
    if any(trig in text_lower for trig in logout_triggers):
        lang = user.get("lang", "en")
        _reply(chat_id, t("use_logout", lang))
        return

    # 2. Sanitise message
    clean_text, is_anomaly = sanitize_user_message(text)
    if is_anomaly:
        just_locked = usr.record_strike(chat_id, strikes=1, is_admin=is_admin)
        if just_locked and int(os.environ.get("LLM_NOTIFY_ON_LOCK", "1")):
            lang = user.get("lang", "en")
            hours = int(os.environ.get("LLM_LOCK_HOURS", "2"))
            _reply(chat_id, t("lock_notice", lang, hours=hours, help=t("help", lang)))
        return

    if not clean_text:
        return

    # 3. Pre-filter jailbreak check (instant 2 strikes and lockout without calling provider)
    if matches_jailbreak_prefilter(clean_text) or matches_jailbreak_prefilter(text):
        just_locked = usr.record_strike(chat_id, strikes=2, is_admin=is_admin)
        if just_locked and int(os.environ.get("LLM_NOTIFY_ON_LOCK", "1")):
            lang = user.get("lang", "en")
            hours = int(os.environ.get("LLM_LOCK_HOURS", "2"))
            _reply(chat_id, t("lock_notice", lang, hours=hours, help=t("help", lang)))
        return

    has_pending = (usr.get_pending(chat_id) is not None)
    stored_lang = user.get("lang", "en")

    rule_res = match_rules(clean_text, has_pending_download=has_pending, stored_lang=stored_lang)
    parsed = None
    use_rules = False

    if can_run_rules_first(clean_text, rule_res, lms):
        record_intent_handled(is_rule_handled=True)
        parsed = rule_res
    else:
        if not is_llm_enabled():
            use_rules = True
        elif not usr.check_and_record_llm_limit(chat_id):
            use_rules = True

        if not use_rules:
            history = get_user_history(chat_id)
            parsed = route_intent(
                clean_text,
                has_pending_download=has_pending,
                stored_lang=stored_lang,
                history=history,
            )
            if parsed is None:
                use_rules = True

        if use_rules:
            if rule_res and rule_res.get("unsupported"):
                resp_lang = rule_res.get("lang", stored_lang)
                if resp_lang != user.get("lang"):
                    usr.upsert_user(chat_id, lang=resp_lang)
                    user["lang"] = resp_lang
                effective_lang = user.get("lang", "en")
                _reply(chat_id, t("unsupported", effective_lang, help=t("help", effective_lang)))
                return
            elif rule_res and rule_res.get("actions"):
                parsed = rule_res
            else:
                if not is_llm_enabled():
                    _reply(chat_id, HELP)
                else:
                    is_busy = _are_all_providers_failing()
                    _maybe_send_help_fallback(chat_id, stored_lang, is_busy=is_busy)
                return

    # Language determination follows CURRENT message unless signal-less
    detected_lang = detect_language(clean_text, stored_lang=stored_lang, is_recent=True)
    if detected_lang and detected_lang != user.get("lang"):
        usr.upsert_user(chat_id, lang=detected_lang)
        user["lang"] = detected_lang
    effective_lang = detected_lang or user.get("lang", "en")

    # Check unsupported from model (grades, marks, timetable, fees, results)
    if parsed.get("unsupported"):
        _reply(chat_id, t("unsupported", effective_lang, help=t("help", effective_lang)))
        return

    # Check off-topic from model
    if parsed.get("off_topic"):
        # Rescue check with rule matcher (Part S.7 & unsupported keywords)
        rule_res = match_rules(clean_text, has_pending_download=has_pending, stored_lang=stored_lang)
        if rule_res:
            if rule_res.get("unsupported"):
                rule_lang = rule_res.get("lang", effective_lang)
                _reply(chat_id, t("unsupported", rule_lang, help=t("help", rule_lang)))
                return
            elif not rule_res.get("off_topic") and rule_res.get("actions"):
                parsed = rule_res

        if parsed.get("off_topic"):
            just_locked = usr.record_strike(chat_id, strikes=1, is_admin=is_admin)
            if just_locked and int(os.environ.get("LLM_NOTIFY_ON_LOCK", "1")):
                lang = user.get("lang", "en")
                hours = int(os.environ.get("LLM_LOCK_HOURS", "2"))
                _reply(chat_id, t("lock_notice", lang, hours=hours, help=t("help", lang)))
            return

    add_user_history(chat_id, clean_text)

    # Clarify
    actions = parsed.get("actions", [])
    clarify = parsed.get("clarify")
    if clarify:
        if clarify == "course":
            courses = lms.get_courses()
            options = format_course_list(courses)
            act_intent = actions[0].get("intent") if actions else "attendance"
            usr.set_pending_action(chat_id, {"intent": act_intent})
            _reply(chat_id, t("which_course", effective_lang, options=options))
        elif clarify == "week":
            _reply(chat_id, t("which_week", effective_lang))
        elif clarify == "what":
            _reply(chat_id, t("what", effective_lang))
        return

    if not actions:
        return

    if len(actions) == 1 and actions[0].get("intent") == "greeting":
        if int(os.environ.get("GREETING_REPLY", "1")):
            _reply(chat_id, t("greeting", effective_lang))
        return

    if actions[0].get("intent") == "greeting":
        if int(os.environ.get("GREETING_REPLY", "1")):
            _reply(chat_id, t("greeting", effective_lang))
        actions = actions[1:]

    # NOTE: Separate ack message before data actions removed entirely (Part D)
    for act in actions:
        _execute_action(chat_id, lms, act, effective_lang)


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

    # 1. Check if there is an in-memory pending selection
    pending = usr.get_pending(chat_id)
    if pending is not None:
        if _handle_pending(chat_id, lms, pending, text):
            return

    # 1a. Check active interactive quiz
    quiz = usr.get_active_quiz(chat_id)
    if quiz is not None:
        if _handle_active_quiz_reply(chat_id, quiz, text, user):
            return

    # 1b. Selection reply guard when NO pending selection exists (or expired) (Part F)
    t_strip = text.strip().lower()
    tokens = [x for x in re.split(r"[\s,]+", text.strip()) if x]
    is_selection_reply = (
        t_strip in ("all", "cancel")
        or (bool(tokens) and all(x.isdigit() for x in tokens))
    )
    if is_selection_reply:
        lang = user.get("lang", "en")
        _reply(chat_id, t("selection_expired", lang))
        return

    # 2. Exact command check
    text_stripped = text.strip()
    word, _, arg = text_stripped.partition(" ")
    word = word.lower().lstrip("/")
    arg = arg.strip()

    exact_cmds = {
        "download", "dl", "attendance", "resources", "clos", "courses",
        "assignments", "upcoming", "grades", "grade", "plan", "digest", "calendar",
        "summarize", "summary", "quiz",
    }
    if word in exact_cmds:
        usr.clear_pending_action(chat_id)
        try:
            _dispatch_exact_cmd(chat_id, lms, word, arg)
        except LMSAuthExpired:
            usr.upsert_user(chat_id, status="expired")
            usr.remove_client(chat_id)
            _reply(chat_id, "Your LMS session expired. Send `login` to sign in again.")
        except Exception:
            log.exception("Command failed for %s…", chat_id[-6:])
            _reply(chat_id, "Something went wrong talking to the LMS. Try again.")
        return

    # 2b. Check pending action follow-up (Part C)
    pending_act = usr.get_pending_action(chat_id)
    if pending_act is not None:
        c_query = clean_course_query(text)
        matches = resolve_course(lms, c_query) if c_query else []
        if matches:
            usr.clear_pending_action(chat_id)
            intent = pending_act.get("intent")
            lang = detect_language(text, stored_lang=user.get("lang", "en"), is_recent=True)
            if len(matches) == 1:
                course = matches[0]
                if intent == "download":
                    is_latest = bool(pending_act.get("latest", False))
                    week = pending_act.get("week")
                    remainder = f"week {week}" if week is not None else pending_act.get("title_query", "")
                    _handle_download_for_course(chat_id, lms, course, remainder, is_latest=is_latest, lang=lang)
                elif intent == "attendance":
                    _show_course_attendance(chat_id, lms, course, lms.get_attendance())
                elif intent == "resources":
                    _show_course_resources(chat_id, lms, course)
                elif intent == "clos":
                    _show_course_clos(chat_id, lms, course)
                elif intent == "grades":
                    _show_course_grades(chat_id, lms, course)
                elif intent == "plan":
                    target = pending_act.get("target")
                    miss = pending_act.get("miss")
                    _show_course_plan(chat_id, lms, course, target=target, miss=miss, lang=lang)
                elif intent in ("summarize", "quiz"):
                    is_quiz = (intent == "quiz")
                    is_latest = bool(pending_act.get("latest", False))
                    week = pending_act.get("week")
                    remainder = f"week {week}" if week is not None else pending_act.get("title_query", "")
                    n = pending_act.get("n", 5)
                    _handle_study_for_course(chat_id, lms, course, remainder=remainder, is_latest=is_latest, is_quiz=is_quiz, n=n, lang=lang)
                return
            else:
                options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
                extra_str = f"week {pending_act.get('week')}" if pending_act.get("week") is not None else pending_act.get("title_query", "")
                if intent == "plan":
                    extra_str = f"miss={pending_act.get('miss')}" if pending_act.get("miss") is not None else f"target={pending_act.get('target', 75)}"
                usr.set_pending(chat_id, {
                    "type": "course",
                    "courses": matches,
                    "command": intent,
                    "extra": extra_str,
                    "is_latest": bool(pending_act.get("latest", False)),
                    "n": pending_act.get("n", 5),
                })
                _reply(chat_id, t("which_course", lang, options=options))
                return
        else:
            usr.clear_pending_action(chat_id)

    # 2c. Course-only menu check (Part B: "Ai course", "Ai", "CC-311" alone, no pending action)
    candidate_tokens = [re.sub(r"^[^\w]+|[^\w]+$", "", t) for t in text.strip().split()]
    candidate_tokens = [t for t in candidate_tokens if t]
    course_generic_words = {"course", "courses", "subject", "subjects", "class", "classes", "kors", "mazmoon", "کورس", "کورسز", "ڪورس", "ڪورسز", "مضمون", "ڪلاس", "کلاس"}
    non_generic = [t for t in candidate_tokens if t.lower() not in course_generic_words]
    c_cand = " ".join(non_generic) if non_generic else ""
    if c_cand:
        # Only treat as course-only menu if the message doesn't match an explicit action intent
        rule_check = match_rules(text)
        has_explicit_intent = rule_check and (
            rule_check.get("unsupported")
            or rule_check.get("off_topic")
            or any(a.get("intent") in ("attendance", "download", "resources", "clos", "grades", "assignments", "upcoming", "help", "greeting", "plan", "digest", "calendar", "summarize", "quiz")
                   for a in rule_check.get("actions", []))
        )
        if not has_explicit_intent:
            matches = resolve_course(lms, c_cand)
            if len(matches) == 1:
                course = matches[0]
                acr = course_acronym(course.get("name", "")) or course["code"]
                course_label = f"{acr} ({course['code']})"
                lang = detect_language(text, stored_lang=user.get("lang", "en"), is_recent=True)
                _reply(chat_id, t("course_menu", lang, course=course_label))
                return
            elif len(matches) > 1:
                options = "\n".join(f"{i}. {c.get('code')} {c.get('name')}" for i, c in enumerate(matches, 1))
                usr.set_pending(chat_id, {"type": "course_menu", "courses": matches})
                lang = detect_language(text, stored_lang=user.get("lang", "en"), is_recent=True)
                _reply(chat_id, t("which_course", lang, options=options))
                return

    # 3. Lockout check (Part G)
    is_admin = bool(user.get("is_admin"))
    if usr.is_llm_locked(chat_id, is_admin=is_admin):
        return

    # 4. Natural language pipeline
    try:
        _handle_natural_language(chat_id, text, user, lms)
    except LMSAuthExpired:
        usr.upsert_user(chat_id, status="expired")
        usr.remove_client(chat_id)
        usr.delete_token_file(chat_id)
        _reply(chat_id, "Your LMS session expired. Send `login` to sign in again.")
    except Exception:
        log.exception("Natural language handling failed for %s…", chat_id[-6:])
        _reply(chat_id, "Something went wrong talking to the LMS. Try again.")


def _do_logout(chat_id: str):
    usr.clear_pending_action(chat_id)
    usr.purge_user_data(chat_id)
    _reply(chat_id, 'Logged out and all your data deleted. Send "Hi TaleemGo AI" to start again.')


def _do_login_reset(chat_id: str):
    usr.clear_pending(chat_id)
    usr.clear_pending_action(chat_id)
    usr.clear_active_quiz(chat_id)
    usr.remove_client(chat_id)
    usr.delete_token_file(chat_id)
    user = usr.get_user(chat_id)
    if user:
        usr.upsert_user(chat_id, status="awaiting_email", attempt=0)
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
    text = (body.get("text") or "").strip()
    is_self = bool(body.get("is_self", False))
    audio_data = body.get("audio")  # Optional: {"mime": str, "data_b64": str, "seconds": float}
    audio_error = bool(body.get("audio_error", False))
    if not chat_id:
        raise HTTPException(400, "chat_id required")
    if audio_error:
        await run_in_threadpool(handle_audio_error, chat_id, is_self)
    elif audio_data is not None:
        await run_in_threadpool(handle_audio, chat_id, audio_data, is_self)
    else:
        await run_in_threadpool(handle, chat_id, text, is_self)
    return {"ok": True}
