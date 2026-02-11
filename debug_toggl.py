import os
import requests
from requests.auth import HTTPBasicAuth
import json

toggl_token = "2ef95512ce5b1528809f9a03a68e02b1"
auth = HTTPBasicAuth(toggl_token, "api_token")
workspace_id = 5952284

# Check projects
response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects", auth=auth)
if response.ok:
    projects = response.json()
    p_ids = [p["id"] for p in projects]
    print(f"Total projects: {len(projects)}")
    print(f"Is 186296615 in projects? {186296615 in p_ids}")
    if 186296615 in p_ids:
        p = next(x for x in projects if x["id"] == 186296615)
        print(f"Project info: {json.dumps(p, indent=2)}")
else:
    print(f"Error loading projects: {response.text}")

# Check time entries again to be sure
response = requests.get("https://api.track.toggl.com/api/v9/me/time_entries", auth=auth)
if response.ok:
    entries = response.json()
    task = next((x for x in entries if x["id"] == 4284328622), None)
    if task:
         print(f"Task 4284328622 fields: {task.keys()}")
         print(f"pid: {task.get('pid')}, project_id: {task.get('project_id')}")
