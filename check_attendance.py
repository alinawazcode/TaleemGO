from collections import Counter
from lms_client import LMSClient

lms = LMSClient()
sessions = lms._get("attendance_sessions", select="*")
records = lms._get("attendance_records", select="*",
                   student_id=f"eq.{lms.student_id}")

print("Session columns:", list(sessions[0].keys()))
print("Record columns:", list(records[0].keys()))
print("Status values:", dict(Counter(r.get("status") for r in records)))

by_session = {r["session_id"]: r for r in records}
print()
for c in lms.get_courses():
    ss = [s for s in sessions if s["course_id"] == c["id"]]
    counts = Counter(
        by_session[s["id"]].get("status") if s["id"] in by_session else "no record"
        for s in ss
    )
    print(c["code"], "| sessions:", len(ss), "|", dict(counts))