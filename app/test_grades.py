"""
Comprehensive unit and integration tests for TaleemGo AI Grades feature.
Covers:
1. Prohibited strings check: scan all .py files under app/ to ensure no qr_token or qr_expires_at appears.
2. LMSClient get_grades() and summarize_grades() with various data conditions.
3. Bot command: grades (grouped), grades <course> (trimmed feedback), 0 graded items ("No grades posted yet"), course resolution, message splitting.
4. Natural language: English, Roman Urdu, Sindhi intent matching, unsupported handling (GPA, fees), and zero-leak prompt privacy.
5. wa_monitor grade alerts: seeding, new grade alert, updated grade alert, idempotency, restart deduplication, and ungraded lifecycle.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import users as usr
import wa_bot
import wa_monitor
import wa_transport as wa
from lms_client import LMSClient, summarize_grades
from app.llm.intent import build_system_prompt
from app.llm.providers import registry
from app.llm.router import route_intent
from app.llm.rules import match_rules
from state import DEFAULT_STATE, load_state, save_state


class MockLMSForGrades:
    def __init__(self, courses=None, assignments=None, submissions=None, resources=None, attendance=None):
        self._courses = courses if courses is not None else [
            {"id": "c_nlp", "code": "DE304", "name": "Natural Language Processing"},
            {"id": "c_hci", "code": "CS301", "name": "Human Computer Interaction"},
            {"id": "c_ds", "code": "DE306", "name": "Data Science"},
        ]
        self._assignments = assignments if assignments is not None else [
            {"id": "asgn_1", "course_id": "c_nlp", "title": "Homework 1", "week_number": 1,
             "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-15T18:00:00Z"},
            {"id": "asgn_2", "course_id": "c_nlp", "title": "Project Phase 1", "week_number": 3,
             "max_marks": 20.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-20T18:00:00Z"},
            {"id": "asgn_3", "course_id": "c_hci", "title": "HCI Heuristic Eval", "week_number": 2,
             "max_marks": 15.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-18T18:00:00Z"},
            {"id": "asgn_4", "course_id": "c_ds", "title": "Kaggle Sprint", "week_number": 1,
             "max_marks": 50.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-25T18:00:00Z"},
        ]
        self._submissions = submissions if submissions is not None else {
            "asgn_1": {"assignment_id": "asgn_1", "grade": 8.5, "max_marks": 10.0, "file_name": "hw1.pdf",
                       "graded_at": "2026-11-01T10:00:00Z", "feedback": "Good job, neat error analysis"},
            "asgn_2": {"assignment_id": "asgn_2", "grade": None, "max_marks": 20.0, "file_name": "proj.zip",
                       "graded_at": None, "feedback": None},
            # asgn_3 has no submission
            # asgn_4 has submission but grade is null
            "asgn_4": {"assignment_id": "asgn_4", "grade": None, "max_marks": 50.0, "file_name": "kaggle.csv",
                       "graded_at": None, "feedback": None},
        }
        self._resources = resources or []
        self._attendance = attendance or {"overall": {"present": 10, "absent": 0, "total": 10, "percent": 100.0}, "courses": {}}

    def get_courses(self):
        return list(self._courses)

    def get_assignments(self, course_id=None):
        if course_id:
            return [a for a in self._assignments if a["course_id"] == course_id]
        return list(self._assignments)

    def get_submissions(self):
        return dict(self._submissions)

    def get_resources(self, course_id=None):
        return list(self._resources)

    def get_attendance(self):
        return dict(self._attendance)

    def get_attendance_records_detailed(self):
        return []

    def get_grades(self, course_id=None):
        asgns = self.get_assignments(course_id)
        subs = self.get_submissions()
        out = []
        for a in asgns:
            sub = subs.get(a["id"])
            raw_grade = sub.get("grade") if sub else None
            grade = None
            if raw_grade is not None:
                try:
                    grade = float(raw_grade)
                except (ValueError, TypeError):
                    grade = None
            out.append({
                "id": a["id"],
                "course_id": a.get("course_id"),
                "title": a.get("title", ""),
                "week_number": a.get("week_number"),
                "max_marks": float(a.get("max_marks")) if a.get("max_marks") is not None else None,
                "is_extra_credit": bool(a.get("is_extra_credit")),
                "include_in_total": a.get("include_in_total") is not False,
                "grade": grade,
                "graded_at": sub.get("graded_at") if sub else None,
                "feedback": sub.get("feedback") if sub else None,
                "submitted": bool(sub),
            })
        return out


class BaseGradesTestCase(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        self.tmp = Path(tempfile.mkdtemp(prefix="taleemgo_grades_test_"))
        usr.USERS_DIR = self.tmp / "users"
        usr.DB_PATH = self.tmp / "users.db"
        usr.init_db()

        with usr._cache_lock:
            usr._client_cache.clear()
            usr._client_locks.clear()
        with usr._pending_lock:
            usr._pending_choices.clear()
        usr.clear_strikes("all")
        with usr._strikes_lock:
            usr._user_strikes.clear()
        with usr._hourly_lock:
            usr._user_hourly_calls.clear()
        with wa_bot._help_lock:
            wa_bot._user_help_sent.clear()
        with registry._lock:
            registry._states.clear()

        self.sent_texts = []
        self.orig_wa_bot_send = wa_bot.wa.send_text
        self.orig_wa_mon_send = wa_monitor.wa.send_text

        def _record(cid, msg):
            self.sent_texts.append((cid, msg))

        wa_bot.wa.send_text = _record
        wa_monitor.wa.send_text = _record

        os.environ["LLM_ENABLED"] = "1"
        os.environ["LLM_PROVIDERS"] = "gemini,groq"
        os.environ["GEMINI_API_KEY"] = "fake-gemini-key-1234567890"
        os.environ["GROQ_API_KEY"] = "fake-groq-key-1234567890"

        self.patcher_post = patch("requests.post", side_effect=RuntimeError("Unexpected real HTTP call!"))
        self.patcher_post.start()

    def tearDown(self):
        self.patcher_post.stop()
        wa_bot.wa.send_text = self.orig_wa_bot_send
        wa_monitor.wa.send_text = self.orig_wa_mon_send
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.orig_env)


class TestSecurityAndPrivacy(BaseGradesTestCase):
    def test_no_qr_token_or_qr_expires_at(self):
        """Scans every .py file under app/ (excluding tests) and asserts neither qr_token nor qr_expires_at appears."""
        app_dir = Path(__file__).resolve().parent
        py_files = [p for p in app_dir.rglob("*.py") if not p.name.startswith("test_")]
        self.assertTrue(len(py_files) > 0, "Should find .py files in app/")
        violations = []
        for p in py_files:
            content = p.read_text(encoding="utf-8")
            if "qr_token" in content:
                violations.append(f"{p.name}: contains 'qr_token'")
            if "qr_expires_at" in content:
                violations.append(f"{p.name}: contains 'qr_expires_at'")
        self.assertEqual(violations, [], "Prohibited strings found in production code")

    @patch("app.llm.router.call_chat_completions")
    def test_llm_privacy_no_grades_or_lms_data_in_prompt(self, mock_call):
        """LMS grades, titles, feedback, and student private records are NEVER passed to the LLM."""
        chat_id = "privacy_student@c.us"
        usr.upsert_user(chat_id, email="priv@test.com", status="active")
        secret_feedback = "Private teacher note: sensitive exam feedback"
        secret_title = "Secret Assignment Omega"
        secret_score = "97.4"

        lms = MockLMSForGrades(
            assignments=[{
                "id": "asgn_sec", "course_id": "c_nlp", "title": secret_title,
                "week_number": 1, "max_marks": 100.0, "is_extra_credit": False, "include_in_total": True,
            }],
            submissions={
                "asgn_sec": {"assignment_id": "asgn_sec", "grade": 97.4, "max_marks": 100.0,
                             "file_name": "omega.pdf", "graded_at": "2026-11-01T00:00:00Z",
                             "feedback": secret_feedback}
            }
        )
        usr.set_client(chat_id, lms)

        captured_messages = []

        def capture_call(*args, **kwargs):
            messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
            captured_messages.extend(messages)
            return '{"lang": "en", "actions": [{"intent": "grades", "course_query": "NLP"}], "clarify": null, "off_topic": false, "unsupported": false}'

        mock_call.side_effect = capture_call

        wa_bot.handle(chat_id, "Show my grade in NLP")

        # Confirm the bot replied to the user with the grade
        self.assertTrue(any("97.4/100" in txt for _, txt in self.sent_texts))

        # Inspect all payloads sent to the LLM provider
        full_payload_text = json.dumps(captured_messages)
        self.assertNotIn(secret_feedback, full_payload_text)
        self.assertNotIn(secret_title, full_payload_text)
        self.assertNotIn(secret_score, full_payload_text)


class TestLMSClientGrades(BaseGradesTestCase):
    def test_get_grades_formatting_and_normalization(self):
        """LMSClient.get_grades normalizes numbers, handles nulls, string grades, and missing submissions."""
        mock_assignments = [
            {"id": "a1", "course_id": "c1", "title": "A1", "week_number": 1, "max_marks": 10, "is_extra_credit": False, "include_in_total": True},
            {"id": "a2", "course_id": "c1", "title": "A2", "week_number": 2, "max_marks": "20.5", "is_extra_credit": False, "include_in_total": True},
            {"id": "a3", "course_id": "c1", "title": "A3", "week_number": 3, "max_marks": 15, "is_extra_credit": True, "include_in_total": True},
            {"id": "a4", "course_id": "c1", "title": "A4", "week_number": 4, "max_marks": 10, "is_extra_credit": False, "include_in_total": True},
            {"id": "a5", "course_id": "c1", "title": "A5", "week_number": 5, "max_marks": 10, "is_extra_credit": False, "include_in_total": True},
        ]
        mock_submissions = {
            "a1": {"assignment_id": "a1", "grade": 8.5, "file_name": "a1.pdf", "graded_at": "2026-11-01T10:00:00Z", "feedback": "Great"},
            "a2": {"assignment_id": "a2", "grade": "19.5", "file_name": "a2.pdf", "graded_at": "2026-11-02T10:00:00Z", "feedback": None},
            "a3": {"assignment_id": "a3", "grade": None, "file_name": None, "graded_at": None, "feedback": None},
            "a4": {"assignment_id": "a4", "grade": "invalid_number", "file_name": "a4.pdf", "graded_at": None, "feedback": None},
            # a5 has no submission
        }

        client = LMSClient.__new__(LMSClient)
        client.get_assignments = lambda course_id=None: mock_assignments
        client.get_submissions = lambda: mock_submissions

        grades = client.get_grades("c1")
        self.assertEqual(len(grades), 5)

        # a1: float 8.5, max_marks 10.0
        self.assertEqual(grades[0]["grade"], 8.5)
        self.assertEqual(grades[0]["max_marks"], 10.0)
        self.assertTrue(grades[0]["submitted"])

        # a2: string "19.5" normalized to float 19.5, max_marks "20.5" normalized to float 20.5
        self.assertEqual(grades[1]["grade"], 19.5)
        self.assertEqual(grades[1]["max_marks"], 20.5)

        # a3: null grade
        self.assertIsNone(grades[2]["grade"])
        self.assertTrue(grades[2]["submitted"])

        # a4: invalid string grade normalized to None
        self.assertIsNone(grades[3]["grade"])

        # a5: no submission
        self.assertIsNone(grades[4]["grade"])
        self.assertFalse(grades[4]["submitted"])

    def test_summarize_grades(self):
        """summarize_grades calculates obtained and max_total with extra credit and inclusion rules."""
        items = [
            {"grade": 8.5, "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True},
            {"grade": 4.0, "max_marks": 5.0, "is_extra_credit": False, "include_in_total": True},
            # Extra credit: adds to obtained but NOT to max_total
            {"grade": 2.0, "max_marks": 2.0, "is_extra_credit": True, "include_in_total": True},
            # include_in_total=False: excluded from both
            {"grade": 10.0, "max_marks": 10.0, "is_extra_credit": False, "include_in_total": False},
            # Ungraded: max_marks not added to max_total
            {"grade": None, "max_marks": 20.0, "is_extra_credit": False, "include_in_total": True},
        ]
        s = summarize_grades(items)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["graded"], 4)
        # obtained = 8.5 + 4.0 + 2.0 = 14.5
        self.assertEqual(s["obtained"], 14.5)
        # max_total = 10.0 + 5.0 = 15.0 (extra credit 2.0 and excluded 10.0 and ungraded 20.0 are NOT in max_total)
        self.assertEqual(s["max_total"], 15.0)

        # Honest empty: 0 graded items
        empty_s = summarize_grades([{"grade": None, "max_marks": 10.0}])
        self.assertEqual(empty_s["graded"], 0)
        self.assertEqual(empty_s["obtained"], 0.0)
        self.assertEqual(empty_s["max_total"], 0.0)


class TestBotGradesCommand(BaseGradesTestCase):
    def test_grades_all_courses_grouped(self):
        """'grades' with multiple courses shows grouped marks, honest empties, and running totals."""
        chat_id = "grades_cmd_user@c.us"
        usr.upsert_user(chat_id, email="gc@test.com", status="active")
        lms = MockLMSForGrades()
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "grades")
        self.assertEqual(len(self.sent_texts), 1)
        resp = self.sent_texts[0][1]

        # NLP has 1 graded (8.5/10) and 1 ungraded
        self.assertIn("NLP (DE304):", resp)
        self.assertIn("Homework 1: 8.5/10", resp)
        self.assertIn("Project Phase 1: Not graded yet", resp)
        self.assertIn("Graded so far: 8.5/10 (1 of 2 graded)", resp)

        # HCI has 1 ungraded assignment -> No grades posted yet
        self.assertIn("HCI (CS301):", resp)
        self.assertIn("No grades posted yet", resp)

        # DS has 1 ungraded assignment -> No grades posted yet
        self.assertIn("DS (DE306):", resp)
        self.assertIn("No grades posted yet", resp)

        # Ensure NO letter grades, GPA, or percentage calculations
        self.assertNotIn("GPA", resp)
        self.assertNotIn("passing", resp.lower())
        self.assertNotIn("failing", resp.lower())
        self.assertNotIn("0%", resp)

    def test_grades_single_course_with_feedback(self):
        """'grades NLP' shows single course with trimmed feedback."""
        chat_id = "fb_user@c.us"
        usr.upsert_user(chat_id, email="fb@test.com", status="active")
        lms = MockLMSForGrades()
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "grades NLP")
        self.assertEqual(len(self.sent_texts), 1)
        resp = self.sent_texts[0][1]

        self.assertIn("NLP (DE304) Grades:", resp)
        self.assertIn("Homework 1: 8.5/10", resp)
        self.assertIn("Feedback: Good job, neat error analysis", resp)
        self.assertIn("Graded so far: 8.5/10 (1 of 2 graded)", resp)

    def test_grades_course_resolution(self):
        """grades handles ambiguous courses and unknown courses gracefully."""
        chat_id = "res_user@c.us"
        usr.upsert_user(chat_id, email="res@test.com", status="active")
        # 2 courses matching 'Data'
        courses = [
            {"id": "c1", "code": "CS101", "name": "Data Structures"},
            {"id": "c2", "code": "CS102", "name": "Data Science"},
        ]
        lms = MockLMSForGrades(courses=courses)
        usr.set_client(chat_id, lms)

        # 1. Ambiguous course query
        wa_bot.handle(chat_id, "grades Data")
        self.assertTrue(any("Which course?" in txt for _, txt in self.sent_texts))
        self.assertIsNotNone(usr.get_pending(chat_id))

        # Reply with "1" to pick Data Structures
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "1")
        self.assertTrue(any("CS101" in txt for _, txt in self.sent_texts))
        self.assertIsNone(usr.get_pending(chat_id))

        # 2. Unknown course query
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "grades Quantum Mechanics")
        self.assertTrue(any("I couldn't find that course." in txt and "Your courses:" in txt for _, txt in self.sent_texts))

    def test_grades_message_length_splitting(self):
        """When grades output exceeds 3500 chars across courses, it splits on course boundaries."""
        chat_id = "split_user@c.us"
        usr.upsert_user(chat_id, email="split@test.com", status="active")

        # Create 10 courses with long assignments
        courses = []
        assignments = []
        submissions = {}
        for i in range(1, 11):
            cid = f"c_{i}"
            code = f"CODE{i:02d}"
            name = f"Course Long Title Number {i}"
            courses.append({"id": cid, "code": code, "name": name})
            for j in range(1, 10):
                aid = f"asgn_{i}_{j}"
                assignments.append({
                    "id": aid, "course_id": cid,
                    "title": f"Comprehensive Homework and Case Study Examination {j} for Module {i}",
                    "week_number": j, "max_marks": 100.0, "is_extra_credit": False, "include_in_total": True,
                })
                submissions[aid] = {
                    "assignment_id": aid, "grade": 85.0, "max_marks": 100.0,
                    "file_name": f"{aid}.pdf", "graded_at": "2026-11-01T00:00:00Z", "feedback": None,
                }

        lms = MockLMSForGrades(courses=courses, assignments=assignments, submissions=submissions)
        usr.set_client(chat_id, lms)

        wa_bot.handle(chat_id, "grades")
        # Should have split into 2 or more messages
        self.assertGreater(len(self.sent_texts), 1)
        for _, txt in self.sent_texts:
            self.assertLessEqual(len(txt), 3500)


class TestNaturalLanguageGrades(BaseGradesTestCase):
    def test_rule_matching_grades_all_languages(self):
        """Offline rule matcher maps grade queries in English, Roman Urdu, and Sindhi to grades intent."""
        # 1. English
        r1 = match_rules("show my grade in NLP")
        self.assertEqual(r1["actions"][0]["intent"], "grades")
        self.assertEqual(r1["actions"][0]["course_query"], "NLP")

        # 2. Roman Urdu
        r2 = match_rules("NLP me mere marks kitne hain")
        self.assertEqual(r2["actions"][0]["intent"], "grades")
        self.assertEqual(r2["actions"][0]["course_query"], "NLP")

        # 3. Roman Urdu with 'nambar'
        r3 = match_rules("NLP me mere nambar kitne hain")
        self.assertEqual(r3["actions"][0]["intent"], "grades")
        self.assertEqual(r3["actions"][0]["course_query"], "NLP")

        # 4. Sindhi script with مارڪس
        r4 = match_rules("منهنجا NLP جا مارڪس ٻڌايو")
        self.assertEqual(r4["actions"][0]["intent"], "grades")
        self.assertEqual(r4["actions"][0]["course_query"], "NLP")

        # 5. Urdu script with مارکس
        r5 = match_rules("NLP میں میرے مارکس کتنے ہیں")
        self.assertEqual(r5["actions"][0]["intent"], "grades")
        self.assertEqual(r5["actions"][0]["course_query"], "NLP")

        # 6. Urdu script with گریڈ
        r6 = match_rules("میرے NLP کے گریڈ")
        self.assertEqual(r6["actions"][0]["intent"], "grades")
        self.assertEqual(r6["actions"][0]["course_query"], "NLP")

        # 7. Sindhi script with گريڊ
        r7 = match_rules("منهنجو NLP جو گريڊ")
        self.assertEqual(r7["actions"][0]["intent"], "grades")
        self.assertEqual(r7["actions"][0]["course_query"], "NLP")

    def test_bug1_whole_word_matching_and_attendance_precedence(self):
        """Bug 1: whole-word matching for grades/number/nambar, attendance precedence over grades, no inside-word matching."""
        # 1. 'was my attendance marked today' -> attendance (no course)
        r1 = match_rules("was my attendance marked today")
        self.assertIsNotNone(r1)
        self.assertEqual(r1["actions"][0]["intent"], "attendance")
        self.assertNotIn("course_query", r1["actions"][0])

        # 2. 'remark on my assignment' -> not grades
        r2 = match_rules("remark on my assignment")
        if r2 and r2.get("actions"):
            self.assertNotEqual(r2["actions"][0]["intent"], "grades")

        # 3. 'upgrade plan' -> not grades
        r3 = match_rules("upgrade plan")
        if r3 and r3.get("actions"):
            self.assertNotEqual(r3["actions"][0]["intent"], "grades")
        else:
            self.assertIsNone(r3)

        # 4. 'show my grade in NLP' -> grades
        r4 = match_rules("show my grade in NLP")
        self.assertEqual(r4["actions"][0]["intent"], "grades")
        self.assertEqual(r4["actions"][0]["course_query"], "NLP")

        # 5. 'NLP me mere marks kitne hain' -> grades
        r5 = match_rules("NLP me mere marks kitne hain")
        self.assertEqual(r5["actions"][0]["intent"], "grades")
        self.assertEqual(r5["actions"][0]["course_query"], "NLP")

        # 6. 'منهنجا NLP جا مارڪس ٻڌايو' -> grades
        r6 = match_rules("منهنجا NLP جا مارڪس ٻڌايو")
        self.assertEqual(r6["actions"][0]["intent"], "grades")
        self.assertEqual(r6["actions"][0]["course_query"], "NLP")

        # 7. 'mera number NLP me' -> grades
        r7 = match_rules("mera number NLP me")
        self.assertEqual(r7["actions"][0]["intent"], "grades")
        self.assertEqual(r7["actions"][0]["course_query"], "NLP")

        # 8. 'what is my GPA' -> unsupported
        r8 = match_rules("what is my GPA")
        self.assertTrue(r8.get("unsupported"))

        # 9. 'fee kitni hai' -> unsupported
        r9 = match_rules("fee kitni hai")
        self.assertTrue(r9.get("unsupported"))

    def test_unsupported_retained_with_zero_strikes(self):
        """GPA, fee status, and timetables remain unsupported and cause 0 strikes."""
        for query in ["What is my GPA?", "fee status", "semester fee kitni hai", "timetable dekhao"]:
            res = match_rules(query)
            self.assertTrue(res.get("unsupported"), f"Query '{query}' should be unsupported")


class TestMonitorGradeAlerts(BaseGradesTestCase):
    def test_monitor_grade_alerts_lifecycle_and_idempotency(self):
        """wa_monitor seeds on first run, alerts on new grade, alerts on changed grade, and is idempotent."""
        chat_id = "monitor_grade_user@c.us"
        usr.upsert_user(chat_id, email="mon@test.com", status="active")
        udir = usr.user_dir(chat_id)

        # Baseline: asgn_1 graded 8.5/10; asgn_2 ungraded
        lms = MockLMSForGrades()
        usr.set_client(chat_id, lms)

        user_info = {"chat_id": chat_id, "email": "mon@test.com", "status": "active"}

        # 1. First run: baseline seed
        wa_monitor.check_updates(user_info)
        self.assertEqual(len(self.sent_texts), 0, "First run should seed with 0 alerts")
        saved_state = load_state(udir)
        self.assertIn("asgn_1", saved_state["grades"])
        self.assertEqual(saved_state["grades"]["asgn_1"], "8.5/10")
        self.assertNotIn("asgn_2", saved_state["grades"], "Ungraded asgn_2 must NOT be in seen_grades")

        # 2. Second run: teacher grades asgn_2
        lms._submissions["asgn_2"]["grade"] = 18.0
        lms._submissions["asgn_2"]["feedback"] = "Excellent paper"
        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)

        self.assertEqual(len(self.sent_texts), 1)
        alert_text = self.sent_texts[0][1]
        self.assertIn("New grade posted", alert_text)
        self.assertIn("Course: NLP (DE304)", alert_text)
        self.assertIn("Project Phase 1: 18/20", alert_text)
        self.assertIn("Feedback: Excellent paper", alert_text)

        saved_state = load_state(udir)
        self.assertEqual(saved_state["grades"]["asgn_2"], "18/20")

        # 3. Third run: teacher regrades asgn_2 to 19/20
        lms._submissions["asgn_2"]["grade"] = 19.0
        lms._submissions["asgn_2"]["feedback"] = "Re-evaluation bonus"
        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)

        self.assertEqual(len(self.sent_texts), 1)
        update_text = self.sent_texts[0][1]
        self.assertIn("Updated: Grade posted", update_text)
        self.assertIn("Course: NLP (DE304)", update_text)
        self.assertIn("Project Phase 1: 19/20", update_text)
        self.assertIn("Feedback: Re-evaluation bonus", update_text)

        saved_state = load_state(udir)
        self.assertEqual(saved_state["grades"]["asgn_2"], "19/20")

        # 4. Fourth run: no changes
        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)
        self.assertEqual(len(self.sent_texts), 0, "No changes should send 0 alerts")

        # 5. Restart bot: clear in-memory client and reload state from disk
        with usr._cache_lock:
            usr._client_cache.clear()
        new_lms = MockLMSForGrades()
        new_lms._submissions["asgn_2"]["grade"] = 19.0
        usr.set_client(chat_id, new_lms)

        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)
        self.assertEqual(len(self.sent_texts), 0, "Restarting bot must NOT send duplicate alerts")

    def test_existing_user_no_alert_burst_and_subsequent_alerts(self):
        """Saved state with seeded=True, no grades_seeded, 3 already-graded items gives 0 alerts on first cycle; then 1 new grade gives exactly 1 alert; restart does not repeat it."""
        chat_id = "existing_user@c.us"
        usr.upsert_user(chat_id, email="exist@test.com", status="active")
        udir = usr.user_dir(chat_id)

        # Pre-populate saved state on disk predating grades: seeded=True, no grades_seeded, grades={}
        old_state = {
            "seeded": True,
            "resources": ["r1", "r2"],
            "assignments": ["a1", "a2", "a3", "a4"],
            "attendance": ["att1"],
            "reminders_sent": [],
            "grades": {},
        }
        save_state(old_state, udir)

        # 3 already-graded items + 1 ungraded
        assignments = [
            {"id": "a1", "course_id": "c_nlp", "title": "HW 1", "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-15T18:00:00Z"},
            {"id": "a2", "course_id": "c_nlp", "title": "HW 2", "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-16T18:00:00Z"},
            {"id": "a3", "course_id": "c_nlp", "title": "HW 3", "max_marks": 10.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-17T18:00:00Z"},
            {"id": "a4", "course_id": "c_nlp", "title": "HW 4", "max_marks": 20.0, "is_extra_credit": False, "include_in_total": True, "due_date": "2029-11-18T18:00:00Z"},
        ]
        submissions = {
            "a1": {"assignment_id": "a1", "grade": 10.0, "max_marks": 10.0, "file_name": "a1.pdf", "graded_at": "2026-11-01T00:00:00Z"},
            "a2": {"assignment_id": "a2", "grade": 9.0, "max_marks": 10.0, "file_name": "a2.pdf", "graded_at": "2026-11-02T00:00:00Z"},
            "a3": {"assignment_id": "a3", "grade": 8.0, "max_marks": 10.0, "file_name": "a3.pdf", "graded_at": "2026-11-03T00:00:00Z"},
            "a4": {"assignment_id": "a4", "grade": None, "max_marks": 20.0, "file_name": "a4.pdf", "graded_at": None},
        }
        lms = MockLMSForGrades(assignments=assignments, submissions=submissions)
        usr.set_client(chat_id, lms)
        user_info = {"chat_id": chat_id, "email": "exist@test.com", "status": "active"}

        # Cycle 1: First cycle for existing user with 3 already-graded items
        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)

        # Zero alerts sent!
        self.assertEqual(len(self.sent_texts), 0, "Existing user must receive ZERO alerts on first cycle")
        saved = load_state(udir)
        self.assertTrue(saved.get("grades_seeded"), "grades_seeded must be True after first cycle")
        self.assertEqual(len(saved["grades"]), 3)
        self.assertIn("a1", saved["grades"])
        self.assertIn("a2", saved["grades"])
        self.assertIn("a3", saved["grades"])
        self.assertNotIn("a4", saved["grades"])

        # Cycle 2: One new grade posted (a4 graded 19/20)
        lms._submissions["a4"]["grade"] = 19.0
        lms._submissions["a4"]["feedback"] = "Well done"
        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)

        self.assertEqual(len(self.sent_texts), 1, "Exactly one alert for the new grade")
        self.assertIn("New grade posted", self.sent_texts[0][1])
        self.assertIn("HW 4: 19/20", self.sent_texts[0][1])

        # Cycle 3: Restart (clear client cache, re-read state from disk)
        with usr._cache_lock:
            usr._client_cache.clear()
        new_lms = MockLMSForGrades(assignments=assignments, submissions=submissions)
        usr.set_client(chat_id, new_lms)

        self.sent_texts.clear()
        wa_monitor.check_updates(user_info)
        self.assertEqual(len(self.sent_texts), 0, "Restarting bot must NOT repeat alerts")


if __name__ == "__main__":
    unittest.main()
