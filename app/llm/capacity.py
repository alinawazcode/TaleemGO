"""
capacity.py - Token-aware rate limiting, sliding windows, concurrency queues,
intent caching, and load shedding for TaleemGo AI.
"""
import collections
import logging
import os
import re
import threading
import time
from typing import Optional

log = logging.getLogger("taleemgo.llm.capacity")

# Verified default ceilings (dated 2026-11-15 against provider docs):
# Groq: ~30 RPM, 8,000 TPM, 1,000 RPD, 200,000 TPD
# Groq Whisper: 20 RPM, 2,000 RPD
# OpenRouter: 20 RPM, 50 RPD
# Cloudflare: 10,000 RPD (neurons/day)
# Mistral: ~30 RPM
# Gemini: varies by AI Studio project, defaults 15 RPM, 1,000,000 TPM, 1,500 RPD
DEFAULT_LIMITS = {
    "groq": {
        "rpm": 30,
        "tpm": 8000,
        "rpd": 1000,
        "tpd": 200000,
    },
    "groq_whisper": {
        "rpm": 20,
        "rpd": 2000,
    },
    "openrouter": {
        "rpm": 20,
        "rpd": 50,
    },
    "gemini": {
        "rpm": 15,
        "tpm": 1000000,
        "rpd": 1500,
    },
    "mistral": {
        "rpm": 30,
    },
    "cloudflare": {
        "rpd": 10000,
    },
    "local": {},  # Unlimited local
}

# Words that flip or negate meaning
MEANING_FLIP_WORDS = {
    "not", "no", "dont", "don't", "nahi", "nahin", "nahe", "mat",
    "without", "bina", "baghair", "نہیں", "نہ", "بغیر", "مت", "نه", "کانسواء",
}


def is_exhibition_mode() -> bool:
    """Returns True if EXHIBITION_MODE is enabled (default 0)."""
    return os.environ.get("EXHIBITION_MODE", "0").strip() in ("1", "true", "yes", "on")


class TokenAwareLimiter:
    """
    In-memory, thread-safe, token-aware rate limiter.
    Tracks sliding windows for RPM, TPM, RPD, and TPD per (provider, model).
    Enforces concurrency caps and queues with priority-based timeouts.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Key: (provider, model) -> dict of deques
        self._windows = {}
        # Per-provider concurrency tracking
        self._cond = threading.Condition(self._lock)
        self._active_concurrency = collections.defaultdict(int)

    def _get_window(self, provider: str, model: str) -> dict:
        key = (provider.lower(), model.lower())
        if key not in self._windows:
            self._windows[key] = {
                "reqs_min": collections.deque(),
                "tokens_min": collections.deque(),
                "reqs_day": collections.deque(),
                "tokens_day": collections.deque(),
            }
        return self._windows[key]

    def _prune(self, w: dict, now: float):
        min_cutoff = now - 60.0
        day_cutoff = now - 86400.0

        rm = w["reqs_min"]
        while rm and rm[0][0] < min_cutoff:
            rm.popleft()

        tm = w["tokens_min"]
        while tm and tm[0][0] < min_cutoff:
            tm.popleft()

        rd = w["reqs_day"]
        while rd and rd[0][0] < day_cutoff:
            rd.popleft()

        td = w["tokens_day"]
        while td and td[0][0] < day_cutoff:
            td.popleft()

    def get_ceilings(self, provider: str) -> dict:
        """Read ceilings from env vars with fallback to verified defaults."""
        p = provider.lower()
        prefix = p.upper()

        defaults = DEFAULT_LIMITS.get(p, {})
        ceilings = {}

        for metric in ("rpm", "tpm", "rpd", "tpd"):
            env_key = f"{prefix}_{metric.upper()}"
            val = os.environ.get(env_key)
            if val is not None and val.strip():
                try:
                    c = int(val.strip())
                    ceilings[metric] = c if c > 0 else None
                except ValueError:
                    ceilings[metric] = defaults.get(metric)
            else:
                ceilings[metric] = defaults.get(metric)

        return ceilings

    def estimate_tokens(self, messages: list[dict], max_tokens: int = 150) -> int:
        """Estimate tokens for a request: character length // 4 + max_tokens."""
        total_chars = sum(len(m.get("content", "") or "") for m in messages)
        return (total_chars // 4) + max_tokens

    def can_call(self, provider: str, model: str, estimated_tokens: int) -> bool:
        """
        Check sliding windows before spending an HTTP request.
        Returns False if call would exceed any known ceiling.
        """
        ceilings = self.get_ceilings(provider)
        now = time.time()

        with self._lock:
            w = self._get_window(provider, model)
            self._prune(w, now)

            # Check RPM
            rpm = ceilings.get("rpm")
            if rpm is not None:
                cur_reqs = sum(cnt for _, cnt in w["reqs_min"])
                if cur_reqs + 1 > rpm:
                    log.info("[LIMITER] Skipping %s/%s: RPM ceiling %d reached (%d reqs)", provider, model, rpm, cur_reqs)
                    return False

            # Check TPM
            tpm = ceilings.get("tpm")
            if tpm is not None:
                cur_tokens = sum(cnt for _, cnt in w["tokens_min"])
                if cur_tokens + estimated_tokens > tpm:
                    log.info("[LIMITER] Skipping %s/%s: TPM ceiling %d reached (%d + %d tokens)", provider, model, tpm, cur_tokens, estimated_tokens)
                    return False

            # Check RPD
            rpd = ceilings.get("rpd")
            if rpd is not None:
                cur_rpd = sum(cnt for _, cnt in w["reqs_day"])
                if cur_rpd + 1 > rpd:
                    log.info("[LIMITER] Skipping %s/%s: RPD ceiling %d reached (%d reqs)", provider, model, rpd, cur_rpd)
                    return False

            # Check TPD
            tpd = ceilings.get("tpd")
            if tpd is not None:
                cur_tpd = sum(cnt for _, cnt in w["tokens_day"])
                if cur_tpd + estimated_tokens > tpd:
                    log.info("[LIMITER] Skipping %s/%s: TPD ceiling %d reached (%d + %d tokens)", provider, model, tpd, cur_tpd, estimated_tokens)
                    return False

            return True

    def record_usage(
        self,
        provider: str,
        model: str,
        total_tokens: int = 0,
        headers: Optional[dict] = None,
    ):
        """Record a completed call in sliding windows and update limits from headers."""
        now = time.time()
        with self._lock:
            w = self._get_window(provider, model)
            self._prune(w, now)

            w["reqs_min"].append((now, 1))
            w["reqs_day"].append((now, 1))
            if total_tokens > 0:
                w["tokens_min"].append((now, total_tokens))
                w["tokens_day"].append((now, total_tokens))

        # Check rate-limit headers if present (e.g. x-ratelimit-remaining-*)
        if headers:
            ra = headers.get("Retry-After") or headers.get("retry-after")
            if ra:
                try:
                    retry_sec = float(ra)
                    from app.llm.providers import registry
                    registry.record_429(provider, retry_sec)
                except (ValueError, TypeError):
                    pass

    def acquire_concurrency(self, provider: str, priority: str = "high") -> bool:
        """
        Acquire a concurrency slot for provider.
        Wait up to 8s for high priority, 2s for low priority.
        Returns True if acquired, False on timeout.
        """
        cap = int(os.environ.get("PROVIDER_CONCURRENCY", "3"))
        timeout = 8.0 if priority == "high" else 2.0
        deadline = time.time() + timeout
        p = provider.lower()

        with self._cond:
            while self._active_concurrency[p] >= cap:
                remaining = deadline - time.time()
                if remaining <= 0:
                    log.warning("[CONCURRENCY] Queue timeout for %s (priority=%s, cap=%d)", provider, priority, cap)
                    return False
                self._cond.wait(timeout=remaining)
            self._active_concurrency[p] += 1
            return True

    def release_concurrency(self, provider: str):
        """Release a concurrency slot and notify waiting callers."""
        p = provider.lower()
        with self._cond:
            if self._active_concurrency[p] > 0:
                self._active_concurrency[p] -= 1
            self._cond.notify_all()

    def get_window_usage(self, provider: str, model: str) -> dict:
        """Return percentage of window used for each ceiling (0..100%)."""
        ceilings = self.get_ceilings(provider)
        now = time.time()

        with self._lock:
            w = self._get_window(provider, model)
            self._prune(w, now)

            cur_rpm = sum(cnt for _, cnt in w["reqs_min"])
            cur_tpm = sum(cnt for _, cnt in w["tokens_min"])
            cur_rpd = sum(cnt for _, cnt in w["reqs_day"])
            cur_tpd = sum(cnt for _, cnt in w["tokens_day"])

        def pct(cur, max_v):
            if max_v is None or max_v <= 0:
                return None
            return min(100.0, round((cur / max_v) * 100, 1))

        return {
            "rpm_used": cur_rpm,
            "rpm_cap": ceilings.get("rpm"),
            "rpm_pct": pct(cur_rpm, ceilings.get("rpm")),
            "tpm_used": cur_tpm,
            "tpm_cap": ceilings.get("tpm"),
            "tpm_pct": pct(cur_tpm, ceilings.get("tpm")),
            "rpd_used": cur_rpd,
            "rpd_cap": ceilings.get("rpd"),
            "rpd_pct": pct(cur_rpd, ceilings.get("rpd")),
            "tpd_used": cur_tpd,
            "tpd_cap": ceilings.get("tpd"),
            "tpd_pct": pct(cur_tpd, ceilings.get("tpd")),
        }

    def is_shedding(self, providers: list[str], registry=None) -> bool:
        """
        Check if low-priority requests should be shed:
        True when there is at least one available provider, but EVERY available provider in the list
        has > 80% usage on RPM or TPM.
        If no provider is available or configured, returns False so normal error handling applies.
        """
        if not providers:
            return False

        if registry is None:
            from app.llm.providers import registry
        from app.llm.providers import get_provider_models

        available_providers_count = 0
        any_provider_healthy = False

        for p in providers:
            if not registry.is_available(p):
                continue
            available_providers_count += 1
            models = get_provider_models(p)
            for m in models:
                usage = self.get_window_usage(p, m)
                rpm_p = usage["rpm_pct"]
                tpm_p = usage["tpm_pct"]

                # If unconstrained or under 80% capacity, provider is healthy
                is_rpm_ok = (rpm_p is None) or (rpm_p < 80.0)
                is_tpm_ok = (tpm_p is None) or (tpm_p < 80.0)

                if is_rpm_ok and is_tpm_ok:
                    any_provider_healthy = True
                    break
            if any_provider_healthy:
                break

        if available_providers_count == 0:
            return False

        return not any_provider_healthy


# Global limiter singleton
limiter = TokenAwareLimiter()


class IntentCache:
    """
    Thread-safe LRU cache for intent JSONs.
    Max 500 entries. TTL 1 hour (2 hours in exhibition mode).
    Shared across users.
    Never caches sign-up text or messages with digits/emails in voice transcripts.
    """

    def __init__(self, capacity: int = 500, ttl_sec: Optional[float] = None):
        self.capacity = capacity
        if ttl_sec is None:
            self.ttl_sec = 7200.0 if is_exhibition_mode() else 3600.0
        else:
            self.ttl_sec = ttl_sec
        self._cache = collections.OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize message text: lowercase, strip punctuation, collapse whitespace."""
        s = text.lower()
        # Replace non-alphanumeric (keep unicode word chars for Arabic/Sindhi/Urdu)
        s = re.sub(r"[^\w\s]", " ", s)
        return " ".join(s.split())

    def get(self, text: str, has_pending_download: bool, stored_lang: str) -> Optional[dict]:
        norm = self.normalize_text(text)
        if not norm:
            return None
        key = f"{norm}|{1 if has_pending_download else 0}|{stored_lang}"
        now = time.time()

        with self._lock:
            if key in self._cache:
                ts, val = self._cache[key]
                if now - ts <= self.ttl_sec:
                    self._cache.move_to_end(key)
                    self._hits += 1
                    return dict(val)
                else:
                    self._cache.pop(key, None)
            self._misses += 1
            return None

    def put(
        self,
        text: str,
        has_pending_download: bool,
        stored_lang: str,
        intent_data: dict,
        is_voice: bool = False,
    ):
        # Security checks: Never cache if message looks like sign-up or contains emails
        if "@" in text:
            return
        # If voice transcript contains digits, skip caching to protect sensitive numbers
        if is_voice and any(ch.isdigit() for ch in text):
            return

        norm = self.normalize_text(text)
        if not norm or not intent_data:
            return

        key = f"{norm}|{1 if has_pending_download else 0}|{stored_lang}"
        now = time.time()

        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (now, dict(intent_data))
            if len(self._cache) > self.capacity:
                self._cache.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self.capacity,
                "ttl_sec": self.ttl_sec,
                "hits": self._hits,
                "misses": self._misses,
            }

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0


# Global intent cache singleton
intent_cache = IntentCache()


# --- Rules-First Fast Path ---

_rules_first_lock = threading.Lock()
_rules_handled_count = 0
_llm_handled_count = 0


def can_run_rules_first(text: str, rule_res: Optional[dict], lms) -> bool:
    """
    Check if a message can be executed rules-first with zero provider calls:
    1. Offline matcher returns exactly one action.
    2. Message has at most 8 words.
    3. Contains NO words that flip or negate meaning (not, no, dont, nahi, mat, without, etc.).
    4. If action specifies a course_query, it resolves unambiguously (exactly 1 course match).
    """
    if not rule_res:
        return False

    actions = rule_res.get("actions", [])
    if len(actions) != 1:
        return False

    act = actions[0]
    intent = act.get("intent")
    # Obvious exhibition intents handled rules-first with zero provider calls
    if intent not in ("attendance", "grades", "calendar", "upcoming", "help"):
        return False

    if rule_res.get("unsupported") or rule_res.get("off_topic"):
        return False

    words = text.strip().split()
    if len(words) > 8:
        return False

    # Check meaning-flipping words
    norm_words = {w.lower().strip("?,.!") for w in words}
    if any(fw in norm_words for fw in MEANING_FLIP_WORDS):
        return False

    # Check course ambiguity if course_query is present
    act = actions[0]
    cq = act.get("course_query")
    if cq and lms:
        try:
            from courses import resolve_course
            hits = resolve_course(lms, cq)
            if len(hits) != 1:
                return False
        except Exception:
            return False

    return True


def record_intent_handled(is_rule_handled: bool):
    global _rules_handled_count, _llm_handled_count
    with _rules_first_lock:
        if is_rule_handled:
            _rules_handled_count += 1
        else:
            _llm_handled_count += 1


def get_rules_first_stats() -> dict:
    with _rules_first_lock:
        tot = _rules_handled_count + _llm_handled_count
        pct = round((_rules_handled_count / tot) * 100, 1) if tot > 0 else 0.0
        return {
            "rules_handled": _rules_handled_count,
            "llm_handled": _llm_handled_count,
            "total": tot,
            "rule_share_pct": pct,
        }


def reset_rules_first_stats():
    global _rules_handled_count, _llm_handled_count
    with _rules_first_lock:
        _rules_handled_count = 0
        _llm_handled_count = 0


# --- Feature Chains ---

def get_intent_providers() -> list[str]:
    """Return ordered provider list for intent classification."""
    raw = os.environ.get("INTENT_PROVIDERS")
    if raw is None or not raw.strip():
        raw = os.environ.get("LLM_PROVIDERS", "gemini,groq")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def get_study_providers() -> list[str]:
    """Return ordered provider list for study helper (summaries & quizzes)."""
    raw = os.environ.get("STUDY_PROVIDERS")
    if raw is None or not raw.strip():
        raw = os.environ.get("LLM_PROVIDERS", "gemini,groq")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def get_stt_providers() -> list[str]:
    """Return ordered provider list for speech-to-text."""
    raw = os.environ.get("STT_PROVIDERS", "groq,gemini")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def generate_capacity_report() -> str:
    """
    Generate an exhibition capacity report:
    Active chains, providers, verified models, ceilings, and estimated throughput.
    """
    from app.llm.providers import PROVIDER_CONFIGS, get_provider_models, registry

    intent_p = get_intent_providers()
    study_p = get_study_providers()
    stt_p = get_stt_providers()

    lines = [
        "============================================================",
        "          TALEEMGO AI CAPACITY & RESILIENCE REPORT          ",
        "============================================================",
        f"Exhibition Mode: {'ON' if is_exhibition_mode() else 'OFF'}",
        f"Intent Cache: {intent_cache.capacity} entries, TTL: {int(intent_cache.ttl_sec)}s",
        f"Provider Concurrency Cap: {os.environ.get('PROVIDER_CONCURRENCY', '3')}",
        "",
        "--- FEATURE CHAINS ---",
        f"Intent Chain (High Priority): {', '.join(intent_p)}",
        f"Study Chain (Low Priority):  {', '.join(study_p)}",
        f"STT Chain (High Priority):    {', '.join(stt_p)}",
        "",
        "--- PROVIDER CEILINGS & WINDOWS ---",
    ]

    total_est_rpm = 0

    all_providers = sorted(set(intent_p + study_p))
    for p in all_providers:
        cfg = PROVIDER_CONFIGS.get(p, {})
        models = get_provider_models(p)
        ceilings = limiter.get_ceilings(p)
        rpm = ceilings.get("rpm", "unmetered")
        tpm = ceilings.get("tpm", "unmetered")
        rpd = ceilings.get("rpd", "unmetered")
        tpd = ceilings.get("tpd", "unmetered")

        if isinstance(rpm, int):
            total_est_rpm += rpm
        elif rpm == "unmetered":
            total_est_rpm += 60

        models_str = ", ".join(models) if models else "none"
        lines.append(f"[{p.upper()}] Models: {models_str}")
        lines.append(f"  Ceilings: RPM={rpm} | TPM={tpm} | RPD={rpd} | TPD={tpd}")

    lines.append("")
    lines.append(f"Estimated absorbable burst throughput: ~{total_est_rpm} reqs/min")
    lines.append("============================================================")
    return "\n".join(lines)
