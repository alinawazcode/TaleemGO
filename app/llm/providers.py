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
    cfg = PROVIDER_CONFIGS.get(provider_name, {})
    if provider_name == "cloudflare":
        acc_id = os.environ.get(cfg.get("env_account_id", "CLOUDFLARE_ACCOUNT_ID"), "").strip()
        return cfg.get("base_url_template", "").format(account_id=acc_id)
    elif provider_name == "local":
        return os.environ.get(cfg.get("env_url", "LOCAL_LLM_URL"), cfg.get("default_url", "http://127.0.0.1:11434/v1")).strip()
    return cfg.get("base_url", "")


def get_provider_models(provider_name: str) -> list[str]:
    """
    Return ordered list of models for provider:
    1. Comma-separated list in env var (e.g. GEMINI_MODELS)
    2. Single legacy model env var (e.g. GEMINI_MODEL)
    3. Default verified list in PROVIDER_CONFIGS
    """
    cfg = PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        return []
    raw_models = os.environ.get(cfg["env_models"], "").strip()
    if raw_models:
        return [m.strip() for m in raw_models.split(",") if m.strip()]
    raw_legacy = os.environ.get(cfg["env_model_legacy"], "").strip()
    if raw_legacy:
        return [raw_legacy]
    return list(cfg["default_models"])


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
        self._states = {name: ProviderState(name=name) for name in PROVIDER_CONFIGS}
        self._lock = threading.Lock()

    def get_state(self, name: str) -> ProviderState:
        with self._lock:
            return self._states.setdefault(name, ProviderState(name=name))

    def is_available(self, name: str) -> bool:
        """Returns True if provider has a valid key, is not disabled, and is not in cooldown."""
        cfg = PROVIDER_CONFIGS.get(name)
        if not cfg:
            return False
        api_key = os.environ.get(cfg["env_key"], "")
        if not is_valid_api_key(api_key, provider_name=name):
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
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.consecutive_429 = 0
            state.consecutive_5xx = 0
            state.cooldown_until = 0.0
            state.calls_today += 1
            state.last_error_class = None
            if model:
                state.active_model = model

    def record_bad_model(self, name: str, model: str, is_final: bool = False):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.bad_models.add(model)
            if is_final:
                state.disabled = True
                state.status_override = "bad model"
                state.last_error_class = "BadModel"
                if not state.logged_model_error:
                    state.logged_model_error = True
                    cfg = PROVIDER_CONFIGS.get(name, {})
                    env_var = cfg.get("env_models", cfg.get("env_model_legacy", "MODEL"))
                    log.error("provider %s: model '%s' not found. Set %s in .env (see README)", name, model, env_var)

    def record_auth_error(self, name: str):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.disabled = True
            state.status_override = "bad key"
            state.last_error_class = "AuthError"
            cfg = PROVIDER_CONFIGS.get(name, {})
            log.error("bad API key for %s in %s", name, cfg.get("env_key", "API_KEY"))

    def record_client_error(self, name: str, status: int):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.calls_today += 1
            state.last_error_class = f"HTTP{status}"
            state.cooldown_until = time.time() + 60.0
            log.warning("Provider %s client error HTTP %d (flat 60s cooldown)", name, status)

    def record_429(self, name: str, retry_after: Optional[float] = None):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.calls_today += 1
            state.last_error_class = "RateLimit429"
            if retry_after is not None and retry_after > 0:
                cooldown = retry_after
            else:
                cooldown = min(3600.0, 60.0 * (2 ** state.consecutive_429))
                state.consecutive_429 += 1
            state.cooldown_until = time.time() + cooldown
            log.warning("Provider %s in 429 cooldown for %.1fs", name, cooldown)

    def record_5xx_or_network(self, name: str, err_class: str = "ServerOrNetworkError"):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.calls_today += 1
            state.last_error_class = err_class
            cooldown = min(600.0, 30.0 * (2 ** state.consecutive_5xx))
            state.consecutive_5xx += 1
            state.cooldown_until = time.time() + cooldown
            log.warning("Provider %s in 5xx/network cooldown for %.1fs (%s)", name, cooldown, err_class)

    def get_summary(self) -> dict:
        """Returns provider status summary with model IDs and status words for admin command."""
        now = time.time()
        summary = {}
        intent_p = get_intent_providers()
        study_p = get_study_providers()
        stt_p = get_stt_providers()

        with self._lock:
            for name, cfg in PROVIDER_CONFIGS.items():
                raw_key = os.environ.get(cfg["env_key"], "")
                state = self._states.get(name, ProviderState(name=name))
                if not is_valid_api_key(raw_key, provider_name=name):
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

                models = get_provider_models(name)
                active = state.active_model or (models[0] if models else "none")

                # Window usage percentages
                usage = limiter.get_window_usage(name, active)

                # Chains this provider belongs to
                chains = []
                if name in intent_p:
                    chains.append("intent")
                if name in study_p:
                    chains.append("study")
                if name in stt_p:
                    chains.append("stt")

                summary[name] = {
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
    Tries models in the provider's ordered list, failing over immediately on 404/model errors.
    Returns the string text inside choices[0].message.content.
    """
    cfg = PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider_name}")

    api_key = os.environ.get(cfg["env_key"], "").strip()
    if not is_valid_api_key(api_key, provider_name=provider_name):
        raise AuthError(f"Missing or invalid API key for {provider_name} in {cfg['env_key']}")

    all_models = get_provider_models(provider_name)
    state = registry.get_state(provider_name)

    if state.disabled:
        if state.status_override == "bad model":
            raise BadModelError(f"provider {provider_name}: bad model")
        raise AuthError(f"provider {provider_name} is disabled")

    # Order candidates: try active_model first if available and not bad
    candidates = []
    if state.active_model and state.active_model not in state.bad_models:
        candidates.append(state.active_model)
    for m in all_models:
        if m not in candidates and m not in state.bad_models:
            candidates.append(m)

    if not candidates:
        last_m = all_models[-1] if all_models else "none"
        registry.record_bad_model(provider_name, last_m, is_final=True)
        raise BadModelError(f"provider {provider_name}: model '{last_m}' not found.")

    base_url = get_provider_base_url(provider_name)
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key or 'local'}",
        "Content-Type": "application/json",
        "User-Agent": "TaleemGo-AI/1.0",
    }
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/alinawazcode/Saathi"
        headers["X-Title"] = "TaleemGo AI"

    # Acquire concurrency slot (wait up to 8s for high priority, 2s for low)
    acquired = limiter.acquire_concurrency(provider_name, priority=priority)
    if not acquired:
        registry.record_429(provider_name, retry_after=5.0)
        raise RateLimitError(f"{provider_name} concurrency queue timeout")

    try:
        skipped_models = 0
        for idx, model in enumerate(candidates):
            is_last_model = (idx == len(candidates) - 1)
            est_tokens = limiter.estimate_tokens(messages, max_tokens=max_tokens)
            if not limiter.can_call(provider_name, model, est_tokens):
                skipped_models += 1
                if is_last_model and skipped_models == len(candidates):
                    raise RateLimitError(f"{provider_name} sliding window ceiling reached for all models")
                continue

            use_json_mode = json_mode and state.supports_json_mode

            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if provider_name == "gemini":
                # Gemini OpenAI compatibility reasoning parameter
                payload["reasoning_effort"] = "low"
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            except requests.Timeout as e:
                registry.record_5xx_or_network(provider_name, "Timeout")
                raise ServerOrNetworkError(f"{provider_name} request timed out") from e
            except requests.RequestException as e:
                registry.record_5xx_or_network(provider_name, "ConnectionError")
                raise ServerOrNetworkError(f"{provider_name} network connection error") from e

            status = r.status_code

            # 1. 401/403 Authentication Error
            if status in (401, 403):
                registry.record_auth_error(provider_name)
                raise AuthError(f"Authentication failed for {provider_name} in {cfg['env_key']}")

            # 2. Model not found / deprecated -> failover to next model
            if _is_model_not_found(status, r.text):
                state.bad_models.add(model)
                if not is_last_model:
                    log.warning("Provider %s model '%s' failed (HTTP %d). Trying next model...", provider_name, model, status)
                    continue
                else:
                    registry.record_bad_model(provider_name, model, is_final=True)
                    raise BadModelError(f"provider {provider_name}: model '{model}' not found.")

            # 3. HTTP 400 response_format error -> retry once without json mode
            if use_json_mode and _is_json_mode_error(status, r.text):
                state.supports_json_mode = False
                log.warning("Provider %s does not support json_mode (HTTP %d). Retrying without json_mode...", provider_name, status)
                payload.pop("response_format", None)
                try:
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    status = r.status_code
                except requests.Timeout as e:
                    registry.record_5xx_or_network(provider_name, "Timeout")
                    raise ServerOrNetworkError(f"{provider_name} request timed out") from e
                except requests.RequestException as e:
                    registry.record_5xx_or_network(provider_name, "ConnectionError")
                    raise ServerOrNetworkError(f"{provider_name} network connection error") from e

                if _is_model_not_found(status, r.text):
                    state.bad_models.add(model)
                    if not is_last_model:
                        continue
                    registry.record_bad_model(provider_name, model, is_final=True)
                    raise BadModelError(f"provider {provider_name}: model '{model}' not found.")

                if status in (401, 403):
                    registry.record_auth_error(provider_name)
                    raise AuthError(f"Authentication failed for {provider_name} in {cfg['env_key']}")

            # 4. 429 Rate limit
            if status == 429:
                retry_after = None
                ra_header = r.headers.get("Retry-After")
                if ra_header:
                    try:
                        retry_after = float(ra_header)
                    except ValueError:
                        pass
                registry.record_429(provider_name, retry_after)
                limiter.record_usage(provider_name, model, 0, r.headers)
                raise RateLimitError(f"{provider_name} rate limit exceeded", retry_after=retry_after)

            # 5. 5xx Server Error or 408 Request Timeout
            if status >= 500 or status == 408:
                registry.record_5xx_or_network(provider_name, f"HTTP{status}")
                raise ServerOrNetworkError(f"{provider_name} server error HTTP {status}")

            # 6. Other 4xx Client Error -> flat 60s cooldown (no doubling)
            if status >= 400:
                registry.record_client_error(provider_name, status)
                raise ClientError(f"{provider_name} returned client error HTTP {status}")

            # 7. Success (HTTP 200)
            try:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                registry.record_success(provider_name, model=model)
                usage = data.get("usage")
                total_tokens = est_tokens
                if usage:
                    total_tokens = usage.get("total_tokens", est_tokens)
                    log.info(
                        "Provider %s [%s] usage: prompt=%s, completion=%s, total=%s",
                        provider_name,
                        model,
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        total_tokens,
                    )
                limiter.record_usage(provider_name, model, total_tokens, r.headers)
                return content
            except Exception as e:
                registry.record_5xx_or_network(provider_name, "InvalidResponseFormat")
                raise ServerOrNetworkError(f"Malformed response payload from {provider_name}") from e

        last_m = candidates[-1] if candidates else "none"
        registry.record_bad_model(provider_name, last_m, is_final=True)
        raise BadModelError(f"provider {provider_name}: model '{last_m}' not found.")
    finally:
        limiter.release_concurrency(provider_name)


def run_live_check(provider_name: str) -> tuple[str, str, float]:
    """
    Make one tiny live test call (prompt 'Reply with OK', max_tokens=5, timeout=6.0).
    Returns (status, model_id, latency_ms).
    status is one of: 'ok', 'bad model', 'bad key', 'rate limited', 'down', 'no key'.
    Never raises, never logs keys.
    """
    cfg = PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        return "down", "unknown", 0.0

    raw_key = os.environ.get(cfg["env_key"], "")
    models = get_provider_models(provider_name)
    primary_model = models[0] if models else "none"

    if not is_valid_api_key(raw_key, provider_name=provider_name):
        return "no key", primary_model, 0.0

    start_t = time.time()
    try:
        call_chat_completions(
            provider_name,
            messages=[{"role": "user", "content": "Reply with OK"}],
            json_mode=False,
            timeout=6.0,
            max_tokens=5,
        )
        latency = (time.time() - start_t) * 1000
        state = registry.get_state(provider_name)
        active = state.active_model or primary_model
        return "ok", active, latency
    except BadModelError:
        state = registry.get_state(provider_name)
        active = state.active_model or primary_model
        return "bad model", active, 0.0
    except AuthError:
        return "bad key", primary_model, 0.0
    except RateLimitError:
        return "rate limited", primary_model, 0.0
    except Exception:
        return "down", primary_model, 0.0
