"""
Comprehensive tests for TaleemGo AI professional conversation behavior:
PART E & PART F scenarios:
1. "Download latest lecture from Ai course" sends 1 file with caption, no list, no ack.
2. "Ai ky lastest lecture bhejo" sends 1 file with caption.
3. "Download latest lecture" (no course) -> asks which course; "Ai course" completes download;
   pending action expires after 10m and wiped on logout.
4. "Ai course" alone, no pending -> course menu line, not full course list.
5. English message right after Roman Urdu gets English reply; bare "2" or "ok" inherits previous language.
6. No separate ack message sent for data actions; failed course lookup has hint line and never preceded by "Bilkul/Sure".
7. latest + explicit week -> week wins; course with no files -> one-line no_files message.
8. Provider prompts contain no course names, file titles, grades, or chat IDs.
9. Whole-word grade keywords and grades_seeded bug fixes still pass.
10. Empty text suppressed in send_text and _reply (logging caller).
11. File caption whitespace falls back to filename.
12. Failed file send produces file_failed message.
13. Numeric reply with no pending selection and after TTL expiry produces selection_expired (0 strikes).
14. Empty cached translation falls back to English; startup check verify_builtin_i18n_keys() passes.
"""

import logging
import os
import re
import tempfile
import time
import unittest
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import courses
import users as usr
import wa_bot
import wa_transport as wa
from app.llm.i18n import t, verify_builtin_i18n_keys, set_db_path
from app.llm.intent import build_system_prompt
from app.llm.lang_data import KEYWORDS
from app.llm.rules import detect_language, match_rules
from app.state import DEFAULT_STATE, load_state, save_state


class ProfessionalMockLMS:
    def __init__(self, courses=None, resources=None, attendance=None, assignments=None):
        self._courses = courses if courses is not None else [
            {"id": "c_ai", "code": "CC-311", "name": "Artificial Intelligence"},
            {"id": "c_nlp", "code": "DE304", "name": "Natural Language Processing"},
            {"id": "c_empty", "code": "CS100", "name": "Empty Course"},
        ]
        self._resources = resources if resources is not None else [
            {"id": "r1", "course_id": "c_ai", "title": "Intro AI.pdf", "week_number": 1, "created_at": "2026-09-01T10:00:00Z"},
            {"id": "r2", "course_id": "c_ai", "title": "Search.pdf", "week_number": 2, "created_at": "2026-09-10T10:00:00Z"},
            {"id": "r3", "course_id": "c_ai", "title": "Neural Networks.pdf", "week_number": 10, "created_at": "2026-11-01T10:00:00Z"},
            {"id": "r4", "course_id": "c_nlp", "title": "N-grams.pdf", "week_number": 3, "created_at": "2026-09-20T10:00:00Z"},
        ]
        self._attendance = attendance or {
            "overall": {"present": 28, "absent": 3, "other": 0, "total": 31, "percent": 90.3},
            "courses": {
                "c_ai": {"present": 10, "absent": 2, "other": 0, "total": 12, "percent": 83.3},
                "c_nlp": {"present": 28, "absent": 3, "other": 0, "total": 31, "percent": 90.3},
            },
        }
        self._assignments = assignments or []

    def get_courses(self):
        return list(self._courses)

    def get_resources(self, course_id=None):
        if course_id:
            return [r for r in self._resources if r.get("course_id") == course_id]
        return list(self._resources)

    def get_attendance(self):
        return dict(self._attendance)

    def get_assignments(self, course_id=None):
        return list(self._assignments)

    def get_grades(self, course_id=None):
        return [
            {"id": "a1", "course_id": "c_ai", "title": "Quiz 1", "week_number": 2,
             "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True,
             "grade": 9.0, "graded_at": "2026-09-15T10:00:00Z", "feedback": "Well done", "submitted": True}
        ]

    def download_resource(self, resource_id, folder=None):
        p = Path(folder or ".") / f"{resource_id}.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 test")
        return str(p)


class TestProfessionalConversations(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        self.tmp = Path(tempfile.mkdtemp(prefix="taleemgo_prof_test_"))
        usr.USERS_DIR = self.tmp / "users"
        usr.DB_PATH = self.tmp / "users.db"
        usr.init_db()
        set_db_path(str(usr.DB_PATH))

        with usr._cache_lock:
            usr._client_cache.clear()
            usr._client_locks.clear()
        with usr._pending_lock:
            usr._pending_choices.clear()
        with usr._pending_action_lock:
            usr._pending_actions.clear()
        usr.clear_strikes("all")
        with wa_bot._help_lock:
            wa_bot._user_help_sent.clear()

        self.sent_texts = []
        self.sent_files = []
        self.orig_send_text = wa_bot.wa.send_text
        self.orig_send_file = wa_bot.wa.send_file
        wa_bot.wa.send_text = lambda cid, msg: self.sent_texts.append((cid, msg))
        wa_bot.wa.send_file = lambda cid, path, caption="": self.sent_files.append((cid, path, caption))

        os.environ["LLM_ENABLED"] = "1"
        os.environ["GREETING_REPLY"] = "1"

    def tearDown(self):
        wa_bot.wa.send_text = self.orig_send_text
        wa_bot.wa.send_file = self.orig_send_file
        os.environ.clear()
        os.environ.update(self.orig_env)
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def test_scenario_1_download_latest_lecture_english(self):
        """Scenario 1: 'Download latest lecture from Ai course' sends 1 file with caption, no file list, no ack."""
        chat_id = "prof1@c.us"
        usr.upsert_user(chat_id, email="s1@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "Download latest lecture from Ai course")

        # No text messages sent (no separate ack, no list of files)
        self.assertEqual(len(self.sent_texts), 0)
        # Exactly 1 file sent
        self.assertEqual(len(self.sent_files), 1)
        cid, path, cap = self.sent_files[0]
        self.assertEqual(cid, chat_id)
        # Week 10 (newest week) sent with caption format
        self.assertIn("Latest in AI (CC-311): Week 10 - Neural Networks.pdf", cap)

    def test_scenario_2_download_latest_lecture_roman_urdu_typo(self):
        """Scenario 2: 'Ai ky lastest lecture bhejo' sends 1 file with caption."""
        chat_id = "prof2@c.us"
        usr.upsert_user(chat_id, email="s2@test.com", status="active", lang="ur-roman")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "Ai ky lastest lecture bhejo")

        self.assertEqual(len(self.sent_texts), 0)
        self.assertEqual(len(self.sent_files), 1)
        cid, path, cap = self.sent_files[0]
        self.assertEqual(cid, chat_id)
        self.assertIn("AI (CC-311)", cap)
        self.assertIn("Week 10 - Neural Networks.pdf", cap)

    def test_scenario_3_pending_action_followup_and_ttl_and_logout(self):
        """Scenario 3: 'Download latest lecture' (no course) asks course, 'Ai course' completes, TTL and logout wipe it."""
        chat_id = "prof3@c.us"
        usr.upsert_user(chat_id, email="s3@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        # 1. Ask without course
        wa_bot.handle(chat_id, "Download latest lecture")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("I couldn't find that course", self.sent_texts[0][1])
        self.assertIn("AI - CC-311", self.sent_texts[0][1])
        # Pending action is set with latest=True
        pending_act = usr.get_pending_action(chat_id)
        self.assertIsNotNone(pending_act)
        self.assertEqual(pending_act["intent"], "download")
        self.assertTrue(pending_act.get("latest"))

        # 2. Reply with 'Ai course' -> completes the download
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "Ai course")
        self.assertEqual(len(self.sent_files), 1)
        self.assertIn("Latest in AI (CC-311): Week 10", self.sent_files[0][2])
        self.assertIsNone(usr.get_pending_action(chat_id))

        # 3. Test TTL expiration
        usr.set_pending_action(chat_id, {"intent": "attendance"})
        # Modify timestamp to expired
        with usr._pending_action_lock:
            usr._pending_actions[chat_id]["expires_at"] = time.time() - 10
        self.assertIsNone(usr.get_pending_action(chat_id))

        # 4. Test wipe on logout
        usr.set_pending_action(chat_id, {"intent": "grades"})
        wa_bot.handle(chat_id, "logout")
        self.assertIsNone(usr.get_pending_action(chat_id))

    def test_scenario_4_course_only_menu_not_full_list(self):
        """Scenario 4: 'Ai course' alone, no pending action -> course menu line, not full course list."""
        chat_id = "prof4@c.us"
        usr.upsert_user(chat_id, email="s4@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "Ai course")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("AI (CC-311). What do you need?", reply)
        self.assertIn("attendance, assignments, lectures, grades, CLOs", reply)
        # Must not list all courses
        self.assertNotIn("Natural Language Processing", reply)
        self.assertNotIn("Empty Course", reply)

    def test_scenario_5_language_stickiness_and_short_message_inheritance(self):
        """Scenario 5: English message right after Roman Urdu gets English reply; bare '2' or 'ok' inherits."""
        chat_id = "prof5@c.us"
        usr.upsert_user(chat_id, email="s5@test.com", status="active", lang="ur-roman")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        # Pre-seed translation for ur-roman greeting so the test is hermetic without external providers or cache
        from app.llm.i18n import _set_cached_translation
        _set_cached_translation("ur-roman", "greeting", "👋 Salam! Main TaleemGo AI hoon, aapka university LMS assistant.")

        # 1. Roman Urdu greeting
        wa_bot.handle(chat_id, "salam")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Main TaleemGo AI hoon", self.sent_texts[0][1])

        # 2. English message right after -> English reply!
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "Show my attendance in AI")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("AI (CC-311): 10/12 present", self.sent_texts[0][1])

        # 3. Bare short message with no signal within 10 min inherits English
        lang = detect_language("ok", stored_lang="en", is_recent=True)
        self.assertEqual(lang, "en")
        lang2 = detect_language("2", stored_lang="ur-roman", is_recent=True)
        self.assertEqual(lang2, "ur-roman")

    def test_scenario_6_no_ack_on_data_and_hint_line_on_not_found(self):
        """Scenario 6: No separate ack message for data actions; failed lookup has hint line and no Bilkul."""
        chat_id = "prof6@c.us"
        usr.upsert_user(chat_id, email="s6@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        # Nonexistent course
        wa_bot.handle(chat_id, "attendance UnknownCourse")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("I couldn't find that course.", reply)
        self.assertIn("Reply with the course name or code, e.g. AI or CC-311.", reply)
        self.assertNotIn("Bilkul", reply)
        self.assertNotIn("Sure", reply)

    def test_scenario_7_latest_with_explicit_week_and_empty_course(self):
        """Scenario 7: latest + explicit week -> week wins; course with no files -> one-line no_files message."""
        chat_id = "prof7@c.us"
        usr.upsert_user(chat_id, email="s7@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        # 1. Explicit week wins over latest
        wa_bot.handle(chat_id, "Download latest week 1 lecture AI")
        # Week 1 has 1 file (Intro AI.pdf) -> sends directly with standard single-file flow
        self.assertEqual(len(self.sent_files), 1)
        self.assertIn("Intro AI.pdf", self.sent_files[0][2])

        # 2. Empty course -> one-line message
        self.sent_texts.clear()
        self.sent_files.clear()
        wa_bot.handle(chat_id, "Download latest lecture from Empty Course")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertEqual(len(self.sent_files), 0)
        self.assertIn("No files found in EC (CS100)", self.sent_texts[0][1])

    def test_scenario_8_prompt_privacy_no_leaks(self):
        """Scenario 8: Provider prompts contain no course names, file titles, grades, or chat IDs."""
        prompt = build_system_prompt()
        self.assertNotIn("Natural Language Processing", prompt)
        self.assertNotIn("Neural Networks.pdf", prompt)
        self.assertNotIn("8.5/10", prompt)
        self.assertNotIn("@c.us", prompt)

    def test_scenario_9_bug_fixes_preserved(self):
        """Scenario 9: Whole-word grade keywords and grades_seeded bug fixes still pass."""
        # 1. Whole word check
        r_up = match_rules("upgrade plan")
        if r_up and r_up.get("actions"):
            self.assertNotEqual(r_up["actions"][0]["intent"], "grades")
        else:
            self.assertIsNone(r_up)

        r_att = match_rules("was my attendance marked today")
        self.assertIsNotNone(r_att)
        self.assertEqual(r_att["actions"][0]["intent"], "attendance")
        self.assertNotIn("course_query", r_att["actions"][0])

        # 2. grades_seeded state
        st = load_state("dummy_nonexistent_path")
        self.assertIn("grades_seeded", st)
        self.assertFalse(st["grades_seeded"])

    def test_scenario_10_empty_text_suppressed(self):
        """Scenario 10: Empty or whitespace text suppressed in send_text and _reply with caller logging."""
        with self.assertLogs("taleemgo.wa_transport", level="INFO") as cm:
            res1 = self.orig_send_text("test@c.us", "")
            res2 = self.orig_send_text("test@c.us", "   \n\t  ")
            self.assertFalse(res1)
            self.assertFalse(res2)
            self.assertTrue(any("empty text suppressed" in log_msg for log_msg in cm.output))

    def test_scenario_11_caption_whitespace_fallback(self):
        """Scenario 11: File caption with only whitespace falls back to filename."""
        captured_captions = []
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": True}

            p = self.tmp / "test_file.pdf"
            p.write_bytes(b"%PDF")

            self.orig_send_file("test@c.us", str(p), caption="   \t  ")
            call_kwargs = mock_post.call_args[1]
            data = call_kwargs.get("json", {})
            self.assertEqual(data.get("caption"), "test_file.pdf")

    def test_scenario_12_failed_file_send_produces_file_failed_message(self):
        """Scenario 12: Failed file send produces file_failed i18n message."""
        chat_id = "prof12@c.us"
        usr.upsert_user(chat_id, email="s12@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        # Mock download_resource to throw exception
        lms.download_resource = MagicMock(side_effect=RuntimeError("Disk read failure"))
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "Download latest lecture from Ai course")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Neural Networks.pdf", self.sent_texts[0][1])

    def test_scenario_13_selection_expired_message_no_strikes(self):
        """Scenario 13: Numeric reply with no pending selection produces selection_expired and 0 strikes."""
        chat_id = "prof13@c.us"
        usr.upsert_user(chat_id, email="s13@test.com", status="active", lang="en")
        lms = ProfessionalMockLMS()
        usr.set_client(chat_id, lms)

        # No pending selection exists
        wa_bot.handle(chat_id, "2")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("expired", self.sent_texts[0][1].lower())
        # Strikes must remain 0
        self.assertEqual(len(usr._user_strikes.get(chat_id, [])), 0)
        self.assertFalse(usr.is_llm_locked(chat_id))

    def test_scenario_14_empty_translation_fallback_and_verify_keys(self):
        """Scenario 14: Empty translation falls back to English; startup check passes."""
        # 1. Startup check
        verify_builtin_i18n_keys()

        # 2. Key with empty string in translation falls back to English
        from app.llm.i18n import _set_cached_translation
        _set_cached_translation("test_lang", "selection_expired", "   ")
        os.environ["LLM_ENABLED"] = "0"
        english_val = t("selection_expired", "en")
        fallback_val = t("selection_expired", "test_lang")
        self.assertEqual(english_val, fallback_val)
        self.assertTrue(len(english_val) > 0)


if __name__ == "__main__":
    unittest.main()
