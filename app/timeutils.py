from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo

TZ_NAME = os.environ.get("TIMEZONE", "Asia/Karachi")
try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Karachi")

KARACHI = LOCAL_TZ  # backwards compatibility


def get_local_now(custom_now=None) -> datetime:
    """Return current datetime in the configured local timezone."""
    if custom_now is not None:
        if isinstance(custom_now, datetime):
            if custom_now.tzinfo is None:
                return custom_now.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            return custom_now.astimezone(LOCAL_TZ)
    return datetime.now(LOCAL_TZ)


def get_local_date_and_time(custom_now=None) -> tuple[str, str]:
    """Return (YYYY-MM-DD, HH:MM) in the configured local timezone."""
    now = get_local_now(custom_now)
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


def to_local(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(LOCAL_TZ).strftime("%d %b, %I:%M %p")
