"""
lms_client.py - Sathi LMS client (single student).

Does:
  - login with email/password (only when no valid saved tokens exist)
  - saves BOTH tokens to tokens.json after every login/refresh
  - refreshes automatically 5 minutes before expiry, and retries once on 401
  - get_courses / get_assignments / get_resources
  - download_resource (uses the website cookie, the only method that works)

.env needs:
  SUPABASE_URL, SUPABASE_ANON_KEY, LMS_EMAIL, LMS_PASSWORD, LMS_BASE_URL

IMPORTANT: add tokens.json, downloads/ and .env to .gitignore.
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_FILE = "tokens.json"
REFRESH_MARGIN = 300            # refresh when < 5 minutes left
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024   # Telegram bot upload limit is 50 MB
ALLOWED_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt"}


class LMSError(Exception):
    pass


class LMSClient:
    def __init__(self):
        self.supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
        self.api_key = os.environ["SUPABASE_ANON_KEY"]
        self.lms_url = os.environ["LMS_BASE_URL"].rstrip("/")
        self.email = os.environ["LMS_EMAIL"]
        self.password = os.environ["LMS_PASSWORD"]

        ref = urlparse(self.supabase_url).hostname.split(".")[0]
        self.cookie_name = f"sb-{ref}-auth-token"
        self.cookie_domain = urlparse(self.lms_url).hostname

        self.session_data = None
        self.web = requests.Session()           # used for website downloads
        self.web.headers["User-Agent"] = "Mozilla/5.0"

        self._load_tokens()

    # ---------- token handling ----------

    @property
    def access_token(self):
        return self.session_data["access_token"] if self.session_data else None

    @property
    def student_id(self):
        self.ensure_session()
        return self.session_data["user"]["id"]

    def _store(self, data):
        """Remember a new session, save to disk, rebuild the website cookie."""
        if "expires_at" not in data:
            data["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
        self.session_data = data

        tmp = TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, TOKEN_FILE)             # safe write, never half-written

        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        self.web.cookies.set(
            self.cookie_name, "base64-" + encoded,
            domain=self.cookie_domain, path="/",
        )

    def _load_tokens(self):
        if not os.path.exists(TOKEN_FILE):
            return
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                self._store(json.load(f))
        except (OSError, ValueError, KeyError):
            self.session_data = None

    def _auth_call(self, grant, body):
        r = requests.post(
            f"{self.supabase_url}/auth/v1/token",
            params={"grant_type": grant},
            headers={"apikey": self.api_key},
            json=body,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def login(self):
        self._store(self._auth_call(
            "password", {"email": self.email, "password": self.password}))

    def refresh(self):
        data = self._auth_call(
            "refresh_token",
            {"refresh_token": self.session_data["refresh_token"]})
        self._store(data)

    def ensure_session(self):
        """Call before every request. Logs in or refreshes only if needed."""
        if not self.session_data:
            self.login()
            return
        if self.session_data["expires_at"] - time.time() < REFRESH_MARGIN:
            try:
                self.refresh()
            except requests.HTTPError:
                self.login()                    # saved refresh token was dead

    # ---------- data (REST) ----------

    def _get(self, table, **params):
        for attempt in (1, 2):
            self.ensure_session()
            r = requests.get(
                f"{self.supabase_url}/rest/v1/{table}",
                params=params,
                headers={"apikey": self.api_key,
                         "Authorization": f"Bearer {self.access_token}"},
                timeout=20,
            )
            if r.status_code == 401 and attempt == 1:
                try:
                    self.refresh()
                except requests.HTTPError:
                    self.login()
                continue
            r.raise_for_status()
            return r.json()

    def get_courses(self):
        rows = self._get(
            "course_enrollments",
            select="course_id,courses(id,name,code,semester,year)",
            student_id=f"eq.{self.student_id}",
        )
        return [r["courses"] for r in rows if r.get("courses")]

    def get_assignments(self, course_id=None):
        params = {"select": "*", "order": "due_date.asc"}
        if course_id:
            params["course_id"] = f"eq.{course_id}"
        return self._get("assignments", **params)

    def get_resources(self, course_id=None):
        params = {"select": "*", "order": "created_at.desc"}
        if course_id:
            params["course_id"] = f"eq.{course_id}"
        return self._get("resources", **params)

    def get_attendance(self):
        """
        Returns {"overall": {...}, "courses": {course_id: {...}}}.
        Each summary: present, absent, other, total, percent (None if no sessions).
        NOTE: never select qr_token from attendance_sessions.
        """
        sessions = self._get("attendance_sessions",
                             select="id,course_id,title,date")
        records = self._get("attendance_records",
                            select="session_id,status",
                            student_id=f"eq.{self.student_id}")
        status_by_session = {r["session_id"]: r["status"] for r in records}

        def summarize(statuses):
            present = statuses.count("present")
            absent = statuses.count("absent")
            total = len(statuses)
            return {
                "present": present,
                "absent": absent,
                "other": total - present - absent,   # e.g. late, excused
                "total": total,
                "percent": round(100 * present / total, 1) if total else None,
            }

        courses = {}
        all_statuses = []
        for c in self.get_courses():
            statuses = [status_by_session[s["id"]] for s in sessions
                        if s["course_id"] == c["id"]
                        and s["id"] in status_by_session]
            courses[c["id"]] = summarize(statuses)
            all_statuses += statuses
        return {"overall": summarize(all_statuses), "courses": courses}

    def get_submissions(self):
        """Your own submission records, keyed by assignment_id for easy lookup."""
        rows = self._get(
            "assignment_submissions",
            select="assignment_id,file_name,submitted_at,grade",
            student_id=f"eq.{self.student_id}",
        )
        return {r["assignment_id"]: r for r in rows}

    def get_assignments_with_status(self, course_id=None):
        """
        Assignments annotated with submission status.
        status: "submitted" (has a file), "logged_no_file" (record but no file),
                "overdue" (past due, nothing submitted), "pending" (not due yet).
        """
        assignments = self.get_assignments(course_id)
        submissions = self.get_submissions()
        now = datetime.now(timezone.utc)

        out = []
        for a in assignments:
            due = datetime.fromisoformat(a["due_date"].replace("Z", "+00:00"))
            sub = submissions.get(a["id"])
            if sub and sub.get("file_name"):
                status = "submitted"
            elif sub:
                status = "logged_no_file"
            elif due < now:
                status = "overdue"
            else:
                status = "pending"
            out.append({**a, "status": status, "submission": sub})
        return out

    def get_attendance_records_detailed(self):
        """
        One row per attendance record, joined with its session's course and date.
        Used by the monitor to detect newly-marked attendance.
        NOTE: selects only id/course_id/title/date from sessions - never qr_token.
        """
        sessions = {s["id"]: s for s in self._get(
            "attendance_sessions", select="id,course_id,title,date")}
        records = self._get(
            "attendance_records", select="id,session_id,status,marked_at",
            student_id=f"eq.{self.student_id}")
        out = []
        for r in records:
            s = sessions.get(r["session_id"])
            if not s:
                continue
            out.append({
                "id": r["id"],
                "course_id": s["course_id"],
                "title": s.get("title"),
                "date": s.get("date"),
                "status": r["status"],
            })
        return out

    def get_clos(self, course_id):
        """Course Learning Outcomes for one course, ordered by clo_number."""
        return self._get(
            "course_clos",
            select="clo_number,description,bt_level",
            course_id=f"eq.{course_id}",
            order="clo_number.asc",
        )

    # ---------- download ----------

    @staticmethod
    def _filename_from_headers(headers, fallback):
        cd = headers.get("Content-Disposition", "")
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
        if m:
            name = unquote(m.group(1).strip('" '))
        else:
            m = re.search(r'filename="?([^";]+)"?', cd, re.I)
            name = m.group(1).strip() if m else fallback
        name = os.path.basename(name)                    # no path tricks
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return name or fallback

    def download_resource(self, resource_id, folder="downloads"):
        """Returns the saved file path. Raises LMSError on any problem."""
        self.ensure_session()
        r = self.web.get(
            f"{self.lms_url}/api/resources/{resource_id}",
            allow_redirects=True, stream=True, timeout=30,
        )
        try:
            if r.status_code != 200:
                raise LMSError(f"Download failed (HTTP {r.status_code})")

            ctype = r.headers.get("Content-Type", "").lower()
            if "text/html" in ctype:
                raise LMSError("Got a web page instead of the file "
                               "(session not accepted).")

            size = int(r.headers.get("Content-Length") or 0)
            if size > MAX_DOWNLOAD_BYTES:
                raise LMSError("File is larger than Telegram's 50 MB limit.")

            name = self._filename_from_headers(r.headers, resource_id)
            if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
                raise LMSError(f"Unsupported file type: {name}")

            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)

            written = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        f.close()
                        os.remove(path)
                        raise LMSError("File is larger than 50 MB.")
                    f.write(chunk)
            return path
        finally:
            r.close()


if __name__ == "__main__":
    lms = LMSClient()

    courses = lms.get_courses()
    print(f"\nCourses: {len(courses)}")
    for c in courses:
        print(f"  {c.get('code')} - {c.get('name')}")

    assignments = lms.get_assignments()
    print(f"\nAssignments: {len(assignments)}")
    for a in assignments:
        print(f"  {a['due_date']}  {a['title']}")

    resources = lms.get_resources()
    print(f"\nResources: {len(resources)} (newest first)")
    for x in resources[:5]:
        print(f"  {x['created_at'][:10]}  {x['title']}")

    if resources:
        path = lms.download_resource(resources[0]["id"])
        print(f"\nDownloaded newest resource to: {path}")