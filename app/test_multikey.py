"""
test_multikey.py - Unit tests for multi-key API support, fair rotation, and ordered failover.
"""
import io
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.llm.capacity import (
    TokenAwareLimiter,
    generate_capacity_report,
    limiter,
)
from app.llm.providers import (
    AuthError,
    ProviderRegistry,
    RateLimitError,
    call_chat_completions,
    get_fair_key_order,
    get_key_for_instance,
    get_provider_key_instances,
    get_provider_keys,
    is_valid_api_key,
    registry,
    run_live_check,
)
from app.llm.router import route_intent
import app.users as usr
import app.wa_bot as wa_bot


VALID_KEY_1 = "valid_test_api_key_number_1_long_enough"
VALID_KEY_2 = "valid_test_api_key_number_2_long_enough"
VALID_KEY_3 = "valid_test_api_key_number_3_long_enough"
GROQ_KEY_1 = "gsk_valid_groq_key_number_1_long_enough"
GROQ_KEY_2 = "gsk_valid_groq_key_number_2_long_enough"


class TestMultiKeyEnvParsing(unittest.TestCase):
    def test_single_key_only_becomes_key_1(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": "",
        }, clear=False):
            keys = get_provider_keys("gemini")
            self.assertEqual(keys, [VALID_KEY_1])
            instances = get_provider_key_instances("gemini")
            self.assertEqual(instances, ["gemini#1"])
            self.assertEqual(get_key_for_instance("gemini#1"), VALID_KEY_1)

    def test_multi_key_comma_separated_with_single_key(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": f"{VALID_KEY_2},{VALID_KEY_3}",
        }, clear=False):
            keys = get_provider_keys("gemini")
            self.assertEqual(keys, [VALID_KEY_1, VALID_KEY_2, VALID_KEY_3])
            instances = get_provider_key_instances("gemini")
            self.assertEqual(instances, ["gemini#1", "gemini#2", "gemini#3"])
            self.assertEqual(get_key_for_instance("gemini#1"), VALID_KEY_1)
            self.assertEqual(get_key_for_instance("gemini#2"), VALID_KEY_2)
            self.assertEqual(get_key_for_instance("gemini#3"), VALID_KEY_3)

    def test_duplicate_and_blank_and_placeholder_filtering(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": f"{VALID_KEY_1}, , your_api_key_here, short, {VALID_KEY_2}, {VALID_KEY_2}",
        }, clear=False):
            keys = get_provider_keys("gemini")
            # Duplicates removed, blanks and invalid keys removed
            self.assertEqual(keys, [VALID_KEY_1, VALID_KEY_2])
            instances = get_provider_key_instances("gemini")
            self.assertEqual(instances, ["gemini#1", "gemini#2"])

    def test_groq_multi_keys(self):
        with patch.dict(os.environ, {
            "GROQ_API_KEY": GROQ_KEY_1,
            "GROQ_API_KEYS": GROQ_KEY_2,
        }, clear=False):
            keys = get_provider_keys("groq")
            self.assertEqual(keys, [GROQ_KEY_1, GROQ_KEY_2])
            instances = get_provider_key_instances("groq")
            self.assertEqual(instances, ["groq#1", "groq#2"])


class TestMultiKeyFailoverAndIsolation(unittest.TestCase):
    def setUp(self):
        with registry._lock:
            registry._states.clear()
        with limiter._lock:
            limiter._windows.clear()

    @patch("requests.post")
    def test_key_1_rate_limited_fails_over_to_key_2_without_error(self, mock_post):
        """When key 1 returns 429, key 2 is tried immediately and succeeds without returning 429."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "30"}
        resp_429.text = '{"error": {"code": 429, "message": "rate limit reached"}}'

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": '{"lang": "en", "actions": [{"intent": "attendance"}]}'}}],
            "usage": {"total_tokens": 100},
        }
        mock_post.side_effect = [resp_429, resp_200]

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
        }):
            content = call_chat_completions("gemini", messages=[{"role": "user", "content": "hi"}])
            self.assertIn("attendance", content)
            self.assertEqual(mock_post.call_count, 2)

            # Key 1 was sent first, then Key 2
            h1 = mock_post.call_args_list[0][1]["headers"]["Authorization"]
            h2 = mock_post.call_args_list[1][1]["headers"]["Authorization"]
            self.assertEqual(h1, f"Bearer {VALID_KEY_1}")
            self.assertEqual(h2, f"Bearer {VALID_KEY_2}")

            # Key 1 is now in cooldown, Key 2 is ok
            state1 = registry.get_state("gemini#1")
            state2 = registry.get_state("gemini#2")
            self.assertGreater(state1.cooldown_until, time.time())
            self.assertEqual(state2.consecutive_429, 0)
            self.assertEqual(state2.calls_today, 1)

    @patch("requests.post")
    def test_key_1_window_ceiling_reached_fails_over_to_key_2(self, mock_post):
        """When key 1 reaches RPM ceiling, limiter skips key 1 and uses key 2 without HTTP call."""
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 50},
        }
        mock_post.return_value = resp_200

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GEMINI_RPM": "1",
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
        }):
            # First call uses key 1 (reaches ceiling of 1 RPM)
            limiter.record_usage("gemini#1", "gemini-3.5-flash-lite", total_tokens=50)

            # Second call should skip key 1 and call key 2
            mock_post.reset_mock()
            call_chat_completions("gemini", messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(mock_post.call_count, 1)
            used_auth = mock_post.call_args[1]["headers"]["Authorization"]
            self.assertEqual(used_auth, f"Bearer {VALID_KEY_2}")

    @patch("requests.post")
    def test_all_gemini_keys_limited_falls_back_to_groq(self, mock_post):
        """When all Gemini keys return 429, route_intent falls back to Groq."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "60"}

        resp_groq = MagicMock()
        resp_groq.status_code = 200
        resp_groq.headers = {}
        resp_groq.json.return_value = {
            "choices": [{"message": {"content": '{"lang": "en", "actions": [{"intent": "grades"}], "clarify": null, "off_topic": false, "unsupported": false}'}}],
            "usage": {"total_tokens": 120},
        }
        # Gemini key 1 -> 429, Gemini key 2 -> 429, Groq key 1 -> 200
        mock_post.side_effect = [resp_429, resp_429, resp_groq]

        with patch.dict(os.environ, {
            "LLM_ENABLED": "1",
            "INTENT_PROVIDERS": "gemini,groq",
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GROQ_API_KEY": GROQ_KEY_1,
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
            "GROQ_MODELS": "openai/gpt-oss-20b",
        }):
            result = route_intent("show my grades")
            self.assertIsNotNone(result)
            self.assertEqual(result["actions"][0]["intent"], "grades")
            self.assertEqual(mock_post.call_count, 3)

            # Gemini keys are both in cooldown
            self.assertFalse(registry.is_available("gemini#1"))
            self.assertFalse(registry.is_available("gemini#2"))
            self.assertFalse(registry.is_available("gemini"))
            self.assertTrue(registry.is_available("groq#1"))

    @patch("requests.post")
    def test_bad_key_disabled_alone(self, mock_post):
        """When key 1 returns 401, only key 1 is disabled; key 2 continues working alone."""
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.text = '{"error": {"code": 401, "message": "API key expired"}}'

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 50},
        }
        mock_post.side_effect = [resp_401, resp_200]

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
        }):
            content = call_chat_completions("gemini", messages=[{"role": "user", "content": "test"}])
            self.assertEqual(content, "ok")

            # Check key 1 is disabled and status is 'bad key'
            state1 = registry.get_state("gemini#1")
            self.assertTrue(state1.disabled)
            self.assertEqual(state1.status_override, "bad key")

            # Key 2 is NOT disabled and remains available
            state2 = registry.get_state("gemini#2")
            self.assertFalse(state2.disabled)
            self.assertTrue(registry.is_available("gemini#2"))
            self.assertTrue(registry.is_available("gemini"))  # Base provider still available via key 2!

            # Subsequent call directly uses key 2 without trying key 1
            mock_post.reset_mock()
            mock_post.side_effect = None
            mock_post.return_value = resp_200

            call_chat_completions("gemini", messages=[{"role": "user", "content": "next"}])
            self.assertEqual(mock_post.call_count, 1)
            used_auth = mock_post.call_args[1]["headers"]["Authorization"]
            self.assertEqual(used_auth, f"Bearer {VALID_KEY_2}")


class TestFairSpreading(unittest.TestCase):
    def setUp(self):
        with registry._lock:
            registry._states.clear()
        with limiter._lock:
            limiter._windows.clear()

    @patch("requests.post")
    def test_fair_rotation_spreads_load_evenly(self, mock_post):
        """Calls alternate fairly between keys of the same provider based on least usage."""
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 50},
        }
        mock_post.return_value = resp_200

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
        }):
            # Call 1: Both have 0 usage -> Key 1 used
            call_chat_completions("gemini", messages=[{"role": "user", "content": "1"}])
            self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], f"Bearer {VALID_KEY_1}")

            # Call 2: Key 1 has 1 req, Key 2 has 0 -> Key 2 used!
            call_chat_completions("gemini", messages=[{"role": "user", "content": "2"}])
            self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], f"Bearer {VALID_KEY_2}")

            # Call 3: Both have 1 req -> Key 1 used (tie-break)
            call_chat_completions("gemini", messages=[{"role": "user", "content": "3"}])
            self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], f"Bearer {VALID_KEY_1}")

            # Call 4: Key 1 has 2 reqs, Key 2 has 1 -> Key 2 used!
            call_chat_completions("gemini", messages=[{"role": "user", "content": "4"}])
            self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], f"Bearer {VALID_KEY_2}")

            # Total calls today on each key
            self.assertEqual(registry.get_state("gemini#1").calls_today, 2)
            self.assertEqual(registry.get_state("gemini#2").calls_today, 2)


class TestKeyPrivacyInLogsAndTelemetry(unittest.TestCase):
    def setUp(self):
        with registry._lock:
            registry._states.clear()
        with limiter._lock:
            limiter._windows.clear()

    @patch("requests.post")
    def test_raw_keys_never_appear_in_logs(self, mock_post):
        """Assert that secret key strings never appear in captured log output."""
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.headers = {}
        resp_401.text = '{"error": "Unauthorized"}'
        mock_post.return_value = resp_401

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)

        try:
            with patch.dict(os.environ, {
                "GEMINI_API_KEY": VALID_KEY_1,
                "GEMINI_API_KEYS": VALID_KEY_2,
                "GEMINI_MODELS": "gemini-3.5-flash-lite",
            }):
                with self.assertRaises(AuthError):
                    call_chat_completions("gemini", messages=[{"role": "user", "content": "test"}])

            captured = log_capture.getvalue()
            # Assert raw keys NEVER appear
            self.assertNotIn(VALID_KEY_1, captured)
            self.assertNotIn(VALID_KEY_2, captured)
            # Assert sanitized instance names DO appear
            self.assertIn("gemini#1", captured)
            self.assertIn("gemini#2", captured)
        finally:
            root_logger.removeHandler(handler)

    def test_admin_llm_status_shows_keys_and_windows_per_key(self):
        """Admin 'llm' output shows 'gemini#1', 'gemini#2', usage %, and cooldown per key."""
        with registry._lock:
            registry._states.clear()

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GROQ_API_KEY": GROQ_KEY_1,
            "LLM_PROVIDERS": "gemini,groq",
        }):
            sent_replies = []
            with patch.object(wa_bot, "_reply", side_effect=lambda cid, msg: sent_replies.append(msg)), \
                 patch.object(wa_bot.usr, "count_locked_users", return_value=0):
                wa_bot._handle_cmd_llm("admin@c.us")

            self.assertEqual(len(sent_replies), 1)
            output = sent_replies[0]
            self.assertIn("gemini#1:", output)
            self.assertIn("gemini#2:", output)
            self.assertIn("groq#1:", output)
            self.assertNotIn(VALID_KEY_1, output)
            self.assertNotIn(VALID_KEY_2, output)

    @patch("requests.post")
    def test_admin_llm_test_checks_each_key_instance(self, mock_post):
        """Admin 'llm test' executes live check on each key instance (gemini#1, gemini#2)."""
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"total_tokens": 5},
        }
        mock_post.return_value = resp_200

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": VALID_KEY_2,
            "GROQ_API_KEY": GROQ_KEY_1,
            "INTENT_PROVIDERS": "gemini,groq",
            "GEMINI_MODELS": "gemini-3.5-flash-lite",
            "GROQ_MODELS": "openai/gpt-oss-20b",
        }):
            sent_replies = []
            with patch.object(wa_bot, "_reply", side_effect=lambda cid, msg: sent_replies.append(msg)):
                wa_bot._handle_cmd_llm_test("admin@c.us")

            self.assertEqual(len(sent_replies), 1)
            output = sent_replies[0]
            self.assertIn("- gemini#1: ok", output)
            self.assertIn("- gemini#2: ok", output)
            self.assertIn("- groq#1: ok", output)
            self.assertNotIn(VALID_KEY_1, output)
            self.assertNotIn(VALID_KEY_2, output)

    def test_capacity_report_shows_key_counts_per_provider(self):
        """Startup capacity report shows count of configured keys per provider."""
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": VALID_KEY_1,
            "GEMINI_API_KEYS": f"{VALID_KEY_2},{VALID_KEY_3}",
            "GROQ_API_KEY": GROQ_KEY_1,
            "INTENT_PROVIDERS": "gemini,groq",
            "STUDY_PROVIDERS": "gemini,groq",
        }):
            report = generate_capacity_report()
            self.assertIn("[GEMINI] Keys: 3 (gemini#1, gemini#2, gemini#3)", report)
            self.assertIn("[GROQ] Keys: 1 (groq#1)", report)
            self.assertNotIn(VALID_KEY_1, report)


if __name__ == "__main__":
    unittest.main()
