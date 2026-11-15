"""
rules.py - Offline rule-based intent matcher for TaleemGo AI with Sindhi, Urdu, and English support.
"""
import re
from typing import Optional

try:
    from .lang_data import KEYWORDS, SINDHI_ORDINALS, UNSUPPORTED_KEYWORDS, convert_to_ascii_digits, fold_arabic_script
except ImportError:
    from llm.lang_data import KEYWORDS, SINDHI_ORDINALS, UNSUPPORTED_KEYWORDS, convert_to_ascii_digits, fold_arabic_script

try:
    from app.courses import clean_course_query
except ImportError:
    try:
        from courses import clean_course_query
    except ImportError:
        def clean_course_query(t):
            return t

WEEK_DIGIT_RE = re.compile(r"\b(?:week|wk|w|lecture|lec|l|hafta|hafto|hafte|هفتو|هفتي|ہفتہ|ہفتے|لیکچر|ليڪچر)\s*(\d+)\b", re.IGNORECASE)

STOP_WORDS = {
    # English
    "the", "a", "an", "of", "in", "to", "for", "is", "my", "me", "show", "give", "please", "what", "any", "how", "much",
    "was", "were", "are", "am", "marked", "mark", "today", "yesterday", "tomorrow", "this", "that", "on", "at", "by", "check", "tell", "did", "do", "does",
    # Urdu/Hindi Latin
    "ka", "ki", "ke", "ko", "se", "par", "hai", "hain", "kya", "kitna", "kitni", "mera", "meri", "mere", "batao", "dikhao", "bhejo", "bhej", "do", "karo", "chahiye",
    "aaj", "kal", "tha", "thi", "the", "lagi", "laga",
    # Urdu script
    "کا", "کی", "کے", "کو", "سے", "پر", "ہے", "ہیں", "کیا", "کتنا", "کتنی", "میرا", "میری", "میرے", "بتاؤ", "دکھاؤ", "بھیجو", "بھیج", "دو", "کرو", "چاہیے",
    "آج", "لگی", "لگا",
    # Sindhi Latin
    "jo", "ji", "ja", "je", "te", "khaan", "aahe", "aahin", "chha", "ketri", "ketro", "munhinjo", "munhinji", "munhinja", "mukhay", "dekharo", "moklyo", "mokal", "diyo", "karyo",
    "aj", "sabhani", "hui", "hua",
    # Sindhi script
    "جو", "جي", "جا", "جيڪو", "تي", "کان", "آهي", "آهن", "ڇا", "ڪيتري", "ڪيترو", "منهنجو", "منهنجي", "منهنجا", "مون", "کي", "ڏيکاريو", "موڪليو", "موڪل", "ڏيو", "ڪريو",
    "اڄ", "سڀاڻي", "هئي", "هو",
}

SINDHI_SPECIFIC_CHARS = set("ٻڀٿٺٽڄڃڇڌڏڊڍڦڪڳڱڻ")
URDU_SPECIFIC_CHARS = set("ٹڈڑںےہ")
ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")


def _edit_distance_1(w1: str, w2: str) -> bool:
    """Return True if Levenshtein distance between w1 and w2 is at most 1."""
    if abs(len(w1) - len(w2)) > 1:
        return False
    if w1 == w2:
        return True
    # One deletion, insertion, or substitution
    if len(w1) > len(w2):
        w1, w2 = w2, w1
    # len(w1) <= len(w2)
    if len(w1) == len(w2):
        diffs = sum(1 for a, b in zip(w1, w2) if a != b)
        return diffs == 1
    # len(w2) == len(w1) + 1
    for i in range(len(w2)):
        if w2[:i] + w2[i + 1:] == w1:
            return True
    return False


ENGLISH_COMMON_MARKERS = {
    "download", "latest", "newest", "recent", "last", "new",
    "lecture", "lectures", "from", "course", "courses", "subject", "subjects",
    "slides", "slide", "notes", "file", "files",
    "show", "what", "is", "my", "give", "please", "me", "send", "attendance", "assignments", "upcoming", "due",
    "grades", "marks", "which", "week", "get", "check", "tell", "any", "the", "in", "to", "for", "of", "and", "or",
    "all", "how", "many", "when", "help", "who", "where", "can", "you", "i", "am", "are", "marked", "mark",
    "today", "yesterday", "tomorrow", "this", "that", "was", "were", "first", "second", "third", "do", "does", "did",
}

SINDHI_ROMAN_MARKERS = {
    "munhinji", "munhinjo", "munhinja", "ketri", "ketro", "aahe", "aahin",
    "moklyo", "mokal", "hafto", "pehrio", "pehrin", "sabhani", "kors", "madad",
    "diyo", "dekharo", "chha", "khaan", "karyo", "aj", "hui", "hua",
    "salam", "salaam", "assalam", "asalam",
}

URDU_ROMAN_MARKERS = {
    "mera", "meri", "mere", "kitna", "kitni", "hai", "hain", "karo", "karna", "bhejo", "bhej",
    "hafta", "hafte", "pehla", "pehle", "kal", "aaj", "madad", "chahiye",
    "ka", "ki", "ke", "ky", "ko", "se", "par", "kya", "batao", "dikhao", "nambar", "akhri", "aakhri",
    "naya", "nayi", "naye", "mein", "wala", "wali", "wale", "kuch",
    "salam", "salaam", "assalam", "asalam",
}

URDU_GRAMMAR_MARKERS = {
    "ka", "ki", "ke", "ky", "ko", "se", "me", "mein", "mera", "meri", "mere",
    "hai", "hain", "bhejo", "bhej", "karo", "karna", "kya", "kitna", "kitni", "dikhao", "batao",
}


def detect_language(text: str, stored_lang: str = "en", is_recent: bool = True) -> str:
    """
    Heuristic language tag detector.
    - Follows CURRENT message when it is clearly in a language.
    - Only inherits previous message's language (within 10 minutes) for very short (<=3 words)
      or signal-less messages (e.g. "2", "ok").
    """
    if not text:
        return stored_lang if (is_recent and stored_lang) else "en"

    if ARABIC_SCRIPT_RE.search(text):
        if any(c in SINDHI_SPECIFIC_CHARS for c in text):
            return "sd"
        if any(c in URDU_SPECIFIC_CHARS for c in text):
            return "ur"
        folded = fold_arabic_script(text)
        sindhi_hits = sum(1 for kw in KEYWORDS["upcoming"] + KEYWORDS["files"] if kw in folded and any(c in kw for c in SINDHI_SPECIFIC_CHARS))
        if sindhi_hits > 0:
            return "sd"
        return "ur"

    # Latin text
    tl = text.lower().strip()
    words = re.findall(r"[a-z0-9]+", tl)
    if not words:
        return stored_lang if (is_recent and stored_lang) else "en"

    sd_score = sum(1 for w in words if w in SINDHI_ROMAN_MARKERS)
    ur_score = sum(1 for w in words if w in URDU_ROMAN_MARKERS)
    en_score = sum(1 for w in words if w in ENGLISH_COMMON_MARKERS)
    has_ur_grammar = any(w in URDU_GRAMMAR_MARKERS for w in words)

    # 1. Very short or signal-less message (<= 3 words, or bare number) with no language signal
    is_bare_number = all(w.isdigit() for w in words)
    has_signal = (sd_score > 0 or ur_score > 0 or en_score > 0 or has_ur_grammar)
    if is_bare_number or (len(words) <= 3 and not has_signal):
        return stored_lang if (is_recent and stored_lang) else "en"

    # 2. Sindhi Roman wins if Sindhi markers dominate
    if sd_score > ur_score and sd_score > 0 and sd_score >= en_score:
        return "sd-roman"

    # 3. Urdu Roman wins if Urdu markers or Urdu grammar present
    if (ur_score > 0 and ur_score >= en_score) or has_ur_grammar:
        if sd_score == ur_score and stored_lang in ("sd", "sd-roman"):
            return "sd-roman"
        return "ur-roman"

    # 4. English wins if English markers present and no Urdu grammar
    if en_score > 0:
        return "en"

    # 5. Short message fallback
    if len(words) <= 3 and is_recent and stored_lang:
        return stored_lang

    return "en"


def match_rules(text: str, has_pending_download: bool = False, stored_lang: str = "en", is_recent: bool = True) -> Optional[dict]:
    """
    Match user message against rule patterns.
    Returns structured intent dict or None.
    """
    if not text:
        return None

    clean_text = convert_to_ascii_digits(text).strip()
    lang = detect_language(clean_text, stored_lang=stored_lang, is_recent=is_recent)

    # 1. Check for pending download selection ("2", "1,3", "all", "cancel")
    if has_pending_download:
        tl = clean_text.lower()
        if tl == "cancel":
            return {"lang": lang, "actions": [{"intent": "choose", "cancel": True}], "clarify": None, "off_topic": False, "unsupported": False}
        if tl == "all":
            return {"lang": lang, "actions": [{"intent": "choose", "all": True}], "clarify": None, "off_topic": False, "unsupported": False}
        tokens = [x for x in re.split(r"[\s,]+", clean_text) if x]
        if tokens and all(x.isdigit() for x in tokens):
            nums = [int(x) for x in tokens if 1 <= int(x) <= 20]
            if nums:
                return {"lang": lang, "actions": [{"intent": "choose", "numbers": nums}], "clarify": None, "off_topic": False, "unsupported": False}

    folded = fold_arabic_script(clean_text)
    folded_lower = folded.lower()
    norm_lower = clean_text.lower()

    # 1b. Check for unsupported university/LMS requests (grades, marks, timetable, fees, etc.)
    def has_keyword(kw_list):
        for kw in kw_list:
            folded_kw = fold_arabic_script(kw).lower()
            if folded_kw in folded_lower or kw.lower() in norm_lower:
                return True
            # Edit distance 1 for Roman Sindhi / Roman Urdu keywords of 5+ letters
            if len(kw) >= 5 and re.match(r"^[a-z]+$", kw.lower()):
                for word in re.findall(r"[a-z]+", norm_lower):
                    if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                        return True
        return False

    if has_keyword(UNSUPPORTED_KEYWORDS):
        return {
            "lang": lang,
            "actions": [],
            "clarify": None,
            "off_topic": False,
            "unsupported": True,
        }

    # 2. Extract week/lecture number
    week_num = None
    m_week = WEEK_DIGIT_RE.search(norm_lower) or WEEK_DIGIT_RE.search(folded_lower)
    if m_week:
        try:
            week_num = int(m_week.group(1))
        except ValueError:
            pass

    # Check ordinals
    if week_num is None:
        words = re.findall(r"[^\s,?!.\(\)]+", norm_lower) + re.findall(r"[^\s,?!.\(\)]+", folded_lower)
        for w in words:
            if w in SINDHI_ORDINALS:
                week_num = SINDHI_ORDINALS[w]
                break

    # 3. Match Intent Keywords
    matched_intent = None

    has_explicit_week = (m_week is not None) or any(
        w in norm_lower or w in folded_lower
        for w in ("week", "wk", "hafta", "hafto", "hafte", "هفتو", "هفتي", "ہفتہ", "ہفتے", "lecture", "lec", "ليڪچر", "لیکچر")
    )

    number_words = ["number", "نمبر", "nambar"]

    # Attendance keywords check
    has_attendance = has_keyword(KEYWORDS["attendance"])

    # Latest keywords check:
    # - Latin whole words and edit distance 1 for typos (len >= 5, e.g. "lastest")
    # - Arabic folded substring
    def has_latest_keyword():
        latest_kws = KEYWORDS.get("latest", [])
        latin_kws = [k for k in latest_kws if re.match(r"^[a-zA-Z]+$", k)]
        arabic_kws = [k for k in latest_kws if not re.match(r"^[a-zA-Z]+$", k)]

        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True
            if len(kw) >= 5:
                for word in re.findall(r"\b[a-z]+\b", norm_lower):
                    if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                        return True

        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True

        return False

    is_latest = has_latest_keyword()

    def has_plan_keyword():
        plan_kws = KEYWORDS.get("plan", [])
        latin_kws = [k for k in plan_kws if re.match(r"^[a-zA-Z\s]+$", k)]
        arabic_kws = [k for k in plan_kws if not re.match(r"^[a-zA-Z\s]+$", k)]

        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True

        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True

        return False

    def has_digest_keyword():
        digest_kws = KEYWORDS.get("digest", [])
        latin_kws = [k for k in digest_kws if re.match(r"^[a-zA-Z\s]+$", k)]
        arabic_kws = [k for k in digest_kws if not re.match(r"^[a-zA-Z\s]+$", k)]

        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True

        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True

        return False

    def has_calendar_keyword():
        cal_kws = KEYWORDS.get("calendar", [])
        latin_kws = [k for k in cal_kws if re.match(r"^[a-zA-Z\s]+$", k)]
        arabic_kws = [k for k in cal_kws if not re.match(r"^[a-zA-Z\s]+$", k)]

        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True

        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True

        return False

    def has_summarize_keyword():
        sum_kws = KEYWORDS.get("summarize", [])
        latin_kws = [k for k in sum_kws if re.match(r"^[a-zA-Z\s]+$", k)]
        arabic_kws = [k for k in sum_kws if not re.match(r"^[a-zA-Z\s]+$", k)]
        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True
        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True
        return False

    def has_quiz_keyword():
        quiz_kws = KEYWORDS.get("quiz", [])
        latin_kws = [k for k in quiz_kws if re.match(r"^[a-zA-Z\s]+$", k)]
        arabic_kws = [k for k in quiz_kws if not re.match(r"^[a-zA-Z\s]+$", k)]
        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True
        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True
        return False

    # Grades keywords check:
    # - Latin-script keywords match as WHOLE WORDS only (regex \b)
    # - Arabic-script keywords keep working as they do now (folded substring)
    def has_grades_keyword():
        grades_kws = KEYWORDS.get("grades", [])
        latin_kws = [k for k in grades_kws if re.match(r"^[a-zA-Z]+$", k)]
        arabic_kws = [k for k in grades_kws if not re.match(r"^[a-zA-Z]+$", k)]

        for kw in latin_kws:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", norm_lower):
                return True
            if len(kw) >= 5:
                for word in re.findall(r"\b[a-z]+\b", norm_lower):
                    if len(word) >= 5 and _edit_distance_1(word, kw.lower()):
                        return True

        for ak in arabic_kws:
            folded_ak = fold_arabic_script(ak).lower()
            if folded_ak in folded_lower or ak.lower() in norm_lower:
                return True

        return False

    # Number / nambar words with mere/course (whole words for Latin, Arabic substring)
    has_number_kw = (
        bool(re.search(r"\b(?:number|nambar)\b", norm_lower))
        or ("نمبر" in folded_lower or "نمبر" in norm_lower)
    )
    has_mere_or_course = (
        bool(re.search(r"\b(?:mere|mera|meri|munhinja|course|subject|kors|mazmoon)\b", norm_lower))
        or any(w in folded_lower for w in ("منهنجا", "میرے", "میرا", "کورس", "ڪورس", "مضمون"))
    )

    plan_miss_num = None
    plan_tgt_num = None
    if has_plan_keyword():
        m_miss = (
            re.search(r"\b(?:miss|bunk|chhutti)\s*(\d+)\b", norm_lower)
            or re.search(r"\b(\d+)\s*(?:miss|bunk|classes\s*miss|chhutti)\b", norm_lower)
            or re.search(r"(\d+)\s*(?:چھٹی|ڪلاسون)", folded_lower)
        )
        if m_miss:
            try:
                plan_miss_num = int(m_miss.group(1))
            except Exception:
                pass
        m_tgt = (
            re.search(r"\b(?:target|hadd)\s*(\d+)\b", norm_lower)
            or re.search(r"\b(\d+)\s*%", norm_lower)
        )
        if m_tgt:
            try:
                plan_tgt_num = int(m_tgt.group(1))
            except Exception:
                pass

    digest_enable = None
    digest_time_str = None
    if has_digest_keyword():
        m_on = bool(re.search(r"\b(?:on|enable|start)\b", norm_lower) or "آن" in folded_lower or "آن" in norm_lower)
        m_off = bool(re.search(r"\b(?:off|disable|stop)\b", norm_lower) or any(w in folded_lower for w in ("آف", "بند")))
        if m_on and not m_off:
            digest_enable = True
        elif m_off:
            digest_enable = False
        m_tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", norm_lower)
        if m_tm:
            digest_time_str = m_tm.group(0)
            if len(digest_time_str.split(":")[0]) == 1:
                digest_time_str = "0" + digest_time_str

    quiz_n = None
    if has_quiz_keyword():
        m_qn = (
            re.search(r"\b([3-9]|10)\s*(?:questions?|sawal|sawalat|سوال|سوالات)\b", norm_lower)
            or re.search(r"\b(?:quiz|test|ڪوئز|کوئز).*?\b([3-9]|10)\b", norm_lower)
        )
        if m_qn:
            try:
                candidate_n = int(m_qn.group(1))
                if week_num is None or candidate_n != week_num:
                    if 3 <= candidate_n <= 10:
                        quiz_n = candidate_n
            except Exception:
                pass

    if has_summarize_keyword():
        matched_intent = "summarize"
    elif has_quiz_keyword():
        matched_intent = "quiz"
    elif has_plan_keyword():
        matched_intent = "plan"
    elif has_digest_keyword():
        matched_intent = "digest"
    elif has_calendar_keyword():
        matched_intent = "calendar"
    elif has_attendance:
        matched_intent = "attendance"
    elif has_grades_keyword() or (has_number_kw and has_mere_or_course):
        matched_intent = "grades"
    elif has_keyword(KEYWORDS["clos"]):
        matched_intent = "clos"
    elif (week_num is not None and has_explicit_week) or has_keyword(KEYWORDS["files"]) or is_latest:
        # Week number with week word or file/download word or latest -> download intent
        matched_intent = "download"
    elif has_keyword(KEYWORDS["upcoming"]):
        matched_intent = "upcoming"
    elif has_keyword(KEYWORDS["assignments"]):
        matched_intent = "assignments"
    elif has_keyword(KEYWORDS["courses"]):
        matched_intent = "courses"
    elif has_keyword(KEYWORDS["help"]):
        matched_intent = "help"
    elif has_keyword(KEYWORDS["greeting"]):
        matched_intent = "greeting"

    if not matched_intent:
        return None

    # 4. Extract course_query candidates
    course_query = None
    if matched_intent in ("attendance", "download", "resources", "clos", "grades", "courses", "plan", "summarize", "quiz"):
        # Remove matched keywords, week digits, ordinals, and stop words
        tokens = re.findall(r"[A-Za-z0-9\u0600-\u06FF]+", clean_text)
        candidates = []
        week_words = {
            "week", "wk", "w", "lecture", "lec", "l", "hafta", "hafto", "hafte",
            "هفتو", "هفتي", "ہفتہ", "ہفتے", "لیکچر", "ليڪچر"
        }
        kw_words = {w for kws in KEYWORDS.values() for kw in kws for w in kw.lower().split()}
        kw_words.update(number_words)
        folded_kw_words = {fold_arabic_script(w) for w in kw_words}

        for t in tokens:
            tl = t.lower()
            folded_t = fold_arabic_script(t).lower()
            if tl.isdigit() and (
                (week_num is not None and int(tl) == week_num)
                or (plan_miss_num is not None and int(tl) == plan_miss_num)
                or (plan_tgt_num is not None and int(tl) == plan_tgt_num)
                or (quiz_n is not None and int(tl) == quiz_n)
            ):
                continue
            if tl in STOP_WORDS or folded_t in STOP_WORDS or tl in week_words or folded_t in week_words:
                continue
            if t in SINDHI_ORDINALS or tl in SINDHI_ORDINALS or folded_t in SINDHI_ORDINALS:
                continue
            if tl in kw_words or folded_t in folded_kw_words:
                continue
            if len(t) >= 2:
                candidates.append(t)

        if candidates:
            # Prefer upper case acronyms / codes like NLP, HCI, DE304
            upper_cands = [c for c in candidates if c.isupper() or any(ch.isdigit() for ch in c)]
            if upper_cands:
                raw_cq = " ".join(upper_cands[:2])
            else:
                raw_cq = " ".join(candidates[:3])
            course_query = clean_course_query(raw_cq) or raw_cq

    action = {"intent": matched_intent}
    if course_query:
        action["course_query"] = course_query
    if is_latest:
        action["latest"] = True
    if matched_intent in ("download", "summarize", "quiz") and week_num is not None:
        action["week"] = week_num
    if matched_intent == "quiz" and quiz_n is not None:
        action["n"] = quiz_n
    if matched_intent == "plan":
        if plan_tgt_num is not None and 1 <= plan_tgt_num <= 99:
            action["target"] = plan_tgt_num
        if plan_miss_num is not None:
            action["miss"] = plan_miss_num
    elif matched_intent == "digest":
        if digest_enable is not None:
            action["enable"] = digest_enable
        if digest_time_str is not None:
            action["time"] = digest_time_str

    return {
        "lang": lang,
        "actions": [action],
        "clarify": None,
        "off_topic": False,
        "unsupported": False,
    }
