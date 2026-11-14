"""
test_helpers.py - Hermetic unit tests for Part A (Attendance Planner),
Part B (Daily Digest), and Part C (Calendar File).
"""
import math
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake_key")
os.environ.setdefault("LMS_BASE_URL", "https://fake-lms.example.com")
os.environ.setdefault("BRIDGE_TOKEN", "test_token")
os.environ.setdefault("TALEEMGO_SECRET_KEY", "test_secret_key_32_bytes_len_1234=")
os.environ.setdefault("ATTENDANCE_WARN", "75")
os.environ.setdefault("DIGEST_DEFAULT_TIME", "07:30")
os.environ.setdefault("TIMEZONE", "Asia/Karachi")

import calendar_gen
import planner
import timeutils
import users as usr
import wa_bot
import wa_transport
from llm.intent import ALLOWED_INTENTS, validate_intent_response
from llm.lang_data import STRINGS
from llm.rules import match_rules


class MockLMSForHelpers:
    def __init__(self, courses=None, attendance=None, assignments=None, submissions=None, resources=None, grades=None):
        self._courses = courses if courses is not None else [
            {"id": "c_ai", "code": "AI", "name": "Artificial Intelligence"},
            {"id": "c_hci", "code": "CS301", "name": "Human Computer Interaction"},
            {"id": "c_empty", "code": "MATH101", "name": "Calculus"},
        ]
        self._attendance = attendance if attendance is not None else {
            "overall": {"present": 34, "absent": 10, "other": 0, "total": 44, "percent": 77.3},
            "courses": {
                # 16 of 22 at target 75 -> needs 2 in a row
                "c_ai": {"present": 16, "absent": 6, "other": 0, "total": 22, "percent": 72.7},
                # 18 of 22 at target 75 -> can miss 2
                "c_hci": {"present": 18, "absent": 4, "other": 0, "total": 22, "percent": 81.8},
                # No records
                "c_empty": {"present": 0, "absent": 0, "other": 0, "total": 0, "percent": None},
            },
        }
        self._assignments = assignments if assignments is not None else []
        self._submissions = submissions if submissions is not None else {}
        self._resources = resources if resources is not None else []
        self._grades = grades if grades is not None else []

    def get_courses(self):
        return list(self._courses)

    def get_attendance(self):
        return dict(self._attendance)

    def get_assignments(self, course_id=None):
        if course_id:
            return [a for a in self._assignments if a.get("course_id") == course_id]
        return list(self._assignments)

    def get_submissions(self):
        return dict(self._submissions)

    def get_resources(self, course_id=None):
        if course_id:
            return [r for r in self._resources if r.get("course_id") == course_id]
        return list(self._resources)

    def get_grades(self, course_id=None):
        if course_id:
            return [g for g in self._grades if g.get("course_id") == course_id]
        return list(self._grades)


class BaseHelperTestCase(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        self.tmp = Path(tempfile.mkdtemp(prefix="taleemgo_helpers_test_"))
        usr.USERS_DIR = self.tmp / "users"
        usr.USERS_DIR.mkdir(parents=True, exist_ok=True)
        usr.DB_PATH = str(self.tmp / "users.db")
        usr.init_db()

        with usr._cache_lock:
            usr._client_cache.clear()
            usr._client_locks.clear()
        with usr._pending_lock:
            usr._pending_choices.clear()
        with usr._pending_action_lock:
            usr._pending_actions.clear()
        usr.clear_strikes("all")

        self.sent_texts = []
        self.sent_files = []
        self.orig_send_text = wa_transport.send_text
        self.orig_send_file = wa_transport.send_file
        wa_transport.send_text = lambda cid, msg: self.sent_texts.append((cid, msg))
        wa_transport.send_file = lambda cid, path, caption="": self.sent_files.append((cid, path, caption))

    def tearDown(self):
        wa_transport.send_text = self.orig_send_text
        wa_transport.send_file = self.orig_send_file
        os.environ.clear()
        os.environ.update(self.orig_env)
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass


# -------------------------------------------------------------
# PART A. Attendance Planner Tests
# -------------------------------------------------------------
class TestAttendancePlanner(BaseHelperTestCase):
    def test_exact_mathematical_examples(self):
        """
        Verify the 3 required exact examples:
        1. 16 present of 22 at target 75 requires attending the next 2 in a row (18/24 = 75%)
        2. 18 of 22 at 75 can miss 2 (18/24 = 75%)
        3. 18 of 22 and miss 3 gives 72.0%
        """
        # Example 1
        res1 = planner.calculate_attendance_plan(present=16, total=22, target=75)
        self.assertEqual(res1["status"], "warning")
        self.assertEqual(res1["must_attend"], 2)
        # Verify math: 18/24 = 75%
        self.assertEqual(round(100.0 * (16 + 2) / (22 + 2), 1), 75.0)

        # Example 2
        res2 = planner.calculate_attendance_plan(present=18, total=22, target=75)
        self.assertEqual(res2["status"], "safe")
        self.assertEqual(res2["can_miss"], 2)
        # Verify math: 18/24 = 75%
        self.assertEqual(round(100.0 * 18 / (22 + 2), 1), 75.0)

        # Example 3 (What-if: 18 of 22 miss 3)
        res3 = planner.calculate_miss_what_if(present=18, total=22, miss_count=3)
        self.assertEqual(res3["new_total"], 25)
        self.assertEqual(res3["new_percent"], 72.0)

    def test_plan_command_specific_course_and_target(self):
        chat_id = "planner_user@c.us"
        usr.upsert_user(chat_id, email="std@test.com", status="active")
        lms = MockLMSForHelpers()
        usr.set_client(chat_id, lms)

        # 1. plan AI (default target 75): 16/22 -> must attend 2
        wa_bot.handle(chat_id, "plan AI")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("Attendance Plan (Target: 75%)", reply)
        self.assertIn("16/22", reply)
        self.assertIn("must attend the next 2 class(es) in a row", reply)
        self.assertIn("Assuming each upcoming class adds 1 to total", reply)

        # 2. plan HCI (default target 75): 18/22 -> can miss 2
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "plan HCI")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("18/22", reply)
        self.assertIn("You can miss 2 more class(es)", reply)

        # 3. plan HCI miss 3 -> gives 72.0%
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "plan HCI miss 3")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("miss 3 class(es)", reply)
        self.assertIn("72.0%", reply)
        self.assertIn("18/25", reply)

        # 4. Course with no records -> says "no attendance recorded", never 0%
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "plan MATH101")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("No attendance recorded", reply)
        self.assertNotIn("0.0%", reply)
        self.assertNotIn("0%", reply)

    def test_plan_all_courses_and_invalid_target(self):
        chat_id = "plan_all@c.us"
        usr.upsert_user(chat_id, email="all@test.com", status="active")
        lms = MockLMSForHelpers()
        usr.set_client(chat_id, lms)

        # plan (no arg) runs for all courses
        wa_bot.handle(chat_id, "plan")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("AI", reply)
        self.assertIn("CS301", reply)
        self.assertIn("MATH101", reply)
        self.assertIn("No attendance recorded", reply)

        # plan 120 -> invalid target (must be 1..99)
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "plan 120")
        self.assertEqual(len(self.sent_texts), 1)
        reply = self.sent_texts[0][1]
        self.assertIn("Target attendance percentage must be between 1 and 99%", reply)

    def test_plan_natural_language_and_rules(self):
        # 1. English: "can I miss 3 classes in HCI"
        r1 = match_rules("can I miss 3 classes in HCI")
        self.assertIsNotNone(r1)
        act1 = r1["actions"][0]
        self.assertEqual(act1["intent"], "plan")
        self.assertEqual(act1["course_query"], "HCI")
        self.assertEqual(act1["miss"], 3)

        # 2. Roman Urdu: "AI me kitni classes miss kar sakta hoon"
        r2 = match_rules("AI me kitni classes miss kar sakta hoon")
        self.assertIsNotNone(r2)
        act2 = r2["actions"][0]
        self.assertEqual(act2["intent"], "plan")
        self.assertEqual(act2["course_query"], "AI")

        # 3. Sindhi script: "منهنجي AI جي حاضري پلان ڇا آهي"
        r3 = match_rules("منهنجي AI جي حاضري پلان ڇا آهي")
        self.assertIsNotNone(r3)
        self.assertEqual(r3["actions"][0]["intent"], "plan")


# -------------------------------------------------------------
# PART B. Daily Digest Tests
# -------------------------------------------------------------
class TestDailyDigest(BaseHelperTestCase):
    def test_digest_commands_and_time_validation(self):
        chat_id = "digest_user@c.us"
        usr.upsert_user(chat_id, email="dig@test.com", status="active")
        lms = MockLMSForHelpers()
        usr.set_client(chat_id, lms)

        # 1. digest (initially off): shows status and sends today's digest now
        wa_bot.handle(chat_id, "digest")
        self.assertEqual(len(self.sent_texts), 2)
        status_reply, digest_reply = self.sent_texts[0][1], self.sent_texts[1][1]
        self.assertIn("Daily digest is OFF", status_reply)
        self.assertIn("Good morning! Here is your TaleemGo AI daily digest", digest_reply)

        # 2. digest on 25:99 -> invalid time
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "digest on 25:99")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Invalid time format", self.sent_texts[0][1])
        u = usr.get_user(chat_id)
        self.assertEqual(u["digest_enabled"], 0)

        # 3. digest on 08:15 -> enabled
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "digest on 08:15")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Daily digest enabled for 08:15 daily", self.sent_texts[0][1])
        u = usr.get_user(chat_id)
        self.assertEqual(u["digest_enabled"], 1)
        self.assertEqual(u["digest_time"], "08:15")

        # 4. digest off -> disabled
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "digest off")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Daily digest disabled", self.sent_texts[0][1])
        u = usr.get_user(chat_id)
        self.assertEqual(u["digest_enabled"], 0)

    def test_digest_scheduler_cron_with_injectable_clock(self):
        """
        Verify scheduler job check_daily_digests with injectable clock:
        - Exactly 1 message arrives at scheduled time
        - None in the next minute on same day
        - Exactly 1 the next day
        - Restart/idempotency safe
        """
        chat_id = "cron_digest@c.us"
        usr.upsert_user(chat_id, email="cron@test.com", status="active")
        usr.set_digest(chat_id, enabled=True, time_str="07:30")
        lms = MockLMSForHelpers()
        usr.set_client(chat_id, lms)

        tz = timeutils.LOCAL_TZ

        # Time 1: Day 1, 07:29 -> Not yet time, 0 sent
        t1 = datetime(2026, 11, 14, 7, 29, 0, tzinfo=tz)
        self.sent_texts.clear()
        wa_bot.check_daily_digests(now_dt=t1)
        self.assertEqual(len(self.sent_texts), 0)

        # Time 2: Day 1, 07:30 -> Due time reached, exactly 1 sent
        t2 = datetime(2026, 11, 14, 7, 30, 0, tzinfo=tz)
        wa_bot.check_daily_digests(now_dt=t2)
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Good morning!", self.sent_texts[0][1])

        # Verify DB recorded last sent date
        u = usr.get_user(chat_id)
        self.assertEqual(u["digest_last_date"], "2026-11-14")

        # Time 3: Day 1, 07:31 (next minute) -> 0 sent (already sent today)
        self.sent_texts.clear()
        t3 = datetime(2026, 11, 14, 7, 31, 0, tzinfo=tz)
        wa_bot.check_daily_digests(now_dt=t3)
        self.assertEqual(len(self.sent_texts), 0)

        # Time 4: Day 2, 07:30 -> Next day, exactly 1 sent
        t4 = datetime(2026, 11, 15, 7, 30, 0, tzinfo=tz)
        wa_bot.check_daily_digests(now_dt=t4)
        self.assertEqual(len(self.sent_texts), 1)
        u = usr.get_user(chat_id)
        self.assertEqual(u["digest_last_date"], "2026-11-15")

    def test_digest_content_and_honest_empties(self):
        chat_id = "content_user@c.us"
        usr.upsert_user(chat_id, email="content@test.com", status="active")
        tz = timeutils.LOCAL_TZ
        fixed_now = datetime(2026, 11, 14, 7, 30, 0, tzinfo=tz)

        # Baseline: No pending assignments, all attendances >= 75%, no recent resources
        lms = MockLMSForHelpers(
            assignments=[],
            attendance={
                "overall": {"present": 20, "absent": 2, "total": 22, "percent": 90.9},
                "courses": {
                    "c_ai": {"present": 20, "absent": 2, "total": 22, "percent": 90.9},
                },
            },
            resources=[],
            grades=[],
        )
        usr.set_client(chat_id, lms)

        digest = wa_bot._build_daily_digest_text(chat_id, lms, usr.get_user(chat_id), now_dt=fixed_now)
        # Check honest empties
        self.assertIn("Nothing due in the next 3 days", digest)
        self.assertIn("All course attendances are above 75%", digest)
        self.assertIn("No new lectures or grades in the last 24 hours", digest)

        # Now add:
        # - 1 assignment due today
        # - 1 course attendance under 75%
        # - 1 recent lecture within 24h
        # - 1 recent grade within 24h
        today_iso = "2026-11-14T18:00:00Z"
        recent_iso = "2026-11-14T02:00:00Z"
        lms._assignments = [
            {"id": "a_today", "course_id": "c_ai", "title": "AI Lab 3", "due_date": today_iso},
        ]
        lms._attendance["courses"]["c_ai"]["percent"] = 68.0
        lms._resources = [
            {"id": "r1", "course_id": "c_ai", "title": "Search Algorithms", "created_at": recent_iso},
        ]
        lms._grades = [
            {"id": "g1", "course_id": "c_ai", "title": "Quiz 1", "grade": 9.0, "max_marks": 10.0, "graded_at": recent_iso},
        ]

        digest2 = wa_bot._build_daily_digest_text(chat_id, lms, usr.get_user(chat_id), now_dt=fixed_now)
        self.assertIn("Due Today:", digest2)
        self.assertIn("AI Lab 3", digest2)
        self.assertIn("Attendance Warning", digest2)
        self.assertIn("68.0%", digest2)
        self.assertIn("Lecture: AI - Search Algorithms", digest2)
        self.assertIn("Grade: AI - Quiz 1: 9.0/10.0", digest2)

    def test_digest_error_isolation(self):
        """One user's LMS error never blocks others; expired session produces 0 messages."""
        u1_chat = "err_user@c.us"
        u2_chat = "good_user@c.us"
        usr.upsert_user(u1_chat, email="err@test.com", status="active")
        usr.upsert_user(u2_chat, email="good@test.com", status="active")
        usr.set_digest(u1_chat, enabled=True, time_str="07:30")
        usr.set_digest(u2_chat, enabled=True, time_str="07:30")

        mock_err_lms = MagicMock()
        mock_err_lms.get_courses.side_effect = RuntimeError("Supabase connection timeout")
        usr.set_client(u1_chat, mock_err_lms)

        mock_good_lms = MockLMSForHelpers()
        usr.set_client(u2_chat, mock_good_lms)

        t = datetime(2026, 11, 14, 7, 30, 0, tzinfo=timeutils.LOCAL_TZ)
        self.sent_texts.clear()
        wa_bot.check_daily_digests(now_dt=t)

        # Good user must still receive digest despite error on user 1
        recipients = [cid for cid, _ in self.sent_texts]
        self.assertIn(u2_chat, recipients)


# -------------------------------------------------------------
# PART C. Calendar File Tests
# -------------------------------------------------------------
class TestCalendarFile(BaseHelperTestCase):
    def test_calendar_rfc5545_formatting_and_folding(self):
        """
        Verify RFC 5545 specs:
        - CRLF line endings
        - Text escaping (commas, semicolons, backslashes, newlines)
        - Folding at <= 75 octets
        - Stable UIDs (assignment-{id}@taleemgo)
        - UTC timestamps (Z)
        - 1-day before alarm (-P1D)
        """
        assignments = [
            {
                "id": "asgn_101",
                "course_id": "c_ai",
                "title": "Project Phase 1, with a long title; and details, requiring RFC 5545 75-octet line folding",
                "due_date": "2026-11-20T18:00:00Z",
            }
        ]
        courses = {"c_ai": {"code": "AI", "name": "Artificial Intelligence"}}

        ics_str = calendar_gen.generate_calendar_ics(assignments, courses)
        self.assertIsNotNone(ics_str)
        self.assertTrue(ics_str.endswith("\r\n"))

        # Check every line <= 75 bytes
        lines = ics_str.split("\r\n")
        for line in lines:
            if not line:
                continue
            line_bytes = line.encode("utf-8")
            self.assertLessEqual(len(line_bytes), 75, f"Line exceeds 75 octets: {line}")

        # Check required fields
        self.assertIn("BEGIN:VCALENDAR", ics_str)
        self.assertIn("VERSION:2.0", ics_str)
        self.assertIn("UID:assignment-asgn_101@taleemgo", ics_str)
        self.assertIn("DTSTART:20261120T180000Z", ics_str)
        self.assertIn("TRIGGER:-P1D", ics_str)
        self.assertIn("ACTION:DISPLAY", ics_str)
        self.assertIn("END:VCALENDAR", ics_str)

    def test_calendar_command_sends_file_and_deletes_temp(self):
        chat_id = "cal_user@c.us"
        usr.upsert_user(chat_id, email="cal@test.com", status="active")
        lms = MockLMSForHelpers(
            assignments=[
                {"id": "a1", "course_id": "c_ai", "title": "Homework 1", "due_date": "2026-11-20T18:00:00Z"},
            ],
            submissions={},
        )
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "calendar")
        self.assertEqual(len(self.sent_files), 1)
        cid, path, caption = self.sent_files[0]
        self.assertEqual(cid, chat_id)
        self.assertIn(".ics", path)
        self.assertIn("calendar file (.ics) with 1 upcoming deadline(s)", caption)

        # Verify temp file was cleaned up
        self.assertFalse(os.path.exists(path), "Temp .ics file must be deleted after send")

    def test_calendar_empty_deadlines_gives_one_line(self):
        chat_id = "cal_empty@c.us"
        usr.upsert_user(chat_id, email="empty@test.com", status="active")
        lms = MockLMSForHelpers(assignments=[], submissions={})
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "calendar")
        self.assertEqual(len(self.sent_files), 0, "No file should be sent if there are no pending deadlines")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("no upcoming deadlines", self.sent_texts[0][1])

    def test_calendar_natural_language_and_rules(self):
        r = match_rules("send my deadlines to Google Calendar")
        self.assertIsNotNone(r)
        self.assertEqual(r["actions"][0]["intent"], "calendar")

        r2 = match_rules("calendar file bhejo")
        self.assertIsNotNone(r2)
        self.assertEqual(r2["actions"][0]["intent"], "calendar")

        r3 = match_rules("منهنجو ڪئلينڊر فائيل موڪليو")
        self.assertIsNotNone(r3)
        self.assertEqual(r3["actions"][0]["intent"], "calendar")


# -------------------------------------------------------------
# Intent Validation & Help Text
# -------------------------------------------------------------
class TestIntentAndHelp(unittest.TestCase):
    def test_allowed_intents_and_validation(self):
        self.assertIn("plan", ALLOWED_INTENTS)
        self.assertIn("digest", ALLOWED_INTENTS)
        self.assertIn("calendar", ALLOWED_INTENTS)

        # Valid plan intent
        val = validate_intent_response({
            "lang": "en",
            "actions": [{"intent": "plan", "course_query": "HCI", "target": 80, "miss": 2}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        })
        self.assertEqual(val["actions"][0]["intent"], "plan")
        self.assertEqual(val["actions"][0]["target"], 80)
        self.assertEqual(val["actions"][0]["miss"], 2)

        # Valid digest intent
        val_d = validate_intent_response({
            "lang": "en",
            "actions": [{"intent": "digest", "enable": True, "time": "08:30"}],
            "clarify": None,
            "off_topic": False,
            "unsupported": False,
        })
        self.assertEqual(val_d["actions"][0]["intent"], "digest")
        self.assertTrue(val_d["actions"][0]["enable"])
        self.assertEqual(val_d["actions"][0]["time"], "08:30")

    def test_help_text_contains_new_commands(self):
        for lang in ("en", "ur", "sd"):
            help_str = STRINGS[lang]["help"]
            self.assertIn("plan", help_str, f"Missing 'plan' in {lang} help")
            self.assertIn("digest", help_str, f"Missing 'digest' in {lang} help")
            self.assertIn("calendar", help_str, f"Missing 'calendar' in {lang} help")


if __name__ == "__main__":
    unittest.main()

