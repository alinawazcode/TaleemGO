from lms_auth import LMSAuth
import requests

RESOURCE_ID = "6a20c1a8-c4d4-4e1e-bd6f-a5f5063a74d5"

auth = LMSAuth()
auth.login()

r = requests.get(
    f"{auth.lms_url}/api/resources/{RESOURCE_ID}",
    headers={"Authorization": f"Bearer {auth.access_token}"},
    allow_redirects=False,
    timeout=20,
)

print("Status:", r.status_code)
print("Content-Type:", r.headers.get("Content-Type"))
print("Redirects to:", (r.headers.get("Location") or "")[:60])