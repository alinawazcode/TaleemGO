from lms_auth import LMSAuth
import requests

auth = LMSAuth()
auth.login()

def get(table, select):
    r = requests.get(
        f"{auth.supabase_url}/rest/v1/{table}",
        params={"select": select},
        headers={"apikey": auth.api_key,
                 "Authorization": f"Bearer {auth.access_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

enr = get("course_enrollments", "course_id")
asg = get("assignments", "id,course_id")
res = get("resources", "id,course_id")

print("Enrollments rows:", len(enr))
print("Assignments rows:", len(asg), "| different courses:", len({a['course_id'] for a in asg}))
print("Resources rows:", len(res), "| different courses:", len({a['course_id'] for a in res}))