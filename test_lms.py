
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
COURSE_ID = os.getenv("COURSE_ID")

def get_assignments():

    if not all([SUPABASE_URL, SUPABASE_KEY, ACCESS_TOKEN, COURSE_ID]):
        raise ValueError("Missing configuration in .env")

    endpoint = f"{SUPABASE_URL}/rest/v1/assignments"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    params = {
        "select": "*",
        "course_id": f"eq.{COURSE_ID}",
        "order": "due_date.asc"
    }

    print("\nConnecting to SAU LMS...")
    
    try:
        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=20
        )

        print("HTTP Status:", response.status_code)

        response.raise_for_status()

        assignments = response.json()

        print(f"\nFound {len(assignments)} assignments\n")

        for i, assignment in enumerate(assignments, 1):

            print("=" * 50)
            print(f"Assignment #{i}")
            print("=" * 50)

            print("Title:", assignment.get("title"))
            print("Type:", assignment.get("type"))
            print("Due Date:", assignment.get("due_date"))
            print("Maximum Marks:", assignment.get("max_marks"))
            print("Description:", assignment.get("description"))
            print("File:", assignment.get("file_name"))
            print("Download URL:", assignment.get("file_url"))

        return assignments

    except requests.exceptions.HTTPError:
        print("HTTP Error:", response.text[:1000])

    except requests.exceptions.RequestException as e:
        print("Connection Error:", str(e))

    return []


if __name__ == "__main__":
    get_assignments()
