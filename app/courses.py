"""
courses.py - Course resolution and formatting for TaleemGo.
"""
import difflib
import re

SMALL_WORDS = {"of", "and", "the", "to", "in", "for", "&"}


def _clean_code(s: str) -> str:
    """Normalize course code by removing spaces, dashes, and lowercasing."""
    return re.sub(r"[\s\-]+", "", s or "").lower()


def course_acronyms(name: str) -> set[str]:
    """Return all valid acronyms for a course name (with and without small words)."""
    words = re.findall(r"[a-zA-Z0-9]+", name or "")
    if not words:
        return set()
    acrs = set()
    acr_all = "".join(w[0] for w in words).upper()
    if acr_all:
        acrs.add(acr_all)

    significant = [w for w in words if w.lower() not in SMALL_WORDS]
    if significant:
        acr_sig = "".join(w[0] for w in significant).upper()
        if acr_sig:
            acrs.add(acr_sig)
    return acrs


def course_acronym(name: str) -> str:
    """Primary display acronym for a course name."""
    words = re.findall(r"[a-zA-Z0-9]+", name or "")
    if not words:
        return ""
    significant = [w for w in words if w.lower() not in SMALL_WORDS]
    if len(significant) >= 2:
        return "".join(w[0] for w in significant).upper()
    return "".join(w[0] for w in words).upper()


def format_course_list(courses: list[dict]) -> str:
    """Format course list with acronyms, e.g. NLP - DE304 - Natural Language Processing."""
    lines = []
    for c in courses:
        acr = course_acronym(c.get("name", ""))
        prefix = f"{acr} - " if acr else ""
        lines.append(f"{prefix}{c.get('code', '')} - {c.get('name', '')}")
    return "\n".join(lines)


def resolve_course(lms, query: str) -> list[dict]:
    """
    Resolve course query against LMS courses in priority order:
    1. Exact match on course code (case-insensitive, ignore spaces/dashes)
    2. Code contains query (case-insensitive, ignore spaces/dashes)
    3. Acronym of the course name (with and without small words)
    4. Every word of query in name, then name contains query
    5. Fuzzy match on name (cutoff 0.6)
    """
    if hasattr(lms, "get_courses"):
        courses = lms.get_courses()
    elif isinstance(lms, list):
        courses = lms
    else:
        courses = []

    q = (query or "").strip()
    if not q or not courses:
        return []

    q_clean = _clean_code(q)

    # 1. Exact match on code
    m1 = [c for c in courses if _clean_code(c.get("code", "")) == q_clean]
    if m1:
        return _dedup(m1)

    # 2. Code contains query
    if q_clean:
        m2 = [c for c in courses if q_clean in _clean_code(c.get("code", ""))]
        if m2:
            return _dedup(m2)

    # 3. Acronym of course name
    q_upper = re.sub(r"[^a-zA-Z0-9]+", "", q).upper()
    if q_upper:
        m3 = [c for c in courses if q_upper in course_acronyms(c.get("name", ""))]
        if m3:
            return _dedup(m3)

    # 4. Words in name, then name contains query
    q_words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", q)]
    if q_words:
        m4_words = [c for c in courses if all(w in (c.get("name") or "").lower() for w in q_words)]
        if m4_words:
            return _dedup(m4_words)

    q_lower = q.lower()
    m4_sub = [c for c in courses if q_lower in (c.get("name") or "").lower()]
    if m4_sub:
        return _dedup(m4_sub)

    # 5. Fuzzy match with difflib.get_close_matches (cutoff 0.6)
    name_map = {}
    for c in courses:
        nm = (c.get("name") or "").lower()
        name_map.setdefault(nm, []).append(c)

    close_names = difflib.get_close_matches(q_lower, list(name_map.keys()), n=10, cutoff=0.6)
    if close_names:
        m5 = []
        for nm in close_names:
            m5.extend(name_map[nm])
        return _dedup(m5)

    return []


def _dedup(courses: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for c in courses:
        cid = c.get("id", c.get("code"))
        if cid not in seen:
            seen.add(cid)
            out.append(c)
    return out

