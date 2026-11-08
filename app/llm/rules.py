"""
rules.py - Offline rule-based intent matcher for TaleemGo AI with Sindhi, Urdu, and English support.
"""
import re
from typing import Optional

try:
    from .lang_data import KEYWORDS, SINDHI_ORDINALS, UNSUPPORTED_KEYWORDS, convert_to_ascii_digits, fold_arabic_script
except ImportError:
    from llm.lang_data import KEYWORDS, SINDHI_ORDINALS, UNSUPPORTED_KEYWORDS, convert_to_ascii_digits, fold_arabic_script

WEEK_DIGIT_RE = re.compile(r"\b(?:week|wk|w|lecture|lec|l|hafta|hafto|hafte|هفتو|هفتي|ہفتہ|ہفتے|لیکچر|ليڪچر)\s*(\d+)\b", re.IGNORECASE)

STOP_WORDS = {
    # English
    "the", "a", "an", "of", "in", "to", "for", "is", "my", "me", "show", "give", "please", "what", "any", "how", "much",
    # Urdu/Hindi Latin
    "ka", "ki", "ke", "ko", "se", "par", "hai", "hain", "kya", "kitna", "kitni", "mera", "meri", "mere", "batao", "dikhao", "bhejo", "bhej", "do", "karo", "chahiye",
    # Urdu script
    "کا", "کی", "کے", "کو", "سے", "پر", "ہے", "ہیں", "کیا", "کتنا", "کتنی", "میرا", "میری", "میرے", "بتاؤ", "دکھاؤ", "بھیجو", "بھیج", "دو", "کرو", "چاہیے",
    # Sindhi Latin
    "jo", "ji", "ja", "je", "te", "khaan", "aahe", "aahin", "chha", "ketri", "ketro", "munhinjo", "munhinji", "munhinja", "mukhay", "dekharo", "moklyo", "mokal", "diyo", "karyo",
    # Sindhi script
    "جو", "جي", "جا", "جيڪو", "تي", "کان", "آهي", "آهن", "ڇا", "ڪيتري", "ڪيترو", "منهنجو", "منهنجي", "منهنجا", "مون", "کي", "ڏيکاريو", "موڪليو", "موڪل", "ڏيو", "ڪريو",
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


def detect_language(text: str, stored_lang: str = "en") -> str:
    """Heuristic language tag detector."""
    if not text:
        return stored_lang

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
    tl = text.lower()
    words = re.findall(r"[a-z0-9]+", tl)

    sindhi_roman_markers = {"munhinji", "munhinjo", "ketri", "aahe", "aahin", "moklyo", "mokal", "hafto", "pehrio", "sabhani", "kors", "madad"}
    urdu_roman_markers = {"mera", "meri", "mere", "kitna", "kitni", "hai", "hain", "karo", "karna", "bhejo", "hafta", "hafte", "pehla", "pehle", "kal", "aaj", "madad", "chahiye"}

    sd_score = sum(1 for w in words if w in sindhi_roman_markers)
    ur_score = sum(1 for w in words if w in urdu_roman_markers)

    if sd_score > ur_score and sd_score > 0:
        return "sd-roman"
    if ur_score > 0:
        return "ur-roman"

    return stored_lang if stored_lang != "en" else "en"


def match_rules(text: str, has_pending_download: bool = False, stored_lang: str = "en") -> Optional[dict]:
    """
    Match user message against rule patterns.
    Returns structured intent dict or None.
    """
    if not text:
        return None

    clean_text = convert_to_ascii_digits(text).strip()
    lang = detect_language(clean_text, stored_lang=stored_lang)

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

    if has_keyword(KEYWORDS["attendance"]):
        matched_intent = "attendance"
    elif has_keyword(KEYWORDS["clos"]):
        matched_intent = "clos"
    elif (week_num is not None and has_explicit_week) or has_keyword(KEYWORDS["files"]):
        # Week number with week word or file/download word -> download intent
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
    if matched_intent in ("attendance", "download", "resources", "clos"):
        # Remove matched keywords, week digits, ordinals, and stop words
        tokens = re.findall(r"[A-Za-z0-9\u0600-\u06FF]+", clean_text)
        candidates = []
        week_words = {
            "week", "wk", "w", "lecture", "lec", "l", "hafta", "hafto", "hafte",
            "هفتو", "هفتي", "ہفتہ", "ہفتے", "لیکچر", "ليڪچر"
        }
        kw_words = {w for kws in KEYWORDS.values() for kw in kws for w in kw.lower().split()}
        folded_kw_words = {fold_arabic_script(w) for w in kw_words}

        for t in tokens:
            tl = t.lower()
            folded_t = fold_arabic_script(t).lower()
            if tl.isdigit() and week_num is not None and int(tl) == week_num:
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
                course_query = " ".join(upper_cands[:2])
            else:
                course_query = " ".join(candidates[:3])

    action = {"intent": matched_intent}
    if course_query:
        action["course_query"] = course_query
    if matched_intent == "download" and week_num is not None:
        action["week"] = week_num

    return {
        "lang": lang,
        "actions": [action],
        "clarify": None,
        "off_topic": False,
        "unsupported": False,
    }
