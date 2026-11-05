"""
bot.py - Sathi Telegram bot (single user).

.env needs (in addition to the LMS vars):
  TELEGRAM_BOT_TOKEN=<from BotFather>
  AUTHORIZED_TELEGRAM_ID=<your numeric Telegram user id>

To find your Telegram ID: message @userinfobot on Telegram.

Run: python bot.py
"""

import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from lms_client import LMSClient, LMSError
from monitor import check_updates
from timeutils import to_local

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sathi")

AUTHORIZED_ID = int(os.environ["AUTHORIZED_TELEGRAM_ID"])
MONITOR_INTERVAL_MIN = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "30"))

lms = LMSClient()  # one shared client; single-user bot


def esc(s):
    return html.escape(str(s))


def find_course(query):
    """Match a course by code or partial name, case-insensitive."""
    query = query.strip().lower()
    for c in lms.get_courses():
        if query == c.get("code", "").lower() or query in c.get("name", "").lower():
            return c
    return None


def authorized(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != AUTHORIZED_ID:
            await update.message.reply_text("This bot is private.")
            log.warning("Rejected id=%s", update.effective_user.id)
            return
        try:
            await handler(update, context)
        except LMSError as e:
            await update.message.reply_text(f"LMS error: {e}")
        except Exception:
            log.exception("Handler failed")
            await update.message.reply_text(
                "Something went wrong talking to the LMS. Try again in a moment.")
    return wrapped


# ---------------- commands ----------------

@authorized
async def start(update, context):
    await update.message.reply_text(
        "Sathi is online.\n\n"
        "/courses - your enrolled courses\n"
        "/assignments - all assignments, with status\n"
        "/upcoming - only pending/overdue, soonest first\n"
        "/resources <course> - lecture files by week\n"
        "/download <course> <text in filename> - send a file\n"
        "/attendance [course] - your attendance\n"
        "/clos <course> - course learning outcomes"
    )


@authorized
async def courses_cmd(update, context):
    lines = [f"<b>{esc(c['code'])}</b> - {esc(c['name'])}" for c in lms.get_courses()]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


STATUS_LABEL = {
    "overdue": "OVERDUE",
    "submitted": "Submitted",
    "logged_no_file": "Logged (no file)",
    "pending": "Pending",
}


@authorized
async def assignments_cmd(update, context):
    items = lms.get_assignments_with_status()
    if not items:
        await update.message.reply_text("No assignments found.")
        return
    lines = []
    for a in items:
        lines.append(f"[{STATUS_LABEL[a['status']]}] {esc(a['title'])}\n"
                      f"    Due: {to_local(a['due_date'])}")
    await update.message.reply_text("\n\n".join(lines))


@authorized
async def upcoming_cmd(update, context):
    items = [a for a in lms.get_assignments_with_status()
             if a["status"] in ("overdue", "pending")]
    items.sort(key=lambda a: a["due_date"])
    if not items:
        await update.message.reply_text("Nothing pending or overdue.")
        return
    lines = [f"[{STATUS_LABEL[a['status']]}] {esc(a['title'])} - {to_local(a['due_date'])}"
              for a in items]
    await update.message.reply_text("\n".join(lines))


@authorized
async def resources_cmd(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /resources <course code or name>")
        return
    course = find_course(" ".join(context.args))
    if not course:
        await update.message.reply_text("Course not found. Try /courses for the list.")
        return

    items = lms.get_resources(course["id"])
    if not items:
        await update.message.reply_text(f"No resources for {course['code']} yet.")
        return

    by_week = {}
    for r in items:
        by_week.setdefault(r.get("week_number"), []).append(r)

    lines = [f"<b>{esc(course['code'])} - {esc(course['name'])}</b>"]
    for wk in sorted(by_week, key=lambda w: (w is None, w)):
        label = f"Week {wk}" if wk is not None else "Other"
        lines.append(f"\n<u>{label}</u>")
        for r in by_week[wk]:
            lines.append(f"  {esc(r['title'])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized
async def download_cmd(update, context):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /download <course code> <part of the file name>")
        return

    course = find_course(context.args[0])
    if not course:
        await update.message.reply_text("Course not found. Try /courses for the list.")
        return

    search = " ".join(context.args[1:]).lower()
    matches = [r for r in lms.get_resources(course["id"])
               if search in r["title"].lower()]

    if not matches:
        await update.message.reply_text("No matching file found.")
        return
    if len(matches) > 1:
        names = "\n".join(f"  - {esc(r['title'])}" for r in matches[:10])
        await update.message.reply_text(
            f"Found {len(matches)} matches, be more specific:\n{names}",
            parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text(f"Downloading {matches[0]['title']}...")
    path = lms.download_resource(matches[0]["id"])
    with open(path, "rb") as f:
        await update.message.reply_document(f)


@authorized
async def attendance_cmd(update, context):
    att = lms.get_attendance()
    if not context.args:
        o = att["overall"]
        lines = [f"Overall: {o['percent']}% "
                 f"({o['present']} present / {o['total']} sessions)"]
        for c in lms.get_courses():
            s = att["courses"][c["id"]]
            if s["total"] == 0:
                lines.append(f"  {c['code']}: no attendance recorded")
            else:
                lines.append(f"  {c['code']}: {s['percent']}%")
        await update.message.reply_text("\n".join(lines))
        return

    course = find_course(" ".join(context.args))
    if not course:
        await update.message.reply_text("Course not found. Try /courses for the list.")
        return
    s = att["courses"][course["id"]]
    if s["total"] == 0:
        await update.message.reply_text(f"No attendance recorded for {course['code']}.")
        return
    await update.message.reply_text(
        f"{course['code']}: {s['percent']}%\n"
        f"{s['present']} present, {s['absent']} absent, {s['total']} total")


@authorized
async def clos_cmd(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /clos <course code or name>")
        return
    course = find_course(" ".join(context.args))
    if not course:
        await update.message.reply_text("Course not found. Try /courses for the list.")
        return
    clos = lms.get_clos(course["id"])
    if not clos:
        await update.message.reply_text(f"No CLOs found for {course['code']}.")
        return
    lines = [f"<b>{esc(course['code'])} - {esc(course['name'])}</b>"]
    for c in clos:
        lines.append(f"\nCLO-{c['clo_number']} [{esc(c['bt_level'])}]\n{esc(c['description'])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("courses", courses_cmd))
    app.add_handler(CommandHandler("assignments", assignments_cmd))
    app.add_handler(CommandHandler("upcoming", upcoming_cmd))
    app.add_handler(CommandHandler("resources", resources_cmd))
    app.add_handler(CommandHandler("download", download_cmd))
    app.add_handler(CommandHandler("attendance", attendance_cmd))
    app.add_handler(CommandHandler("clos", clos_cmd))

    # background monitor: shares this bot's event loop, no separate scheduler needed
    app.bot_data["lms"] = lms
    app.bot_data["authorized_id"] = AUTHORIZED_ID
    app.job_queue.run_repeating(
        check_updates, interval=MONITOR_INTERVAL_MIN * 60, first=15)

    log.info("Sathi bot starting (long polling, monitor every %d min)...",
              MONITOR_INTERVAL_MIN)
    app.run_polling()


if __name__ == "__main__":
    main()