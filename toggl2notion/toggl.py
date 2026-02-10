import os
from requests.auth import HTTPBasicAuth
import pendulum
import requests
from .notion_helper import NotionHelper
from . import utils

from .config import time_properties_type_dict, TAG_ICON_URL
from .utils import get_icon, split_emoji_from_string
from dotenv import load_dotenv
load_dotenv()

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
        clients = response.json()
        utils.log(f"Loaded {len(clients)} clients for workspace {workspace_id}")
        for c in clients:
            client_cache[c["id"]] = c["name"]
    else:
        utils.log(f"Failed to load clients for workspace {workspace_id}: {response.status_code} {response.text}")
    
    # Load Projects
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects", auth=auth)
    if response.ok:
        projects = response.json()
        utils.log(f"Loaded {len(projects)} projects for workspace {workspace_id}")
        for p in projects:
            project_cache[p["id"]] = {
                "name": p["name"],
                "client_id": p.get("client_id")
            }
    else:
        utils.log(f"Failed to load projects for workspace {workspace_id}: {response.status_code} {response.text}")

def get_time_entries(start_date, end_date):
    """Fetch raw time entries using Track API v9 (Free)"""
    url = "https://api.track.toggl.com/api/v9/me/time_entries"
    # Toggl v9 API expects ISO8601, preferably in UTC or with explicit offset
    # Using .format("YYYY-MM-DDTHH:mm:ssZ") ensures compatibility
    params = {
        "start_date": start_date.format("YYYY-MM-DDTHH:mm:ssZ"),
        "end_date": end_date.format("YYYY-MM-DDTHH:mm:ssZ"),
    }
    response = requests.get(url, params=params, auth=auth)
    if response.ok:
        return response.json()
    else:
        utils.log(f"Failed to fetch time entries ({start_date.to_date_string()} to {end_date.to_date_string()}): {response.status_code} {response.text}")
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
    description = task.get("description")
    
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
        if pid:
             utils.log(f"⚠️ Project ID {pid} not found in cache. Falling back to description.")
        item["标题"] = description or "无描述"
        
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
        latest_page = response.get("results")[0]
        latest_id = latest_page.get("id")
        # Try to get title for logging
        properties = latest_page.get("properties", {})
        title_list = properties.get("标题", {}).get("title", [])
        title = title_list[0].get("text", {}).get("content", "Untitled") if title_list else "Untitled"
        
        date_prop = properties.get("时间", {}).get("date")
        if date_prop and date_prop.get("end"):
             start = pendulum.parse(date_prop.get("end")).in_timezone("Asia/Shanghai").add(seconds=1)
             utils.log(f"🔍 Found latest entry in Notion: [{title}] (ID: {latest_id}) with end time {date_prop.get('end')}")
        elif date_prop and date_prop.get("start"):
             start = pendulum.parse(date_prop.get("start")).in_timezone("Asia/Shanghai").add(seconds=1)
             utils.log(f"🔍 Found latest entry in Notion (start only): [{title}] (ID: {latest_id}) at {date_prop.get('start')}")
    
    if not start:
        start = get_created_at().in_timezone("Asia/Shanghai")
        utils.log(f"Notion is empty. Starting from account registration date: {start.to_date_string()}")

    # Track API v9 returns all entries for the user. We only need to load workspace projects once.
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("No workspaces found or API error.")
        return

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
                
                toggl_id = task.get('id')
                # Check for existing page to support updates
                existing_page_id = notion_helper.get_page_by_toggl_id(toggl_id)
                
                utils.log(f"📝 {'Updating' if existing_page_id else 'Syncing'}: [{task.get('description') or '无描述'}] ({task.get('start')})")
                try:
                    parent, properties, icon = process_entry(task)
                    if existing_page_id:
                        notion_helper.update_page(page_id=existing_page_id, properties=properties, icon=icon)
                    else:
                        notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                except Exception as e:
                    utils.log(f"Error processing task {task.get('id')}: {e}")
        
        if current_start <= start:
            break
        current_end = current_start.subtract(seconds=1)

if __name__ == "__main__":
    if init():
        insert_to_notion()
