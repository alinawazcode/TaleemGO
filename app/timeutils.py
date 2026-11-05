from datetime import datetime
from zoneinfo import ZoneInfo

KARACHI = ZoneInfo("Asia/Karachi")


def to_local(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(KARACHI).strftime("%d %b, %I:%M %p")