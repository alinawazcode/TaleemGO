"""
planner.py - Attendance planning formulas and formatting for TaleemGo AI.
"""
import math
import os
import re
from typing import Optional


def get_default_target() -> int:
    try:
        val = int(os.environ.get("ATTENDANCE_WARN", "75"))
        if 1 <= val <= 99:
            return val
    except Exception:
        pass
    return 75


def get_planned_classes_for_course(course_code: str = "") -> Optional[int]:
    """
    Get planned total classes for a course from environment configuration:
    1. CLASSES_PER_COURSE_<CODE> (e.g. CLASSES_PER_COURSE_CS401 or CLASSES_PER_COURSE_CS_401)
    2. Fall back to CLASSES_PER_COURSE (global default)
    Returns int if set and > 0, otherwise None (unknown).
    """
    if course_code:
        # Clean punctuation from code, e.g. "CS-401" -> "CS401", "CS_401"
        clean1 = re.sub(r"[^A-Za-z0-9]", "", course_code).upper()
        clean2 = re.sub(r"[^A-Za-z0-9]", "_", course_code).upper()
        for k in (f"CLASSES_PER_COURSE_{clean1}", f"CLASSES_PER_COURSE_{clean2}"):
            v = os.environ.get(k)
            if v and v.strip().isdigit():
                val = int(v.strip())
                if val > 0:
                    return val

    # Global default
    glob = os.environ.get("CLASSES_PER_COURSE", "").strip()
    if glob.isdigit():
        val = int(glob)
        if val > 0:
            return val

    return None


def calculate_attendance_plan(
    present: int,
    total: int,
    target: float,
    remaining: Optional[int] = None,
) -> dict:
    """
    Calculate attendance planning metrics for a given course.
    - If total == 0: no records recorded
    - If remaining is known:
        - can_miss is capped at remaining
        - if must_attend > remaining, status is 'unreachable', shows max_pct and reachable_target
        - shows max_percent if all remaining attended, min_percent if all missed
    - If remaining is None:
        - unknown_remaining is True, marks if must_attend > 20 as unlikely
    """
    if total <= 0:
        return {
            "status": "no_records",
            "present": present,
            "total": total,
            "percent": None,
            "target": target,
            "remaining": remaining,
        }

    pct = round(100.0 * present / total, 1)

    if remaining is not None:
        remaining = max(0, int(remaining))
        max_tot = total + remaining
        max_pres = present + remaining
        max_pct = round(100.0 * max_pres / max_tot, 1) if max_tot > 0 else 0.0
        min_pct = round(100.0 * present / max_tot, 1) if max_tot > 0 else 0.0

        if pct >= target:
            raw_k = max(0, math.floor(present / (target / 100.0) - total))
            k = min(raw_k, remaining)
            return {
                "status": "safe",
                "present": present,
                "total": total,
                "percent": pct,
                "target": target,
                "remaining": remaining,
                "can_miss": k,
                "max_pct": max_pct,
                "min_pct": min_pct,
            }
        else:
            denom = 100.0 - target
            if denom <= 0:
                must_attend = 0
            else:
                must_attend = max(0, math.ceil((target * total - 100.0 * present) / denom))

            if must_attend > remaining:
                reachable_tgt = int(math.floor(max_pct))
                return {
                    "status": "unreachable",
                    "present": present,
                    "total": total,
                    "percent": pct,
                    "target": target,
                    "remaining": remaining,
                    "must_attend": must_attend,
                    "max_pct": max_pct,
                    "min_pct": min_pct,
                    "reachable_target": reachable_tgt,
                    "pres_plus_left": max_pres,
                    "tot_plus_left": max_tot,
                }
            else:
                return {
                    "status": "warning",
                    "present": present,
                    "total": total,
                    "percent": pct,
                    "target": target,
                    "remaining": remaining,
                    "must_attend": must_attend,
                    "max_pct": max_pct,
                    "min_pct": min_pct,
                }

    # Remaining is unknown
    if pct >= target:
        k = max(0, math.floor(present / (target / 100.0) - total))
        return {
            "status": "safe",
            "present": present,
            "total": total,
            "percent": pct,
            "target": target,
            "can_miss": k,
            "remaining": None,
            "unknown_remaining": True,
        }
    else:
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
            "remaining": None,
            "unknown_remaining": True,
            "is_unlikely": (x > 20),
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
