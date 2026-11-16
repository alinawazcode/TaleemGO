"""
lms_client.py - TaleemGo LMS client (multi-user capable).

LMSClient(email, password, token_file) for per-user instances.
LMSClient()                             for the legacy Telegram bot (reads from .env).

.env needs: SUPABASE_URL, SUPABASE_ANON_KEY, LMS_BASE_URL
Optional:   LMS_EMAIL, LMS_PASSWORD (Telegram bot only)
            TALEEMGO_SECRET_KEY (Fernet key; auto-generated on first run if absent; falls back to SATHI_SECRET_KEY)
"""

import base64
import concurrent.futures
import inspect
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests
import urllib3.util.connection as urllib3_cn
from dotenv import load_dotenv

# Force IPv4 (VPN in PK doesn't tunnel IPv6)
def _allowed_gai_family():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = _allowed_gai_family

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT_DIR / ".env")

TOKEN_FILE = str(DATA_DIR / "tokens.json")          # legacy path (Telegram bot)
DEFAULT_DOWNLOAD_DIR = str(DATA_DIR / "downloads")
REFRESH_MARGIN = 300
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024              # 100 MB (WhatsApp doc limit)
ALLOWED_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt"}

_timing_callback = None


def set_timing_callback(cb):
    global _timing_callback
    _timing_callback = cb


class LMSError(Exception):
    pass


class LMSAuthExpired(LMSError):
    """Raised when the saved session is dead and no password is held."""
    pass


_deprecated_key_logged = False


def _fernet():
    """Return a Fernet instance, generating+saving TALEEMGO_SECRET_KEY if missing (falls back to SATHI_SECRET_KEY)."""
    global _deprecated_key_logged
    from cryptography.fernet import Fernet
    import logging
    log = logging.getLogger("taleemgo.lms")
    key = os.environ.get("TALEEMGO_SECRET_KEY", "")
    if not key:
        key = os.environ.get("SATHI_SECRET_KEY", "")
        if key and not _deprecated_key_logged:
            log.info("Using deprecated SATHI_SECRET_KEY; please rename to TALEEMGO_SECRET_KEY in .env")
            _deprecated_key_logged = True
    if not key:
        key = Fernet.generate_key().decode()
        env_path = ROOT_DIR / ".env"
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nTALEEMGO_SECRET_KEY={key}\n")
        os.environ["TALEEMGO_SECRET_KEY"] = key
        log.warning(
            "Generated new TALEEMGO_SECRET_KEY and appended to .env — back it up!")
    return Fernet(key.encode() if isinstance(key, str) else key)


class LMSClient:
    def __init__(self, email=None, password=None, token_file=None, load_tokens: bool = True):
        self.supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
        self.api_key = os.environ["SUPABASE_ANON_KEY"]
        self.lms_url = os.environ["LMS_BASE_URL"].rstrip("/")

        self.email = email or os.environ.get("LMS_EMAIL", "")
        self._password = password or os.environ.get("LMS_PASSWORD", "")  # cleared after first successful store
        self.token_file = token_file or TOKEN_FILE

        ref = urlparse(self.supabase_url).hostname.split(".")[0]
        self.cookie_name = f"sb-{ref}-auth-token"
        self.cookie_domain = urlparse(self.lms_url).hostname

        self.session_data = None
        self.web = requests.Session()
        self.web.headers["User-Agent"] = "Mozilla/5.0"

        # Per-user in-memory LMS read cache (TTL: LMS_CACHE_TTL_SECONDS, default 60s)
        self._cache = {}
        self._cache_lock = threading.Lock()
        self.chat_id = None

        if load_tokens:
            self._load_tokens()

    def _record_timing(self, ms: float):
        if _timing_callback is not None:
            cid = getattr(self, "chat_id", None)
            try:
                _timing_callback(cid, ms)
            except Exception:
                pass

    # ---------- in-memory caching helpers ----------

    @staticmethod
    def _invoke(fn, *args, **kwargs):
        try:
            sig = inspect.signature(fn)
            if "refresh" in kwargs and "refresh" not in sig.parameters:
                kwargs.pop("refresh", None)
            if "bypass" in kwargs and "bypass" not in sig.parameters:
                kwargs.pop("bypass", None)
        except Exception:
            pass
        return fn(*args, **kwargs)

    def _ensure_cache(self):
        if not hasattr(self, "_cache") or self._cache is None:
            self._cache = {}
        if not hasattr(self, "_cache_lock") or self._cache_lock is None:
            self._cache_lock = threading.Lock()

    def get_cached(self, key: str, bypass: bool = False):
        if bypass:
            return None
        self._ensure_cache()
        ttl = int(os.environ.get("LMS_CACHE_TTL_SECONDS", "60"))
        if os.environ.get("EXHIBITION_MODE", "0").strip() in ("1", "true", "yes", "on"):
            ttl *= 2
        with self._cache_lock:
            item = self._cache.get(key)
            if item:
                ts, val = item
                if time.time() - ts < ttl:
                    return val
                self._cache.pop(key, None)
        return None

    def set_cached(self, key: str, val):
        self._ensure_cache()
        with self._cache_lock:
            self._cache[key] = (time.time(), val)

    def clear_cache(self, key: Optional[str] = None):
        self._ensure_cache()
        with self._cache_lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    # ---------- encryption helpers ----------

    @property
    def _encrypted(self):
        return self.token_file.endswith(".enc")

    def _read_token_bytes(self):
        with open(self.token_file, "rb") as f:
            data = f.read()
        if self._encrypted:
            return _fernet().decrypt(data)
        return data

    def _write_token_bytes(self, data: bytes):
        if self._encrypted:
            data = _fernet().encrypt(data)
        tmp = self.token_file + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.token_file)

    # ---------- token handling ----------

    @property
    def access_token(self):
        return self.session_data["access_token"] if self.session_data else None

    @property
    def student_id(self):
        self.ensure_session()
        return self.session_data["user"]["id"]

    def _store(self, data):
        """Remember session, save to disk (encrypted if .enc), set web cookie."""
        self.clear_cache()
        if "expires_at" not in data:
            data["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
        self.session_data = data
        # Only discard the password for encrypted tokens (.enc) — legacy unencrypted tokens keep it
        if self._encrypted:
            self._password = None

        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._write_token_bytes(raw)

        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        self.web.cookies.set(
            self.cookie_name, "base64-" + encoded,
            domain=self.cookie_domain, path="/",
        )

    def _load_tokens(self):
        if not os.path.exists(self.token_file):
            return
        try:
            raw = self._read_token_bytes()
            self._store(json.loads(raw))
            # _store clears _password — restore it if we still need it (first login hasn't happened)
            # Actually _store is only called here with existing token data; password already not needed.
        except Exception:
            self.session_data = None

    def _auth_call(self, grant, body):
        t0 = time.perf_counter()
        r = requests.post(
            f"{self.supabase_url}/auth/v1/token",
            params={"grant_type": grant},
            headers={"apikey": self.api_key},
            json=body,
            timeout=20,
        )
        self._record_timing((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        return r.json()

    def login(self):
        if not self._password:
            raise LMSAuthExpired("No password held — session expired. Send `login` to re-authenticate.")
        self._store(self._auth_call(
            "password", {"email": self.email, "password": self._password}))

    def refresh(self):
        data = self._auth_call(
            "refresh_token",
            {"refresh_token": self.session_data["refresh_token"]})
        self._store(data)

    def ensure_session(self):
        """Call before every request. Refreshes if near expiry. Raises LMSAuthExpired if refresh fails and no password."""
        if not self.session_data:
            if self._password:
                self.login()
                return
            raise LMSAuthExpired("No session and no password.")
        if self.session_data["expires_at"] - time.time() < REFRESH_MARGIN:
            try:
                self.refresh()
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (400, 401):
                    if self._password:
                        self.login()
                    else:
                        raise LMSAuthExpired("Refresh token expired.") from e
                else:
                    raise

    # ---------- data (REST) ----------

    def _get(self, table, **params):
        for attempt in (1, 2):
            self.ensure_session()
            t0 = time.perf_counter()
            r = requests.get(
                f"{self.supabase_url}/rest/v1/{table}",
                params=params,
                headers={"apikey": self.api_key,
                         "Authorization": f"Bearer {self.access_token}"},
                timeout=20,
            )
            self._record_timing((time.perf_counter() - t0) * 1000)
            if r.status_code == 401 and attempt == 1:
                try:
                    self.refresh()
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code in (400, 401):
                        if self._password:
                            self.login()
                        else:
                            raise LMSAuthExpired("Session dead; re-authenticate.") from e
                    else:
                        raise
                continue
            r.raise_for_status()
            return r.json()

    def get_courses(self, refresh: bool = False):
        cached = self.get_cached("courses", bypass=refresh)
        if cached is not None:
            return cached
        rows = self._get(
            "course_enrollments",
            select="course_id,courses(id,name,code,semester,year)",
            student_id=f"eq.{self.student_id}",
        )
        res = [r["courses"] for r in rows if r.get("courses")]
        self.set_cached("courses", res)
        return res

    def get_assignments(self, course_id=None, refresh: bool = False):
        ck = f"assignments:{course_id or 'all'}"
        cached = self.get_cached(ck, bypass=refresh)
        if cached is not None:
            return cached
        params = {"select": "*", "order": "due_date.asc"}
        if course_id:
            params["course_id"] = f"eq.{course_id}"
        res = self._get("assignments", **params)
        self.set_cached(ck, res)
        return res

    def get_resources(self, course_id=None, refresh: bool = False):
        ck = f"resources:{course_id or 'all'}"
        cached = self.get_cached(ck, bypass=refresh)
        if cached is not None:
            return cached
        params = {"select": "*", "order": "created_at.desc"}
        if course_id:
            params["course_id"] = f"eq.{course_id}"
        res = self._get("resources", **params)
        self.set_cached(ck, res)
        return res

    def get_attendance(self, refresh: bool = False):
        cached = self.get_cached("attendance", bypass=refresh)
        if cached is not None:
            return cached

        # Run independent calls in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            fut_sess = executor.submit(self._get, "attendance_sessions", select="id,course_id,title,date")
            fut_rec = executor.submit(
                self._get, "attendance_records", select="session_id,status", student_id=f"eq.{self.student_id}"
            )
            fut_crs = executor.submit(self._invoke, self.get_courses, refresh=refresh)
            sessions = fut_sess.result()
            records = fut_rec.result()
            courses_list = fut_crs.result()

        status_by_session = {r["session_id"]: r["status"] for r in records}

        def summarize(statuses):
            present = statuses.count("present")
            absent = statuses.count("absent")
            total = len(statuses)
            return {
                "present": present, "absent": absent,
                "other": total - present - absent,
                "total": total,
                "percent": round(100 * present / total, 1) if total else None,
            }

        courses = {}
        all_statuses = []
        for c in courses_list:
            statuses = [status_by_session[s["id"]] for s in sessions
                        if s["course_id"] == c["id"] and s["id"] in status_by_session]
            courses[c["id"]] = summarize(statuses)
            all_statuses += statuses
        res = {"overall": summarize(all_statuses), "courses": courses}
        self.set_cached("attendance", res)
        return res

    def get_submissions(self, refresh: bool = False):
        cached = self.get_cached("submissions", bypass=refresh)
        if cached is not None:
            return cached
        rows = self._get(
            "assignment_submissions",
            select="assignment_id,file_name,submitted_at,grade,graded_at,feedback",
            student_id=f"eq.{self.student_id}",
        )
        res = {r["assignment_id"]: r for r in rows}
        self.set_cached("submissions", res)
        return res

    def get_grades(self, course_id=None, refresh: bool = False):
        ck = f"grades:{course_id or 'all'}"
        cached = self.get_cached(ck, bypass=refresh)
        if cached is not None:
            return cached

        # Parallel fetch of assignments and submissions
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = executor.submit(self._invoke, self.get_assignments, course_id, refresh=refresh)
            fut_s = executor.submit(self._invoke, self.get_submissions, refresh=refresh)
            assignments = fut_a.result()
            submissions = fut_s.result()

        out = []
        for a in assignments:
            sub = submissions.get(a["id"])
            raw_grade = sub.get("grade") if sub else None
            grade = None
            if raw_grade is not None:
                try:
                    grade = float(raw_grade)
                except (ValueError, TypeError):
                    grade = None

            max_marks = a.get("max_marks")
            if max_marks is not None:
                try:
                    max_marks = float(max_marks)
                except (ValueError, TypeError):
                    pass

            out.append({
                "id": a["id"],
                "course_id": a.get("course_id"),
                "title": a.get("title", ""),
                "week_number": a.get("week_number"),
                "max_marks": max_marks,
                "is_extra_credit": bool(a.get("is_extra_credit")),
                "include_in_total": a.get("include_in_total") is not False,
                "grade": grade,
                "graded_at": sub.get("graded_at") if sub else None,
                "feedback": sub.get("feedback") if sub else None,
                "submitted": bool(sub),
            })
        self.set_cached(ck, out)
        return out

    def get_assignments_with_status(self, course_id=None, refresh: bool = False):
        ck = f"assignments_status:{course_id or 'all'}"
        cached = self.get_cached(ck, bypass=refresh)
        if cached is not None:
            return cached

        # Parallel fetch of assignments and submissions
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = executor.submit(self._invoke, self.get_assignments, course_id, refresh=refresh)
            fut_s = executor.submit(self._invoke, self.get_submissions, refresh=refresh)
            assignments = fut_a.result()
            submissions = fut_s.result()

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
        self.set_cached(ck, out)
        return out

    def get_attendance_records_detailed(self):
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
        name = os.path.basename(name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return name or fallback

    def download_resource(self, resource_id, folder=DEFAULT_DOWNLOAD_DIR):
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
                raise LMSError("Got a web page instead of the file (session not accepted).")
            size = int(r.headers.get("Content-Length") or 0)
            if size > MAX_DOWNLOAD_BYTES:
                raise LMSError("File is larger than the 100 MB limit.")
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
                        raise LMSError("File exceeds 100 MB limit.")
                    f.write(chunk)
            return path
        finally:
            r.close()

    # ---------- class helper ----------

    @classmethod
    def login_once(cls, email, password, token_file=None):
        """Create a fresh client, log in, return it. Raises requests.HTTPError on bad credentials."""
        # Always start fresh: remove any existing token file (and .tmp) so stale tokens never clear password
        if token_file:
            for tf_path in (token_file, token_file + ".tmp"):
                try:
                    if os.path.exists(tf_path):
                        try:
                            os.chmod(tf_path, 0o666)
                        except Exception:
                            pass
                        os.remove(tf_path)
                except Exception:
                    pass
        client = cls(email=email, password=password, token_file=token_file, load_tokens=False)
        client.login()          # raises HTTPError on 400/401
        return client


def summarize_grades(items):
    """
    Summarize raw marks for a list of grade items.
    Returns {graded, total, obtained, max_total}.

    Note: The LMS has no category weights or grade scales configured;
    this is a plain marks total (obtained / max_total), not an official or weighted course grade.
    """
    total = len(items)
    graded = 0
    obtained = 0.0
    max_total = 0.0
    for it in items:
        grade = it.get("grade")
        if grade is not None:
            graded += 1
            if it.get("include_in_total") is not False:
                obtained += float(grade)
                if not it.get("is_extra_credit"):
                    mm = it.get("max_marks")
                    if mm is not None:
                        try:
                            max_total += float(mm)
                        except (ValueError, TypeError):
                            pass
    return {
        "graded": graded,
        "total": total,
        "obtained": round(obtained, 2),
        "max_total": round(max_total, 2),
    }


if __name__ == "__main__":
    lms = LMSClient()
    courses = lms.get_courses()
    print(f"\nCourses: {len(courses)}")
    for c in courses:
        print(f"  {c.get('code')} - {c.get('name')}")
    assignments = lms.get_assignments()
    print(f"\nAssignments: {len(assignments)}")
    resources = lms.get_resources()
    print(f"\nResources: {len(resources)} (newest first)")
    for x in resources[:5]:
        print(f"  {x['created_at'][:10]}  {x['title']}")
