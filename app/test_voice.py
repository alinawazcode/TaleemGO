"""
test_voice.py - Hermetic tests for Phase 3: Voice note transcription.

All tests use mocks. No network calls, no disk writes beyond existing test infrastructure.
"""
import base64
import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Bootstrap: make sure app/ modules import correctly from tests run from repo root
# ---------------------------------------------------------------------------
APP_DIR = os.path.join(os.path.dirname(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Minimal stubs for modules that require DB / network on import
def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod

# ---------------------------------------------------------------------------
# Helpers used across tests
# ---------------------------------------------------------------------------

FAKE_AUDIO_B64 = base64.b64encode(b"\xff\xfb" * 100).decode("ascii")  # ~200 raw bytes
FAKE_MIME = "audio/ogg"
FAKE_SECONDS = 5.0
FAKE_CHAT = "123456789@c.us"
FAKE_TRANSCRIPT = "Show my attendance"

VALID_GROQ_KEY = "gsk_live_abcdef1234567890abcdef1234567890"
VALID_GEMINI_KEY = "AIzaSyLive_abcdef1234567890abcdef123456"


def _make_audio_data(mime=FAKE_MIME, data_b64=FAKE_AUDIO_B64, seconds=FAKE_SECONDS):
    return {"mime": mime, "data_b64": data_b64, "seconds": seconds}


def _active_user(lang="en", is_admin=False, status="active"):
    return {"chat_id": FAKE_CHAT, "status": status, "lang": lang,
            "is_admin": 1 if is_admin else 0, "llm_locked_until": None}


# ---------------------------------------------------------------------------
# Test: voice accepted for active user — STT called, "I heard:" prepended
# ---------------------------------------------------------------------------

class TestVoiceAcceptedActiveUser(unittest.TestCase):
    def test_voice_accepted_active_user(self):
        import users as usr
        import wa_bot

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value=FAKE_TRANSCRIPT), \
             patch.object(wa_bot, "_reply") as mock_reply, \
             patch.object(wa_bot, "_handle_active") as mock_handle_active:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        # First reply should be "I heard:"
        self.assertTrue(mock_reply.call_count >= 1)
        first_call_text = mock_reply.call_args_list[0][0][1]
        self.assertIn("I heard", first_call_text)
        self.assertIn(FAKE_TRANSCRIPT, first_call_text)
        # _handle_active should be called with transcript
        mock_handle_active.assert_called_once()
        args = mock_handle_active.call_args[0]
        self.assertEqual(args[0], FAKE_CHAT)
        self.assertIn(FAKE_TRANSCRIPT, args[1])


class TestVoiceRejectedAwaitingEmail(unittest.TestCase):
    def test_voice_rejected_awaiting_email(self):
        import users as usr
        import wa_bot

        user = _active_user(status="awaiting_email")

        stt_called = []
        def fake_stt(*args, **kwargs):
            stt_called.append(True)
            return "transcript"

        with patch.object(usr, "get_user", return_value=user), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=fake_stt), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(len(stt_called), 0, "STT must NOT be called during sign-up")
        self.assertEqual(mock_reply.call_count, 1)
        reply_text = mock_reply.call_args[0][1]
        self.assertIn("voice", reply_text.lower())


class TestVoiceRejectedAwaitingPassword(unittest.TestCase):
    def test_voice_rejected_awaiting_password(self):
        import users as usr
        import wa_bot

        user = _active_user(status="awaiting_password")

        stt_called = []
        with patch.object(usr, "get_user", return_value=user), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(len(stt_called), 0, "STT must NOT be called during awaiting_password")
        self.assertEqual(mock_reply.call_count, 1)


class TestVoiceIgnoredUnknownChat(unittest.TestCase):
    def test_voice_ignored_unknown_chat(self):
        import users as usr
        import wa_bot

        stt_called = []
        with patch.object(usr, "get_user", return_value=None), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(len(stt_called), 0)
        self.assertEqual(mock_reply.call_count, 0, "No reply to unknown user")


class TestVoiceLockedOut(unittest.TestCase):
    def test_voice_locked_out_user(self):
        import users as usr
        import wa_bot

        stt_called = []
        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(len(stt_called), 0, "STT must NOT be called for locked user")
        self.assertEqual(mock_reply.call_count, 0, "No reply to locked user")


class TestVoiceTooLong(unittest.TestCase):
    def test_voice_too_long(self):
        import users as usr
        import wa_bot

        # Set VOICE_MAX_SECONDS to 30
        os.environ.setdefault("VOICE_MAX_SECONDS", "30")
        wa_bot.VOICE_MAX_SECONDS = 30

        stt_called = []
        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(seconds=45.0), is_self=False)

        self.assertEqual(len(stt_called), 0)
        self.assertEqual(mock_reply.call_count, 1)
        self.assertIn("long", mock_reply.call_args[0][1].lower())


class TestVoiceWrongMime(unittest.TestCase):
    def test_voice_wrong_mime(self):
        import users as usr
        import wa_bot

        stt_called = []
        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(mime="image/jpeg"), is_self=False)

        self.assertEqual(len(stt_called), 0)
        self.assertEqual(mock_reply.call_count, 1)


class TestVoiceHourlyLimit(unittest.TestCase):
    def test_voice_hourly_limit(self):
        import users as usr
        import wa_bot

        stt_called = []
        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=False), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(len(stt_called), 0)
        self.assertEqual(mock_reply.call_count, 1)
        self.assertIn("voice", mock_reply.call_args[0][1].lower())


class TestVoiceEmptyTranscript(unittest.TestCase):
    def test_voice_empty_transcript(self):
        import users as usr
        import wa_bot

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value=""), \
             patch.object(wa_bot, "_reply") as mock_reply, \
             patch.object(wa_bot, "_handle_active") as mock_active:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(mock_active.call_count, 0)
        self.assertEqual(mock_reply.call_count, 1)
        reply_text = mock_reply.call_args[0][1]
        self.assertTrue("make out" in reply_text.lower() or "understand" in reply_text.lower())


class TestVoiceTooLarge(unittest.TestCase):
    def test_voice_too_large(self):
        import users as usr
        import wa_bot

        wa_bot.VOICE_MAX_BYTES = 2000000

        # Create large base64 payload (> 2MB raw bytes)
        large_b64 = "A" * (2800000)

        stt_called = []
        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", side_effect=lambda *a, **k: stt_called.append(True) or "x"), \
             patch.object(wa_bot, "_reply") as mock_reply:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(data_b64=large_b64), is_self=False)

        self.assertEqual(len(stt_called), 0)
        self.assertEqual(mock_reply.call_count, 1)
        self.assertIn("large", mock_reply.call_args[0][1].lower())


class TestVoiceNoneTranscript(unittest.TestCase):
    def test_voice_none_transcript(self):
        import users as usr
        import wa_bot

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value=None), \
             patch.object(wa_bot, "_reply") as mock_reply, \
             patch.object(wa_bot, "_handle_active") as mock_active:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        self.assertEqual(mock_active.call_count, 0)
        self.assertEqual(mock_reply.call_count, 1)


# --- STT unit tests ---

class TestSTTGroqOk(unittest.TestCase):
    def test_groq_transcribes_ogg(self):
        """Groq provider returns transcript for OGG audio."""
        import llm.stt as stt
        # Reset cooldowns
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"text": "My attendance please"}

        with patch.dict(os.environ, {"GROQ_API_KEY": VALID_GROQ_KEY, "GROQ_STT_MODELS": "whisper-large-v3-turbo"}), \
             patch("llm.stt.requests.post", return_value=fake_resp):
            result = stt._transcribe_groq(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")

        self.assertEqual(result, "My attendance please")


class TestSTTGroqRateLimit(unittest.TestCase):
    def test_groq_429_sets_cooldown(self):
        """429 from Groq sets cooldown; provider becomes unavailable."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 429
        fake_resp.headers = {"retry-after": "60"}

        with patch.dict(os.environ, {"GROQ_API_KEY": VALID_GROQ_KEY, "GROQ_STT_MODELS": "whisper-large-v3-turbo"}), \
             patch("llm.stt.requests.post", return_value=fake_resp):
            result = stt._transcribe_groq(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")

        self.assertIsNone(result)
        self.assertFalse(stt._stt_is_available("groq"))


class TestSTTGroqBadKey(unittest.TestCase):
    def test_groq_bad_key_disables(self):
        """401 from Groq disables the provider."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 401

        with patch.dict(os.environ, {"GROQ_API_KEY": VALID_GROQ_KEY, "GROQ_STT_MODELS": "whisper-large-v3-turbo"}), \
             patch("llm.stt.requests.post", return_value=fake_resp):
            result = stt._transcribe_groq(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")

        self.assertIsNone(result)
        with stt._stt_registry_lock:
            self.assertTrue(stt._stt_registry.get("groq", {}).get("disabled"))


class TestSTTGeminiOk(unittest.TestCase):
    def test_gemini_transcribes(self):
        """Gemini provider returns transcript and sends key in x-goog-api-key header."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Hello world"}]}}]
        }

        test_key = VALID_GEMINI_KEY
        with patch.dict(os.environ, {"GEMINI_API_KEY": test_key, "GEMINI_STT_MODELS": "gemini-3.5-flash-lite"}), \
             patch("llm.stt.requests.post", return_value=fake_resp) as mock_post:
            result = stt._transcribe_gemini(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")

        self.assertEqual(result, "Hello world")
        called_url = mock_post.call_args[0][0]
        called_kwargs = mock_post.call_args[1]
        self.assertNotIn("key=", called_url, "API key must not be passed in the URL")
        self.assertEqual(called_kwargs["headers"].get("x-goog-api-key"), test_key)


class TestSTTNoKeyInLogsOnConnectionError(unittest.TestCase):
    def test_key_not_in_logs_on_connection_error(self):
        """When connection error occurs, API key must never appear in logs."""
        import logging
        import requests
        import llm.stt as stt

        secret_key = "AIzaSySecretKeyNeverLogThis"
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        log_records = []
        class CapturingHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        handler = CapturingHandler()
        stt_log = logging.getLogger("taleemgo.stt")
        stt_log.addHandler(handler)
        old_level = stt_log.level
        stt_log.setLevel(logging.DEBUG)

        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": secret_key, "GEMINI_STT_MODELS": "gemini-3.5-flash-lite"}), \
                 patch("llm.stt.requests.post", side_effect=requests.ConnectionError("Failed to connect to host")):
                stt._transcribe_gemini(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")
        finally:
            stt_log.removeHandler(handler)
            stt_log.setLevel(old_level)

        self.assertTrue(len(log_records) > 0)
        for msg in log_records:
            self.assertNotIn(secret_key, msg, f"Secret key leaked in log message: {msg}")


class TestSTTNoProviders(unittest.TestCase):
    def test_no_providers_returns_none(self):
        """Empty STT_PROVIDERS → transcribe returns None."""
        import llm.stt as stt
        with patch.dict(os.environ, {"STT_PROVIDERS": ""}):
            result = stt.transcribe(FAKE_AUDIO_B64, FAKE_MIME, 3.0)
        self.assertIsNone(result)


class TestSTTFailover(unittest.TestCase):
    def test_failover_to_gemini_when_groq_unavailable(self):
        """When groq is in cooldown, Gemini is tried."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()
            stt._stt_registry["groq"] = {"cooldown_until": 9999999999.0}

        fake_gemini_resp = MagicMock()
        fake_gemini_resp.status_code = 200
        fake_gemini_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Failover works"}]}}]
        }

        with patch.dict(os.environ, {
            "STT_PROVIDERS": "groq,gemini",
            "GROQ_API_KEY": VALID_GROQ_KEY,
            "GEMINI_API_KEY": VALID_GEMINI_KEY,
            "GEMINI_STT_MODELS": "gemini-3.5-flash-lite",
        }), patch("llm.stt.requests.post", return_value=fake_gemini_resp):
            result = stt.transcribe(FAKE_AUDIO_B64, FAKE_MIME, 3.0)

        self.assertEqual(result, "Failover works")


class TestSTTModelFailover404(unittest.TestCase):
    def test_model_failover_on_404(self):
        """When the first model returns 404, Groq tries the second model."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.text = "Model not found"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"text": "Model fallback success"}

        with patch.dict(os.environ, {
            "GROQ_API_KEY": VALID_GROQ_KEY,
            "GROQ_STT_MODELS": "dead-model,whisper-large-v3-turbo",
        }), patch("llm.stt.requests.post", side_effect=[resp_404, resp_200]):
            result = stt._transcribe_groq(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")

        self.assertEqual(result, "Model fallback success")


class TestSTTNoAudioInLogs(unittest.TestCase):
    def test_no_audio_or_transcript_in_logs(self):
        """Log records must not contain base64 audio data or transcript text."""
        import logging
        import llm.stt as stt

        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        secret_transcript = "super_secret_medical_info_XYZ"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"text": secret_transcript}

        log_records = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        handler = CapturingHandler()
        stt_log = logging.getLogger("taleemgo.stt")
        stt_log.addHandler(handler)
        old_level = stt_log.level
        stt_log.setLevel(logging.DEBUG)

        try:
            with patch.dict(os.environ, {"GROQ_API_KEY": VALID_GROQ_KEY, "GROQ_STT_MODELS": "whisper-large-v3-turbo"}), \
                 patch("llm.stt.requests.post", return_value=fake_resp):
                stt._transcribe_groq(b"\xff\xfb" * 50, "audio/ogg", 3.0, "en")
        finally:
            stt_log.removeHandler(handler)
            stt_log.setLevel(old_level)

        for msg in log_records:
            self.assertNotIn(secret_transcript, msg, f"Transcript found in log: {msg}")
            self.assertNotIn(FAKE_AUDIO_B64[:20], msg, f"Audio b64 found in log: {msg}")


class TestVoiceNothingWrittenToDisk(unittest.TestCase):
    def test_nothing_written_to_disk(self):
        """Audio message handling must not create any files on disk."""
        import users as usr
        import wa_bot

        before_files = set()
        if os.path.exists(usr.USERS_DIR):
            for root, _, files in os.walk(usr.USERS_DIR):
                for f in files:
                    before_files.add(os.path.join(root, f))

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value=FAKE_TRANSCRIPT), \
             patch.object(wa_bot, "_reply"), \
             patch.object(wa_bot, "_handle_active"):

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        after_files = set()
        if os.path.exists(usr.USERS_DIR):
            for root, _, files in os.walk(usr.USERS_DIR):
                for f in files:
                    after_files.add(os.path.join(root, f))

        new_files = after_files - before_files
        self.assertEqual(len(new_files), 0, f"Unexpected files created: {new_files}")


class TestVoicePendingSelectionPreserved(unittest.TestCase):
    def test_pending_selection_preserved(self):
        """A voice note can answer an active pending selection (e.g. file choice)."""
        import users as usr
        import wa_bot

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value="2"), \
             patch.object(wa_bot, "_reply") as mock_reply, \
             patch.object(wa_bot, "_handle_active") as mock_active:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        mock_active.assert_called_once_with(FAKE_CHAT, "2", _active_user())


class TestVoicePendingActionPreserved(unittest.TestCase):
    def test_pending_action_preserved(self):
        """A voice note can provide course name for a pending action."""
        import users as usr
        import wa_bot

        with patch.object(usr, "get_user", return_value=_active_user()), \
             patch.object(usr, "upsert_user"), \
             patch.object(usr, "is_llm_locked", return_value=False), \
             patch.object(usr, "check_and_record_voice_limit", return_value=True), \
             patch.object(usr, "get_lock", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))), \
             patch.object(wa_bot, "stt_transcribe", return_value="Artificial Intelligence"), \
             patch.object(wa_bot, "_reply") as mock_reply, \
             patch.object(wa_bot, "_handle_active") as mock_active:

            wa_bot._handle_audio_locked(FAKE_CHAT, _make_audio_data(), is_self=False)

        mock_active.assert_called_once_with(FAKE_CHAT, "Artificial Intelligence", _active_user())


class TestNoLMSDataInSTT(unittest.TestCase):
    def test_stt_receives_no_lms_or_user_data(self):
        """STT provider calls only get audio data and language hint, no user credentials or LMS data."""
        import llm.stt as stt
        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        captured_requests = []
        def fake_post(url, *args, **kwargs):
            captured_requests.append({"url": url, "kwargs": kwargs})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"text": "hello"}
            return resp

        with patch.dict(os.environ, {
            "STT_PROVIDERS": "groq",
            "GROQ_API_KEY": VALID_GROQ_KEY,
            "GROQ_STT_MODELS": "whisper-large-v3-turbo",
        }), patch.object(stt.requests, "post", side_effect=fake_post):
            stt.transcribe(FAKE_AUDIO_B64, FAKE_MIME, 3.0, lang_hint="ur")

        self.assertEqual(len(captured_requests), 1)
        req = captured_requests[0]
        data = req["kwargs"].get("data", {})
        self.assertEqual(set(data.keys()), {"model", "language"})
        self.assertEqual(data["language"], "ur")


# --- Voice rate limit counter tests ---

class TestVoiceRateLimitCounter(unittest.TestCase):
    def test_rate_limit_allows_up_to_limit(self):
        """10 voice calls within hour all pass; 11th is rejected."""
        import users as usr
        test_chat = "rate_limit_test_chat_voice"
        usr.clear_voice_counts(test_chat)

        with patch.dict(os.environ, {"VOICE_LIMIT_PER_HOUR": "10"}):
            for i in range(10):
                result = usr.check_and_record_voice_limit(test_chat)
                self.assertTrue(result, f"Call {i+1} should be allowed")
            # 11th should be rejected
            result = usr.check_and_record_voice_limit(test_chat)
            self.assertFalse(result, "11th call should be rejected")

        usr.clear_voice_counts(test_chat)

    def test_purge_clears_voice_counts(self):
        """purge_user_data clears voice rate-limit counter."""
        import users as usr
        # Register a fake user so purge works (or just test clear_voice_counts directly)
        test_chat = "purge_voice_test_chat"
        usr.clear_voice_counts(test_chat)
        # Record some calls
        with patch.dict(os.environ, {"VOICE_LIMIT_PER_HOUR": "10"}):
            usr.check_and_record_voice_limit(test_chat)
            usr.check_and_record_voice_limit(test_chat)
        # Clear directly
        usr.clear_voice_counts(test_chat)
        with patch.dict(os.environ, {"VOICE_LIMIT_PER_HOUR": "10"}):
            result = usr.check_and_record_voice_limit(test_chat)
        self.assertTrue(result, "After clear, should be allowed")
        usr.clear_voice_counts(test_chat)


# --- lang_data voice strings tests ---

class TestVoiceStrings(unittest.TestCase):
    def test_all_languages_have_voice_keys(self):
        """en, ur, sd and ur-roman all have all voice_* keys."""
        try:
            from app.llm.lang_data import STRINGS
        except ImportError:
            from llm.lang_data import STRINGS

        voice_keys = [
            "voice_signup_rejected",
            "voice_too_long",
            "voice_too_large",
            "voice_rate_limit",
            "voice_wrong_mime",
            "voice_not_understood",
            "voice_heard",
        ]
        for lang in ("en", "ur", "sd", "ur-roman"):
            for key in voice_keys:
                self.assertIn(key, STRINGS.get(lang, {}),
                              f"Missing key '{key}' in STRINGS['{lang}']")

    def test_ur_roman_strings_are_latin_only(self):
        """ur-roman voice strings must contain only Latin characters (no Urdu/Arabic script)."""
        try:
            from app.llm.lang_data import STRINGS
        except ImportError:
            from llm.lang_data import STRINGS

        import re
        arabic_re = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
        ur_roman = STRINGS.get("ur-roman", {})
        voice_keys = [k for k in ur_roman if k.startswith("voice_")]
        self.assertTrue(len(voice_keys) > 0, "No voice keys found in ur-roman")
        for key in voice_keys:
            val = ur_roman[key]
            match = arabic_re.search(val)
            self.assertIsNone(match,
                f"ur-roman STRINGS['{key}'] contains Arabic/Urdu script: {val!r}")


class TestAdminLLMStatusAndTestSTT(unittest.TestCase):
    def test_llm_status_shows_stt_providers(self):
        """Admin running 'llm' displays STT providers section."""
        import wa_bot
        import users as usr
        import llm.stt as stt

        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        replies = []
        with patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)), \
             patch.dict(os.environ, {
                 "GROQ_API_KEY": VALID_GROQ_KEY,
                 "GEMINI_API_KEY": VALID_GEMINI_KEY,
                 "STT_PROVIDERS": "groq,gemini",
             }):
            wa_bot._handle_cmd_llm("admin@c.us")

        self.assertEqual(len(replies), 1)
        output = replies[0]
        self.assertIn("STT Providers:", output)
        self.assertIn("groq (STT):", output)
        self.assertIn("gemini (STT):", output)

    def test_llm_test_checks_stt_with_silent_wav(self):
        """Admin running 'llm test' checks STT with 1-second silent WAV and outputs result lines."""
        import wa_bot
        import llm.stt as stt

        with stt._stt_registry_lock:
            stt._stt_registry.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"text": ""}

        replies = []
        with patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)), \
             patch.dict(os.environ, {
                 "GROQ_API_KEY": VALID_GROQ_KEY,
                 "GEMINI_API_KEY": VALID_GEMINI_KEY,
                 "STT_PROVIDERS": "groq,gemini",
                 "GROQ_STT_MODELS": "whisper-large-v3-turbo",
                 "GEMINI_STT_MODELS": "gemini-3.5-flash-lite",
             }), \
             patch.object(stt.requests, "post", return_value=fake_resp):
            wa_bot._handle_cmd_llm_test("admin@c.us")

        self.assertEqual(len(replies), 1)
        output = replies[0]
        self.assertIn("TaleemGo AI LLM Test:", output)
        self.assertIn("groq (STT): ok", output)
        self.assertIn("gemini (STT): ok", output)
        self.assertIn("whisper-large-v3-turbo", output)
        self.assertIn("gemini-3.5-flash-lite", output)


class TestVoiceDownloadFailureHandling(unittest.TestCase):
    def test_download_failure_active_user_all_languages(self):
        """Active user receives localized voice_download_failed message."""
        import wa_bot
        import users as usr
        from llm.lang_data import STRINGS

        for lang in ("en", "ur", "sd", "ur-roman"):
            user_data = _active_user(lang=lang)
            replies = []
            with patch.object(usr, "get_user", return_value=user_data), \
                 patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)), \
                 patch.object(usr, "is_llm_locked", return_value=False):
                wa_bot.handle_audio_error(FAKE_CHAT)

            self.assertEqual(len(replies), 1, f"Expected 1 reply for lang={lang}")
            expected_msg = STRINGS[lang]["voice_download_failed"]
            self.assertEqual(replies[0], expected_msg, f"Mismatch for lang={lang}")

    def test_download_failure_unknown_user_ignored(self):
        """Unknown user is silently ignored on download failure."""
        import wa_bot
        import users as usr

        replies = []
        with patch.object(usr, "get_user", return_value=None), \
             patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)):
            wa_bot.handle_audio_error("unknown@c.us")

        self.assertEqual(len(replies), 0)

    def test_download_failure_signup_states_rejected(self):
        """User in signup receives voice_signup_rejected line."""
        import wa_bot
        import users as usr
        from llm.lang_data import STRINGS

        for st in ("awaiting_email", "awaiting_password"):
            user_data = {"chat_id": FAKE_CHAT, "status": st, "lang": "en", "is_admin": 0}
            replies = []
            with patch.object(usr, "get_user", return_value=user_data), \
                 patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)):
                wa_bot.handle_audio_error(FAKE_CHAT)

            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0], STRINGS["en"]["voice_signup_rejected"])

    def test_download_failure_locked_user_ignored(self):
        """Locked-out user is silently ignored."""
        import wa_bot
        import users as usr

        user_data = _active_user(lang="en")
        replies = []
        with patch.object(usr, "get_user", return_value=user_data), \
             patch.object(usr, "is_llm_locked", return_value=True), \
             patch.object(wa_bot, "_reply", side_effect=lambda cid, txt: replies.append(txt)):
            wa_bot.handle_audio_error(FAKE_CHAT)

        self.assertEqual(len(replies), 0)

    def test_voice_download_failed_strings_present_and_clean(self):
        """Ensure voice_download_failed exists in en, ur, sd, ur-roman and ur-roman has no Arabic script."""
        import re
        from llm.lang_data import STRINGS

        for lang in ("en", "ur", "sd", "ur-roman"):
            self.assertIn("voice_download_failed", STRINGS[lang])
            self.assertTrue(len(STRINGS[lang]["voice_download_failed"]) > 0)

        arabic_re = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
        ur_roman_str = STRINGS["ur-roman"]["voice_download_failed"]
        self.assertIsNone(arabic_re.search(ur_roman_str), f"ur-roman contains Arabic characters: {ur_roman_str}")

    def test_incoming_endpoint_with_audio_error(self):
        """POST /incoming with audio_error dispatches to handle_audio_error."""
        import wa_bot
        from starlette.testclient import TestClient

        client = TestClient(wa_bot.api)
        with patch.object(wa_bot, "handle_audio_error") as mock_err:
            resp = client.post(
                "/incoming",
                json={"chat_id": FAKE_CHAT, "audio_error": True},
                headers={"x-token": wa_bot.TOKEN},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True})
            mock_err.assert_called_once_with(FAKE_CHAT, False)


if __name__ == "__main__":
    unittest.main()
