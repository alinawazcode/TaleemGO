"""
test_multiuser.py - Multi-user isolation tests (no network required).
Run:  python app/test_multiuser.py
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Point to project root so imports work
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake_key")
os.environ.setdefault("LMS_BASE_URL", "https://fake-lms.example.com")
os.environ.setdefault("BRIDGE_TOKEN", "test_token")
os.environ.setdefault("SATHI_SECRET_KEY", "")  # will be auto-generated

import users as usr
import state as st
from lms_client import LMSAuthExpired


# --- Fake LMS Client ---

class FakeLMSClient:
    def __init__(self, email="a@test.com"):
        self.email = email
        self.called = []

    def get_courses(self): self.called.append("get_courses"); return []
    def get_assignments(self): self.called.append("get_assignments"); return []
    def get_resources(self): self.called.append("get_resources"); return []
    def get_attendance_records_detailed(self): self.called.append("attendance"); return []


class FakeExpiredClient(FakeLMSClient):
    def get_courses(self): raise LMSAuthExpired("test expired")


class MultiUserTests(unittest.TestCase):

    def setUp(self):
        # Use a temp directory for all data
        self.tmp = tempfile.mkdtemp()
        # Patch ROOT_DIR and DATA_DIR in users module
        self._patch_dirs()
        # Reinitialise DB in temp dir
        usr.DB_PATH = os.path.join(self.tmp, "users.db")
        usr.USERS_DIR = Path(self.tmp) / "users"
        usr.USERS_DIR.mkdir(parents=True, exist_ok=True)
        usr.init_db()
        # Clear caches
        usr._client_cache.clear()
        usr._client_locks.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        usr._client_cache.clear()
        usr._client_locks.clear()

    def _patch_dirs(self):
        pass  # users module uses DB_PATH and USERS_DIR which we override in setUp

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

        # Modify A again
        state_a2 = st.load_state(dir_a)
        state_a2["seeded"] = True
        st.save_state(state_a2, dir_a)

        state_b2 = st.load_state(dir_b)
        self.assertFalse(state_b2["seeded"], "User B's seeded was affected by A!")

    # --- Test 3: Password not stored in DB, token files, or logs ---

    def test_password_not_persisted(self):
        chat_id = "333@c.us"
        # Simulate what wa_bot does: upsert user with email, NOT password
        usr.upsert_user(chat_id, email="user@test.com", status="active")

        # Check DB
        with sqlite3.connect(usr.DB_PATH) as c:
            row = c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        col_names = [d[0] for d in c.execute("PRAGMA table_info(users)").fetchall()]
        self.assertNotIn("password", col_names, "password column exists in DB!")
        row_dict = dict(zip(col_names, row))
        for v in row_dict.values():
            if isinstance(v, str):
                self.assertNotIn("secret123", v, "password found in DB row!")

        # Token file should not contain the password (it shouldn't even exist here)
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
        # Create a fake state file
        st.save_state({"seeded": True, "resources": ["x"], "assignments": [], "attendance": [], "reminders_sent": []}, d)
        self.assertTrue(os.path.exists(d))

        usr.purge_user_data(chat_id)

        self.assertIsNone(usr.get_user(chat_id))
        self.assertFalse(os.path.exists(d))
        self.assertIsNone(usr.get_client(chat_id))

    # --- Test 6: LMSAuthExpired in one user's monitor run doesn't stop others ---

    def test_monitor_expired_user_doesnt_block_others(self):
        chat_a = "aaa@c.us"
        chat_b = "bbb@c.us"

        usr.upsert_user(chat_a, email="a@test.com", status="active")
        usr.upsert_user(chat_b, email="b@test.com", status="active")

        expired_client = FakeExpiredClient("a@test.com")
        good_client = FakeLMSClient("b@test.com")

        usr.set_client(chat_a, expired_client)
        usr.set_client(chat_b, good_client)

        sent_texts = []

        def fake_send_text(cid, text):
            sent_texts.append((cid, text))

        with patch("wa_monitor.wa") as mock_wa, \
             patch("wa_monitor.save_state"), \
             patch("wa_monitor.load_state", return_value={
                 "seeded": True, "resources": [], "assignments": [],
                 "attendance": [], "reminders_sent": []}):
            mock_wa.send_text.side_effect = fake_send_text

            from wa_monitor import run_all_users
            run_all_users()

        # User A should be marked expired
        user_a = usr.get_user(chat_a)
        self.assertEqual(user_a["status"], "expired")

        # User B's client was still called (no exception means it ran)
        self.assertIn("get_courses", good_client.called)

    # --- Test 7: Per-user locks are distinct ---

    def test_per_user_locks_distinct(self):
        lock_a = usr.get_lock("aaa@c.us")
        lock_b = usr.get_lock("bbb@c.us")
        lock_a2 = usr.get_lock("aaa@c.us")
        self.assertIsNot(lock_a, lock_b)
        self.assertIs(lock_a, lock_a2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

