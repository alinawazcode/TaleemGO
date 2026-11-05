from collections import Counter
from lms_client import LMSClient

lms = LMSClient()
res = lms.get_resources()
print("Total:", len(res))
print("Missing week_number:", sum(1 for r in res if r.get("week_number") is None))
print("Per course:", Counter(r["course_id"] for r in res))