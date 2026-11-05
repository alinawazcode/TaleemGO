from lms_auth import LMSAuth

auth = LMSAuth()
auth.login()

old_refresh = auth.refresh_token
old_access = auth.access_token
print("expires_in:", auth.session_data.get("expires_in"), "seconds")

auth.refresh()

print("New access token is different:", auth.access_token != old_access)
print("New refresh token is different:", auth.refresh_token != old_refresh)

# Try the OLD refresh token again
auth.refresh_token = old_refresh
try:
    auth.refresh()
    print("Old refresh token still works")
except Exception as e:
    print("Old refresh token is dead:", e)