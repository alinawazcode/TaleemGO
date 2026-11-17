"""
test_llm_400.py - Unit tests for HTTP 400 handling, minimal payload retry,
unsupported parameter memory, safe error redaction, empty message rejection,
and absence of global cooldown.
"""
import io
import json
import logging
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from app.llm.providers import (
    ClientError,
    ProviderRegistry,
    _extract_safe_400_reason,
    _is_auth_error,
    call_chat_completions,
    get_invalid_messages_count,
    record_invalid_message,
    registry,
    reset_invalid_messages_count,
    validate_chat_messages,
)
from app.llm.router import route_intent
from app.llm.rules import match_rules
import app.users as usr
import app.wa_bot as wa_bot


TEST_KEY_GEMINI = "AIzaSyD_test_key_gemini_long_enough_123456"
TEST_KEY_GROQ = "gsk_test_key_groq_long_enough_123456"


class TestLLM400Handling(unittest.TestCase):
    def setUp(self):
        reset_invalid_messages_count()
        with registry._lock:
            registry._states.clear()

    def tearDown(self):
        reset_invalid_messages_count()
        with registry._lock:
            registry._states.clear()

    @patch("requests.post")
    def test_400_first_attempt_success_on_minimal_retry(self, mock_post):
        """Initial HTTP 400 (e.g. json_validate_failed) retries with minimal payload and succeeds."""
        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = json.dumps({
            "error": {
                "message": "Failed to validate JSON. Please adjust your prompt.",
                "type": "invalid_request_error",
                "code": "json_validate_failed",
            }
        })

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": '{"lang": "en", "actions": [{"intent": "attendance"}]}'}}],
            "usage": {"total_tokens": 42},
        }

        mock_post.side_effect = [resp_400, resp_200]

        with patch.dict(os.environ, {"GROQ_API_KEY": TEST_KEY_GROQ}):
            content = call_chat_completions(
                "groq",
                messages=[{"role": "user", "content": "attendance"}],
                json_mode=True,
            )

        self.assertIn("attendance", content)
        self.assertEqual(mock_post.call_count, 2)

        # 2nd call was the minimal retry: verify no response_format and max_tokens=800
        retry_call_json = mock_post.call_args_list[1].kwargs.get("json", {})
        self.assertNotIn("response_format", retry_call_json)
        self.assertNotIn("reasoning_effort", retry_call_json)
        self.assertEqual(retry_call_json.get("max_tokens"), 800)

        # Verify state remembers that response_format is unsupported for this model
        state = registry.get_state("groq#1")
        self.assertFalse(state.supports_json_mode)
        active_model = state.active_model
        self.assertTrue(state.is_param_unsupported(active_model, "response_format"))
        # Key must remain available!
        self.assertTrue(registry.is_available("groq#1"))

    @patch("requests.post")
    def test_unsupported_params_remembered_on_subsequent_calls(self, mock_post):
        """Once unsupported param is recorded, subsequent requests do not send it."""
        state = registry.get_state("groq#1")
        model = "openai/gpt-oss-20b"
        state.active_model = model
        state.mark_unsupported_param(model, "response_format")
        state.supports_json_mode = False

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": '{"lang": "en", "actions": [{"intent": "courses"}]}'}}],
            "usage": {"total_tokens": 20},
        }
        mock_post.return_value = resp_200

        with patch.dict(os.environ, {"GROQ_API_KEY": TEST_KEY_GROQ}):
            content = call_chat_completions(
                "groq#1",
                messages=[{"role": "user", "content": "courses"}],
                json_mode=True,
            )

        self.assertIn("courses", content)
        self.assertEqual(mock_post.call_count, 1)
        sent_json = mock_post.call_args.kwargs.get("json", {})
        self.assertNotIn("response_format", sent_json)

    @patch("requests.post")
    def test_400_does_not_trigger_global_cooldown(self, mock_post):
        """A persistent 400 fails for that request only; key remains available without 60s cooldown."""
        resp_400_1 = MagicMock()
        resp_400_1.status_code = 400
        resp_400_1.text = '{"error": {"message": "Invalid request parameter value"}}'

        resp_400_2 = MagicMock()
        resp_400_2.status_code = 400
        resp_400_2.text = '{"error": {"message": "Invalid request parameter value on retry"}}'

        mock_post.side_effect = [resp_400_1, resp_400_2]

        with patch.dict(os.environ, {"GROQ_API_KEY": TEST_KEY_GROQ}):
            with self.assertRaises(ClientError):
                call_chat_completions(
                    "groq#1",
                    messages=[{"role": "user", "content": "bad query"}],
                )

        state = registry.get_state("groq#1")
        # Critical assertion: cooldown_until must NOT be in the future!
        self.assertLessEqual(state.cooldown_until, time.time())
        # Key must remain immediately available for other requests!
        self.assertTrue(registry.is_available("groq#1"))
        self.assertIn("Invalid request parameter value", state.last_400_reason)

    @patch("requests.post")
    def test_five_400s_within_two_minutes_triggers_short_cooldown(self, mock_post):
        """5 different 400 requests within 2 minutes trigger a 30s cooldown with 'rejecting requests'."""
        state = registry.get_state("groq#1")

        now = time.time()
        for i in range(4):
            is_rej = state.record_400(now=now + i)
            self.assertFalse(is_rej)

        # 5th 400 event within 2 minutes
        is_rej = state.record_400(now=now + 5)
        self.assertTrue(is_rej)
        self.assertEqual(state.status_override, "rejecting requests")
        self.assertGreater(state.cooldown_until, now + 25)

        with patch.dict(os.environ, {"GROQ_API_KEY": TEST_KEY_GROQ}):
            self.assertFalse(registry.is_available("groq#1"))
            summary = registry.get_summary()
            self.assertIn("rejecting requests", summary["groq#1"]["status"])

    def test_safe_logging_redacts_keys_emails_numbers(self):
        """_extract_safe_400_reason redacts API keys, emails, and numbers >= 6 digits, truncated to <= 200 chars."""
        raw_error = json.dumps({
            "error": {
                "message": (
                    "Failed request for user ali.nawaz@iba-suk.edu.pk using key "
                    "AIzaSyD_secret_token_123456789012345 with student ID 9876543210. "
                    + ("Very long explanation " * 15)
                )
            }
        })

        safe = _extract_safe_400_reason(raw_error)
        self.assertLessEqual(len(safe), 200)
        self.assertNotIn("ali.nawaz@iba-suk.edu.pk", safe)
        self.assertNotIn("AIzaSyD_secret_token_123456789012345", safe)
        self.assertNotIn("9876543210", safe)
        self.assertIn("[REDACTED_EMAIL]", safe)
        self.assertIn("[REDACTED_KEY]", safe)
        self.assertIn("[REDACTED_NUMBER]", safe)

    @patch("requests.post")
    def test_gemini_400_invalid_auth_key_treated_as_auth_error(self, mock_post):
        """Gemini returning HTTP 400 with 'Invalid Auth key.' is classified as AuthError, disabling key without 60s cooldown."""
        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = json.dumps({
            "error": {
                "code": 400,
                "message": "Invalid Auth key.",
                "status": "INVALID_ARGUMENT",
            }
        })
        mock_post.return_value = resp_400

        with patch.dict(os.environ, {"GEMINI_API_KEY": TEST_KEY_GEMINI}):
            with self.assertRaises(Exception):
                call_chat_completions("gemini#1", messages=[{"role": "user", "content": "hi"}])

        state = registry.get_state("gemini#1")
        self.assertTrue(state.disabled)
        self.assertEqual(state.status_override, "bad key")
        self.assertEqual(state.last_error_class, "AuthError")
        self.assertFalse(registry.is_available("gemini#1"))

    def test_validate_chat_messages_and_empty_message_rejection(self):
        """Empty or whitespace-only messages are rejected upfront and increment counter."""
        self.assertFalse(validate_chat_messages([]))
        self.assertFalse(validate_chat_messages([{"role": "user", "content": ""}]))
        self.assertFalse(validate_chat_messages([{"role": "user", "content": "   "}]))
        self.assertFalse(validate_chat_messages([{"role": "invalid_role", "content": "hello"}]))
        self.assertTrue(validate_chat_messages([{"role": "user", "content": "valid prompt"}]))

        # call_chat_completions upfront check
        with patch.dict(os.environ, {"GROQ_API_KEY": TEST_KEY_GROQ}):
            with self.assertRaises(ValueError):
                call_chat_completions("groq#1", messages=[{"role": "user", "content": " "}])
        self.assertGreaterEqual(get_invalid_messages_count(), 1)

        # route_intent upfront check
        count_before = get_invalid_messages_count()
        res = route_intent("   ")
        self.assertIsNone(res)
        self.assertEqual(get_invalid_messages_count(), count_before + 1)

    def test_admin_llm_shows_last_400_reason_and_invalid_counter(self):
        """Admin 'llm' output displays 'last 400: <reason>' and invalid message count."""
        state = registry.get_state("groq#1")
        state.last_400_reason = "json_validate_failed"
        record_invalid_message()
        record_invalid_message()

        with patch.dict(os.environ, {
            "GROQ_API_KEY": TEST_KEY_GROQ,
            "LLM_PROVIDERS": "groq",
        }):
            sent = []
            with patch.object(wa_bot, "_reply", side_effect=lambda cid, msg: sent.append(msg)), \
                 patch.object(wa_bot.usr, "count_locked_users", return_value=0):
                wa_bot._handle_cmd_llm("admin@c.us")

        self.assertEqual(len(sent), 1)
        out = sent[0]
        self.assertIn("last 400: json_validate_failed", out)
        self.assertIn("Invalid messages rejected: 2", out)

    def test_rules_safety_net_across_languages_when_llm_fails(self):
        """Offline rules handle queries across en, ur, ur-roman, sd when LLM fails or is disabled."""
        queries = [
            ("attendance", "attendance"),
            ("میری حاضری کتنی ہے", "attendance"),
            ("meri attendance kitni hai", "attendance"),
            ("منھنجي حاضري ڪيتري آهي", "attendance"),
            ("show my courses", "courses"),
            ("میرے کورسز دکھاؤ", "courses"),
            ("kors dekhain", "courses"),
            ("ڪورسز ڏيکاريو", "courses"),
        ]

        for text, expected_intent in queries:
            res = match_rules(text)
            self.assertIsNotNone(res, f"Rule match failed for query: {text}")
            actions = res.get("actions", [])
            self.assertTrue(any(a.get("intent") == expected_intent for a in actions),
                            f"Expected intent '{expected_intent}' not found for: {text}")


if __name__ == "__main__":
    unittest.main()
