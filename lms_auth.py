
import os
import json
import base64
import requests
from dotenv import load_dotenv

load_dotenv()


class LMSAuth:

    def __init__(self):
        self.supabase_url = os.getenv("SUPABASE_URL").rstrip("/")
        self.api_key = os.getenv("SUPABASE_ANON_KEY")
        self.email = os.getenv("LMS_EMAIL")
        self.password = os.getenv("LMS_PASSWORD")
        self.lms_url = os.getenv("LMS_BASE_URL").rstrip("/")

        self.project_ref = self.supabase_url.split("//")[1].split(".")[0]
        self.cookie_name = f"sb-{self.project_ref}-auth-token"

        self.access_token = None
        self.refresh_token = None
        self.session_data = None

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "apikey": self.api_key
        })

    def _save_session(self, data):

        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.session_data = data

        # Supabase SSR cookie encoding
        raw = json.dumps(
            data,
            separators=(",", ":")
        ).encode("utf-8")

        encoded = base64.urlsafe_b64encode(raw).decode("ascii")
        encoded = encoded.rstrip("=")

        cookie_value = "base64-" + encoded

        self.session.cookies.set(
            self.cookie_name,
            cookie_value,
            domain="saus-lms.vercel.app",
            path="/"
        )

        self.session.headers["Authorization"] = (
            f"Bearer {self.access_token}"
        )

    def login(self):

        print("Authenticating with SAU LMS...")

        url = (
            f"{self.supabase_url}/auth/v1/token"
            "?grant_type=password"
        )

        response = requests.post(
            url,
            headers={"apikey": self.api_key},
            json={
                "email": self.email,
                "password": self.password
            },
            timeout=20
        )

        response.raise_for_status()

        self._save_session(response.json())

        print("Login successful!")
        print("Student ID:", self.session_data["user"]["id"])

    def refresh(self):

        if not self.refresh_token:
            raise RuntimeError("No refresh token available")

        print("Refreshing student session...")

        url = (
            f"{self.supabase_url}/auth/v1/token"
            "?grant_type=refresh_token"
        )

        response = requests.post(
            url,
            headers={"apikey": self.api_key},
            json={"refresh_token": self.refresh_token},
            timeout=20
        )

        response.raise_for_status()

        # Supabase rotates refresh tokens
        self._save_session(response.json())

        print("Session refreshed!")

    def ensure_login(self):
        if not self.access_token:
            self.login()

    def get_lms_session(self):
        self.ensure_login()
        return self.session
