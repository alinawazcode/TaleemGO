"""
courses.py - Course resolution and formatting for TaleemGo.
"""
import difflib
import re

try:
    from app.llm.lang_data import COURSE_FILLER_WORDS_LATIN, COURSE_FILLER_WORDS_ARABIC, fold_arabic_script
except ImportError:
    try:
        from llm.lang_data import COURSE_FILLER_WORDS_LATIN, COURSE_FILLER_WORDS_ARABIC, fold_arabic_script
    except ImportError:
        COURSE_FILLER_WORDS_LATIN = [
            "course", "courses", "subject", "subjects", "class", "classes",
            "lecture", "lectures", "lec", "slides", "slide", "file", "files", "notes", "note",
            "from", "of", "for", "in", "to", "at", "my", "mere", "mera", "meri",
            "ka", "ki", "ke", "ky", "ko", "me", "mein", "se", "te", "jo", "ji", "ja", "je", "khaan",
            "wala", "wali", "wale", "bhejo", "bhej", "do", "dena", "de", "send", "show", "download", "get",
            "latest", "newest", "recent", "last", "new", "naya", "nayi", "naye", "akhri", "aakhri", "lastest",
            "moklyo", "mokal", "diyo", "dikhao", "batao", "dekharo", "chahiye",
            "aahe", "aahin", "hai", "hain", "chha", "kya", "ketri", "ketro", "kitna", "kitni",
            "munhinjo", "munhinji", "munhinja",
        ]
        COURSE_FILLER_WORDS_ARABIC = [
            "کورس", "کورسز", "ڪورس", "ڪورسز", "مضمون", "ڪلاس", "کلاس",
            "لیکچر", "ليڪچر", "سلائیڈز", "سلائيڊس", "فائل", "فائلیں", "فائيل", "نوٹس", "نوٽس",
            "سے", "کان", "کا", "کی", "کے", "کو", "میں", "۾", "تي", "جو", "جي", "جا",
            "میرا", "میری", "میرے", "منهنجو", "منهنجي", "منهنجا",
            "بھیجو", "بھیج", "دو", "موڪليو", "موڪل", "ڏيو",
            "ڈاؤنلوڈ", "ڊائونلوڊ", "آخری", "آخري", "نیا", "نئی", "نئون",
            "دکھاؤ", "بتاؤ", "ڏيکاريو", "ہے", "ہیں", "آهي", "آهن",
        ]

        def fold_arabic_script(s):
            return s

SMALL_WORDS = {"of", "and", "the", "to", "in", "for", "&"}


def _edit_distance_1(w1: str, w2: str) -> bool:
    """Return True if Levenshtein distance between w1 and w2 is at most 1."""
    if abs(len(w1) - len(w2)) > 1:
        return False
    if w1 == w2:
        return True
    if len(w1) > len(w2):
        w1, w2 = w2, w1
    if len(w1) == len(w2):
        return sum(1 for a, b in zip(w1, w2) if a != b) == 1
    for i in range(len(w2)):
        if w2[:i] + w2[i + 1:] == w1:
            return True
    return False


def clean_course_query(text: str) -> str:
    """
    Remove generic/filler words (Latin, Urdu, Sindhi) from course query.
    Preserves course acronyms (AI, NLP, HCG, SE, CA, PP) and codes.
    "Ai course" -> "Ai", "latest from Ai" -> "Ai", "Ai ky lastest" -> "Ai".
    """
    if not text:
        return ""

    raw_tokens = text.strip().split()
    if not raw_tokens:
        return ""

    latin_fillers = {w.lower() for w in COURSE_FILLER_WORDS_LATIN}
    long_latin_fillers = {w for w in latin_fillers if len(w) >= 5}

    arabic_fillers = {fold_arabic_script(w).lower() for w in COURSE_FILLER_WORDS_ARABIC}
    arabic_fillers.update({w.lower() for w in COURSE_FILLER_WORDS_ARABIC})

    kept_tokens = []
    for token in raw_tokens:
        clean_tok = re.sub(r"^[^\w]+|[^\w]+$", "", token)
        if not clean_tok:
            continue

        tok_lower = clean_tok.lower()
        folded_tok = fold_arabic_script(clean_tok).lower()

        # Never strip known uppercase course acronyms or course codes (e.g. AI, NLP, DE304, CC-311)
        if (clean_tok.isupper() and len(clean_tok) >= 2) or any(ch.isdigit() for ch in clean_tok):
            kept_tokens.append(clean_tok)
            continue

        if tok_lower in latin_fillers:
            continue

        if tok_lower in arabic_fillers or folded_tok in arabic_fillers:
            continue

        is_typo_filler = False
        if len(tok_lower) >= 5 and re.match(r"^[a-z]+$", tok_lower):
            for fw in long_latin_fillers:
                if _edit_distance_1(tok_lower, fw):
                    is_typo_filler = True
                    break
        if is_typo_filler:
            continue

        kept_tokens.append(clean_tok)

    return " ".join(kept_tokens).strip()


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


def _resolve_course_inner(courses: list[dict], q: str) -> list[dict]:
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


def resolve_course(lms, query: str) -> list[dict]:
    """
    Resolve course query against LMS courses in priority order:
    1. Exact match on course code (case-insensitive, ignore spaces/dashes)
    2. Code contains query (case-insensitive, ignore spaces/dashes)
    3. Acronym of the course name (with and without small words)
    4. Every word of query in name, then name contains query
    5. Fuzzy match on name (cutoff 0.6)
    Also strips trailing course/subject/class and applies clean_course_query.
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

    # Strip trailing course/subject/class as safety net
    q_stripped = re.sub(r"\s+(?:courses?|subjects?|classes?|کورس|ڪورس|مضمون|کلاس|ڪلاس)\s*$", "", q, flags=re.IGNORECASE).strip()
    if q_stripped:
        q = q_stripped

    res = _resolve_course_inner(courses, q)
    if not res:
        cleaned = clean_course_query(q)
        if cleaned and cleaned != q:
            res = _resolve_course_inner(courses, cleaned)
    return res


def _dedup(courses: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for c in courses:
        cid = c.get("id", c.get("code"))
        if cid not in seen:
            seen.add(cid)
            out.append(c)
    return out

