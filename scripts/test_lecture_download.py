
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("LMS_BASE_URL")
COOKIE = os.getenv("LMS_SESSION_COOKIE")

cookie_name = "sb-bdpsfnrkhzosrzxdephz-auth-token"

session = requests.Session()

session.cookies.set(
    cookie_name,
    COOKIE,
    domain="saus-lms.vercel.app",
    path="/"
)

session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
})

tests = [
    "/",
    "/student/courses/2e7dc486-966f-44be-92e7-5957078501b1/resources",
    "/api/resources/6a20c1a8-c4d4-4e1e-bd6f-a5f5063a74d5"
]

for path in tests:

    response = session.get(
        BASE_URL + path,
        allow_redirects=False,
        timeout=20
    )

    print("\nPath:", path)
    print("Status:", response.status_code)
    print("Location:", response.headers.get("Location"))
    print("Content-Type:", response.headers.get("Content-Type"))

session.close()
