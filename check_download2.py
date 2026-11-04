from lms_auth import LMSAuth

RESOURCE_ID = "6a20c1a8-c4d4-4e1e-bd6f-a5f5063a74d5"

auth = LMSAuth()
auth.login()
auth.refresh()   # simulate the hourly refresh

session = auth.get_lms_session()
r = session.get(
    f"{auth.lms_url}/api/resources/{RESOURCE_ID}",
    allow_redirects=True,
    stream=True,
    timeout=30,
)

print("Status:", r.status_code)
print("Content-Type:", r.headers.get("Content-Type"))
print("Size header:", r.headers.get("Content-Length"))
r.close()