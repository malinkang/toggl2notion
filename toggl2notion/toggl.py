import os
from requests.auth import HTTPBasicAuth
import pendulum
from retrying import retry
import requests
from .notion_helper import NotionHelper
from . import utils

from .config import time_properties_type_dict, TAG_ICON_URL
from .utils import get_icon, split_emoji_from_string
from dotenv import load_dotenv
load_dotenv()



def get_created_at():
    response = requests.get("https://api.track.toggl.com/api/v9/me", auth=auth)
    if response.ok:
        data = response.json()
        return pendulum.parse(data.get("created_at"))
    else:
        utils.log(f"Failed to get user info: {response.text}")
        return pendulum.datetime(2010, 1, 1, tz="Asia/Shanghai")


auth = None
notion_helper = None

def init():
    global auth, notion_helper
    notion_helper = NotionHelper()
    toggl_token = os.getenv("TOGGL_TOKEN")
    if not toggl_token:
        utils.log("❌ Missing TOGGL_TOKEN environment variable.")
        return False
    auth = HTTPBasicAuth(f"{toggl_token}", "api_token")
    return True

auth = None
notion_helper = None
project_cache = {}
client_cache = {}

def init():
    global auth, notion_helper
    notion_helper = NotionHelper()
    toggl_token = os.getenv("TOGGL_TOKEN")
    if not toggl_token:
        utils.log("❌ Missing TOGGL_TOKEN environment variable.")
        return False
    auth = HTTPBasicAuth(f"{toggl_token}", "api_token")
    return True

def get_created_at():
    response = requests.get("https://api.track.toggl.com/api/v9/me", auth=auth)
    if response.ok:
        data = response.json()
        return pendulum.parse(data.get("created_at"))
    else:
        utils.log(f"Failed to get user info: {response.text}")
        return pendulum.datetime(2010, 1, 1, tz="Asia/Shanghai")

def get_workspaces():
    response = requests.get(
        "https://api.track.toggl.com/api/v9/me/workspaces", auth=auth
    )
    if response.ok:
        return response.json()
    else:
        utils.log(f"Failed to get workspaces: {response.text}")
        return []

def load_workspace_cache(workspace_id):
    global project_cache, client_cache
    # Load Clients
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients", auth=auth)
    if response.ok:
        for c in response.json():
            client_cache[c["id"]] = c["name"]
    
    # Load Projects
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects", auth=auth)
    if response.ok:
        for p in response.json():
            project_cache[p["id"]] = {
                "name": p["name"],
                "client_id": p.get("client_id")
            }

def get_time_entries(start_date, end_date):
    """Fetch raw time entries using Track API v9 (Free)"""
    url = "https://api.track.toggl.com/api/v9/me/time_entries"
    params = {
        "start_date": start_date.to_iso8601_string(),
        "end_date": end_date.to_iso8601_string(),
    }
    response = requests.get(url, params=params, auth=auth)
    if response.ok:
        return response.json()
    else:
        utils.log(f"Failed to fetch time entries: {response.text}")
        return []

def process_entry(task):
    item = {}
    tags = task.get("tags")
    if tags:
        item["标签"] = [
            notion_helper.get_relation_id(
                tag, notion_helper.tag_database_id, get_icon(TAG_ICON_URL)
            )
            for tag in tags
        ]
    
    item["Id"] = task.get("id")
    
    start = pendulum.parse(task.get("start"))
    stop = pendulum.parse(task.get("stop") or task.get("end") or pendulum.now().to_iso8601_string())
    start_ts = start.in_timezone("Asia/Shanghai").int_timestamp
    stop_ts = stop.in_timezone("Asia/Shanghai").int_timestamp
    item["时间"] = (start_ts, stop_ts)
    
    pid = task.get("project_id") or task.get("pid")
    if pid and pid in project_cache:
        project_info = project_cache[pid]
        project_name = project_info["name"]
        emoji, project_name = split_emoji_from_string(project_name)
        item["标题"] = project_name
        
        client_id = project_info.get("client_id")
        project_properties = {"金币":{"number": 1}}
        
        if client_id and client_id in client_cache:
            client_name = client_cache[client_id]
            client_emoji, client_name = split_emoji_from_string(client_name)
            item["Client"] = [
                notion_helper.get_relation_id(
                    client_name,
                    notion_helper.client_database_id,
                    {"type": "emoji", "emoji": client_emoji},
                )
            ]
            project_properties["Client"] = {
                "relation": [{"id": id} for id in item.get("Client")]
            }
            
        item["Project"] = [
            notion_helper.get_relation_id(
                project_name,
                notion_helper.project_database_id,
                {"type": "emoji", "emoji": emoji},
                properties=project_properties,
            )
        ]
    else:
        item["标题"] = task.get("description") or "无描述"
        
    description = task.get("description")
    if description:
        item["备注"] = description
        
    properties = utils.get_properties(item, time_properties_type_dict)
    parent = {
        "database_id": notion_helper.time_database_id,
        "type": "database_id",
    }
    notion_helper.get_date_relation(
        properties, pendulum.from_timestamp(stop_ts, tz="Asia/Shanghai")
    )
    
    icon = None
    if 'emoji' in locals() and emoji:
         icon = {"type": "emoji", "emoji": emoji}
         
    return parent, properties, icon


def insert_to_notion():
    now = pendulum.now("Asia/Shanghai")
    start = None
    
    # Check latest entry in Notion
    sorts = [{"property": "时间", "direction": "descending"}]
    response = notion_helper.query(
        database_id=notion_helper.time_database_id, sorts=sorts, page_size=1
    )
    
    if len(response.get("results")) > 0:
        date_prop = response.get("results")[0].get("properties").get("时间").get("date")
        if date_prop and date_prop.get("end"):
             start = pendulum.parse(date_prop.get("end")).in_timezone("Asia/Shanghai")
    
    if not start:
        start = get_created_at().in_timezone("Asia/Shanghai")
        utils.log(f"Notion is empty. Starting from account registration date: {start.to_date_string()}")

    # Track API v9 returns all entries for the user. We only need to load workspace projects once.
    workspaces = get_workspaces()
    for ws in workspaces:
        load_workspace_cache(ws["id"])

    # Loop backward from now to start in smaller chunks (e.g., 10 days) to avoid API limits and provide feedback
    current_end = now
    utils.log(f"Synchronizing from {start.to_iso8601_string()} to {current_end.to_iso8601_string()}")

    while current_end > start:
        current_start = current_end.subtract(days=10)
        if current_start < start:
            current_start = start
        
        entries = get_time_entries(current_start, current_end)
        
        if entries:
            utils.log(f"Found {len(entries)} entries from {current_start.to_date_string()} to {current_end.to_date_string()}. Processing...")
            # Sort newest first
            entries.sort(key=lambda x: x['start'], reverse=True)
            
            for task in entries:
                if task.get("server_deleted_at"):
                    continue
                try:
                    parent, properties, icon = process_entry(task)
                    notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                except Exception as e:
                    utils.log(f"Error processing task {task.get('id')}: {e}")
        
        if current_start <= start:
            break
        current_end = current_start.subtract(seconds=1)

if __name__ == "__main__":
    if init():
        insert_to_notion()

