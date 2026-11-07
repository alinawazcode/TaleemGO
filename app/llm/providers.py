"""
providers.py - Generic OpenAI-compatible chat completions client with thread-safe cooldowns.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("taleemgo.llm.providers")

# Config table of supported providers
# Model IDs verified against official provider docs (Nov 2026)
PROVIDER_CONFIGS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "env_model": "GEMINI_MODEL",
        "default_model": "gemini-2.0-flash",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "env_model": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "env_model": "OPENROUTER_MODEL",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "env_model": "CEREBRAS_MODEL",
        "default_model": "llama3.1-8b",
    },
}


class LLMError(Exception):
    """Base exception for LLM provider errors."""
    pass


class AuthError(LLMError):
    """401/403 Authentication failure."""
    pass


class RateLimitError(LLMError):
    """429 Quota or rate limit exceeded."""
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerOrNetworkError(LLMError):
    """5xx server error, timeout, or network connection error."""
    pass


@dataclass
class ProviderState:
    name: str
    disabled: bool = False
    cooldown_until: float = 0.0
    consecutive_429: int = 0
    consecutive_5xx: int = 0
    calls_today: int = 0
    last_error_class: Optional[str] = None


class ProviderRegistry:
    def __init__(self):
        self._states = {name: ProviderState(name=name) for name in PROVIDER_CONFIGS}
        self._lock = threading.Lock()

    def get_state(self, name: str) -> ProviderState:
        with self._lock:
            return self._states.setdefault(name, ProviderState(name=name))

    def is_available(self, name: str) -> bool:
        """Returns True if provider has an API key, is not disabled, and is not in cooldown."""
        cfg = PROVIDER_CONFIGS.get(name)
        if not cfg:
            return False
        api_key = os.environ.get(cfg["env_key"], "").strip()
        if not api_key:
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

    def record_success(self, name: str):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.consecutive_429 = 0
            state.consecutive_5xx = 0
            state.cooldown_until = 0.0
            state.calls_today += 1
            state.last_error_class = None

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

    def record_auth_error(self, name: str):
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name=name))
            state.disabled = True
            state.last_error_class = "AuthError"
            log.error("bad API key for %s", name)

    def get_summary(self) -> dict:
        """Returns provider status summary for admin command."""
        now = time.time()
        summary = {}
        with self._lock:
            for name, cfg in PROVIDER_CONFIGS.items():
                api_key = os.environ.get(cfg["env_key"], "").strip()
                state = self._states.get(name, ProviderState(name=name))
                if not api_key:
                    status = "no key"
                elif state.disabled:
                    status = "disabled"
                elif state.cooldown_until > now:
                    left = int(state.cooldown_until - now)
                    status = f"cooldown {left}s"
                else:
                    status = "ok"

                summary[name] = {
                    "status": status,
                    "calls_today": state.calls_today,
                    "last_error": state.last_error_class or "none",
                }
        return summary


# Global registry singleton
registry = ProviderRegistry()


def call_chat_completions(
    provider_name: str,
    messages: list[dict],
    json_mode: bool = True,
    timeout: float = 6.0,
    temperature: float = 0.0,
    max_tokens: int = 200,
) -> str:
    """
    Call OpenAI-compatible chat completions endpoint with requests.
    Returns the string text inside choices[0].message.content.
    Raises AuthError, RateLimitError, or ServerOrNetworkError.
    """
    cfg = PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider_name}")

    api_key = os.environ.get(cfg["env_key"], "").strip()
    if not api_key:
        raise AuthError(f"Missing API key for {provider_name}")

    model = os.environ.get(cfg["env_model"], cfg["default_model"]).strip()
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "TaleemGo-AI/1.0",
    }
    # OpenRouter specific headers
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/alinawazcode/Saathi"
        headers["X-Title"] = "TaleemGo AI"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
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

    if status in (401, 403):
        registry.record_auth_error(provider_name)
        raise AuthError(f"Authentication failed for {provider_name} (HTTP {status})")

    if status == 429:
        retry_after = None
        ra_header = r.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = float(ra_header)
            except ValueError:
                pass
        registry.record_429(provider_name, retry_after)
        raise RateLimitError(f"{provider_name} rate limit exceeded", retry_after=retry_after)

    if status >= 500:
        registry.record_5xx_or_network(provider_name, f"HTTP{status}")
        raise ServerOrNetworkError(f"{provider_name} server error HTTP {status}")

    if status >= 400:
        # Client error (e.g. 400 bad request)
        registry.record_5xx_or_network(provider_name, f"HTTP{status}")
        raise ServerOrNetworkError(f"{provider_name} returned client error HTTP {status}")

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        registry.record_success(provider_name)
        return content
    except Exception as e:
        registry.record_5xx_or_network(provider_name, "InvalidResponseFormat")
        raise ServerOrNetworkError(f"Malformed response payload from {provider_name}") from e
