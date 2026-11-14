"""
stt.py - Speech-to-text transcription with Groq Whisper and Gemini fallback.

Privacy rules (non-negotiable):
- Audio is passed as base64 bytes, never written to disk.
- The transcript is NEVER logged. Only provider name, latency, and duration are logged.
- Returns str transcript or None on failure.

STT providers verified 2026-11-15:
- Groq: POST https://api.groq.com/openai/v1/audio/transcriptions
  Model: whisper-large-v3-turbo (supports ogg, mp3, mp4, wav, webm, flac, m4a)
  Docs: https://console.groq.com/docs/speech-text
- Gemini: POST via google-generativeai with inline audio data
  Model: gemini-1.5-flash (supports audio/ogg, audio/mp3, audio/wav, etc.)
  Docs: https://ai.google.dev/gemini-api/docs/audio
"""
import base64
import io
import logging
import os
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("taleemgo.stt")

# Groq Whisper endpoint
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

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
_stt_registry: dict = {}  # provider -> {"cooldown_until": float, "disabled": bool}


def _stt_is_available(provider: str) -> bool:
    with _stt_registry_lock:
        state = _stt_registry.get(provider, {})
    if state.get("disabled"):
        return False
    if time.time() < state.get("cooldown_until", 0.0):
        return False
    return True


def _stt_record_cooldown(provider: str, seconds: float):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["cooldown_until"] = time.time() + seconds
    log.warning("[STT] %s in cooldown for %.1fs", provider, seconds)


def _stt_disable(provider: str, reason: str):
    with _stt_registry_lock:
        state = _stt_registry.setdefault(provider, {})
        state["disabled"] = True
    log.error("[STT] %s disabled: %s", provider, reason)


def _get_stt_providers() -> list:
    raw = os.environ.get("STT_PROVIDERS", "groq,gemini")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _mime_base(mime: str) -> str:
    """Return mime type without codec suffixes."""
    return mime.split(";")[0].strip().lower()


def _ext_for_mime(mime: str) -> str:
    base = _mime_base(mime)
    return _MIME_TO_EXT.get(mime.lower(), _MIME_TO_EXT.get(base, "ogg"))


# --- Groq Whisper provider ---

def _transcribe_groq(audio_bytes: bytes, mime: str, seconds: float, lang_hint: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key or len(api_key.strip()) < 20:
        return None

    models_raw = os.environ.get("GROQ_STT_MODELS", "whisper-large-v3-turbo")
    models = [m.strip() for m in models_raw.split(",") if m.strip()]

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
                log.info("[STT] groq ok model=%s latency=%.2fs seconds=%.1f", model, latency, seconds)
                return transcript if transcript else None

            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "60"))
                _stt_record_cooldown("groq", min(retry_after, 300.0))
                return None

            elif resp.status_code in (401, 403):
                _stt_disable("groq", f"bad key HTTP {resp.status_code}")
                return None

            elif resp.status_code == 404 or resp.status_code in (400, 422):
                txt = resp.text.lower()
                if any(p in txt for p in ("not found", "does not exist", "not supported", "decommissioned", "unknown model", "invalid model")):
                    log.warning("[STT] groq model %s not found, trying next", model)
                    continue
                log.warning("[STT] groq client error %d for model %s", resp.status_code, model)
                _stt_record_cooldown("groq", 60.0)
                return None

            else:
                log.warning("[STT] groq HTTP %d for model %s", resp.status_code, model)
                _stt_record_cooldown("groq", 30.0)
                return None

        except requests.Timeout:
            log.warning("[STT] groq timeout model=%s", model)
            _stt_record_cooldown("groq", 30.0)
            return None
        except requests.ConnectionError:
            log.warning("[STT] groq connection error model=%s", model)
            _stt_record_cooldown("groq", 30.0)
            return None
        except Exception as e:
            log.warning("[STT] groq unexpected error model=%s: %s", model, e)
            _stt_record_cooldown("groq", 30.0)
            return None

    # All models exhausted
    log.warning("[STT] groq all models exhausted")
    _stt_record_cooldown("groq", 60.0)
    return None


# --- Gemini audio provider ---

def _transcribe_gemini(audio_bytes: bytes, mime: str, seconds: float, lang_hint: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or len(api_key.strip()) < 20:
        return None

    models_raw = os.environ.get("GEMINI_STT_MODELS", "gemini-1.5-flash")
    models = [m.strip() for m in models_raw.split(",") if m.strip()]

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
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key.strip()}",
                json=payload,
                timeout=PER_PROVIDER_TIMEOUT,
            )
            latency = time.time() - start

            if resp.status_code == 200:
                data = resp.json()
                try:
                    transcript = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    log.info("[STT] gemini ok model=%s latency=%.2fs seconds=%.1f", model, latency, seconds)
                    return transcript if transcript else None
                except (KeyError, IndexError):
                    log.warning("[STT] gemini unexpected response structure model=%s", model)
                    continue

            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "60"))
                _stt_record_cooldown("gemini", min(retry_after, 300.0))
                return None

            elif resp.status_code in (401, 403):
                _stt_disable("gemini", f"bad key HTTP {resp.status_code}")
                return None

            elif resp.status_code == 404 or resp.status_code in (400, 422):
                txt = resp.text.lower()
                if any(p in txt for p in ("not found", "does not exist", "not supported", "decommissioned")):
                    log.warning("[STT] gemini model %s not found, trying next", model)
                    continue
                log.warning("[STT] gemini client error %d for model %s", resp.status_code, model)
                _stt_record_cooldown("gemini", 60.0)
                return None

            else:
                log.warning("[STT] gemini HTTP %d for model %s", resp.status_code, model)
                _stt_record_cooldown("gemini", 30.0)
                return None

        except requests.Timeout:
            log.warning("[STT] gemini timeout model=%s", model)
            _stt_record_cooldown("gemini", 30.0)
            return None
        except requests.ConnectionError:
            log.warning("[STT] gemini connection error model=%s", model)
            _stt_record_cooldown("gemini", 30.0)
            return None
        except Exception as e:
            log.warning("[STT] gemini unexpected error model=%s: %s", model, e)
            _stt_record_cooldown("gemini", 30.0)
            return None

    log.warning("[STT] gemini all models exhausted")
    _stt_record_cooldown("gemini", 60.0)
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
    except Exception:
        log.warning("[STT] failed to decode base64 audio")
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
            log.warning("[STT] provider %s raised exception: %s", provider, e)

    return None
