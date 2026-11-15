"""
router.py - LLM provider failover orchestrator with time budgets and retries.
"""
import json
import logging
import os
import re
import time
from typing import Optional

try:
    from .intent import build_system_prompt, build_user_payload, validate_intent_response
    from .providers import (
        AuthError,
        BadModelError,
        ClientError,
        RateLimitError,
        ServerOrNetworkError,
        call_chat_completions,
        registry,
    )
    from .capacity import get_intent_providers, intent_cache, record_intent_handled
except ImportError:
    from llm.intent import build_system_prompt, build_user_payload, validate_intent_response
    from llm.providers import (
        AuthError,
        BadModelError,
        ClientError,
        RateLimitError,
        ServerOrNetworkError,
        call_chat_completions,
        registry,
    )
    from llm.capacity import get_intent_providers, intent_cache, record_intent_handled

log = logging.getLogger("taleemgo.llm.router")

DEFAULT_PROVIDERS = "gemini,groq"
TOTAL_TIME_BUDGET = 12.0
PER_REQUEST_TIMEOUT = 6.0


def get_configured_providers() -> list[str]:
    return get_intent_providers()


def is_llm_enabled() -> bool:
    val = os.environ.get("LLM_ENABLED", "1").strip().lower()
    return val in ("1", "true", "yes", "on")


def _extract_json_block(text: str) -> dict:
    """Extract and parse defensive first JSON object block from response text."""
    if not text:
        raise ValueError("Empty response from model")
    # Search for first {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidate = m.group(0)
    else:
        candidate = text
    return json.loads(candidate)


def route_intent(
    sanitized_text: str,
    has_pending_download: bool = False,
    stored_lang: str = "en",
    history: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    Query provider chain with fallback and 12s overall time budget.
    Returns validated intent dict or None if all providers fail.
    Checks intent cache first for zero-provider response.
    """
    if not is_llm_enabled():
        return None

    # Check intent cache (shared across users; 0 provider calls on hit)
    cached = intent_cache.get(sanitized_text, has_pending_download, stored_lang)
    if cached is not None:
        log.info("[CACHE] Intent cache hit lang=%s pending=%s", stored_lang, has_pending_download)
        record_intent_handled(is_rule_handled=False)
        return cached

    providers = get_configured_providers()
    deadline = time.time() + TOTAL_TIME_BUDGET

    system_prompt = build_system_prompt(
        user_text=sanitized_text,
        has_pending_download=has_pending_download,
        lang=stored_lang,
    )
    user_payload = build_user_payload(
        sanitized_text,
        has_pending_download=has_pending_download,
        stored_lang=stored_lang,
        history=history,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload},
    ]

    for prov in providers:
        time_left = deadline - time.time()
        if time_left <= 0.5:
            log.warning("Time budget exhausted before calling %s", prov)
            break

        if not registry.is_available(prov):
            continue

        req_timeout = min(PER_REQUEST_TIMEOUT, time_left)

        # Attempt up to 2 tries on the same provider (retry once on JSON/schema error)
        for attempt in (1, 2):
            time_left = deadline - time.time()
            if time_left <= 0.5:
                break
            req_timeout = min(PER_REQUEST_TIMEOUT, time_left)

            try:
                start_t = time.time()
                raw_output = call_chat_completions(
                    prov,
                    messages=messages,
                    json_mode=True,
                    timeout=req_timeout,
                    temperature=0.0,
                    max_tokens=150,
                    priority="high",
                )
                latency = time.time() - start_t

                parsed = _extract_json_block(raw_output)
                validated = validate_intent_response(parsed, has_pending_download=has_pending_download)
                intent_cache.put(sanitized_text, has_pending_download, stored_lang, validated)
                record_intent_handled(is_rule_handled=False)
                log.info("LLM success provider=%s latency=%.2fs off_topic=%s unsupported=%s",
                         prov, latency, validated.get("off_topic"), validated.get("unsupported"))
                return validated

            except (json.JSONDecodeError, ValueError) as err:
                log.warning("Provider %s attempt %d returned invalid JSON/schema: %s", prov, attempt, err)
                if attempt == 1:
                    time.sleep(0.2)
                    continue
                else:
                    registry.record_5xx_or_network(prov, "InvalidSchema")
                    break
            except BadModelError as err:
                log.warning("Provider %s bad model error: %s", prov, err)
                break
            except ClientError as err:
                log.warning("Provider %s client error: %s", prov, err)
                break
            except RateLimitError as err:
                log.warning("Provider %s rate limit: %s", prov, err)
                registry.record_429(prov, getattr(err, "retry_after", None))
                break
            except AuthError as err:
                log.warning("Provider %s auth error: %s", prov, err)
                registry.record_auth_error(prov)
                break
            except ServerOrNetworkError as err:
                log.warning("Provider %s network/server error: %s", prov, err)
                registry.record_5xx_or_network(prov, "ServerOrNetworkError")
                break
            except Exception as err:
                log.exception("Unexpected error calling provider %s: %s", prov, err)
                registry.record_5xx_or_network(prov, "UnexpectedError")
                break

    return None
