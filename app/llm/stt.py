"""
stt.py - Speech-to-text transcription with Groq Whisper and Gemini fallback.

Privacy rules (non-negotiable):
- Audio is passed as base64 bytes, never written to disk.
- The transcript is NEVER logged. Only provider name, latency, and duration are logged.
- Everywhere in stt.py, log only the exception CLASS NAME, never str(e), so no URL or key reaches logs.
- Gemini API key is sent via x-goog-api-key header, NEVER in the URL.
- Returns str transcript or None on failure.

STT providers verified 2026-11-15:
- Groq: POST https://api.groq.com/openai/v1/audio/transcriptions
  Models: whisper-large-v3-turbo, whisper-large-v3
  Docs: https://console.groq.com/docs/speech-text
- Gemini: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
  Header: x-goog-api-key: {key}
  Models: gemini-3.5-flash-lite, gemini-3.8-flash, gemini-3.6-flash
  Docs: https://ai.google.dev/gemini-api/docs/audio & https://ai.google.dev/gemini-api/docs/models/gemini
"""
import base64
import io
import logging
import os
import threading
import time
import wave
from typing import Optional

import requests

log = logging.getLogger("taleemgo.stt")

# Groq Whisper endpoint
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Config table of supported STT providers
# Model IDs verified on 2026-11-15 against official provider documentation
STT_PROVIDER_CONFIGS = {
    "groq": {
        "env_key": "GROQ_API_KEY",
        "env_models": "GROQ_STT_MODELS",
        # Verified 2026-11-15: whisper-large-v3-turbo, whisper-large-v3
        # Docs: https://console.groq.com/docs/speech-text
        "default_models": ["whisper-large-v3-turbo", "whisper-large-v3"],
    },
    "gemini": {
        "env_key": "GEMINI_API_KEY",
        "env_models": "GEMINI_STT_MODELS",
        # Verified 2026-11-15: gemini-3.5-flash-lite, gemini-3.8-flash, gemini-3.6-flash
        # Docs: https://ai.google.dev/gemini-api/docs/audio & https://ai.google.dev/gemini-api/docs/models/gemini
        "default_models": ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash"],
    },
}

# Language codes supported by Whisper for language-hint parameter
_WHISPER_LANG_MAP = {
    "en": "en",
    "ur": "ur",
    "sd": "sd",
    "hi": "hi",
    "ur-roman": "ur",
    "sd-roman": "sd",
}

# Supported mime-type to file extension mapping for Groq multipart upload
_MIME_TO_EXT = {
    "audio/ogg": "ogg",
    "audio/ogg; codecs=opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/m4a": "m4a",
}

TOTAL_STT_BUDGET = 15.0
PER_PROVIDER_TIMEOUT = 10.0

# --- Thread-safe cooldown registry for STT providers ---

_stt_registry_lock = threading.Lock()
_stt_registry: dict = {}  # provider -> dict of state


def is_valid_api_key(key: Optional[str]) -> bool:
    """Treat an env key as missing when empty, shorter than 20 chars, or containing placeholder markers."""
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


def _stt_is_available(provider: str) -> bool:
    with _stt_registry_lock:
        state = _stt_registry.get(provider, {})
    if state.get("disabled"):
        return False
    if time.time() < state.get("cooldown_until", 0.0):
        return False
    return True


def _stt_record_success(provider: str, model: Optional[str] = None):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["cooldown_until"] = 0.0
        state["calls_today"] = state.get("calls_today", 0) + 1
        state["last_error"] = None
        if model:
            state["active_model"] = model


def _stt_record_cooldown(provider: str, seconds: float, err_class: str = "Error"):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["cooldown_until"] = time.time() + seconds
        state["calls_today"] = state.get("calls_today", 0) + 1
        state["last_error"] = err_class
    log.warning("[STT] %s in cooldown for %.1fs (%s)", provider, seconds, err_class)


def _stt_disable(provider: str, reason: str):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["disabled"] = True
        state["status_override"] = reason
        state["last_error"] = "AuthError"
    log.error("[STT] %s disabled: %s", provider, reason)


def _stt_record_bad_model(provider: str, model: str):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["last_error"] = "BadModel"
        state["status_override"] = "bad model"
    log.warning("[STT] %s bad model: %s", provider, model)


def get_configured_stt_providers() -> list[str]:
    try:
        from .capacity import get_stt_providers
        return get_stt_providers()
    except ImportError:
        try:
            from app.llm.capacity import get_stt_providers
            return get_stt_providers()
        except ImportError:
            raw = os.environ.get("STT_PROVIDERS", "groq,gemini")
            return [p.strip().lower() for p in raw.split(",") if p.strip()]


_get_stt_providers = get_configured_stt_providers


def _get_provider_models(provider_name: str) -> list[str]:
    cfg = STT_PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        return []
    raw = os.environ.get(cfg["env_models"], "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(cfg["default_models"])


def _mime_base(mime: str) -> str:
    """Return mime type without codec suffixes."""
    return mime.split(";")[0].strip().lower()


def _ext_for_mime(mime: str) -> str:
    base = _mime_base(mime)
    return _MIME_TO_EXT.get(mime.lower(), _MIME_TO_EXT.get(base, "ogg"))


def generate_silent_wav(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Generate 1 second of silent 16-bit mono PCM WAV audio in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration_seconds))
    return buf.getvalue()


# --- Groq Whisper provider ---

def _transcribe_groq(audio_bytes: bytes, mime: str, seconds: float, lang_hint: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not is_valid_api_key(api_key):
        return None

    models = _get_provider_models("groq")
    ext = _ext_for_mime(mime)
    filename = f"audio.{ext}"

    # Map stored language to Whisper language code
    whisper_lang = _WHISPER_LANG_MAP.get(lang_hint, None) if lang_hint else None

    for model in models:
        try:
            files = {
                "file": (filename, io.BytesIO(audio_bytes), mime.split(";")[0].strip()),
            }
            data = {"model": model}
            if whisper_lang:
                data["language"] = whisper_lang

            start = time.time()
            resp = requests.post(
                GROQ_STT_URL,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                files=files,
                data=data,
                timeout=PER_PROVIDER_TIMEOUT,
            )
            latency = time.time() - start

            if resp.status_code == 200:
                result = resp.json()
                transcript = result.get("text", "").strip()
                _stt_record_success("groq", model)
                log.info("[STT] groq ok model=%s latency=%.2fs seconds=%.1f", model, latency, seconds)
                return transcript if transcript else None

            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "60"))
                _stt_record_cooldown("groq", min(retry_after, 300.0), err_class="RateLimit429")
                return None

            elif resp.status_code in (401, 403):
                _stt_disable("groq", f"bad key HTTP {resp.status_code}")
                return None

            elif resp.status_code == 404 or (resp.status_code in (400, 422) and any(
                p in resp.text.lower() for p in ("not found", "does not exist", "not supported", "decommissioned", "unknown model", "invalid model")
            )):
                _stt_record_bad_model("groq", model)
                continue

            else:
                _stt_record_cooldown("groq", 30.0, err_class=f"HTTP{resp.status_code}")
                return None

        except requests.Timeout:
            log.warning("[STT] groq timeout model=%s", model)
            _stt_record_cooldown("groq", 30.0, err_class="Timeout")
            return None
        except requests.ConnectionError:
            log.warning("[STT] groq connection error model=%s", model)
            _stt_record_cooldown("groq", 30.0, err_class="ConnectionError")
            return None
        except Exception as e:
            log.warning("[STT] groq error %s model=%s", type(e).__name__, model)
            _stt_record_cooldown("groq", 30.0, err_class=type(e).__name__)
            return None

    # All models exhausted
    log.warning("[STT] groq all models exhausted")
    _stt_record_cooldown("groq", 60.0, err_class="BadModel")
    return None


# --- Gemini audio provider ---

def _transcribe_gemini(audio_bytes: bytes, mime: str, seconds: float, lang_hint: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not is_valid_api_key(api_key):
        return None

    models = _get_provider_models("gemini")

    # Build language hint text
    lang_text = ""
    if lang_hint in ("ur", "ur-roman"):
        lang_text = " The audio is in Urdu or Roman Urdu."
    elif lang_hint in ("sd", "sd-roman"):
        lang_text = " The audio is in Sindhi or Roman Sindhi."
    elif lang_hint == "en":
        lang_text = " The audio is in English."

    for model in models:
        try:
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            mime_base = _mime_base(mime)

            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_base,
                                "data": audio_b64,
                            }
                        },
                        {
                            "text": f"Transcribe this audio message exactly as spoken. Return only the transcript text, nothing else.{lang_text}"
                        }
                    ]
                }]
            }

            start = time.time()
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": api_key.strip(),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=PER_PROVIDER_TIMEOUT,
            )
            latency = time.time() - start

            if resp.status_code == 200:
                data = resp.json()
                try:
                    transcript = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    _stt_record_success("gemini", model)
                    log.info("[STT] gemini ok model=%s latency=%.2fs seconds=%.1f", model, latency, seconds)
                    return transcript if transcript else None
                except (KeyError, IndexError):
                    log.warning("[STT] gemini unexpected response structure model=%s", model)
                    continue

            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "60"))
                _stt_record_cooldown("gemini", min(retry_after, 300.0), err_class="RateLimit429")
                return None

            elif resp.status_code in (401, 403):
                _stt_disable("gemini", f"bad key HTTP {resp.status_code}")
                return None

            elif resp.status_code == 404 or (resp.status_code in (400, 422) and any(
                p in resp.text.lower() for p in ("not found", "does not exist", "not supported", "decommissioned")
            )):
                _stt_record_bad_model("gemini", model)
                continue

            else:
                _stt_record_cooldown("gemini", 30.0, err_class=f"HTTP{resp.status_code}")
                return None

        except requests.Timeout:
            log.warning("[STT] gemini timeout model=%s", model)
            _stt_record_cooldown("gemini", 30.0, err_class="Timeout")
            return None
        except requests.ConnectionError:
            log.warning("[STT] gemini connection error model=%s", model)
            _stt_record_cooldown("gemini", 30.0, err_class="ConnectionError")
            return None
        except Exception as e:
            log.warning("[STT] gemini error %s model=%s", type(e).__name__, model)
            _stt_record_cooldown("gemini", 30.0, err_class=type(e).__name__)
            return None

    log.warning("[STT] gemini all models exhausted")
    _stt_record_cooldown("gemini", 60.0, err_class="BadModel")
    return None


_PROVIDER_FUNCS = {
    "groq": _transcribe_groq,
    "gemini": _transcribe_gemini,
}


def transcribe(audio_b64: str, mime: str, seconds: float, lang_hint: Optional[str] = None) -> Optional[str]:
    """
    Transcribe audio (base64-encoded) using configured STT providers.
    Returns transcript string or None if all providers fail or return empty.
    Audio bytes are NEVER written to disk. Transcript is NEVER logged.
    """
    providers = _get_stt_providers()
    if not providers:
        return None

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as e:
        log.warning("[STT] failed to decode base64 audio: %s", type(e).__name__)
        return None

    deadline = time.time() + TOTAL_STT_BUDGET

    for provider in providers:
        if time.time() >= deadline:
            log.warning("[STT] time budget exhausted before %s", provider)
            break
        if not _stt_is_available(provider):
            continue
        func = _PROVIDER_FUNCS.get(provider)
        if not func:
            continue
        try:
            result = func(audio_bytes, mime, seconds, lang_hint)
            if result:
                return result
        except Exception as e:
            log.warning("[STT] provider %s raised %s", provider, type(e).__name__)

    return None


def run_live_check(provider_name: str) -> tuple[str, str, float]:
    """
    Test STT provider with a 1 second generated silent WAV.
    Returns (status, model_id, latency_ms).
    status is one of: 'ok', 'bad model', 'bad key', 'rate limited', 'down', 'no key'.
    Never raises, never logs keys or URLs.
    """
    cfg = STT_PROVIDER_CONFIGS.get(provider_name)
    if not cfg:
        return "down", "unknown", 0.0

    raw_key = os.environ.get(cfg["env_key"], "")
    models = _get_provider_models(provider_name)
    primary_model = models[0] if models else "none"

    if not is_valid_api_key(raw_key):
        return "no key", primary_model, 0.0

    silent_wav = generate_silent_wav(1.0)
    start_t = time.time()

    try:
        if provider_name == "groq":
            files = {
                "file": ("silent.wav", io.BytesIO(silent_wav), "audio/wav"),
            }
            data = {"model": primary_model}
            resp = requests.post(
                GROQ_STT_URL,
                headers={"Authorization": f"Bearer {raw_key.strip()}"},
                files=files,
                data=data,
                timeout=PER_PROVIDER_TIMEOUT,
            )
        elif provider_name == "gemini":
            audio_b64 = base64.b64encode(silent_wav).decode("ascii")
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": audio_b64,
                            }
                        },
                        {
                            "text": "Transcribe this audio"
                        }
                    ]
                }]
            }
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{primary_model}:generateContent",
                headers={
                    "x-goog-api-key": raw_key.strip(),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=PER_PROVIDER_TIMEOUT,
            )
        else:
            return "down", primary_model, 0.0

        latency = (time.time() - start_t) * 1000

        if resp.status_code == 200:
            _stt_record_success(provider_name, primary_model)
            return "ok", primary_model, latency
        elif resp.status_code in (401, 403):
            _stt_disable(provider_name, "bad key")
            return "bad key", primary_model, 0.0
        elif resp.status_code == 429:
            _stt_record_cooldown(provider_name, 60.0, err_class="RateLimit429")
            return "rate limited", primary_model, 0.0
        elif resp.status_code == 404 or (resp.status_code in (400, 422) and any(
            p in resp.text.lower() for p in ("not found", "does not exist", "not supported", "decommissioned")
        )):
            _stt_record_bad_model(provider_name, primary_model)
            return "bad model", primary_model, 0.0
        else:
            _stt_record_cooldown(provider_name, 30.0, err_class="ServerOrNetworkError")
            return "down", primary_model, 0.0

    except requests.Timeout:
        log.warning("[STT] %s live check timeout model=%s", provider_name, primary_model)
        _stt_record_cooldown(provider_name, 30.0, err_class="Timeout")
        return "down", primary_model, 0.0
    except requests.ConnectionError:
        log.warning("[STT] %s live check connection error model=%s", provider_name, primary_model)
        _stt_record_cooldown(provider_name, 30.0, err_class="ConnectionError")
        return "down", primary_model, 0.0
    except Exception as e:
        log.warning("[STT] %s live check error %s model=%s", provider_name, type(e).__name__, primary_model)
        _stt_record_cooldown(provider_name, 30.0, err_class=type(e).__name__)
        return "down", primary_model, 0.0


def get_summary() -> dict:
    """Returns STT provider status summary with model IDs and status words for admin command."""
    now = time.time()
    summary = {}
    with _stt_registry_lock:
        for name in _get_stt_providers():
            cfg = STT_PROVIDER_CONFIGS.get(name, {})
            env_key = cfg.get("env_key", "")
            raw_key = os.environ.get(env_key, "")
            state = _stt_registry.get(name, {})
            if not is_valid_api_key(raw_key):
                status = "no key"
            elif state.get("disabled"):
                status = state.get("status_override", "disabled")
            elif state.get("cooldown_until", 0.0) > now:
                left = int(state["cooldown_until"] - now)
                if state.get("last_error") == "RateLimit429":
                    status = f"rate limited ({left}s)"
                else:
                    status = f"cooldown {left}s"
            else:
                status = "ok"

            models = _get_provider_models(name)
            active = state.get("active_model") or (models[0] if models else "none")

            summary[name] = {
                "status": status,
                "model": active,
                "calls_today": state.get("calls_today", 0),
                "last_error": state.get("last_error") or "none",
            }
    return summary
