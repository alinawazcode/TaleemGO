"""
test_relogin.py - Tests for re-login, token cleanup, robust purge, monitor error handling, and bridge crash safety.
"""
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake_key")
os.environ.setdefault("LMS_BASE_URL", "https://fake-lms.example.com")
os.environ.setdefault("BRIDGE_TOKEN", "test_token")
os.environ.setdefault("TALEEMGO_SECRET_KEY", "test_secret_key_32_bytes_len_1234=")

import users as usr
import wa_bot
import wa_monitor
import wa_transport
from lms_client import LMSAuthExpired, LMSClient, _fernet
from state import save_state, load_state


class FakeLMSForRelogin:
    def __init__(self, email="user@test.com"):
        self.email = email

    def get_courses(self):
        return [{"id": "c1", "code": "CS101", "name": "Intro to CS"}]

    def get_resources(self, course_id=None):
        return []

    def get_assignments(self, course_id=None):
        return []

    def get_attendance_records_detailed(self):
        return []

    def get_grades(self, course_id=None):
        return []


class ReloginAndResilienceTests(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        self.tmp = Path(tempfile.mkdtemp(prefix="taleemgo_relogin_test_"))
        usr.USERS_DIR = self.tmp / "users"
        usr.USERS_DIR.mkdir(parents=True, exist_ok=True)
        usr.DB_PATH = str(self.tmp / "users.db")
        usr.init_db()

        self.test_fernet_key = Fernet.generate_key().decode()
        os.environ["TALEEMGO_SECRET_KEY"] = self.test_fernet_key

        with usr._cache_lock:
            usr._client_cache.clear()
            usr._client_locks.clear()
        with usr._pending_lock:
            usr._pending_choices.clear()
        with usr._pending_action_lock:
            usr._pending_actions.clear()
        usr.clear_strikes("all")

        self.sent_texts = []
        self.orig_send_text = wa_transport.send_text
        wa_transport.send_text = lambda cid, msg: self.sent_texts.append((cid, msg))

    def tearDown(self):
        wa_transport.send_text = self.orig_send_text
        os.environ.clear()
        os.environ.update(self.orig_env)
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    # 1. Leftover .enc token file then login_once with good fake auth succeeds
    @patch.object(LMSClient, "_auth_call")
    def test_leftover_enc_token_file_login_once_succeeds(self, mock_auth):
        mock_auth.return_value = {
            "access_token": "fresh_access_token_abc",
            "refresh_token": "fresh_refresh_token_xyz",
            "expires_in": 3600,
            "user": {"id": "std_fresh_123"},
        }
        token_path = self.tmp / "tokens.enc"
        # Pre-populate token_path with a stale token file (encrypted)
        fer = _fernet()
        stale_data = json.dumps({"access_token": "stale_old", "expires_at": 100}).encode()
        token_path.write_bytes(fer.encrypt(stale_data))
        self.assertTrue(token_path.exists())

        # login_once should delete stale token file and succeed without "No password held" error
        client = LMSClient.login_once("test@lms.com", "my_password", token_file=str(token_path))
        self.assertEqual(client.access_token, "fresh_access_token_abc")
        self.assertIsNone(client._password)  # password cleared after store for .enc
        # Verify the saved token file contains the fresh tokens
        saved_bytes = fer.decrypt(token_path.read_bytes())
        saved_dict = json.loads(saved_bytes)
        self.assertEqual(saved_dict["access_token"], "fresh_access_token_abc")

    # 2. Wrong password still gives the retry flow
    @patch.object(LMSClient, "login_once")
    def test_wrong_password_retry_flow(self, mock_login_once):
        resp_401 = requests.Response()
        resp_401.status_code = 401
        mock_login_once.side_effect = requests.HTTPError(response=resp_401)

        chat_id = "retry_user@c.us"
        usr.upsert_user(chat_id, email="u@test.com", status="awaiting_password", attempt=0)

        # 1st attempt
        wa_bot.handle(chat_id, "wrong_pw_1")
        u = usr.get_user(chat_id)
        self.assertEqual(u["attempt"], 1)
        self.assertEqual(u["status"], "awaiting_password")
        self.assertIn("1/3 attempts", self.sent_texts[-1][1])

        # 2nd attempt
        wa_bot.handle(chat_id, "wrong_pw_2")
        u = usr.get_user(chat_id)
        self.assertEqual(u["attempt"], 2)
        self.assertIn("2/3 attempts", self.sent_texts[-1][1])

        # 3rd attempt -> resets to awaiting_email
        wa_bot.handle(chat_id, "wrong_pw_3")
        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "awaiting_email")
        self.assertIn("Too many failed attempts", self.sent_texts[-1][1])

    # 3. Login command after an expired session works when a stale token file exists
    @patch.object(LMSClient, "login_once")
    def test_login_command_after_expired_session_with_stale_tokens(self, mock_login_once):
        chat_id = "expired_user@c.us"
        usr.upsert_user(chat_id, email="exp@test.com", status="expired", attempt=0)
        tf = Path(usr.token_file(chat_id))
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_bytes(b"stale_encrypted_token_bytes")
        self.assertTrue(tf.exists())

        # Sending 'login' should wipe client and stale token file and move to awaiting_email
        wa_bot.handle(chat_id, "login")
        self.assertFalse(tf.exists())
        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "awaiting_email")
        self.assertIn("send your LMS email", self.sent_texts[-1][1])

        # Send email -> moves to awaiting_password
        wa_bot.handle(chat_id, "exp@test.com")
        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "awaiting_password")

        # Send password -> mock login_once succeeds
        mock_lms = FakeLMSForRelogin("exp@test.com")
        mock_login_once.return_value = mock_lms
        wa_bot.handle(chat_id, "new_secret_pw")
        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "active")
        self.assertTrue(any("Connected as exp@test.com" in txt for _, txt in self.sent_texts))

    # 4. purge_user_data removes a read-only token file and retries on locked files
    def test_purge_user_data_removes_readonly_token_file(self):
        chat_id = "readonly_user@c.us"
        usr.upsert_user(chat_id, email="ro@test.com", status="active")
        tf = Path(usr.token_file(chat_id))
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text("test_token_data")
        # Set file to read-only
        os.chmod(str(tf), stat.S_IREAD)

        # purge_user_data must remove read-only token file and user dir without crashing
        usr.purge_user_data(chat_id)
        self.assertFalse(tf.exists())
        self.assertFalse(tf.parent.exists())
        self.assertIsNone(usr.get_user(chat_id))

    def test_purge_user_data_retries_and_logs_warning_if_locked(self):
        chat_id = "locked_user@c.us"
        usr.upsert_user(chat_id, email="lock@test.com", status="active")
        d = Path(usr.user_dir(chat_id))
        locked_file = d / "in_use.txt"
        locked_file.write_text("locked")

        # Simulate locked file that fails rmtree on all attempts
        with patch("shutil.rmtree", side_effect=PermissionError("Locked by another process")):
            with self.assertLogs("taleemgo.users", level="WARNING") as cm:
                usr.purge_user_data(chat_id)
            self.assertTrue(any("Leftover file after purge: in_use.txt" in log_msg for log_msg in cm.output))

    # 5. Expired-session notice is sent once
    @patch("wa_monitor.load_state")
    def test_expired_session_notice_sent_once(self, mock_load):
        chat_id = "exp_notice@c.us"
        usr.upsert_user(chat_id, email="exp@test.com", status="active")
        mock_load.return_value = {"seeded": True}

        fake_client = MagicMock()
        fake_client.get_courses.side_effect = LMSAuthExpired("Session dead")
        usr.set_client(chat_id, fake_client)

        # Cycle 1: active -> expired, notice sent
        wa_monitor._run_for_user(chat_id)
        u = usr.get_user(chat_id)
        self.assertEqual(u["status"], "expired")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Your LMS session has expired", self.sent_texts[0][1])

        # Cycle 2: user is already expired -> skipped immediately, no second notice sent
        self.sent_texts.clear()
        wa_monitor._run_for_user(chat_id)
        self.assertEqual(len(self.sent_texts), 0)

    # 6. Monitor skips an expired user
    @patch("wa_monitor._run_for_user")
    def test_monitor_skips_expired_user(self, mock_run):
        chat_id = "skipped_user@c.us"
        usr.upsert_user(chat_id, email="skip@test.com", status="expired")

        wa_monitor.run_all_users()
        # mock_run must not be called for the expired user
        mock_run.assert_not_called()

    # 7. Bridge failure containing detached Frame / Target closed / Session closed triggers exit
    def test_bridge_fatal_browser_error_detection_helper(self):
        # Test node helper directly via node subprocess
        script = (
            "const { isFatalBrowserError } = require('./whatsapp_bridge/bridge.js');\n"
            "const assert = require('assert');\n"
            "assert.strictEqual(isFatalBrowserError('Protocol error: detached Frame'), true);\n"
            "assert.strictEqual(isFatalBrowserError('Protocol error: Target closed'), true);\n"
            "assert.strictEqual(isFatalBrowserError('Protocol error: Session closed'), true);\n"
            "assert.strictEqual(isFatalBrowserError('Execution context was destroyed'), true);\n"
            "assert.strictEqual(isFatalBrowserError('Evaluation failed: some error'), false);\n"
            "assert.strictEqual(isFatalBrowserError(null), false);\n"
            "assert.strictEqual(isFatalBrowserError(''), false);\n"
            "console.log('NODE_OK');\n"
        )
        res = subprocess.run(["node", "-e", script], cwd=str(ROOT), capture_output=True, text=True, check=True)
        self.assertIn("NODE_OK", res.stdout)

    # 8. Deprecated-key log appears once per process
    def test_deprecated_key_log_appears_once(self):
        import lms_client
        lms_client._deprecated_key_logged = False
        os.environ.pop("TALEEMGO_SECRET_KEY", None)
        os.environ["SATHI_SECRET_KEY"] = Fernet.generate_key().decode()

        with self.assertLogs("taleemgo.lms", level="INFO") as cm:
            for _ in range(5):
                _fernet()

        deprecated_logs = [msg for msg in cm.output if "Using deprecated SATHI_SECRET_KEY" in msg]
        self.assertEqual(len(deprecated_logs), 1)

    # 9. 'LMS is unreachable' text appears only for real network errors
    @patch.object(LMSClient, "login_once")
    def test_lms_unreachable_only_on_network_errors_not_auth_expired(self, mock_login_once):
        chat_id = "network_test@c.us"
        usr.upsert_user(chat_id, email="net@test.com", status="awaiting_password")

        # Case A: LMSAuthExpired -> replies login_failed_password, NEVER LMS is unreachable
        mock_login_once.side_effect = LMSAuthExpired("No password held")
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "my_password")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Login failed, please send your LMS password again", self.sent_texts[0][1])
        self.assertNotIn("unreachable", self.sent_texts[0][1].lower())

        # Case B: requests.ConnectionError (DNS/Host resolution failure) -> replies LMS is unreachable
        mock_login_once.side_effect = requests.exceptions.ConnectionError("Failed to resolve host")
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "my_password")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("LMS is unreachable right now", self.sent_texts[0][1])

        # Case C: requests.Timeout -> replies LMS is unreachable
        mock_login_once.side_effect = requests.exceptions.Timeout("Connection timed out")
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "my_password")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("LMS is unreachable right now", self.sent_texts[0][1])

        # Case D: HTTP 500 server error -> replies LMS is unreachable
        resp_500 = requests.Response()
        resp_500.status_code = 500
        mock_login_once.side_effect = requests.HTTPError(response=resp_500)
        self.sent_texts.clear()
        wa_bot.handle(chat_id, "my_password")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("LMS is unreachable right now", self.sent_texts[0][1])


if __name__ == "__main__":
    unittest.main()
