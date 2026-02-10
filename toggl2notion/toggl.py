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

def get_detailed_report(workspace_id, start_date, end_date):
    url = "https://api.track.toggl.com/reports/api/v2/details"
    params = {
        "workspace_id": workspace_id,
        "since": start_date,
        "until": end_date,
        "user_agent": "toggl2notion",
        "page": 1,
    }
    all_entries = []
    
    while True:
        utils.log(f"Fetching report page {params['page']} for workspace {workspace_id} from {start_date} to {end_date}")
        response = requests.get(url, params=params, auth=auth)
        if response.ok:
            data = response.json()
            entries = data.get("data", [])
            all_entries.extend(entries)
            
            total_count = data.get("total_count", 0)
            per_page = data.get("per_page", 50)
            
            if len(all_entries) >= total_count:
                break
            
            params["page"] += 1
        else:
            utils.log(f"Failed to fetch report: {response.text}")
            break
            
    return all_entries

def process_entry(task, workspace_id):
    item = {}
    tags = task.get("tags")
    if tags:
        item["标签"] = [
            notion_helper.get_relation_id(
                tag, notion_helper.tag_database_id, get_icon(TAG_ICON_URL)
            )
            for tag in tags
        ]
    
    id = task.get("id")
    item["Id"] = id 
    
    start = pendulum.parse(task.get("start"))
    stop = pendulum.parse(task.get("end")) 
    start_ts = start.in_timezone("Asia/Shanghai").int_timestamp
    stop_ts = stop.in_timezone("Asia/Shanghai").int_timestamp
    item["时间"] = (start_ts, stop_ts)
    
    project_name = task.get("project")
    if project_name:
        emoji, project_name = split_emoji_from_string(project_name)
        item["标题"] = project_name
        
        client_name = task.get("client")
        project_properties = {"金币":{"number": 1}}
        
        if client_name:
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
    
    sorts = [{"property": "时间", "direction": "descending"}]
    page_size = 1
    response = notion_helper.query(
        database_id=notion_helper.time_database_id, sorts=sorts, page_size=page_size
    )
    
    if len(response.get("results")) > 0:
        notion_latest_end = (
            response.get("results")[0]
            .get("properties")
            .get("时间")
            .get("date")
            .get("end")
        )
        if notion_latest_end:
             start = pendulum.parse(notion_latest_end).in_timezone("Asia/Shanghai")
    
    if not start:
        reg_time = get_created_at()
        start = reg_time.in_timezone("Asia/Shanghai")
        utils.log(f"Notion is empty. Starting from account registration date: {start.to_date_string()}")

    end = now
    utils.log(f"Synchronizing from {start.to_iso8601_string()} to {end.to_iso8601_string()}")

    workspaces = get_workspaces()
    if not workspaces:
        utils.log("No workspaces found.")
        return

    for ws in workspaces:
        ws_id = ws.get("id")
        utils.log(f"Processing workspace: {ws.get('name')} ({ws_id})")
        
        # Reports API limit is 1 year per request. Loop by year, newest first.
        current_end = end
        while current_end > start:
            current_start = current_end.subtract(years=1).add(days=1)
            if current_start < start:
                current_start = start
            
            entries = get_detailed_report(
                ws_id, 
                current_start.to_date_string(), 
                current_end.to_date_string()
            )
            
            if entries:
                utils.log(f"Found {len(entries)} entries for period {current_start.to_date_string()} to {current_end.to_date_string()}. Inserting in reverse order...")
                # Reports API returns newest first? No, usually sorted by date. 
                # Let's sort them descending (newest first) for immediate feedback in Notion.
                entries.sort(key=lambda x: x['start'], reverse=True)
                
                for task in entries:
                    try:
                        parent, properties, icon = process_entry(task, ws_id)
                        notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                    except Exception as e:
                        utils.log(f"Error processing task {task.get('id')}: {e}")
            else:
                utils.log(f"No entries found for period {current_start.to_date_string()} to {current_end.to_date_string()}")
            
            if current_start <= start:
                break
            current_end = current_start.subtract(days=1)

if __name__ == "__main__":
    if init():
        insert_to_notion()

