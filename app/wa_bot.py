"""WhatsApp command router. Run with:  uvicorn app.wa_bot:api --port 8000
Plain-text commands: help, courses, assignments, upcoming, resources <course>,
attendance [course], clos <course>, download <course> <part of title>
"""
import logging
import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv()

from lms_client import LMSClient          # noqa: E402
from timeutils import to_local            # noqa: E402  (same helper bot.py used)
import wa_transport as wa                 # noqa: E402

log = logging.getLogger("sathi.wa")
logging.basicConfig(level=logging.INFO)
import threading                          # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from wa_monitor import check_updates      # noqa: E402

api = FastAPI()
lms = LMSClient()
LMS_LOCK = threading.Lock()  # commands and monitor share one client/token file


def _monitor_job():
    with LMS_LOCK:
        check_updates(lms)


@api.on_event("startup")
def _start_monitor():
    minutes = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "30"))
    sched = BackgroundScheduler()
    sched.add_job(_monitor_job, "interval", minutes=minutes, next_run_time=None)
    sched.start()
    # first run after 60s, giving the bridge time to connect
    from datetime import datetime, timedelta
    sched.modify_job(sched.get_jobs()[0].id, next_run_time=datetime.now() + timedelta(seconds=60))
    api.state.sched = sched
    log.info("Monitor scheduled every %d min", minutes)
TOKEN = os.environ.get("BRIDGE_TOKEN", "")

HELP = (
    "Sathi commands:\n"
    "courses\nassignments\nupcoming\n"
    "resources <course>\nattendance [course]\nclos <course>\n"
    "download <course> <part of title>"
)


def find_course(query: str):
    q = query.strip().lower()
    for c in lms.get_courses():
        if q and (q in c["code"].lower() or q in c["name"].lower()):
            return c
    return None


def cmd_courses(_):
    return "\n".join(f"{c['code']} - {c['name']}" for c in lms.get_courses())


def cmd_assignments(_):
    rows = lms.get_assignments_with_status()
    return "\n".join(f"[{a['status']}] {to_local(a['due_date'])}  {a['title']}" for a in rows) or "No assignments."


def cmd_upcoming(_):
    rows = [a for a in lms.get_assignments_with_status() if a["status"] == "pending"]
    return "\n".join(f"{to_local(a['due_date'])}  {a['title']}" for a in rows) or "Nothing pending."


def cmd_resources(arg):
    c = find_course(arg)
    if not c:
        return "Course not found. Try: resources AI"
    res = [r for r in lms.get_resources() if r["course_id"] == c["id"]]
    res.sort(key=lambda r: (r.get("week_number") is None, r.get("week_number") or 0))
    lines = [f"Week {r['week_number'] if r.get('week_number') is not None else '-'}: {r['title']}" for r in res]
    return f"{c['code']} {c['name']}\n" + "\n".join(lines)


def cmd_attendance(arg):
    att = lms.get_attendance()
    if not arg:
        return f"Overall attendance: {att['overall']}"
    c = find_course(arg)
    return f"{c['code']}: {att['courses'][c['id']]}" if c else "Course not found."


def cmd_clos(arg):
    c = find_course(arg)
    if not c:
        return "Course not found."
    return "\n".join(str(x) for x in lms.get_clos(c["id"]))


def cmd_download(arg):
    parts = arg.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: download <course> <part of title>"
    c = find_course(parts[0])
    if not c:
        return "Course not found."
    hits = [r for r in lms.get_resources()
            if r["course_id"] == c["id"] and parts[1].lower() in r["title"].lower()]
    if not hits:
        return "No matching file."
    if len(hits) > 1:
        return "Several matches, be more specific:\n" + "\n".join(r["title"] for r in hits[:10])
    r = hits[0]
    # ADAPT: use whatever your client's download method is called
    path = lms.download_resource(r["id"])
    wa.send_file(path, r["title"])
    return None  # file already sent


ROUTES = {"courses": cmd_courses, "assignments": cmd_assignments, "upcoming": cmd_upcoming,
          "resources": cmd_resources, "attendance": cmd_attendance, "clos": cmd_clos,
          "download": cmd_download}


@api.post("/incoming")
async def incoming(request: Request, x_token: str = Header(default="")):
    if x_token != TOKEN:
        raise HTTPException(401)
    text = (await request.json()).get("text", "").strip()
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(handle, text)


def handle(text: str):  # sync on purpose: lms_client uses blocking requests
    word, _, arg = text.partition(" ")
    fn = ROUTES.get(word.lower().lstrip("/"))
    try:
        with LMS_LOCK:
            reply = fn(arg) if fn else HELP
    except Exception:
        log.exception("handler failed")
        reply = "Something went wrong talking to the LMS."
    if reply:
        wa.send_text(reply)
    return {"ok": True}