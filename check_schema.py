import requests
from lms_client import LMSClient

lms = LMSClient()
lms.ensure_session()

r = requests.get(
    f"{lms.supabase_url}/rest/v1/",
    headers={"apikey": lms.api_key,
             "Authorization": f"Bearer {lms.access_token}"},
    timeout=20,
)
print("Status:", r.status_code)

if r.ok:
    spec = r.json()
    for name, info in sorted(spec.get("definitions", {}).items()):
        print(name, "->", ", ".join(info.get("properties", {}).keys()))
else:
    print(r.text[:300])