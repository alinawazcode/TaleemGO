"""
calendar_gen.py - RFC 5545 iCalendar (.ics) generator for student assignments and deadlines.
Standard library only: CRLF line endings, text escaping, 75-octet line folding, stable UIDs, UTC timestamps, 1-day alarm.
"""
from datetime import datetime, timezone
from typing import Optional


def escape_ics_text(text: str) -> str:
    """Escape text according to RFC 5545: backslashes, semicolons, commas, newlines."""
    if not text:
        return ""
    s = str(text)
    # Normalize CRLF or CR to LF first
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Backslash first
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    s = s.replace("\n", "\\n")
    return s


def fold_ics_line(line: str, max_octets: int = 75) -> str:
    """
    Fold long lines at max_octets (RFC 5545 specifies 75 octets).
    Continuation lines begin with a single SPACE character (CRLF + space).
    """
    raw_bytes = line.encode("utf-8")
    if len(raw_bytes) <= max_octets:
        return line

    parts = []
    idx = 0
    first = True
    while idx < len(raw_bytes):
        # On continuation lines, the leading space takes 1 octet
        limit = max_octets if first else (max_octets - 1)
        remaining = len(raw_bytes) - idx
        if remaining <= limit:
            chunk = raw_bytes[idx:].decode("utf-8")
            parts.append(chunk if first else (" " + chunk))
            break

        # Find safe UTF-8 split point
        split = idx + limit
        while split > idx and (raw_bytes[split] & 0xC0) == 0x80:
            split -= 1

        chunk = raw_bytes[idx:split].decode("utf-8")
        parts.append(chunk if first else (" " + chunk))
        first = False
        idx = split

    return "\r\n".join(parts)


def format_utc_dt(dt: datetime) -> str:
    """Format datetime as YYYYMMDDTHHMMSSZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def generate_calendar_ics(
    assignments: list[dict],
    courses: dict[str, dict],
    now_dt: Optional[datetime] = None,
) -> Optional[str]:
    """
    Build an RFC 5545 iCalendar (.ics) string for pending assignments.
    Returns None if there are no assignments with valid due dates.
    """
    if not assignments:
        return None

    now = now_dt if now_dt is not None else datetime.now(timezone.utc)
    dtstamp_str = format_utc_dt(now)

    valid_events = []
    for a in assignments:
        due_raw = a.get("due_date")
        if not due_raw:
            continue
        try:
            due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        c = courses.get(a.get("course_id", ""), {})
        c_code = c.get("code", "")
        c_name = c.get("name", "")
        c_label = f"{c_code} - {c_name}".strip(" -") if (c_code or c_name) else "University Course"

        title = a.get("title", "Assignment")
        aid = a.get("id", "0")
        uid = f"assignment-{aid}@taleemgo"
        due_str = format_utc_dt(due_dt)

        summary = f"{title} ({c_code})" if c_code else title
        desc = f"Assignment: {title}\\nCourse: {c_label}\\nDue Date: {due_raw}"

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp_str}",
            f"DTSTART:{due_str}",
            f"DTEND:{due_str}",
            f"SUMMARY:{escape_ics_text(summary)}",
            f"DESCRIPTION:{escape_ics_text(desc)}",
            "STATUS:CONFIRMED",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Reminder: {escape_ics_text(title)} due tomorrow",
            "END:VALARM",
            "END:VEVENT",
        ]
        valid_events.append(event_lines)

    if not valid_events:
        return None

    raw_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TaleemGo AI//LMS Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:TaleemGo Deadlines",
    ]
    for ev in valid_events:
        raw_lines.extend(ev)
    raw_lines.append("END:VCALENDAR")

    folded_lines = [fold_ics_line(l) for l in raw_lines]
    # RFC 5545 requires CRLF line terminators and a trailing CRLF
    return "\r\n".join(folded_lines) + "\r\n"

