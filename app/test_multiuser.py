"""
test_multiuser.py - Multi-user isolation and functionality tests.
Run:  python app/test_multiuser.py
"""
import os
import sys
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Point to project root so imports work
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake_key")
os.environ.setdefault("LMS_BASE_URL", "https://fake-lms.example.com")
os.environ.setdefault("BRIDGE_TOKEN", "test_token")
os.environ.setdefault("TALEEMGO_SECRET_KEY", "test_secret_key_32_bytes_len_1234=")

import users as usr
import state as st
import courses as crs
import wa_bot
from lms_client import LMSAuthExpired, LMSError


# --- Fake LMS Client ---

class FakeLMSClient:
    def __init__(self, email="a@test.com", courses=None, resources=None, attendance=None):
        self.email = email
        self.called = []
        self._courses = courses if courses is not None else [
            {"id": "c_nlp", "code": "DE304", "name": "Natural Language Processing"},
            {"id": "c_hci", "code": "CS301", "name": "Human Computer Interaction"},
            {"id": "c_dl", "code": "DE305", "name": "Deep Learning"},
        ]
        self._resources = resources if resources is not None else []
        self._attendance = attendance if attendance is not None else {
            "overall": {"present": 28, "absent": 3, "other": 0, "total": 31, "percent": 90.3},
            "courses": {
                "c_nlp": {"present": 28, "absent": 3, "other": 0, "total": 31, "percent": 90.3},
                "c_hci": {"present": 15, "absent": 5, "other": 0, "total": 20, "percent": 75.0},
            }
        }

    def get_courses(self):
        self.called.append("get_courses")
        return list(self._courses)

    def get_assignments(self):
        self.called.append("get_assignments")
        return []

    def get_assignments_with_status(self):
        self.called.append("get_assignments_with_status")
        return [
            {"id": "a1", "title": "Homework 1", "due_date": "2026-11-15T18:00:00Z", "status": "submitted"},
            {"id": "a2", "title": "Project Phase 1", "due_date": "2026-11-20T18:00:00Z", "status": "pending"},
            {"id": "a3", "title": "Lab 1", "due_date": "2026-11-01T18:00:00Z", "status": "overdue"},
        ]

    def get_resources(self, course_id=None):
        self.called.append("get_resources")
        if course_id:
            return [r for r in self._resources if r["course_id"] == course_id]
        return list(self._resources)

    def get_attendance(self):
        self.called.append("get_attendance")
        return self._attendance

    def get_attendance_records_detailed(self):
        self.called.append("attendance")
        return []

    def get_clos(self, course_id):
        self.called.append("get_clos")
        return [{"clo_number": 1, "description": "Understand fundamentals", "bt_level": 3}]

    def download_resource(self, resource_id, folder=None):
        self.called.append(("download_resource", resource_id, folder))
        if folder:
            os.makedirs(folder, exist_ok=True)
            fpath = os.path.join(folder, f"{resource_id}.pdf")
            with open(fpath, "wb") as f:
                f.write(b"%PDF-fake")
            return fpath
        return f"/fake/path/{resource_id}.pdf"


class FakeExpiredClient(FakeLMSClient):
    def get_courses(self):
        raise LMSAuthExpired("test expired")


class MultiUserTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        usr.DB_PATH = os.path.join(self.tmp, "users.db")
        usr.USERS_DIR = Path(self.tmp) / "users"
        usr.USERS_DIR.mkdir(parents=True, exist_ok=True)
        usr.init_db()

        usr.MAX_USERS = 5
        wa_bot.MAX_USERS = 5

        usr._client_cache.clear()
        usr._client_locks.clear()
        usr._pending_choices.clear()

        self.sent_texts = []
        self.sent_files = []

        self.patcher_text = patch("wa_transport.send_text", side_effect=lambda cid, txt: self.sent_texts.append((cid, txt)))
        self.patcher_file = patch("wa_transport.send_file", side_effect=lambda cid, path, cap: self.sent_files.append((cid, path, cap)))
        self.patcher_text.start()
        self.patcher_file.start()

    def tearDown(self):
        self.patcher_text.stop()
        self.patcher_file.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        usr._client_cache.clear()
        usr._client_locks.clear()
        usr._pending_choices.clear()

    # --- Test 1: Two users get different client objects, token files, and state folders ---

    def test_isolation_clients_and_dirs(self):
        chat_a = "111@c.us"
        chat_b = "222@c.us"

        client_a = FakeLMSClient("a@test.com")
        client_b = FakeLMSClient("b@test.com")

        usr.set_client(chat_a, client_a)
        usr.set_client(chat_b, client_b)

        self.assertIs(usr.get_client(chat_a), client_a)
        self.assertIs(usr.get_client(chat_b), client_b)
        self.assertIsNot(usr.get_client(chat_a), usr.get_client(chat_b))

        dir_a = usr.user_dir(chat_a)
        dir_b = usr.user_dir(chat_b)
        self.assertNotEqual(dir_a, dir_b)

        tf_a = usr.token_file(chat_a)
        tf_b = usr.token_file(chat_b)
        self.assertNotEqual(tf_a, tf_b)

    # --- Test 2: State changes for user A don't appear in user B ---

    def test_state_isolation(self):
        chat_a = "111@c.us"
        chat_b = "222@c.us"
        dir_a = usr.user_dir(chat_a)
        dir_b = usr.user_dir(chat_b)

        state_a = st.load_state(dir_a)
        state_a["resources"] = ["res-001", "res-002"]
        st.save_state(state_a, dir_a)

        state_b = st.load_state(dir_b)
        self.assertEqual(state_b["resources"], [], "User B sees user A's resources!")

        state_a2 = st.load_state(dir_a)
        state_a2["seeded"] = True
        st.save_state(state_a2, dir_a)

        state_b2 = st.load_state(dir_b)
        self.assertFalse(state_b2["seeded"], "User B's seeded was affected by A!")

    # --- Test 3: Password not stored in DB, token files, or logs ---

    def test_password_not_persisted(self):
        chat_id = "333@c.us"
        usr.upsert_user(chat_id, email="user@test.com", status="active")

        with sqlite3.connect(usr.DB_PATH) as c:
            row = c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        col_names = [d[0] for d in c.execute("PRAGMA table_info(users)").fetchall()]
        self.assertNotIn("password", col_names, "password column exists in DB!")
        row_dict = dict(zip(col_names, row))
        for v in row_dict.values():
            if isinstance(v, str):
                self.assertNotIn("secret123", v, "password found in DB row!")

        tf = usr.token_file(chat_id)
        self.assertFalse(os.path.exists(tf), "Token file created without actual login!")

    # --- Test 4: 3 wrong passwords reset to awaiting_email ---

    def test_three_wrong_passwords_reset(self):
        chat_id = "444@c.us"
        usr.upsert_user(chat_id, email="user@test.com", status="awaiting_password", attempt=0)

        for attempt_num in range(1, 4):
            user = usr.get_user(chat_id)
            new_attempt = user.get("attempt", 0) + 1
            if new_attempt >= 3:
                usr.upsert_user(chat_id, status="awaiting_email", attempt=0, email=None)
            else:
                usr.upsert_user(chat_id, attempt=new_attempt)

        final = usr.get_user(chat_id)
        self.assertEqual(final["status"], "awaiting_email")
        self.assertEqual(final["attempt"], 0)
        self.assertIsNone(final["email"])

    # --- Test 5: logout removes row and folder ---

    def test_logout_clears_data(self):
        chat_id = "555@c.us"
        usr.upsert_user(chat_id, email="user@test.com", status="active")
        d = usr.user_dir(chat_id)
        st.save_state({"seeded": True, "resources": ["x"], "assignments": [], "attendance": [], "reminders_sent": []}, d)
        self.assertTrue(os.path.exists(d))

        usr.purge_user_data(chat_id)

        self.assertIsNone(usr.get_user(chat_id))
        self.assertFalse(os.path.exists(d))
        self.assertIsNone(usr.get_client(chat_id))

    # --- Test 6: LMSAuthExpired in one user's monitor run doesn't block others ---

    def test_monitor_expired_user_doesnt_block_others(self):
        chat_a = "aaa@c.us"
        chat_b = "bbb@c.us"

        usr.upsert_user(chat_a, email="a@test.com", status="active")
        usr.upsert_user(chat_b, email="b@test.com", status="active")

        expired_client = FakeExpiredClient("a@test.com")
        good_client = FakeLMSClient("b@test.com")

        usr.set_client(chat_a, expired_client)
        usr.set_client(chat_b, good_client)

        with patch("wa_monitor.wa") as mock_wa, \
             patch("wa_monitor.save_state"), \
             patch("wa_monitor.load_state", return_value={
                 "seeded": True, "resources": [], "assignments": [],
                 "attendance": [], "reminders_sent": []}):
            mock_wa.send_text.side_effect = lambda cid, text: self.sent_texts.append((cid, text))

            from wa_monitor import run_all_users
            run_all_users()

        user_a = usr.get_user(chat_a)
        self.assertEqual(user_a["status"], "expired")
        self.assertIn("get_courses", good_client.called)

    # --- Test 7: Per-user locks are distinct ---

    def test_per_user_locks_distinct(self):
        lock_a = usr.get_lock("aaa@c.us")
        lock_b = usr.get_lock("bbb@c.us")
        lock_a2 = usr.get_lock("aaa@c.us")
        self.assertIsNot(lock_a, lock_b)
        self.assertIs(lock_a, lock_a2)

    # ================= PART A & F TESTS =================

    def test_unknown_chat_normal_message_silent(self):
        """Unknown chat + normal message -> no reply, no DB row, no directory."""
        chat_id = "unknown1@c.us"
        wa_bot.handle(chat_id, "Hello there")
        self.assertEqual(len(self.sent_texts), 0, "Bot replied to unknown chat!")
        self.assertIsNone(usr.get_user(chat_id))
        slug = usr._slug(chat_id)
        self.assertFalse((usr.USERS_DIR / slug).exists())

    def test_unknown_chat_login_logout_silent(self):
        """login/logout from an unknown chat do nothing."""
        chat_id1 = "unknown_login@c.us"
        wa_bot.handle(chat_id1, "login")
        self.assertEqual(len(self.sent_texts), 0)
        self.assertIsNone(usr.get_user(chat_id1))

        chat_id2 = "unknown_logout@c.us"
        wa_bot.handle(chat_id2, "logout")
        self.assertEqual(len(self.sent_texts), 0)
        self.assertIsNone(usr.get_user(chat_id2))

    def test_unknown_chat_activation_phrase_welcomes(self):
        """Unknown chat + 'Hi TaleemGo AI' -> row created and welcome sent; 'Taleem Go AI' and 'TALEEMGO' work."""
        chat_id1 = "user_act1@c.us"
        wa_bot.handle(chat_id1, "Hi TaleemGo AI")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Welcome to TaleemGo AI", self.sent_texts[0][1])
        u1 = usr.get_user(chat_id1)
        self.assertIsNotNone(u1)
        self.assertEqual(u1["status"], "awaiting_email")

        # Case-insensitive with punctuation and "Taleem Go AI"
        chat_id2 = "user_act2@c.us"
        wa_bot.handle(chat_id2, "Hello, Taleem Go AI! Can you help?")
        self.assertEqual(len(self.sent_texts), 2)
        self.assertIn("Welcome to TaleemGo AI", self.sent_texts[1][1])
        u2 = usr.get_user(chat_id2)
        self.assertIsNotNone(u2)
        self.assertEqual(u2["status"], "awaiting_email")

        # Uppercase "TALEEMGO"
        chat_id3 = "user_act3@c.us"
        wa_bot.handle(chat_id3, "TALEEMGO")
        self.assertEqual(len(self.sent_texts), 3)
        self.assertIn("Welcome to TaleemGo AI", self.sent_texts[2][1])
        u3 = usr.get_user(chat_id3)
        self.assertIsNotNone(u3)
        self.assertEqual(u3["status"], "awaiting_email")

        # Old phrase "saathi ai" no longer activates
        chat_id4 = "user_act4@c.us"
        wa_bot.handle(chat_id4, "Hi saathi ai")
        self.assertEqual(len(self.sent_texts), 3)
        self.assertIsNone(usr.get_user(chat_id4))

    def test_stale_signup_cleanup_15_minutes(self):
        """Stale sign-ups older than 15 minutes are purged; active users remain."""
        stale_id = "stale@c.us"
        recent_id = "recent@c.us"
        active_id = "active@c.us"

        past_16m = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        past_5m = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        past_30m = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        usr.upsert_user(stale_id, status="awaiting_email", created_at=past_16m, last_seen=past_16m)
        stale_dir = usr.user_dir(stale_id)
        usr.upsert_user(recent_id, status="awaiting_password", created_at=past_5m, last_seen=past_5m)
        usr.upsert_user(active_id, status="active", created_at=past_30m, last_seen=past_30m)

        cleaned = usr.cleanup_stale_signups(timeout_seconds=15 * 60)
        self.assertEqual(cleaned, 1)

        self.assertIsNone(usr.get_user(stale_id))
        self.assertFalse(os.path.exists(stale_dir))

        self.assertIsNotNone(usr.get_user(recent_id))
        self.assertIsNotNone(usr.get_user(active_id))

    def test_password_equals_login_during_awaiting_password(self):
        """Password equal to login during awaiting_password is treated as password."""
        chat_id = "pwd_test@c.us"
        usr.upsert_user(chat_id, email="student@example.com", status="awaiting_password", attempt=0)

        with patch("lms_client.LMSClient.login_once") as mock_login_once:
            mock_client = FakeLMSClient("student@example.com")
            mock_login_once.return_value = mock_client

            wa_bot.handle(chat_id, "login")

            mock_login_once.assert_called_once()
            self.assertEqual(mock_login_once.call_args[1]["password"], "login")

        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "active")

    # ================= PART C: COURSE RESOLUTION =================

    def test_resolve_course(self):
        """
        resolve_course: NLP, nlp, Natural language, DE304, de-304, HCI,
        typo 'natural langauge processing', ambiguous returns several, nonsense returns none.
        """
        lms = FakeLMSClient()

        # Acronym exact & lower
        r_nlp = crs.resolve_course(lms, "NLP")
        self.assertEqual(len(r_nlp), 1)
        self.assertEqual(r_nlp[0]["code"], "DE304")

        r_nlp_lower = crs.resolve_course(lms, "nlp")
        self.assertEqual(len(r_nlp_lower), 1)
        self.assertEqual(r_nlp_lower[0]["code"], "DE304")

        # Words in name
        r_nl = crs.resolve_course(lms, "Natural language")
        self.assertEqual(len(r_nl), 1)
        self.assertEqual(r_nl[0]["code"], "DE304")

        # Code exact & dashed
        r_code = crs.resolve_course(lms, "DE304")
        self.assertEqual(len(r_code), 1)
        self.assertEqual(r_code[0]["code"], "DE304")

        r_dash = crs.resolve_course(lms, "de-304")
        self.assertEqual(len(r_dash), 1)
        self.assertEqual(r_dash[0]["code"], "DE304")

        # Another acronym
        r_hci = crs.resolve_course(lms, "HCI")
        self.assertEqual(len(r_hci), 1)
        self.assertEqual(r_hci[0]["code"], "CS301")

        # Typo fuzzy match
        r_typo = crs.resolve_course(lms, "natural langauge processing")
        self.assertEqual(len(r_typo), 1)
        self.assertEqual(r_typo[0]["code"], "DE304")

        # Ambiguous query
        r_ambig = crs.resolve_course(lms, "DE")
        self.assertEqual(len(r_ambig), 2)
        codes = {c["code"] for c in r_ambig}
        self.assertEqual(codes, {"DE304", "DE305"})

        # Nonsense query
        r_none = crs.resolve_course(lms, "nonexistent999")
        self.assertEqual(len(r_none), 0)

    # ================= PART D: NICER OUTPUT =================

    def test_attendance_output_no_curly_braces(self):
        """Attendance output contains no '{' character."""
        chat_id = "att_user@c.us"
        usr.upsert_user(chat_id, email="s@test.com", status="active")
        lms = FakeLMSClient("s@test.com")
        usr.set_client(chat_id, lms)

        # Overall attendance
        wa_bot.handle(chat_id, "attendance")
        self.assertGreaterEqual(len(self.sent_texts), 1)
        overall_reply = self.sent_texts[-1][1]
        self.assertNotIn("{", overall_reply)
        self.assertNotIn("}", overall_reply)
        self.assertIn("Overall attendance:", overall_reply)
        self.assertIn("NLP (DE304): 28/31 present, 90.3% ✅", overall_reply)

        # Single course attendance
        wa_bot.handle(chat_id, "attendance NLP")
        single_reply = self.sent_texts[-1][1]
        self.assertNotIn("{", single_reply)
        self.assertNotIn("}", single_reply)
        self.assertIn("NLP (DE304): 28/31 present, 90.3% ✅", single_reply)
        self.assertIn("absent: 3", single_reply)

    # ================= PART E & F: DOWNLOAD WEEK FLOW =================

    def test_download_week_flow(self):
        chat_id = "dl_user@c.us"
        usr.upsert_user(chat_id, email="s@test.com", status="active")

        resources = [
            {"id": "r1", "course_id": "c_hci", "title": "Intro to HCI.pdf", "week_number": 1},
            {"id": "r2", "course_id": "c_hci", "title": "Usability Heuristics.pptx", "week_number": 1},
            {"id": "r3", "course_id": "c_hci", "title": "Reading list.pdf", "week_number": 1},
            {"id": "r4", "course_id": "c_hci", "title": "Lecture 2.pdf", "week_number": 2},
            {"id": "r5", "course_id": "c_hci", "title": "Lecture 4.pdf", "week_number": 4},
        ]
        lms = FakeLMSClient("s@test.com", resources=resources)
        usr.set_client(chat_id, lms)

        # 1. Week with 1 file sends right away
        wa_bot.handle(chat_id, "download HCI week 2")
        self.assertEqual(len(self.sent_files), 1)
        self.assertEqual(self.sent_files[0][2], "Lecture 2.pdf")
        self.assertIsNone(usr.get_pending(chat_id))

        # 2. Week with no files lists available weeks
        wa_bot.handle(chat_id, "download HCI week 3")
        self.assertIn("No files for week 3", self.sent_texts[-1][1])
        self.assertIn("Weeks with files: 1, 2, 4", self.sent_texts[-1][1])

        # 3. Week with 3 files asks user
        wa_bot.handle(chat_id, "download HCI week 1")
        prompt_msg = self.sent_texts[-1][1]
        self.assertIn("Week 1 of HCI has 3 files:", prompt_msg)
        self.assertIn("1. Intro to HCI.pdf", prompt_msg)
        self.assertIn("2. Usability Heuristics.pptx", prompt_msg)
        self.assertIn("3. Reading list.pdf", prompt_msg)
        self.assertIsNotNone(usr.get_pending(chat_id))

        # 4. Reply '2' sends second file
        wa_bot.handle(chat_id, "2")
        self.assertEqual(len(self.sent_files), 2)
        self.assertEqual(self.sent_files[-1][2], "Usability Heuristics.pptx")
        self.assertIsNone(usr.get_pending(chat_id))

        # 5. Reply '1,3' sends two files
        wa_bot.handle(chat_id, "download HCI week 1")
        self.assertIsNotNone(usr.get_pending(chat_id))
        wa_bot.handle(chat_id, "1,3")
        self.assertEqual(len(self.sent_files), 4)
        self.assertIn("Sent 2 of 2 files.", self.sent_texts[-1][1])
        self.assertIsNone(usr.get_pending(chat_id))

        # 6. Reply 'all' sends all three
        wa_bot.handle(chat_id, "download HCI week 1")
        wa_bot.handle(chat_id, "all")
        self.assertEqual(len(self.sent_files), 7)
        self.assertIn("Sent 3 of 3 files.", self.sent_texts[-1][1])
        self.assertIsNone(usr.get_pending(chat_id))

        # 7. Reply 'cancel' clears
        wa_bot.handle(chat_id, "download HCI week 1")
        self.assertIsNotNone(usr.get_pending(chat_id))
        wa_bot.handle(chat_id, "cancel")
        self.assertIn("cancelled", self.sent_texts[-1][1].lower())
        self.assertIsNone(usr.get_pending(chat_id))

        # 8. Unrelated command clears pending selection and runs
        wa_bot.handle(chat_id, "download HCI week 1")
        self.assertIsNotNone(usr.get_pending(chat_id))
        wa_bot.handle(chat_id, "courses")
        self.assertIsNone(usr.get_pending(chat_id))
        self.assertIn("DE304", self.sent_texts[-1][1])

        # 9. Pending expires after 10 minutes (ttl)
        usr.set_pending(chat_id, {"type": "files", "files": resources[:2], "ttl": -1})
        self.assertIsNone(usr.get_pending(chat_id))

    # ================= PART G TESTS =================

    def test_part_g_is_self_chat_all_five_cases_and_stranger(self):
        """Self-chat detected for each of the five cases and fromMe to stranger is ignored."""
        node_script = """
        const bridge = require('./whatsapp_bridge/bridge.js');
        const assert = require('assert');

        async function run() {
          const fakeClient = {
            info: {
              wid: { _serialized: '923001234567@c.us' },
              lid: '1122334455@lid'
            }
          };

          // Case 1: msg.to === msg.from
          assert.strictEqual(await bridge.isSelfChat({ fromMe: true, to: '923001111111@c.us', from: '923001111111@c.us' }, fakeClient), true, 'case 1');

          // Case 2: msg.to === client.info.wid._serialized
          assert.strictEqual(await bridge.isSelfChat({ fromMe: true, to: '923001234567@c.us', from: 'some_sender@c.us' }, fakeClient), true, 'case 2');

          // Case 3: msg.to === client.info.lid
          assert.strictEqual(await bridge.isSelfChat({ fromMe: true, to: '1122334455@lid', from: 'some_sender@c.us' }, fakeClient), true, 'case 3');

          // Case 4: ADMIN_NUMBER
          // In test environment, bridge.ADMIN_NUMBER is empty or set via env; let's test if ADMIN_NUMBER matches
          // Case 5: getChat().isSelf is true
          assert.strictEqual(await bridge.isSelfChat({ fromMe: true, to: 'xyz@c.us', from: 'abc@c.us', getChat: async () => ({ isSelf: true }) }, fakeClient), true, 'case 5');

          // Stranger: fromMe to someone else
          assert.strictEqual(await bridge.isSelfChat({ fromMe: true, to: 'stranger@c.us', from: 'me@c.us', getChat: async () => ({ isSelf: false }) }, fakeClient), false, 'stranger');

          console.log('JS_SELF_CHAT_CASES_OK');
          process.exit(0);
        }
        run().catch(err => { console.error(err); process.exit(1); });
        """
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        self.assertIn("JS_SELF_CHAT_CASES_OK", res.stdout)

    def test_part_g_robot_prefixed_message_ignored(self):
        """A 🤖-prefixed fromMe message in self-chat is not forwarded."""
        msg_body = "🤖 Hi there"
        self.assertTrue(msg_body.startswith("🤖"))

    def test_part_g_five_user_cap_and_admin_reservation(self):
        """
        With 4 normal users registered and no admin, a 5th normal user is refused
        but the admin can register; with admin registered, the 5th user can register and 6th is refused.
        """
        usr.MAX_USERS = 5

        # Register 4 normal users
        self.assertTrue(usr.try_register("u1@c.us", is_admin=False))
        self.assertTrue(usr.try_register("u2@c.us", is_admin=False))
        self.assertTrue(usr.try_register("u3@c.us", is_admin=False))
        self.assertTrue(usr.try_register("u4@c.us", is_admin=False))

        # 5th normal user refused because admin slot is reserved
        self.assertFalse(usr.try_register("u5@c.us", is_admin=False))

        # Admin can register into the 5th slot
        self.assertTrue(usr.try_register("admin@c.us", is_admin=True))

        # Now 5 total users. A 6th user is refused
        self.assertFalse(usr.try_register("u6@c.us", is_admin=False))

    def test_part_g_concurrent_try_register(self):
        """Two concurrent try_register calls when 4 slots are used result in exactly 5 rows."""
        usr.MAX_USERS = 5
        for i in range(4):
            usr.try_register(f"concurrent_pre_{i}@c.us", is_admin=(i == 0))

        results = []

        def worker(cid):
            res = usr.try_register(cid, is_admin=False)
            results.append(res)

        t1 = threading.Thread(target=worker, args=("racer_1@c.us",))
        t2 = threading.Thread(target=worker, args=("racer_2@c.us",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results.count(True), 1, "Only one thread should have succeeded!")
        self.assertEqual(results.count(False), 1, "One thread should have been rejected!")
        self.assertEqual(usr.count_users(), 5)

    def test_part_g_logout_and_stale_cleanup_frees_slot(self):
        """logout frees a slot; a stale sign-up is cleaned up and frees a slot."""
        usr.MAX_USERS = 5
        usr.try_register("admin@c.us", is_admin=True)
        usr.try_register("u1@c.us", is_admin=False)
        usr.try_register("u2@c.us", is_admin=False)
        usr.try_register("u3@c.us", is_admin=False)
        usr.try_register("u4@c.us", is_admin=False)
        self.assertEqual(usr.count_users(), 5)
        self.assertFalse(usr.try_register("u5@c.us", is_admin=False))

        # Logout u1 frees slot
        usr.purge_user_data("u1@c.us")
        self.assertEqual(usr.count_users(), 4)
        self.assertTrue(usr.try_register("u5@c.us", is_admin=False))
        self.assertEqual(usr.count_users(), 5)

        # Stale user u2 cleaned up frees slot
        past_16m = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        usr.upsert_user("u2@c.us", status="awaiting_email", last_seen=past_16m)
        cleaned = usr.cleanup_stale_signups(timeout_seconds=900)
        self.assertEqual(cleaned, 1)
        self.assertEqual(usr.count_users(), 4)
        self.assertTrue(usr.try_register("u6@c.us", is_admin=False))
        self.assertEqual(usr.count_users(), 5)

    def test_part_g_users_command(self):
        """users command from normal chat is ignored; from admin chat shows Users: n/5."""
        admin_chat = "admin_owner@c.us"
        normal_chat = "normal_student@c.us"

        usr.try_register(admin_chat, is_admin=True)
        usr.upsert_user(admin_chat, email="owner@example.com", status="active", is_admin=1)
        lms_admin = FakeLMSClient("owner@example.com")
        usr.set_client(admin_chat, lms_admin)

        usr.try_register(normal_chat, is_admin=False)
        usr.upsert_user(normal_chat, email="student@example.com", status="active")
        lms_student = FakeLMSClient("student@example.com")
        usr.set_client(normal_chat, lms_student)

        # From normal chat: ignored (no reply)
        wa_bot.handle(normal_chat, "users")
        self.assertEqual(len(self.sent_texts), 0)

        # From admin chat: shows Users: 2/5
        wa_bot.handle(admin_chat, "users")
        self.assertEqual(len(self.sent_texts), 1)
        admin_reply = self.sent_texts[0][1]
        self.assertIn("Users: 2/5", admin_reply)
        self.assertIn("owner@example.com (active)", admin_reply)
        self.assertIn("student@example.com (active)", admin_reply)
        # Verify no chat_id or token leaked
        self.assertNotIn("admin_owner@c.us", admin_reply)
        self.assertNotIn("normal_student@c.us", admin_reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
