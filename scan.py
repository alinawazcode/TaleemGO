import os, requests

class LMS:
    def __init__(self):
        self.url = os.environ["LMS_API_URL"].rstrip("/")   # https://<project>.supabase.co
        self.key = os.environ["LMS_API_KEY"]               # public client key
        self.access = self.refresh = None

    def login(self, email, password):
        d = self._auth("password", {"email": email, "password": password})
        self.access, self.refresh = d["access_token"], d["refresh_token"]

    def _auth(self, grant, body):
        r = requests.post(f"{self.url}/auth/v1/token?grant_type={grant}",
                          headers={"apikey": self.key}, json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def _get(self, table, **params):
        for attempt in (1, 2):
            r = requests.get(f"{self.url}/rest/v1/{table}", params=params, timeout=20,
                headers={"apikey": self.key, "Authorization": f"Bearer {self.access}"})
            if r.status_code == 401 and attempt == 1:
                d = self._auth("refresh_token", {"refresh_token": self.refresh})
                self.access, self.refresh = d["access_token"], d["refresh_token"]
                continue
            r.raise_for_status()
            return r.json()

    def assignments(self, course_id):
        return self._get("assignments", select="*", course_id=f"eq.{course_id}")