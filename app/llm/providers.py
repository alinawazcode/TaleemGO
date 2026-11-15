"""
providers.py - Generic OpenAI-compatible chat completions client with thread-safe cooldowns,
model list failover, and strict error classification.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

try:
    from .capacity import limiter, get_intent_providers, get_study_providers, get_stt_providers
except ImportError:
    try:
        from app.llm.capacity import limiter, get_intent_providers, get_study_providers, get_stt_providers
    except ImportError:
        from llm.capacity import limiter, get_intent_providers, get_study_providers, get_stt_providers

log = logging.getLogger("taleemgo.llm.providers")

# Config table of supported providers
# Model IDs verified on 2026-11-15 against official provider documentation
PROVIDER_CONFIGS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "env_models": "GEMINI_MODELS",
        "env_model_legacy": "GEMINI_MODEL",
        # Verified 2026-11-08 (gemini-3.5-flash-lite, gemini-3.8-flash, gemini-3.6-flash)
        "default_models": ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash"],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "env_models": "GROQ_MODELS",
        "env_model_legacy": "GROQ_MODEL",
        # Verified 2026-11-15 (openai/gpt-oss-20b, openai/gpt-oss-120b, qwen/qwen3.8-27b)
        "default_models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "env_models": "OPENROUTER_MODELS",
        "env_model_legacy": "OPENROUTER_MODEL",
        # Verified 2026-11-08
        "default_models": ["meta-llama/llama-3.3-70b-instruct:free"],
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "env_models": "CEREBRAS_MODELS",
        "env_model_legacy": "CEREBRAS_MODEL",
        # Verified 2026-11-08
        "default_models": ["llama3.1-8b"],
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY",
        "env_models": "MISTRAL_MODELS",
        "env_model_legacy": "MISTRAL_MODEL",
        # Verified 2026-11-15: mistral-small-latest, open-mistral-7b
        "default_models": ["mistral-small-latest", "open-mistral-7b"],
    },
    "cloudflare": {
        "base_url_template": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "env_key": "CLOUDFLARE_API_TOKEN",
        "env_account_id": "CLOUDFLARE_ACCOUNT_ID",
        "env_models": "CLOUDFLARE_MODELS",
        "env_model_legacy": "CLOUDFLARE_MODEL",
        # Verified 2026-11-15: @cf/meta/llama-3.1-8b-instruct, @cf/meta/llama-3-8b-instruct
        "default_models": ["@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3-8b-instruct"],
    },
    "local": {
        "env_url": "LOCAL_LLM_URL",
        "default_url": "http://127.0.0.1:11434/v1",
        "env_key": "LOCAL_LLM_KEY",
        "env_models": "LOCAL_LLM_MODELS",
        "env_model_legacy": "LOCAL_LLM_MODEL",
        # Verified 2026-11-15: llama3.2:3b, qwen2.5:3b
        "default_models": ["llama3.2:3b", "qwen2.5:3b"],
        "no_auth_required": True,
    },
}


class LLMError(Exception):
    """Base exception for LLM provider errors."""
    pass


class AuthError(LLMError):
    """401/403 Authentication failure."""
    pass


class BadModelError(LLMError):
    """Configuration error: all models in provider list failed or decommissioned."""
    pass


class RateLimitError(LLMError):
    """429 Quota or rate limit exceeded."""
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerOrNetworkError(LLMError):
    """5xx server error, timeout, or network connection error."""
    pass


class ClientError(LLMError):
    """4xx client error (flat 60s cooldown)."""
    pass


def is_valid_api_key(key: Optional[str], provider_name: Optional[str] = None) -> bool:
    """
    Treat an env key as missing when empty, shorter than 20 chars,
    or containing placeholder markers like 'your', 'here', 'xxxx', 'changeme'.
    Special cases:
    - 'local' provider requires no auth (key can be empty or anything).
    - 'cloudflare' provider requires both valid token and non-empty CLOUDFLARE_ACCOUNT_ID.
    """
    if provider_name == "local":
        return True
    if provider_name == "cloudflare":
        acc_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if not acc_id or len(acc_id) < 5 or any(m in acc_id.lower() for m in ("your", "here", "xxxx", "changeme")):
            return False
    if not key:
        return False
    k = key.strip()
    if len(k) < 20:
        return False
    kl = k.lower()
    for marker in ("your", "here", "xxxx", "changeme", "change_me"):
        if marker in kl:
            return False
    return True


def get_provider_base_url(provider_name: str) -> str:
    """Resolve base URL for provider, including dynamic templating for cloudflare and local."""
    base_p = provider_name.lower().split("#")[0]
    cfg = PROVIDER_CONFIGS.get(base_p, {})
    if base_p == "cloudflare":
        acc_id = os.environ.get(cfg.get("env_account_id", "CLOUDFLARE_ACCOUNT_ID"), "").strip()
        return cfg.get("base_url_template", "").format(account_id=acc_id)
    elif base_p == "local":
        return os.environ.get(cfg.get("env_url", "LOCAL_LLM_URL"), cfg.get("default_url", "http://127.0.0.1:11434/v1")).strip()
    return cfg.get("base_url", "")


def get_provider_models(provider_name: str) -> list[str]:
    """
    Return ordered list of models for provider:
    1. Comma-separated list in env var (e.g. GEMINI_MODELS)
    2. Single legacy model env var (e.g. GEMINI_MODEL)
    3. Default verified list in PROVIDER_CONFIGS
    """
    base_p = provider_name.lower().split("#")[0]
    cfg = PROVIDER_CONFIGS.get(base_p)
    if not cfg:
        return []
    raw_models = os.environ.get(cfg["env_models"], "").strip()
    if raw_models:
        return [m.strip() for m in raw_models.split(",") if m.strip()]
    raw_legacy = os.environ.get(cfg["env_model_legacy"], "").strip()
    if raw_legacy:
        return [raw_legacy]
    return list(cfg["default_models"])


def get_provider_keys(provider_name: str) -> list[str]:
    """
    Return list of valid, non-placeholder API keys for provider.
    Order:
    1. Single primary key from env_key (e.g. GEMINI_API_KEY) as key #1.
    2. Any additional keys from f"{prefix}_API_KEYS" (e.g. GEMINI_API_KEYS).
    Blanks and invalid/placeholder keys are filtered out.
    Duplicates are removed while preserving first occurrence order.
    """
    base_p = provider_name.lower().split("#")[0]
    cfg = PROVIDER_CONFIGS.get(base_p)
    if not cfg:
        return []

    if base_p == "local":
        return ["local"]

    keys = []
    # 1. Primary key from env_key
    primary = os.environ.get(cfg["env_key"], "").strip()
    if is_valid_api_key(primary, provider_name=base_p):
        keys.append(primary)

    # 2. Multi-key env var (e.g. GEMINI_API_KEYS, GROQ_API_KEYS)
    prefix = base_p.upper()
    multi_raw = os.environ.get(f"{prefix}_API_KEYS", "")
    if multi_raw:
        for raw in multi_raw.split(","):
            k = raw.strip()
            if is_valid_api_key(k, provider_name=base_p) and k not in keys:
                keys.append(k)

    return keys


def get_provider_key_instances(provider_name: str) -> list[str]:
    """Return list of instance identifiers for provider, e.g. ['gemini#1', 'gemini#2']."""
    base_p = provider_name.lower().split("#")[0]
    keys = get_provider_keys(base_p)
    if not keys:
        return [f"{base_p}#1"]
    return [f"{base_p}#{i+1}" for i in range(len(keys))]


def get_key_for_instance(instance_name: str) -> Optional[str]:
    """Resolve raw API key for a key instance (e.g. 'gemini#2'). Never log this value."""
    base_p = instance_name.lower().split("#")[0]
    keys = get_provider_keys(base_p)
    if "#" in instance_name:
        try:
            idx = int(instance_name.split("#")[1]) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
            return None
        except ValueError:
            return None
    return keys[0] if keys else None


def get_fair_key_order(provider_name: str) -> list[str]:
    """
    Return candidate key instances for provider_name.
    Available keys are ordered fairly: least requests in the current 60s sliding window first.
    Tie-breaker is key index order (#1, #2...).
    Unavailable keys (in cooldown or disabled) are placed at the end as fallback.
    """
    instances = get_provider_key_instances(provider_name)
    if not instances:
        return [f"{provider_name}#1"]

    available = []
    unavailable = []
    for inst in instances:
        if registry.is_available(inst):
            usage = limiter.get_key_recent_usage(inst)
            available.append((usage, inst))
        else:
            unavailable.append(inst)

    # Sort available by usage ascending; stable sort preserves #1 before #2 on tie
    available.sort(key=lambda x: x[0])
    ordered_available = [inst for _, inst in available]

    return ordered_available + unavailable


class SummaryDict(dict):
    """
    Dictionary subclass for registry.get_summary().
    Maps key instances (e.g. 'gemini#1', 'groq#1') as primary keys, while allowing
    transparent backward-compatible lookup by base provider name (e.g. summary['groq'] -> summary['groq#1']).
    """

    def __getitem__(self, key):
        if super().__contains__(key):
            return super().__getitem__(key)
        alias = f"{key}#1"
        if super().__contains__(alias):
            return super().__getitem__(alias)
        return super().__getitem__(key)

    def __contains__(self, key):
        return super().__contains__(key) or super().__contains__(f"{key}#1")

    def get(self, key, default=None):
        if super().__contains__(key):
            return super().get(key, default)
        alias = f"{key}#1"
        if super().__contains__(alias):
            return super().get(alias, default)
        return default


@dataclass
class ProviderState:
    name: str
    disabled: bool = False
    status_override: Optional[str] = None  # e.g. "bad model", "bad key", "down"
    cooldown_until: float = 0.0
    consecutive_429: int = 0
    consecutive_5xx: int = 0
    calls_today: int = 0
    last_error_class: Optional[str] = None
    active_model: Optional[str] = None
    bad_models: set = field(default_factory=set)
    supports_json_mode: bool = True
    logged_model_error: bool = False


class ProviderRegistry:
    def __init__(self):
        self._states = {}
        self._lock = threading.Lock()

    def get_state(self, name: str) -> ProviderState:
        with self._lock:
            key_name = name if "#" in name else f"{name}#1"
            return self._states.setdefault(key_name, ProviderState(name=key_name))

    def is_available(self, name: str) -> bool:
        """
        Returns True if provider/instance has a valid key, is not disabled, and is not in cooldown.
        If name has no '#', returns True if ANY configured key instance of the provider is available.
        """
        if "#" not in name:
            instances = get_provider_key_instances(name)
            if not instances:
                return False
            keys = get_provider_keys(name)
            if not keys and name != "local":
                return False
            return any(self.is_available(inst) for inst in instances)

        base_p = name.split("#")[0].lower()
        cfg = PROVIDER_CONFIGS.get(base_p)
        if not cfg:
            return False

        api_key = get_key_for_instance(name)
        if not is_valid_api_key(api_key, provider_name=base_p):
            return False

        with self._lock:
            state = self._states.get(name)
            if not state:
                return True
            if state.disabled:
                return False
            now = time.time()
            if state.cooldown_until > now:
                return False
            return True

    def record_success(self, name: str, model: Optional[str] = None):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.consecutive_429 = 0
            state.consecutive_5xx = 0
            state.cooldown_until = 0.0
            state.calls_today += 1
            state.last_error_class = None
            if model:
                state.active_model = model

    def record_bad_model(self, name: str, model: str, is_final: bool = False):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.bad_models.add(model)
            if is_final:
                state.disabled = True
                state.status_override = "bad model"
                state.last_error_class = "BadModel"
                if not state.logged_model_error:
                    state.logged_model_error = True
                    base_p = name.split("#")[0]
                    cfg = PROVIDER_CONFIGS.get(base_p, {})
                    env_var = cfg.get("env_models", cfg.get("env_model_legacy", "MODEL"))
                    log.error("provider %s: model '%s' not found. Set %s in .env (see README)", key_name, model, env_var)

    def record_auth_error(self, name: str):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.disabled = True
            state.status_override = "bad key"
            state.last_error_class = "AuthError"
            log.error("bad API key for %s", key_name)

    def record_client_error(self, name: str, status: int):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.calls_today += 1
            state.last_error_class = f"HTTP{status}"
            state.cooldown_until = time.time() + 60.0
            log.warning("Provider %s client error HTTP %d (flat 60s cooldown)", key_name, status)

    def record_429(self, name: str, retry_after: Optional[float] = None):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.calls_today += 1
            state.last_error_class = "RateLimit429"
            if retry_after is not None and retry_after > 0:
                cooldown = retry_after
            else:
                cooldown = min(3600.0, 60.0 * (2 ** state.consecutive_429))
                state.consecutive_429 += 1
            state.cooldown_until = time.time() + cooldown
            log.warning("Provider %s in 429 cooldown for %.1fs", key_name, cooldown)

    def record_5xx_or_network(self, name: str, err_class: str = "ServerOrNetworkError"):
        key_name = name if "#" in name else f"{name}#1"
        with self._lock:
            state = self._states.setdefault(key_name, ProviderState(name=key_name))
            state.calls_today += 1
            state.last_error_class = err_class
            cooldown = min(600.0, 30.0 * (2 ** state.consecutive_5xx))
            state.consecutive_5xx += 1
            state.cooldown_until = time.time() + cooldown
            log.warning("Provider %s in 5xx/network cooldown for %.1fs (%s)", key_name, cooldown, err_class)

    def get_summary(self) -> dict:
        """Returns provider status summary with model IDs and status words for admin command."""
        now = time.time()
        summary = SummaryDict()
        intent_p = get_intent_providers()
        study_p = get_study_providers()
        stt_p = get_stt_providers()

        with self._lock:
            for base_name, cfg in PROVIDER_CONFIGS.items():
                instances = get_provider_key_instances(base_name)
                for inst in instances:
                    raw_key = get_key_for_instance(inst)
                    state = self._states.get(inst, ProviderState(name=inst))
                    if not is_valid_api_key(raw_key, provider_name=base_name):
                        status = "no key"
                    elif state.status_override:
                        status = state.status_override
                    elif state.disabled:
                        status = "disabled"
                    elif state.cooldown_until > now:
                        left = int(state.cooldown_until - now)
                        if state.last_error_class == "RateLimit429":
                            status = f"rate limited ({left}s)"
                        else:
                            status = f"cooldown {left}s"
                    else:
                        status = "ok"

                    models = get_provider_models(base_name)
                    active = state.active_model or (models[0] if models else "none")

                    # Window usage percentages
                    usage = limiter.get_window_usage(inst, active)

                    # Chains this provider belongs to
                    chains = []
                    if base_name in intent_p:
                        chains.append("intent")
                    if base_name in study_p:
                        chains.append("study")
                    if base_name in stt_p:
                        chains.append("stt")

                    summary[inst] = {
                        "status": status,
                        "model": active,
                        "calls_today": state.calls_today,
                        "last_error": state.last_error_class or "none",
                        "rpm_pct": usage.get("rpm_pct"),
                        "tpm_pct": usage.get("tpm_pct"),
                        "rpd_pct": usage.get("rpd_pct"),
                        "tpd_pct": usage.get("tpd_pct"),
                        "chains": chains,
                    }
        return summary


# Global registry singleton
registry = ProviderRegistry()


def _is_model_not_found(status_code: int, response_text: str) -> bool:
    """Return True if response indicates model does not exist or is deprecated."""
    if status_code == 404:
        return True
    if status_code in (400, 422):
        txt = (response_text or "").lower()
        for phrase in (
            "not found", "does not exist", "not supported",
            "decommissioned", "model_not_found", "no longer available",
            "unknown model", "invalid model",
        ):
            if phrase in txt:
                return True
    return False


def _is_json_mode_error(status_code: int, response_text: str) -> bool:
    """Return True if response indicates response_format/json mode is unsupported."""
    if status_code in (400, 422):
        txt = (response_text or "").lower()
        if "response_format" in txt or "json_object" in txt or "json mode" in txt:
            return True
    return False


def call_chat_completions(
    provider_name: str,
    messages: list[dict],
    json_mode: bool = True,
    timeout: float = 6.0,
    temperature: float = 0.0,
    max_tokens: int = 800,
    priority: str = "high",
) -> str:
    """
    Call OpenAI-compatible chat completions endpoint with requests.
    Supports multi-key failover and fair rotation across keys of provider_name.
    Tries models in the provider's ordered list, failing over immediately on 404/model errors.
    Returns the string text inside choices[0].message.content.
    """
    if "#" in provider_name:
        base_provider = provider_name.split("#")[0].lower()
        candidate_keys = [provider_name]
    else:
        base_provider = provider_name.lower()
        candidate_keys = get_fair_key_order(base_provider)

    cfg = PROVIDER_CONFIGS.get(base_provider)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider_name}")

    all_models = get_provider_models(base_provider)
    base_url = get_provider_base_url(base_provider)
    url = f"{base_url.rstrip('/')}/chat/completions"

    last_error = None
    rate_limit_occurred = False
    auth_error_occurred = False
    bad_model_occurred = False

    for key_idx, key_inst in enumerate(candidate_keys):
        api_key = get_key_for_instance(key_inst)

        if not is_valid_api_key(api_key, provider_name=base_provider):
            auth_error_occurred = True
            registry.record_auth_error(key_inst)
            continue

        state = registry.get_state(key_inst)
        if state.disabled:
            if state.status_override == "bad model":
                bad_model_occurred = True
            else:
                auth_error_occurred = True
            continue

        now = time.time()
        if state.cooldown_until > now:
            rate_limit_occurred = True
            continue

        # Candidate models for this key
        candidates = []
        if state.active_model and state.active_model not in state.bad_models:
            candidates.append(state.active_model)
        for m in all_models:
            if m not in candidates and m not in state.bad_models:
                candidates.append(m)

        if not candidates:
            last_m = all_models[-1] if all_models else "none"
            registry.record_bad_model(key_inst, last_m, is_final=True)
            bad_model_occurred = True
            continue

        headers = {
            "Authorization": f"Bearer {api_key or 'local'}",
            "Content-Type": "application/json",
            "User-Agent": "TaleemGo-AI/1.0",
        }
        if base_provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/alinawazcode/Saathi"
            headers["X-Title"] = "TaleemGo AI"

        # Acquire concurrency slot for this key instance
        acquired = limiter.acquire_concurrency(key_inst, priority=priority)
        if not acquired:
            registry.record_429(key_inst, retry_after=5.0)
            rate_limit_occurred = True
            log.warning("Key instance %s concurrency timeout, trying next key", key_inst)
            continue

        try:
            skipped_models = 0
            for idx, model in enumerate(candidates):
                is_last_model = (idx == len(candidates) - 1)
                est_tokens = limiter.estimate_tokens(messages, max_tokens=max_tokens)
                if not limiter.can_call(key_inst, model, est_tokens):
                    skipped_models += 1
                    if is_last_model and skipped_models == len(candidates):
                        log.info("[LIMITER] Provider %s window ceiling reached for all models, trying next key...", key_inst)
                        rate_limit_occurred = True
                        break
                    continue

                use_json_mode = json_mode and state.supports_json_mode

                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if base_provider == "gemini":
                    payload["reasoning_effort"] = "low"
                if use_json_mode:
                    payload["response_format"] = {"type": "json_object"}

                try:
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                except requests.Timeout as e:
                    registry.record_5xx_or_network(key_inst, "Timeout")
                    last_error = ServerOrNetworkError(f"{key_inst} request timed out")
                    break
                except requests.RequestException as e:
                    registry.record_5xx_or_network(key_inst, "ConnectionError")
                    last_error = ServerOrNetworkError(f"{key_inst} network connection error")
                    break

                status = r.status_code

                # 1. 401/403 Authentication Error -> disable this key, failover to next key
                if status in (401, 403):
                    registry.record_auth_error(key_inst)
                    auth_error_occurred = True
                    log.warning("Provider %s auth failure (HTTP %d). Failing over to next key...", key_inst, status)
                    break

                # 2. Model not found / deprecated -> failover to next model on same key
                if _is_model_not_found(status, r.text):
                    state.bad_models.add(model)
                    if not is_last_model:
                        log.warning("Provider %s model '%s' failed (HTTP %d). Trying next model...", key_inst, model, status)
                        continue
                    else:
                        registry.record_bad_model(key_inst, model, is_final=True)
                        bad_model_occurred = True
                        break

                # 3. HTTP 400 response_format error -> retry once without json mode
                if use_json_mode and _is_json_mode_error(status, r.text):
                    state.supports_json_mode = False
                    log.warning("Provider %s does not support json_mode (HTTP %d). Retrying without json_mode...", key_inst, status)
                    payload.pop("response_format", None)
                    try:
                        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                        status = r.status_code
                    except requests.Timeout as e:
                        registry.record_5xx_or_network(key_inst, "Timeout")
                        last_error = ServerOrNetworkError(f"{key_inst} request timed out")
                        break
                    except requests.RequestException as e:
                        registry.record_5xx_or_network(key_inst, "ConnectionError")
                        last_error = ServerOrNetworkError(f"{key_inst} network connection error")
                        break

                    if _is_model_not_found(status, r.text):
                        state.bad_models.add(model)
                        if not is_last_model:
                            continue
                        registry.record_bad_model(key_inst, model, is_final=True)
                        bad_model_occurred = True
                        break

                    if status in (401, 403):
                        registry.record_auth_error(key_inst)
                        auth_error_occurred = True
                        break

                # 4. 429 Rate limit -> cooldown on this key, failover to next key
                if status == 429:
                    retry_after = None
                    ra_header = r.headers.get("Retry-After")
                    if ra_header:
                        try:
                            retry_after = float(ra_header)
                        except ValueError:
                            pass
                    registry.record_429(key_inst, retry_after)
                    limiter.record_usage(key_inst, model, 0, r.headers)
                    rate_limit_occurred = True
                    log.warning("Provider %s rate limited (HTTP 429). Failing over to next key...", key_inst)
                    break

                # 5. 5xx Server Error or 408 Request Timeout
                if status >= 500 or status == 408:
                    registry.record_5xx_or_network(key_inst, f"HTTP{status}")
                    last_error = ServerOrNetworkError(f"{key_inst} server error HTTP {status}")
                    break

                # 6. Other 4xx Client Error -> flat 60s cooldown
                if status >= 400:
                    registry.record_client_error(key_inst, status)
                    last_error = ClientError(f"{key_inst} returned client error HTTP {status}")
                    break

                # 7. Success (HTTP 200)
                try:
                    data = r.json()
                    content = data["choices"][0]["message"]["content"]
                    registry.record_success(key_inst, model=model)
                    usage = data.get("usage")
                    total_tokens = est_tokens
                    if usage:
                        total_tokens = usage.get("total_tokens", est_tokens)
                        log.info(
                            "Provider %s [%s] usage: prompt=%s, completion=%s, total=%s",
                            key_inst,
                            model,
                            usage.get("prompt_tokens"),
                            usage.get("completion_tokens"),
                            total_tokens,
                        )
                    limiter.record_usage(key_inst, model, total_tokens, r.headers)
                    return content
                except Exception as e:
                    registry.record_5xx_or_network(key_inst, "InvalidResponseFormat")
                    last_error = ServerOrNetworkError(f"Malformed response payload from {key_inst}")
                    break
        finally:
            limiter.release_concurrency(key_inst)

    # If all candidate keys failed:
    if rate_limit_occurred:
        raise RateLimitError(f"{provider_name} rate limit exceeded across all available keys")
    if auth_error_occurred:
        raise AuthError(f"Authentication failed for {provider_name} across all configured keys")
    if bad_model_occurred:
        raise BadModelError(f"provider {provider_name}: bad model across all candidate keys")
    if last_error:
        raise last_error

    raise ServerOrNetworkError(f"All keys for {provider_name} exhausted or unavailable")


def run_live_check(provider_name: str) -> tuple[str, str, float]:
    """
    Make one tiny live test call (prompt 'Reply with OK', max_tokens=5, timeout=6.0).
    Returns (status, model_id, latency_ms).
    status is one of: 'ok', 'bad model', 'bad key', 'rate limited', 'down', 'no key'.
    Never raises, never logs keys.
    """
    base_provider = provider_name.split("#")[0].lower()
    cfg = PROVIDER_CONFIGS.get(base_provider)
    if not cfg:
        return "down", "unknown", 0.0

    key_inst = provider_name if "#" in provider_name else f"{base_provider}#1"
    raw_key = get_key_for_instance(key_inst)
    models = get_provider_models(base_provider)
    primary_model = models[0] if models else "none"

    if not is_valid_api_key(raw_key, provider_name=base_provider):
        return "no key", primary_model, 0.0

    start_t = time.time()
    try:
        call_chat_completions(
            key_inst,
            messages=[{"role": "user", "content": "Reply with OK"}],
            json_mode=False,
            timeout=6.0,
            max_tokens=5,
        )
        latency = (time.time() - start_t) * 1000
        state = registry.get_state(key_inst)
        active = state.active_model or primary_model
        return "ok", active, latency
    except BadModelError:
        state = registry.get_state(key_inst)
        active = state.active_model or primary_model
        return "bad model", active, 0.0
    except AuthError:
        return "bad key", primary_model, 0.0
    except RateLimitError:
        return "rate limited", primary_model, 0.0
    except Exception:
        return "down", primary_model, 0.0
