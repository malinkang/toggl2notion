import os
import requests
from requests.auth import HTTPBasicAuth
import json

toggl_token = "2ef95512ce5b1528809f9a03a68e02b1"
auth = HTTPBasicAuth(toggl_token, "api_token")

# Check specific task
response = requests.get("https://api.track.toggl.com/api/v9/me/time_entries", auth=auth)
if response.ok:
    entries = response.json()
    task = next((x for x in entries if x["id"] == 4237392615), None)
    if task:
         print(f"Task 4237392615 info: {json.dumps(task, indent=2)}")
    else:
        print("Task not found in recent entries.")
else:
    print(f"Error: {response.status_code} {response.text}")
