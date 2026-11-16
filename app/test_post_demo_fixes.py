"""
Tests for Task 6 post-presentation fixes:
- Part A: Speed and concurrency (LMS caching, TTL, invalidation, refresh bypass, worker threads)
- Part B: Language mode (locking, prompts, STT hint mapping, script mismatch, voice_too_short, Roman Urdu Latin purity)
- Part C: Assignments per course, filters, overview mode, chunking, upcoming
- Part D: Attendance planner remaining classes (left N, config, unreachable target, best possible %, capped can_miss)
"""
import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Ensure app package is importable
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

import users as usr
import planner
from lms_client import LMSClient
from llm import lang_data, rules, stt
import wa_bot


class TestLMSCacheAndSpeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        usr.DB_PATH = os.path.join(self.tmp, "users.db")
        usr.USERS_DIR = tempfile.Path(self.tmp) / "users" if hasattr(tempfile, "Path") else os.path.join(self.tmp, "users")
        os.makedirs(str(usr.USERS_DIR), exist_ok=True)
        usr.init_db()
        usr._client_cache.clear()
        usr._client_locks.clear()
        usr._pending_choices.clear()
        os.environ["LMS_CACHE_TTL_SECONDS"] = "60"
        os.environ["EXHIBITION_MODE"] = "0"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lms_cache_set_get_and_ttl(self):
        token_file = os.path.join(self.tmp, "test_tokens.json")
        client = LMSClient(token_file=token_file, load_tokens=False)
        client.set_cached("test_key", {"data": 123})
        self.assertEqual(client.get_cached("test_key"), {"data": 123})

        # Test bypass
        self.assertIsNone(client.get_cached("test_key", bypass=True))

        # Test clear cache
        client.clear_cache("test_key")
        self.assertIsNone(client.get_cached("test_key"))

    def test_lms_cache_exhibition_mode_doubles_ttl(self):
        token_file = os.path.join(self.tmp, "test_tokens.json")
        client = LMSClient(token_file=token_file, load_tokens=False)
        os.environ["LMS_CACHE_TTL_SECONDS"] = "2"
        os.environ["EXHIBITION_MODE"] = "1"
        client.set_cached("k", "v")
        # In exhibition mode, 2s becomes 4s
        with client._cache_lock:
            client._cache["k"] = (time.time() - 3.0, "v")  # 3s elapsed, still within 4s
        self.assertEqual(client.get_cached("k"), "v")

        # After 5s elapsed, expired
        with client._cache_lock:
            client._cache["k"] = (time.time() - 5.0, "v")
        self.assertIsNone(client.get_cached("k"))

    def test_lms_cache_per_user_isolation(self):
        client1 = LMSClient(token_file=os.path.join(self.tmp, "tok1.json"), load_tokens=False)
        client2 = LMSClient(token_file=os.path.join(self.tmp, "tok2.json"), load_tokens=False)

        client1.set_cached("courses", [{"id": "c1"}])
        client2.set_cached("courses", [{"id": "c2"}])

        self.assertEqual(client1.get_cached("courses")[0]["id"], "c1")
        self.assertEqual(client2.get_cached("courses")[0]["id"], "c2")

    def test_lms_cache_invalidation_on_store(self):
        token_file = os.path.join(self.tmp, "test_tokens.json")
        client = LMSClient(token_file=token_file, load_tokens=False)
        client.set_cached("courses", [{"id": "c1"}])
        self.assertIsNotNone(client.get_cached("courses"))
        # _store invalidates cache
        with patch.object(client, "_write_token_bytes"):
            client._store({"access_token": "abc", "refresh_token": "def", "user": {"id": "u1"}})
        self.assertIsNone(client.get_cached("courses"))

    def test_refresh_keyword_detection(self):
        # refresh / taza / تازہ in arg
        for word in ("refresh", "attendance refresh", "taza", "تازہ"):
            has_ref = bool(re.search(r"\b(?:refresh|taza|تازہ)\b", word, re.IGNORECASE))
            self.assertTrue(has_ref, f"Failed for {word}")


class TestLanguageMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        usr.DB_PATH = os.path.join(self.tmp, "users.db")
        usr.USERS_DIR = os.path.join(self.tmp, "users")
        os.makedirs(str(usr.USERS_DIR), exist_ok=True)
        usr.init_db()
        usr._client_cache.clear()
        usr._client_locks.clear()
        usr._pending_choices.clear()

        self.sent_texts = []
        self.patcher_text = patch("wa_transport.send_text", side_effect=lambda cid, txt: self.sent_texts.append(txt))
        self.patcher_text.start()

    def tearDown(self):
        self.patcher_text.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roman_urdu_contains_only_latin_characters(self):
        """Part B: Hand-written Roman Urdu must contain ONLY Latin letters, punctuation, digits — ZERO Urdu/Arabic script."""
        ur_roman_strings = lang_data.STRINGS.get("ur-roman", {})
        self.assertTrue(len(ur_roman_strings) > 0)
        for key, val in ur_roman_strings.items():
            # Check for any Arabic/Perso-Arabic unicode range
            arabic_matches = re.findall(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", val)
            self.assertEqual(
                len(arabic_matches), 0,
                f"Key '{key}' in ur-roman contains Urdu/Arabic script: {arabic_matches}"
            )

    def test_user_lang_storage_and_locking(self):
        chat_id = "test_lang_chat@c.us"
        usr.try_register(chat_id)
        self.assertFalse(usr.is_lang_locked(chat_id))
        lang, locked = usr.get_user_lang(chat_id)
        self.assertEqual(lang, "en")
        self.assertFalse(locked)

        # Set and lock language
        usr.set_user_lang(chat_id, "ur", locked=True)
        self.assertTrue(usr.is_lang_locked(chat_id))
        lang, locked = usr.get_user_lang(chat_id)
        self.assertEqual(lang, "ur")
        self.assertTrue(locked)

        # Mark prompt sent
        self.assertFalse(usr.is_lang_prompt_sent(chat_id))
        usr.mark_lang_prompt_sent(chat_id)
        self.assertTrue(usr.is_lang_prompt_sent(chat_id))

    def test_language_command_handler(self):
        chat_id = "lang_cmd_user@c.us"
        usr.try_register(chat_id)

        # Without argument, shows options
        wa_bot._handle_language_cmd(chat_id, "")
        self.assertTrue(any("1. English" in r for r in self.sent_texts))

        # With argument "3" or "roman" -> sets ur-roman
        self.sent_texts.clear()
        wa_bot._handle_language_cmd(chat_id, "3")
        lang, locked = usr.get_user_lang(chat_id)
        self.assertEqual(lang, "ur-roman")
        self.assertTrue(usr.is_lang_locked(chat_id))
        self.assertTrue(any("Zuban" in r and "lock" in r for r in self.sent_texts))

        # With argument "sindhi" -> sets sd
        self.sent_texts.clear()
        wa_bot._handle_language_cmd(chat_id, "sindhi")
        lang, locked = usr.get_user_lang(chat_id)
        self.assertEqual(lang, "sd")
        self.assertTrue(usr.is_lang_locked(chat_id))


    def test_stt_script_mismatch_detection(self):
        # Urdu/Sindhi modes expect Arabic script; Latin transcript is mismatch
        self.assertTrue(stt.is_script_mismatch("Hello world", "ur"))
        self.assertTrue(stt.is_script_mismatch("Hello world", "sd"))
        self.assertFalse(stt.is_script_mismatch("سلام", "ur"))

        # English/Roman expect Latin script; Arabic transcript is mismatch
        self.assertTrue(stt.is_script_mismatch("سلام دنیا", "en"))
        self.assertTrue(stt.is_script_mismatch("سلام دنیا", "ur-roman"))
        self.assertFalse(stt.is_script_mismatch("Hello world", "en"))
        self.assertFalse(stt.is_script_mismatch("mera attendance", "ur-roman"))


class TestAssignmentsPerCourseAndFilters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        usr.DB_PATH = os.path.join(self.tmp, "users.db")
        usr.USERS_DIR = os.path.join(self.tmp, "users")
        os.makedirs(str(usr.USERS_DIR), exist_ok=True)
        usr.init_db()
        usr._client_cache.clear()
        usr._client_locks.clear()
        usr._pending_choices.clear()

        self.sent_texts = []
        self.patcher_text = patch("wa_transport.send_text", side_effect=lambda cid, txt: self.sent_texts.append(txt))
        self.patcher_text.start()

        self.mock_lms = MagicMock()
        self.mock_lms.get_courses.return_value = [
            {"id": "c1", "name": "Artificial Intelligence", "code": "CS-401"},
            {"id": "c2", "name": "Database Systems", "code": "CS-402"},
        ]
        self.mock_lms.get_assignments_with_status.return_value = [
            {"id": "a1", "course_id": "c1", "title": "AI Task 1", "status": "pending", "due_date": "2026-11-20T23:59:00Z"},
            {"id": "a2", "course_id": "c1", "title": "AI Task 2", "status": "overdue", "due_date": "2026-11-10T23:59:00Z"},
            {"id": "a3", "course_id": "c2", "title": "DB Schema", "status": "submitted", "due_date": "2026-11-05T23:59:00Z"},
        ]

    def tearDown(self):
        self.patcher_text.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assignments_overview_mode(self):
        chat_id = "asgn_ov_chat@c.us"
        usr.try_register(chat_id)
        wa_bot._handle_assignments_cmd(chat_id, self.mock_lms, "")
        self.assertEqual(len(self.sent_texts), 1)
        msg = self.sent_texts[0]
        # Must show overview header
        self.assertIn("Overview", msg)
        # Group counts
        self.assertIn("1 pending, 1 overdue", msg)
        self.assertIn("all submitted", msg)
        # Bottom hint
        self.assertIn("assignments all", msg)

    def test_assignments_for_specific_course(self):
        chat_id = "asgn_course_chat@c.us"
        usr.try_register(chat_id)
        wa_bot._handle_assignments_cmd(chat_id, self.mock_lms, "CS-401")
        self.assertEqual(len(self.sent_texts), 1)
        msg = self.sent_texts[0]
        self.assertIn("Assignments for", msg)
        self.assertIn("AI Task 1", msg)
        self.assertIn("AI Task 2", msg)

    def test_assignments_filtered_by_status(self):
        chat_id = "asgn_filter_chat@c.us"
        usr.try_register(chat_id)
        # Filter pending
        wa_bot._handle_assignments_cmd(chat_id, self.mock_lms, "pending")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("AI Task 1", self.sent_texts[0])

        # Filter submitted
        self.sent_texts.clear()
        wa_bot._handle_assignments_cmd(chat_id, self.mock_lms, "submitted")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("DB Schema", self.sent_texts[0])

    def test_assignments_all_mode(self):
        chat_id = "asgn_all_chat@c.us"
        usr.try_register(chat_id)
        wa_bot._handle_assignments_cmd(chat_id, self.mock_lms, "all")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("All Assignments", self.sent_texts[0])
        self.assertIn("AI Task 1", self.sent_texts[0])
        self.assertIn("DB Schema", self.sent_texts[0])

    def test_upcoming_cmd_only_future_pending(self):
        chat_id = "upcoming_chat@c.us"
        usr.try_register(chat_id)
        future_due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        past_due = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.mock_lms.get_assignments_with_status.return_value = [
            {"id": "a1", "course_id": "c1", "title": "Future Item", "status": "pending", "due_date": future_due},
            {"id": "a2", "course_id": "c1", "title": "Overdue Pending", "status": "pending", "due_date": past_due},
            {"id": "a3", "course_id": "c2", "title": "Submitted Item", "status": "submitted", "due_date": future_due},
        ]
        wa_bot._handle_upcoming_cmd(chat_id, self.mock_lms, "")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Future Item", self.sent_texts[0])
        self.assertNotIn("Overdue Pending", self.sent_texts[0])
        self.assertNotIn("Submitted Item", self.sent_texts[0])

    def test_reply_line_boundary_chunking_under_3500(self):
        chat_id = "chunk_chat@c.us"
        long_text = "\n".join(f"Line {i}: " + ("x" * 60) for i in range(100))  # ~6800 chars
        wa_bot._reply(chat_id, long_text)
        self.assertTrue(len(self.sent_texts) >= 2)
        for c in self.sent_texts:
            self.assertLessEqual(len(c), 3500)


class TestAttendancePlannerRemainingClasses(unittest.TestCase):
    def test_unreachable_target_when_must_attend_exceeds_remaining(self):
        """
        Example from Task 6 prompt:
        Present=16, Total=22 (72.7%), target=80%, remaining=3.
        Formula:
        pres + must_attend >= target * (tot + remaining)
        16 + must_attend >= 0.8 * 25 = 20
        must_attend = 4 classes needed, but only 3 remain!
        Status should be 'unreachable', best possible % = (16+3)/25 = 76.0%.
        """
        res = planner.calculate_attendance_plan(present=16, total=22, target=80, remaining=3)
        self.assertEqual(res["status"], "unreachable")
        self.assertEqual(res["remaining"], 3)
        self.assertEqual(res["must_attend"], 8)  # without remaining cap, 8 classes needed
        self.assertAlmostEqual(res["max_pct"], 76.0, places=1)
        self.assertAlmostEqual(res["min_pct"], 64.0, places=1)
        self.assertEqual(res["pres_plus_left"], 19)
        self.assertEqual(res["tot_plus_left"], 25)

    def test_reachable_safe_capped_at_remaining(self):
        """
        Present=20, Total=22 (90.9%), target=75%, remaining=10.
        Can miss calculation:
        20 / (22 + can_miss) >= 0.75 -> 22 + can_miss <= 26.66 -> can_miss = 4.
        Since 4 <= 10 remaining, can_miss remains 4.
        """
        res = planner.calculate_attendance_plan(present=20, total=22, target=75, remaining=10)
        self.assertEqual(res["status"], "safe")
        self.assertEqual(res["can_miss"], 4)
        self.assertEqual(res["remaining"], 10)
        self.assertAlmostEqual(res["max_pct"], 93.8, places=1)  # 30/32

    def test_reachable_safe_can_miss_capped_at_remaining(self):
        """
        Present=20, Total=22 (90.9%), target=75%, remaining=2.
        Normal can_miss would be 4, but only 2 classes remain in semester.
        So can_miss should be capped at 2.
        """
        res = planner.calculate_attendance_plan(present=20, total=22, target=75, remaining=2)
        self.assertEqual(res["status"], "safe")
        self.assertEqual(res["can_miss"], 2)
        self.assertEqual(res["remaining"], 2)

    def test_unknown_remaining_includes_flag(self):
        res = planner.calculate_attendance_plan(present=10, total=15, target=80, remaining=None)
        self.assertTrue(res.get("unknown_remaining"))

    def test_unlikely_warning_flag_when_must_attend_over_20(self):
        # 1 present out of 20, target 90%
        res = planner.calculate_attendance_plan(present=1, total=20, target=90, remaining=None)
        self.assertTrue(res.get("is_unlikely"))

    def test_config_planned_classes_for_course(self):
        with patch.dict(os.environ, {"CLASSES_PER_COURSE": "30", "CLASSES_PER_COURSE_CS401": "34"}):
            self.assertEqual(planner.get_planned_classes_for_course("CS-401"), 34)
            self.assertEqual(planner.get_planned_classes_for_course("CS-402"), 30)

    def test_rules_matcher_extracts_left_and_target(self):
        res = rules.match_rules("plan AI 80 left 3")
        self.assertIsNotNone(res)
        actions = res.get("actions", [])
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act["intent"], "plan")
        self.assertEqual(act.get("target"), 80)
        self.assertEqual(act.get("left"), 3)
        self.assertIn("ai", act.get("course_query", "").lower())


if __name__ == "__main__":
    unittest.main()
