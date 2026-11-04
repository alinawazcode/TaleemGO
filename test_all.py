"""
test_all.py - Run every LMS function the Telegram bot will call.

Goal: find bugs in lms_client.py now, not after the bot is wired up.
Run this, read the output section by section, and tell me which
section (if any) looks wrong or crashes.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from lms_client import LMSClient

KARACHI = ZoneInfo("Asia/Karachi")


def to_local(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(KARACHI).strftime("%d %b %Y, %I:%M %p")


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


lms = LMSClient()

# ---------- courses ----------
section("COURSES")
courses = lms.get_courses()
for c in courses:
    print(f"  {c.get('code'):10s} {c.get('name')}")
print(f"  -> {len(courses)} courses")

# ---------- assignments (submission-aware) ----------
section("ASSIGNMENTS (status checked against your submissions)")
assignments = lms.get_assignments_with_status()
for a in assignments:
    print(f"  [{a['status']:14s}] {to_local(a['due_date'])}  {a['title']}")
print(f"  -> {len(assignments)} assignments")

# ---------- resources, grouped like the bot will show them ----------
section("RESOURCES (by course, by week)")
resources = lms.get_resources()
by_course = {}
for r in resources:
    by_course.setdefault(r["course_id"], []).append(r)

for c in courses:
    items = by_course.get(c["id"], [])
    if not items:
        continue
    print(f"\n  {c['code']} - {c['name']} ({len(items)} files)")
    by_week = {}
    for r in items:
        wk = r.get("week_number")
        by_week.setdefault(wk, []).append(r)
    for wk in sorted(by_week, key=lambda w: (w is None, w)):
        label = f"Week {wk}" if wk is not None else "Other"
        for r in by_week[wk]:
            print(f"    {label:8s} {r['title']}")

# ---------- attendance ----------
section("ATTENDANCE")
att = lms.get_attendance()
o = att["overall"]
print(f"  Overall: {o['percent']}%  ({o['present']} present / {o['total']} sessions)")
for c in courses:
    s = att["courses"][c["id"]]
    if s["total"] == 0:
        print(f"  {c['code']:10s} no attendance recorded")
    else:
        print(f"  {c['code']:10s} {s['percent']}%  "
              f"({s['present']} present, {s['absent']} absent, {s['total']} total)")

# ---------- CLOs ----------
section("CLOs (first course that has any)")
for c in courses:
    clos = lms.get_clos(c["id"])
    if clos:
        print(f"  {c['code']} - {c['name']}")
        for clo in clos:
            print(f"    CLO-{clo['clo_number']} [{clo['bt_level']}] {clo['description'][:70]}")
        break
else:
    print("  No course returned any CLOs.")

# ---------- download ----------
section("DOWNLOAD (newest resource)")
if resources:
    newest = sorted(resources, key=lambda r: r["created_at"], reverse=True)[0]
    print(f"  Downloading: {newest['title']}")
    try:
        path = lms.download_resource(newest["id"])
        print(f"  -> saved to {path}")
    except Exception as e:
        print(f"  -> FAILED: {e}")
else:
    print("  No resources to download.")

print("\nDone. Check each section above for anything wrong or missing.")