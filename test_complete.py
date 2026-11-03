
import os
from urllib.parse import urlparse
from lms_auth import LMSAuth

RESOURCE_ID = "6a20c1a8-c4d4-4e1e-bd6f-a5f5063a74d5"

auth = LMSAuth()
session = auth.get_lms_session()

url = f"{auth.lms_url}/api/resources/{RESOURCE_ID}"

print("\nRequesting lecture download...")

response = session.get(
    url,
    allow_redirects=True,
    stream=True,
    timeout=30
)

print("Final status:", response.status_code)
print("Final domain:", urlparse(response.url).hostname)
print("Content type:", response.headers.get("Content-Type"))

response.raise_for_status()

content_type = response.headers.get("Content-Type", "")

if "text/html" in content_type.lower():
    response.close()
    raise RuntimeError(
        "Received HTML instead of the lecture. "
        "Check whether the LMS accepted the session cookie."
    )

filename = "lecture_download"

disposition = response.headers.get("Content-Disposition", "")

if "filename=" in disposition:
    filename = disposition.split("filename=")[-1]
    filename = filename.split(";")[0].strip('" ')

os.makedirs("downloads", exist_ok=True)

path = os.path.join("downloads", filename)

with open(path, "wb") as f:
    for chunk in response.iter_content(8192):
        if chunk:
            f.write(chunk)

response.close()

print("\nSUCCESS!")
print("Downloaded:", path)
print("Size:", os.path.getsize(path), "bytes")
