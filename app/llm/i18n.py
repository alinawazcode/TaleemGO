"""
i18n.py - Internationalization and dynamic translation caching for TaleemGo AI.
"""
import logging
import re
import sqlite3
import threading
from typing import Optional

try:
    from .lang_data import STRINGS
    from .providers import call_chat_completions, registry
    from .router import get_configured_providers, is_llm_enabled
except ImportError:
    from llm.lang_data import STRINGS
    from llm.providers import call_chat_completions, registry
    from llm.router import get_configured_providers, is_llm_enabled

log = logging.getLogger("taleemgo.llm.i18n")

_cache_lock = threading.Lock()
_db_path = None

COMMAND_WORDS = {
    "courses", "assignments", "upcoming", "resources",
    "attendance", "clos", "download", "logout", "login",
}


def set_db_path(path: str):
    global _db_path
    _db_path = path
    init_i18n_db()


def _conn():
    try:
        import users as usr
    except ImportError:
        from app import users as usr
    path = _db_path or usr.DB_PATH
    return sqlite3.connect(path, check_same_thread=False)


def init_i18n_db():
    with _cache_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS i18n_cache (
                lang TEXT,
                key  TEXT,
                text TEXT,
                PRIMARY KEY (lang, key)
            )
        """)
        c.commit()


def _get_cached_translation(lang: str, key: str) -> Optional[str]:
    try:
        with _cache_lock, _conn() as c:
            row = c.execute("SELECT text FROM i18n_cache WHERE lang=? AND key=?", (lang, key)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _set_cached_translation(lang: str, key: str, text: str):
    try:
        with _cache_lock, _conn() as c:
            c.execute("INSERT OR REPLACE INTO i18n_cache (lang, key, text) VALUES (?, ?, ?)", (lang, key, text))
            c.commit()
    except Exception as e:
        log.warning("Failed to cache translation for %s/%s: %s", lang, key, e)


def _validate_translation(english_source: str, translated_text: str) -> bool:
    """
    Validate translation:
    - Must be under 600 characters
    - Must contain the same {placeholders} as English source
    - Must contain the same command words as English source
    """
    if not translated_text or len(translated_text) > 600:
        return False

    # Check placeholders
    src_placeholders = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", english_source))
    trans_placeholders = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", translated_text))
    if src_placeholders != trans_placeholders:
        return False

    # Check command words present in source
    for word in COMMAND_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", english_source, re.IGNORECASE):
            if not re.search(r"\b" + re.escape(word) + r"\b", translated_text, re.IGNORECASE):
                return False

    return True


def _translate_with_llm(english_text: str, target_lang: str) -> Optional[str]:
    """Translate fixed English text to target_lang using available provider chain."""
    if not is_llm_enabled():
        return None

    providers = get_configured_providers()
    prompt = (
        f"Translate the following UI message into language code '{target_lang}'.\n"
        "RULES:\n"
        "1. Keep all emojis unchanged.\n"
        "2. Keep all {placeholders} like {hours}, {courses}, {count} exactly unchanged.\n"
        "3. Keep the brand name 'TaleemGo AI' unchanged.\n"
        "4. Keep command words courses, assignments, upcoming, resources, attendance, clos, download, logout, login unchanged.\n"
        "5. Output ONLY the translated string without any quotes or explanations.\n\n"
        f"Text to translate:\n{english_text}"
    )
    messages = [{"role": "user", "content": prompt}]

    for prov in providers:
        if not registry.is_available(prov):
            continue
        try:
            res = call_chat_completions(
                prov,
                messages=messages,
                json_mode=False,
                timeout=6.0,
                temperature=0.0,
                max_tokens=300,
            )
            res = res.strip().strip('"').strip("'")
            if _validate_translation(english_text, res):
                return res
        except Exception:
            continue
    return None


def t(key: str, lang: str = "en", **kwargs) -> str:
    """
    Return localized text for key and format with kwargs.
    - en, ur, sd use hand-written built-in strings.
    - Other languages check i18n_cache, or translate once via LLM and cache.
    - Falls back to English if key missing or translation fails validation.
    """
    clean_lang = (lang or "en").lower()

    # 1. Built-in languages
    if clean_lang in ("en", "ur", "sd"):
        template = STRINGS.get(clean_lang, {}).get(key) or STRINGS["en"].get(key, "")
        try:
            return template.format(**kwargs) if kwargs else template
        except Exception:
            return template

    # 2. Check SQLite cache
    cached = _get_cached_translation(clean_lang, key)
    if cached:
        try:
            return cached.format(**kwargs) if kwargs else cached
        except Exception:
            return cached

    # 3. Dynamic translation via LLM
    en_template = STRINGS["en"].get(key, "")
    translated = _translate_with_llm(en_template, clean_lang)
    if translated and _validate_translation(en_template, translated):
        _set_cached_translation(clean_lang, key, translated)
        template = translated
    else:
        template = en_template

    try:
        return template.format(**kwargs) if kwargs else template
    except Exception:
        return template
