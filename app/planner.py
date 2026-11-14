"""
planner.py - Attendance planning formulas and formatting for TaleemGo AI.
"""
import math
import os
from typing import Optional


def get_default_target() -> int:
    try:
        val = int(os.environ.get("ATTENDANCE_WARN", "75"))
        if 1 <= val <= 99:
            return val
    except Exception:
        pass
    return 75


def calculate_attendance_plan(present: int, total: int, target: float) -> dict:
    """
    Calculate attendance planning metrics for a given course.
    - If total == 0: no records recorded
    - If current percent >= target: max classes student can miss while staying >= target
      miss k classes: floor(present / (T/100) - total), never below 0
    - If current percent < target: classes in a row student must attend to reach target
      attend x in a row: ceil((T*total - 100*present) / (100 - T))
    """
    if total <= 0:
        return {
            "status": "no_records",
            "present": present,
            "total": total,
            "percent": None,
            "target": target,
        }

    pct = round(100.0 * present / total, 1)

    if pct >= target:
        # Can miss k classes
        k = max(0, math.floor(present / (target / 100.0) - total))
        return {
            "status": "safe",
            "present": present,
            "total": total,
            "percent": pct,
            "target": target,
            "can_miss": k,
        }
    else:
        # Must attend x classes in a row
        denom = 100.0 - target
        if denom <= 0:
            x = 0
        else:
            x = max(0, math.ceil((target * total - 100.0 * present) / denom))
        return {
            "status": "warning",
            "present": present,
            "total": total,
            "percent": pct,
            "target": target,
            "must_attend": x,
        }


def calculate_miss_what_if(present: int, total: int, miss_count: int) -> dict:
    """
    Calculate resulting attendance percentage if the next miss_count classes are missed.
    """
    new_total = total + max(0, miss_count)
    if new_total <= 0:
        new_pct = None
    else:
        new_pct = round(100.0 * present / new_total, 1)

    return {
        "present": present,
        "total": total,
        "miss_count": miss_count,
        "new_total": new_total,
        "new_percent": new_pct,
    }

