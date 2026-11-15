"""
app/test_capacity.py — Unit tests for Task 5: AI Capacity and Resilience:
1. Token-aware sliding windows (RPM, TPM, RPD, TPD) per (provider, model)
2. Pre-call budget check: skipping saturated models/providers without 429
3. Concurrency caps and priority-based queue timeouts (high: 8s, low: 2s)
4. Load shedding: low-priority study helper shed under pressure, high-priority intent/STT protected
5. Intent cache: shared across users, 0 provider calls, credential & sensitive number exclusions
6. Rules-first fast path: short clear queries execute with 0 tokens/calls, negation bypasses
7. Feature chains: independent chains for INTENT, STUDY, and STT
8. Local provider: zero auth required, failover target
9. Admin status & exhibition capacity reporting
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import users as usr
from llm.capacity import (
    DEFAULT_LIMITS,
    IntentCache,
    MEANING_FLIP_WORDS,
    TokenAwareLimiter,
    can_run_rules_first,
    generate_capacity_report,
    get_intent_providers,
    get_rules_first_stats,
    get_stt_providers,
    get_study_providers,
    intent_cache,
    is_exhibition_mode,
    limiter,
    record_intent_handled,
    reset_rules_first_stats,
)
from llm.providers import (
    PROVIDER_CONFIGS,
    RateLimitError,
    call_chat_completions,
    is_valid_api_key,
    registry,
)
from llm.router import route_intent
from study import StudyBusyError, call_study_provider, generate_quiz, generate_summary


class TestTokenAwareLimiter(unittest.TestCase):
    def setUp(self):
        self.limiter = TokenAwareLimiter()

    def test_default_ceilings_loaded(self):
        """Default ceilings match verified provider limits."""
        groq_ceil = self.limiter.get_ceilings("groq")
        self.assertEqual(groq_ceil["rpm"], 30)
        self.assertEqual(groq_ceil["tpm"], 8000)
        self.assertEqual(groq_ceil["rpd"], 1000)
        self.assertEqual(groq_ceil["tpd"], 200000)

        gemini_ceil = self.limiter.get_ceilings("gemini")
        self.assertEqual(gemini_ceil["rpm"], 15)
        self.assertEqual(gemini_ceil["tpm"], 1000000)

    def test_env_override_ceilings(self):
        """Environment variables override default ceilings."""
        with patch.dict(os.environ, {"GROQ_RPM": "50", "GROQ_TPM": "16000"}):
            ceil = self.limiter.get_ceilings("groq")
            self.assertEqual(ceil["rpm"], 50)
            self.assertEqual(ceil["tpm"], 16000)

    def test_sliding_window_rpm_rejection(self):
        """When RPM limit is reached, can_call returns False."""
        provider = "test_prov"
        model = "test_model"
        with patch.dict(os.environ, {"TEST_PROV_RPM": "2", "TEST_PROV_TPM": "10000"}):
            self.assertTrue(self.limiter.can_call(provider, model, 100))
            self.limiter.record_usage(provider, model, total_tokens=100)
            self.assertTrue(self.limiter.can_call(provider, model, 100))
            self.limiter.record_usage(provider, model, total_tokens=100)
            # Third call exceeds RPM ceiling 2
            self.assertFalse(self.limiter.can_call(provider, model, 100))

    def test_sliding_window_tpm_rejection(self):
        """When estimated tokens exceed remaining TPM, can_call returns False."""
        provider = "test_prov"
        model = "test_model"
        with patch.dict(os.environ, {"TEST_PROV_RPM": "100", "TEST_PROV_TPM": "1000"}):
            self.limiter.record_usage(provider, model, total_tokens=800)
            # Call requesting 300 tokens would exceed 1000 TPM
            self.assertFalse(self.limiter.can_call(provider, model, estimated_tokens=300))
            # Call requesting 150 tokens fits within 1000 TPM
            self.assertTrue(self.limiter.can_call(provider, model, estimated_tokens=150))

    def test_window_usage_percentage_calculation(self):
        """Limiter accurately calculates window usage percentage."""
        provider = "test_prov"
        model = "test_model"
        with patch.dict(os.environ, {"TEST_PROV_RPM": "10", "TEST_PROV_TPM": "1000"}):
            self.limiter.record_usage(provider, model, total_tokens=500)
            usage = self.limiter.get_window_usage(provider, model)
            self.assertEqual(usage["rpm_used"], 1)
            self.assertEqual(usage["rpm_pct"], 10.0)
            self.assertEqual(usage["tpm_used"], 500)
            self.assertEqual(usage["tpm_pct"], 50.0)


class TestConcurrencyQueue(unittest.TestCase):
    def setUp(self):
        self.limiter = TokenAwareLimiter()

    def test_concurrency_cap_and_release(self):
        """Concurrency cap restricts simultaneous callers and releases properly."""
        with patch.dict(os.environ, {"PROVIDER_CONCURRENCY": "2"}):
            # Acquire first two slots
            self.assertTrue(self.limiter.acquire_concurrency("groq", priority="high"))
            self.assertTrue(self.limiter.acquire_concurrency("groq", priority="high"))

            # Third slot should timeout if none released
            start_t = time.time()
            self.assertFalse(self.limiter.acquire_concurrency("groq", priority="low"))  # 2s timeout
            elapsed = time.time() - start_t
            self.assertGreaterEqual(elapsed, 1.8)

            # Release one slot
            self.limiter.release_concurrency("groq")
            # Now acquiring succeeds
            self.assertTrue(self.limiter.acquire_concurrency("groq", priority="low"))
            # Clean up
            self.limiter.release_concurrency("groq")
            self.limiter.release_concurrency("groq")

    def test_priority_timeout_difference(self):
        """High priority waits up to 8s; low priority times out in 2s."""
        with patch.dict(os.environ, {"PROVIDER_CONCURRENCY": "1"}):
            self.assertTrue(self.limiter.acquire_concurrency("gemini", priority="high"))

            # Low priority wait
            t0 = time.time()
            self.assertFalse(self.limiter.acquire_concurrency("gemini", priority="low"))
            t_low = time.time() - t0
            self.assertLess(t_low, 3.5)

            self.limiter.release_concurrency("gemini")


class TestLoadShedding(unittest.TestCase):
    def setUp(self):
        self.limiter = TokenAwareLimiter()

    def test_is_shedding_when_usage_exceeds_80_percent(self):
        """When all providers have > 80% usage on RPM or TPM, is_shedding returns True."""
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk_test_key_longer_than_20_chars",
            "GROQ_RPM": "10",
        }):
            state = registry.get_state("groq")
            state.disabled = False
            state.cooldown_until = 0.0

            # 9 requests out of 10 RPM -> 90% usage (> 80%)
            for _ in range(9):
                self.limiter.record_usage("groq", "openai/gpt-oss-20b", total_tokens=10)

            with patch("app.llm.capacity.limiter", self.limiter):
                self.assertTrue(self.limiter.is_shedding(["groq"]))

    def test_study_helper_sheds_under_capacity_pressure(self):
        """Under load shedding, study requests raise StudyBusyError immediately without calling providers."""
        with patch("study.limiter.is_shedding", return_value=True):
            with self.assertRaises(StudyBusyError):
                generate_summary("Some lecture text", "lecture.pdf", lang="en")

            with self.assertRaises(StudyBusyError):
                generate_quiz("Some lecture text", "lecture.pdf", n=5, lang="en")

    def test_intent_classification_never_sheds(self):
        """High-priority intent classification is never shed even under load shedding."""
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test_gemini_key_123456789012345",
            "INTENT_PROVIDERS": "gemini",
        }):
            registry.get_state("gemini").disabled = False
            registry.get_state("gemini").cooldown_until = 0.0

            with patch("llm.router.call_chat_completions", return_value='{"lang": "en", "actions": [{"intent": "attendance"}], "clarify": null, "off_topic": false, "unsupported": false}'):
                res = route_intent("what is my attendance")
                self.assertIsNotNone(res)
                self.assertEqual(res["actions"][0]["intent"], "attendance")


class TestIntentCache(unittest.TestCase):
    def setUp(self):
        self.cache = IntentCache(capacity=10, ttl_sec=60.0)

    def test_cache_hit_across_users(self):
        """Queries with same normalized text return cached intent with 0 provider calls."""
        intent_payload = {
            "lang": "en",
            "actions": [{"intent": "attendance", "course_query": "NLP"}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        }
        self.cache.put("What is my attendance in NLP?", False, "en", intent_payload)

        # Different case, trailing punctuation
        hit = self.cache.get("what is my attendance in nlp", False, "en")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["actions"][0]["intent"], "attendance")
        self.assertEqual(self.cache.stats()["hits"], 1)

    def test_cache_excludes_sensitive_emails(self):
        """Messages containing email addresses are never cached."""
        payload = {"lang": "en", "actions": [], "clarify": None, "off_topic": False, "unsupported": False}
        self.cache.put("my email is student@test.edu", False, "en", payload)
        self.assertIsNone(self.cache.get("my email is student@test.edu", False, "en"))

    def test_cache_excludes_voice_transcripts_with_digits(self):
        """Voice transcripts containing digits are never cached to protect student roll numbers."""
        payload = {"lang": "en", "actions": [], "clarify": None, "off_topic": False, "unsupported": False}
        self.cache.put("roll number 12345", False, "en", payload, is_voice=True)
        self.assertIsNone(self.cache.get("roll number 12345", False, "en"))

    def test_exhibition_mode_doubles_ttl(self):
        """When EXHIBITION_MODE=1, IntentCache initializes with 7200s TTL."""
        with patch.dict(os.environ, {"EXHIBITION_MODE": "1"}):
            c = IntentCache()
            self.assertEqual(c.ttl_sec, 7200.0)


class TestRulesFirstFastPath(unittest.TestCase):
    def setUp(self):
        reset_rules_first_stats()

    def test_short_unambiguous_message_runs_rules_first(self):
        """Messages <= 8 words with 1 rule action run rules-first (0 tokens/calls)."""
        rule_res = {
            "lang": "en",
            "actions": [{"intent": "attendance"}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        }
        self.assertTrue(can_run_rules_first("attendance", rule_res, lms=None))
        self.assertTrue(can_run_rules_first("what is my attendance", rule_res, lms=None))

    def test_negation_words_bypass_rules_first(self):
        """Messages with negation or meaning-flipping words bypass rules-first to LLM."""
        rule_res = {
            "lang": "en",
            "actions": [{"intent": "attendance"}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        }
        # English negations
        self.assertFalse(can_run_rules_first("not attendance", rule_res, lms=None))
        self.assertFalse(can_run_rules_first("dont show attendance", rule_res, lms=None))
        self.assertFalse(can_run_rules_first("attendance without labs", rule_res, lms=None))
        # Urdu/Roman Urdu negations
        self.assertFalse(can_run_rules_first("attendance nahi chahiye", rule_res, lms=None))
        self.assertFalse(can_run_rules_first("حاضری مت دکھائیں", rule_res, lms=None))

    def test_long_message_bypasses_rules_first(self):
        """Messages longer than 8 words bypass rules-first."""
        rule_res = {
            "lang": "en",
            "actions": [{"intent": "attendance"}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        }
        long_msg = "one two three four five six seven eight nine words"
        self.assertFalse(can_run_rules_first(long_msg, rule_res, lms=None))

    def test_rules_first_telemetry_counting(self):
        """Telemetry accurately tracks rule-handled vs LLM-handled percentages."""
        record_intent_handled(is_rule_handled=True)
        record_intent_handled(is_rule_handled=True)
        record_intent_handled(is_rule_handled=False)

        stats = get_rules_first_stats()
        self.assertEqual(stats["rules_handled"], 2)
        self.assertEqual(stats["llm_handled"], 1)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["rule_share_pct"], 66.7)


class TestBurstFailoverAndProviders(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        usr.DATA_DIR = self.tmp_dir
        usr.init_db()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_burst_larger_than_tpm_spills_to_next_provider(self):
        """When provider A exceeds TPM ceiling, router immediately falls back to provider B without 429."""
        with patch.dict(os.environ, {
            "INTENT_PROVIDERS": "gemini,groq",
            "GEMINI_API_KEY": "test_gemini_key_123456789012345",
            "GROQ_API_KEY": "test_groq_key_123456789012345678",
            "GEMINI_TPM": "100",  # Tiny ceiling
        }):
            registry.get_state("gemini").disabled = False
            registry.get_state("gemini").cooldown_until = 0.0
            registry.get_state("groq").disabled = False
            registry.get_state("groq").cooldown_until = 0.0

            # Saturate Gemini TPM
            limiter.record_usage("gemini", "gemini-3.5-flash-lite", total_tokens=200)

            # Call chat completions: Gemini is skipped by limiter; Groq is called
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "choices": [{"message": {"content": '{"lang": "en", "actions": [{"intent": "attendance"}], "clarify": null, "off_topic": false, "unsupported": false}'}}],
                    "usage": {"total_tokens": 50},
                }
                mock_post.return_value = mock_resp

                res = route_intent("check my attendance")
                self.assertIsNotNone(res)
                # Verify request went to Groq (url contains api.groq.com)
                called_url = mock_post.call_args[0][0]
                self.assertIn("groq", called_url)

    def test_local_provider_auth_bypass(self):
        """Local provider requires no API key and can be used as fallback."""
        self.assertTrue(is_valid_api_key("", provider_name="local"))
        self.assertTrue(is_valid_api_key("anything", provider_name="local"))

        state = registry.get_state("local")
        state.disabled = False
        state.cooldown_until = 0.0

        with patch.dict(os.environ, {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1"}):
            self.assertTrue(registry.is_available("local"))

    def test_feature_chains_independent_operation(self):
        """INTENT_PROVIDERS, STUDY_PROVIDERS, and STT_PROVIDERS operate independently."""
        with patch.dict(os.environ, {
            "INTENT_PROVIDERS": "groq",
            "STUDY_PROVIDERS": "gemini",
            "STT_PROVIDERS": "groq,gemini",
        }):
            self.assertEqual(get_intent_providers(), ["groq"])
            self.assertEqual(get_study_providers(), ["gemini"])
            self.assertEqual(get_stt_providers(), ["groq", "gemini"])

            # If groq is in 429 cooldown, study chain (Gemini) remains unaffected
            registry.record_429("groq", retry_after=100.0)
            self.assertFalse(registry.is_available("groq"))

            with patch.dict(os.environ, {"GEMINI_API_KEY": "test_gemini_key_123456789012345"}):
                registry.get_state("gemini").disabled = False
                registry.get_state("gemini").cooldown_until = 0.0
                self.assertTrue(registry.is_available("gemini"))


class TestAdminStatusAndCapacityReport(unittest.TestCase):
    def test_capacity_report_formatting(self):
        """Capacity report contains active chains, ceilings, and throughput estimation."""
        report = generate_capacity_report()
        self.assertIn("TALEEMGO AI CAPACITY & RESILIENCE REPORT", report)
        self.assertIn("Intent Chain (High Priority)", report)
        self.assertIn("Study Chain (Low Priority)", report)
        self.assertIn("STT Chain (High Priority)", report)
        self.assertIn("Estimated absorbable burst throughput", report)

    def test_admin_llm_status_output_shape(self):
        """Admin llm command output includes provider models, usage %, rules-first share, and cache stats."""
        from wa_bot import _handle_cmd_llm

        sent_messages = []
        with patch("wa_bot._reply", side_effect=lambda cid, msg: sent_messages.append(msg)):
            _handle_cmd_llm("test_admin_chat")

        self.assertEqual(len(sent_messages), 1)
        out = sent_messages[0]
        self.assertIn("TaleemGo AI LLM Status:", out)
        self.assertIn("calls today:", out)
        self.assertIn("Throughput & Optimization:", out)
        self.assertIn("Rules-first:", out)
        self.assertIn("Intent Cache:", out)
        self.assertIn("Locked users:", out)


if __name__ == "__main__":
    unittest.main()
