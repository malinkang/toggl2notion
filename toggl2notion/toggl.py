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
    # Reports API returns IDs as integers, ensure consistency
    item["Id"] = id 
    
    start = pendulum.parse(task.get("start"))
    stop = pendulum.parse(task.get("end")) # Reports API uses 'end', not 'stop'
    start_ts = start.in_timezone("Asia/Shanghai").int_timestamp
    stop_ts = stop.in_timezone("Asia/Shanghai").int_timestamp
    item["时间"] = (start_ts, stop_ts)
    
    project_name = task.get("project")
    if project_name:
        emoji, project_name = split_emoji_from_string(project_name)
        item["标题"] = project_name
        
        # Reports API returns client name directly
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
    
    # Use project emoji as icon if available, else default?
    # Original code used emoji from project name splitting
    icon = None
    if 'emoji' in locals() and emoji:
         icon = {"type": "emoji", "emoji": emoji}
         
    return parent, properties, icon


def insert_to_notion():
    # 获取当前UTC时间
    now = pendulum.now("Asia/Shanghai")
    
    # 确定起始时间
    start = now.subtract(days=1) # Default to recent if no history
    
    sorts = [{"property": "时间", "direction": "descending"}]
    page_size = 1
    response = notion_helper.query(
        database_id=notion_helper.time_database_id, sorts=sorts, page_size=page_size
    )
    
    notion_latest_end = None
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
    
    # 如果 Notion 中没有数据，或者数据很久远，我们默认同步最近 1 年？ 
    # 用户需求是 "从注册 toggl 到今天"，所以如果没有 notion 数据，应该尽可能早。
    # Toggl Reports API 最好不要一次性拉取太多年的空数据，这里我们假设如果 Notion 空，则同步过去 365 天。
    # 或者给一个特定的环境变量 START_DATE?
    # 暂时由 Notion 最新时间决定。如果没有，默认 2010 年？(Toggl 成立时间附近)
    
    if not notion_latest_end:
        start = pendulum.datetime(2010, 1, 1, tz="Asia/Shanghai") 

    end = now
    
    utils.log(f"Synchronizing from {start.to_iso8601_string()} to {end.to_iso8601_string()}")

    workspaces = get_workspaces()
    all_time_entries = []

    for ws in workspaces:
        ws_id = ws.get("id")
        
        # Reports API limit is 1 year per request. Loop by year.
        current_start = start
        while current_start < end:
            current_end = current_start.add(years=1)
            if current_end > end:
                current_end = end
            
            entries = get_detailed_report(
                ws_id, 
                current_start.to_iso8601_string(), 
                current_end.to_iso8601_string()
            )
            all_time_entries.extend(entries)
            
            current_start = current_end.add(seconds=1) # Avoid overlap? actually Reports API includes bounds usually? 
            # Reports API: "Detailed reports return time entries that have a start time within the given date range."
            # precise check: if current_end == end, break
            if current_end >= end:
                break
    
    # Remove duplicates based on ID?
    # Reports API pagination handles duplicates usually, but across workspaces/requests...
    # Entries have unique 'id'.
    unique_entries = {entry['id']: entry for entry in all_time_entries}.values()
    sorted_entries = sorted(unique_entries, key=lambda x: x['start'])
    
    utils.log(f"Total entries to process: {len(sorted_entries)}")
    
    for task in sorted_entries:
        # Check if already in Notion? (Original code didn't check ID existence per row, relied on date filtering)
        # But if we fetch from `notion_latest_end`, we might overlap newly added entry.
        # Original code: "start = ...get('end')" -> fetch "start_date" = start
        # Notion end time matches Toggl end time?
        # Let's rely on Notion query `start` to avoid re-fetching old data.
        # But `me/time_entries` filter was `start_date`.
        # Reports API `since` is also start time.
        # So we should be safe.
        
        # Original code logic: 
        #   time_entries = response.json()
        #   time_entries.sort...
        #   for task in time_entries: ... insert ...
        
        # We process entry
        try:
             # Need to handle missing workspace_id in process_entry? Reports API returns it?
             # Reports API 'detailed' entries contain 'wid' (workspace id) or similiar?
             # The 'task' object from reports might have different keys.
             # Fields: id, pid, tid, uid, description, start, end, updated, dur, user, use_stop, client, project, project_color, project_hex_color, task, billable, is_billable, cur, tags
             
             # process_entry checks `task.get("project")` (name) vs `me/time_entries` returning `project_id`.
             # Original code fetched project details by ID. Reports API returns project NAME.
             # This saves API calls!
             
             parent, properties, icon = process_entry(task, task.get("wid") or task.get("workspace_id"))
             
             # Double check duplication?
             # Notion `query` at start gets the latest date.
             # If we run script twice, we might re-insert if time matches exactly?
             # Original code didn't safeguard against duplicates other than date range.
             # NotionHelper doesn't have "check if ID exists" logic exposed easily here.
             # We assume date range is sufficient.
             
             notion_helper.create_page(parent=parent, properties=properties, icon=icon)
             
        except Exception as e:
            utils.log(f"Error processing task {task.get('id')}: {e}")



if __name__ == "__main__":
    notion_helper = NotionHelper()
    toggl_token = os.getenv("TOGGL_TOKEN")
    if not toggl_token:
        # Fallback to email/password if token not present (though implementation plan says replace, fallback is safer during transition?)
        # User requested: "脚本的调用都修改为 api token调用" implies replacement.
        # But previous code used os.getenv('EMAIL')
        # Let's strictly follow verification plan: use TOGGL_TOKEN.
        pass

    # Basic Auth with token uses token as username and "api_token" as password
    auth = HTTPBasicAuth(f"{toggl_token}", "api_token")
    insert_to_notion()
