
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("SUPABASE_URL")
API_KEY = os.getenv("SUPABASE_ANON_KEY")
TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
COURSE_ID = os.getenv("COURSE_ID")

headers = {
    "apikey": API_KEY,
    "Authorization": f"Bearer {TOKEN}"
}

endpoint = f"{BASE_URL}/rest/v1/assignments"

params = {
    "select": "title,file_url,file_name",
    "course_id": f"eq.{COURSE_ID}"
}

os.makedirs("downloads", exist_ok=True)

response = requests.get(
    endpoint,
    headers=headers,
    params=params,
    timeout=20
)

response.raise_for_status()

assignments = response.json()

for assignment in assignments:

    file_url = assignment.get("file_url")
    filename = assignment.get("file_name")

    if not file_url or not filename:
        continue

    print("\n" + "=" * 50)
    print("File:", filename)
    print("URL:", file_url)

    try:
        # Public Storage URL: test without student credentials
        file_response = requests.get(
            file_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30
        )

        print("Download Status:", file_response.status_code)
        print("Content Type:",
              file_response.headers.get("Content-Type"))
        print("Content Length:",
              file_response.headers.get("Content-Length"))

        if file_response.status_code != 200:
            print("ERROR RESPONSE:")
            print(file_response.text[:2000])
            continue

        path = os.path.join("downloads", filename)

        with open(path, "wb") as f:
            f.write(file_response.content)

        print("SUCCESS! Saved:", path)
        print("Downloaded bytes:", len(file_response.content))

    except requests.exceptions.RequestException as e:
        print("Download failed:", e)
