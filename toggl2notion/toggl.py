import os
from requests.auth import HTTPBasicAuth
import pendulum
import requests
from .notion_helper import NotionHelper
from . import utils

from .config import TAG_ICON_URL
from .utils import get_icon, split_emoji_from_string
from dotenv import load_dotenv
from notionhub.log import sync_notification
load_dotenv()

auth = None
notion_helper = None
project_cache = {}
client_cache = {}
project_name_cache = {}
client_name_cache = {}

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
    response = requests.get("https://api.track.toggl.com/api/v9/me", auth=auth, timeout=15)
    if response.ok:
        data = response.json()
        return pendulum.parse(data.get("created_at"))
    else:
        utils.log(f"Failed to get user info: {response.text}")
        return pendulum.datetime(2010, 1, 1, tz="Asia/Shanghai")

def get_workspaces():
    response = requests.get(
        "https://api.track.toggl.com/api/v9/me/workspaces", auth=auth, timeout=15
    )
    if response.ok:
        return response.json()
    else:
        utils.log(f"Failed to get workspaces: {response.text}")
        return []

def normalize_cache_name(name):
    return (name or "").strip().lower()


def load_workspace_cache(workspace_id):
    global project_cache, client_cache, project_name_cache, client_name_cache
    # Load Clients
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients", auth=auth, timeout=15)
    if response.ok:
        clients = response.json()
        utils.log(f"Loaded {len(clients)} clients for workspace {workspace_id}")
        for c in clients:
            client_cache[c["id"]] = c["name"]
            client_name_cache[(workspace_id, normalize_cache_name(c.get("name")))] = c["id"]
    else:
        utils.log(f"Failed to load clients for workspace {workspace_id}: {response.status_code} {response.text}")
    
    # Load Projects
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects", auth=auth, timeout=15)
    if response.ok:
        projects = response.json()
        utils.log(f"Loaded {len(projects)} projects for workspace {workspace_id}")
        for p in projects:
            project_cache[p["id"]] = {
                "name": p["name"],
                "client_id": p.get("client_id"),
                "workspace_id": workspace_id,
            }
            project_name_cache[
                (workspace_id, normalize_cache_name(p.get("name")), p.get("client_id"))
            ] = p["id"]
            project_name_cache[
                (workspace_id, normalize_cache_name(p.get("name")), None)
            ] = p["id"]
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
    response = requests.get(url, params=params, auth=auth, timeout=15)
    if response.ok:
        return response.json(), 200
    else:
        utils.log(f"Failed to fetch time entries ({start_date.to_date_string()} to {end_date.to_date_string()}): {response.status_code} {response.text}")
        return None, response.status_code

def create_toggl_entry(workspace_id, description, start, duration, pid=None):
    """Create a time entry in Toggl Track."""
    data = {
        "workspace_id": int(workspace_id),
        "description": description,
        "start": start,
        "duration": int(duration),
        "created_with": "toggl2notion",
    }
    if pid:
        data["project_id"] = int(pid)
    
    response = requests.post(
        f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/time_entries",
        auth=auth,
        json=data,
        timeout=15
    )
    if response.ok:
        entry = response.json()
        utils.log(f"✅ Created Toggl entry: [{description}] (ID: {entry['id']})")
        return entry.get("id")
    else:
        utils.log(f"Failed to create Toggl entry: {response.status_code} {response.text}")
        return None


def create_toggl_client(workspace_id, name):
    """Create a Toggl client and update local caches."""
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    cache_key = (workspace_id, normalize_cache_name(clean_name))
    if cache_key in client_name_cache:
        return client_name_cache[cache_key]

    response = requests.post(
        f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients",
        auth=auth,
        json={"name": clean_name},
        timeout=15,
    )
    if not response.ok:
        utils.log(f"Failed to create Toggl client '{clean_name}': {response.status_code} {response.text}")
        return None
    client = response.json()
    client_id = client.get("id")
    if client_id:
        client_cache[client_id] = client.get("name") or clean_name
        client_name_cache[cache_key] = client_id
        utils.log(f"✅ Created Toggl client: [{clean_name}] (ID: {client_id})")
    return client_id


def create_toggl_project(workspace_id, name, client_id=None):
    """Create a Toggl project and update local caches."""
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    cache_key = (workspace_id, normalize_cache_name(clean_name), client_id)
    fallback_key = (workspace_id, normalize_cache_name(clean_name), None)
    if cache_key in project_name_cache:
        return project_name_cache[cache_key]
    if fallback_key in project_name_cache:
        project_id = project_name_cache[fallback_key]
        cached_project = project_cache.get(project_id, {})
        if not client_id or cached_project.get("client_id") in (None, client_id):
            return project_id

    payload = {
        "name": clean_name,
        "workspace_id": int(workspace_id),
        "active": True,
        "is_private": False,
    }
    if client_id:
        payload["client_id"] = int(client_id)
    response = requests.post(
        f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects",
        auth=auth,
        json=payload,
        timeout=15,
    )
    if not response.ok:
        utils.log(f"Failed to create Toggl project '{clean_name}': {response.status_code} {response.text}")
        return None
    project = response.json()
    project_id = project.get("id")
    if project_id:
        project_cache[project_id] = {
            "name": project.get("name") or clean_name,
            "client_id": project.get("client_id") or client_id,
            "workspace_id": workspace_id,
        }
        project_name_cache[(workspace_id, normalize_cache_name(clean_name), project_cache[project_id].get("client_id"))] = project_id
        project_name_cache[fallback_key] = project_id
        utils.log(f"✅ Created Toggl project: [{clean_name}] (ID: {project_id})")
    return project_id


def ensure_remote_client(client_page_id, workspace_id):
    if not client_page_id:
        return None
    remote_id = notion_helper.get_remote_id_from_page(client_page_id)
    if remote_id:
        return int(remote_id)

    client_name, _ = notion_helper.get_page_title(client_page_id)
    if not client_name:
        utils.log(f"⚠️ Client page {client_page_id} has no title. Skipping client sync.")
        return None

    client_id = create_toggl_client(workspace_id, client_name)
    if client_id:
        notion_helper.update_page(client_page_id, {"Id": {"number": int(client_id)}})
        utils.log(f"🔗 Linked Notion client '{client_name}' with Toggl ID {client_id}")
    return client_id


def ensure_remote_project(project_page_id, workspace_id, client_page_id_override=None):
    if not project_page_id:
        return None
    remote_id = notion_helper.get_remote_id_from_page(project_page_id)
    if remote_id:
        return int(remote_id)

    project_name, project_page = notion_helper.get_page_title(project_page_id)
    if not project_name:
        utils.log(f"⚠️ Project page {project_page_id} has no title. Skipping project sync.")
        return None

    client_page_id = (
        notion_helper.get_relation_page(project_page, ["Client", "客户", "客户端"])
        or client_page_id_override
    )
    client_id = ensure_remote_client(client_page_id, workspace_id)
    project_id = create_toggl_project(workspace_id, project_name, client_id)
    if project_id:
        notion_helper.update_page(project_page_id, {"Id": {"number": int(project_id)}})
        utils.log(f"🔗 Linked Notion project '{project_name}' with Toggl ID {project_id}")
    return project_id


def reverse_sync_notion_to_toggl():
    """Find entries in Notion without Toggl IDs and create them in Toggl."""
    utils.log("🔄 Checking for Notion entries to sync back to Toggl...")
    notion_helper.ensure_time_id_property()
    missing_entries = notion_helper.query_missing_toggl_id()
    if not missing_entries:
        utils.log("No missing Toggl IDs found in Notion.")
        return

    # Use the first workspace as default for new entries
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("Cannot perform reverse sync: No Toggl workspaces found.")
        return
    fallback_workspace_id = workspaces[0]["id"]

    for page in missing_entries:
        props = page.get("properties", {})
        title = notion_helper.get_title_from_page(page) or "无描述"
        
        date_prop = props.get("时间", {}).get("date", {})
        if not date_prop or not date_prop.get("start"):
            utils.log(f"⚠️ Skipping Notion page {page.get('id')}: missing start time.")
            continue
            
        start_time = date_prop.get("start")
        end_time = date_prop.get("end")
        
        # Calculate duration in seconds
        start_p = pendulum.parse(start_time)
        if end_time:
            end_p = pendulum.parse(end_time)
            duration = (end_p - start_p).total_seconds()
        else:
            utils.log(f"⚠️ Skipping Notion page {page.get('id')}: missing end time.")
            continue
        if duration <= 0:
            utils.log(f"⚠️ Skipping Notion page {page.get('id')}: duration must be positive.")
            continue
            
        # Get Project ID from Notion relation
        pid = None
        workspace_id = fallback_workspace_id
        client_page_id = notion_helper.get_relation_page(page, ["Client", "客户", "客户端"])
        if client_page_id:
            ensure_remote_client(client_page_id, workspace_id)

        project_page_id = notion_helper.get_relation_page(page, ["Project", "项目"])
        if project_page_id:
            pid = ensure_remote_project(project_page_id, workspace_id, client_page_id_override=client_page_id)
            if not pid:
                utils.log(f"⚠️ Project in Notion for '{title}' does not have a Toggl ID. Creating without Project.")
            elif pid in project_cache:
                workspace_id = project_cache[pid].get("workspace_id", fallback_workspace_id)
            else:
                utils.log(
                    f"⚠️ Project ID {pid} not found in Toggl cache. Creating '{title}' without Project."
                )
                pid = None
        else:
            if client_page_id:
                utils.log(f"⚠️ '{title}' has Client but no Project; Toggl time entries can only attach Client through a Project.")

        # Create in Toggl
        new_toggl_id = create_toggl_entry(workspace_id, title, start_time, duration, pid)
        
        # Write ID back to Notion
        if new_toggl_id:
            try:
                notion_helper.update_page(page["id"], {"Id": {"number": int(new_toggl_id)}})
                utils.log(f"🔗 Linked Notion page {page['id']} with Toggl ID {new_toggl_id}")
            except Exception as e:
                utils.log(f"Failed to update Notion with new Toggl ID: {e}")

def process_entry(task):
    item = {}
    tags = task.get("tags")
    if tags:
        item["标签"] = [
            notion_helper.get_relation_id(
                tag, notion_helper.tag_data_source_id, get_icon(TAG_ICON_URL)
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
    emoji = None

    if pid and pid in project_cache:
        project_info = project_cache[pid]
        raw_project_name = project_info["name"]
        emoji, project_display_name = split_emoji_from_string(raw_project_name)
        
        # 标注展示规则：有描述显描述，没描述显项目名
        item["标题"] = description if description else project_display_name
        
        client_id = project_info.get("client_id")
        project_properties = {"金币":{"number": 1}}
        
        if client_id and client_id in client_cache:
            client_name = client_cache[client_id]
            client_emoji, client_name = split_emoji_from_string(client_name)
            item["Client"] = [
                notion_helper.get_relation_id(
                    client_name,
                    notion_helper.client_data_source_id,
                    {"type": "emoji", "emoji": client_emoji},
                    remote_id=client_id
                )
            ]
            project_properties["Client"] = {
                "relation": [{"id": id} for id in item.get("Client")]
            }
            
        item["Project"] = [
            notion_helper.get_relation_id(
                project_display_name,
                notion_helper.project_data_source_id,
                {"type": "emoji", "emoji": emoji} if emoji else None,
                properties=project_properties,
                remote_id=pid
            )
        ]
    else:
        if pid:
             utils.log(f"⚠️ Project ID {pid} not found in cache. Falling back to description.")
        item["标题"] = description or "无描述"
        
    if description:
        item["备注"] = description
        
    properties = utils.get_properties(item, notion_helper.time_props)
    parent = {
        "data_source_id": notion_helper.time_data_source_id,
        "type": "data_source_id",
    }
    notion_helper.get_date_relation(
        properties, pendulum.from_timestamp(stop_ts, tz="Asia/Shanghai")
    )
    
    icon = None
    if emoji:
         icon = {"type": "emoji", "emoji": emoji}
         
    return parent, properties, icon


import time

def get_detailed_report(workspace_id, start_date, end_date):
    """Fetch detailed report from Toggl Reports API (supports >90 days)."""
    url = "https://api.track.toggl.com/reports/api/v2/details"
    headers = {"Content-Type": "application/json"}
    
    # Reports API requires a user_agent
    params = {
        "workspace_id": workspace_id,
        "since": start_date.to_date_string(),
        "until": end_date.to_date_string(),
        "user_agent": "toggl2notion",
        "page": 1
    }
    
    all_entries = []
    rate_limit_retries = 0
    max_rate_limit_retries = 10
    while True:
        try:
            response = requests.get(url, params=params, auth=auth, headers=headers, timeout=15)
            if response.status_code == 429:
                rate_limit_retries += 1
                if rate_limit_retries > max_rate_limit_retries:
                    utils.log(f"⚠️ Reports API rate limit 连续 {max_rate_limit_retries} 次，放弃重试")
                    return None, 429
                utils.log(f"⚠️ Reports API rate limit hit ({rate_limit_retries}/{max_rate_limit_retries}). Sleeping for 2 seconds...")
                time.sleep(2)
                continue
                
            if not response.ok:
                utils.log(f"Failed to fetch detailed report: {response.status_code} {response.text}")
                return None, response.status_code
            
            data = response.json()
            entries = data.get("data", [])
            all_entries.extend(entries)
            
            utils.log(f"Fetched page {params['page']} ({len(entries)} entries)...")
            
            if len(entries) < data.get("per_page", 50):
                break
                
            params["page"] += 1
            time.sleep(1.1)  # Rate limiting (conservative)
            
        except Exception as e:
            utils.log(f"Exception during report fetch: {e}")
            return None, 500
            
    # Transform to match Time Entries API format
    transformed_entries = []
    for entry in all_entries:
        # Map Reports API fields to Time Entries API fields
        transformed = {
            "id": entry.get("id"),
            "description": entry.get("description"),
            "start": entry.get("start"),
            "stop": entry.get("end"), # Report API uses 'end'
            "duration": entry.get("dur") / 1000, # Report API uses milliseconds
            "tags": entry.get("tags", []),
            "pid": entry.get("pid"), # Project ID
            "project_id": entry.get("pid"), # Keep consistency
            "project": entry.get("project"), # Project Name (Bonus: Reports API gives name!)
            "client": entry.get("client"),   # Client Name (Bonus!)
            # 'project_hex_color': entry.get('project_hex_color')
        }
        
        # Populate cache with names from report if available (Optimization)
        if entry.get("pid"):
            parsed_project_name = entry.get("project")
            # If names are avail, update cache to avoid lookups
            if parsed_project_name:
                 # Note: project_cache structure is {"name": ..., "client_id": ...}
                 # We might miss client_id here if not careful, but name is key
                 if entry.get("pid") not in project_cache:
                      project_cache[entry.get("pid")] = {"name": parsed_project_name}
        
        transformed_entries.append(transformed)
        
    return transformed_entries, 200


def get_historical_entries(workspace_ids, start_date, end_date):
    """Fetch historical entries across all workspaces via Reports API."""
    all_entries = []
    seen_ids = set()

    for workspace_id in workspace_ids:
        utils.log(f"Fetching historical entries for workspace {workspace_id}...")
        entries, status_code = get_detailed_report(workspace_id, start_date, end_date)
        if status_code != 200:
            return None, status_code

        for entry in entries or []:
            entry_id = entry.get("id")
            dedupe_key = entry_id if entry_id is not None else (
                workspace_id,
                entry.get("start"),
                entry.get("description"),
            )
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            all_entries.append(entry)

    return all_entries, 200


def sync_data_range(start_date, end_date, workspace_ids, force_reports_api=False):
    """Sync data for a specific date range."""
    notion_helper.ensure_time_id_property()
    utils.log(f"Synchronizing from {start_date.to_iso8601_string()} to {end_date.to_iso8601_string()}")
    
    current_end = end_date
    while current_end > start_date:
        current_start = current_end.subtract(days=10)
        if current_start < start_date:
            current_start = start_date
            
        entries = None
        status_code = 200
        
        # Check if we are clearly out of 90 days range? 
        days_diff = (pendulum.now("Asia/Shanghai") - current_end).days
        use_reports_api = force_reports_api or (days_diff > 85)
        
        if not use_reports_api:
            entries, status_code = get_time_entries(current_start, current_end)
            if status_code == 400:
                 utils.log(f"⚠️ Standard API failed with 400 (likely historical limit). Retrying with Reports API...")
                 use_reports_api = True
                 status_code = 200 # Reset for retry
            elif status_code == 402:
                 utils.log(f"🛑 Hit Toggl API limit (402). Stopping.")
                 return False # Stop sync

        if use_reports_api:
            entries, status_code = get_historical_entries(workspace_ids, current_start, current_end)
            
            if status_code == 402:
                # Special handling for Free Tier limit on historical reports
                utils.log(f"🛑 Payment Required (402) for range {current_start.to_date_string()} - {current_end.to_date_string()}.")
                utils.log(f"⚠️ Likely reached the limit of historical data access for Free Plan (approx 1 year).")
                utils.log(f"🛑 Stoping backfill to avoid further errors.")
                return False # Stop sync completely for deeper history
            
            if status_code != 200:
                utils.log(f"🛑 Reports API failed with {status_code}. Stopping sync for this chunk.")
                return False

        if entries:
            utils.log(f"Found {len(entries)} entries from {current_start.to_date_string()} to {current_end.to_date_string()}. Processing...")
            # Sort newest first
            entries.sort(key=lambda x: pendulum.parse(x['start']), reverse=True)
            
            for task in entries:
                if task.get("server_deleted_at"):
                    continue
                
                toggl_id = task.get('id')
                existing_page_id = notion_helper.get_page_by_toggl_id(toggl_id)
                
                description_display = task.get('description') or '无描述'
                action = "Updating" if existing_page_id else "Syncing"
                
                utils.log(f"📝 {action}: [{description_display}] ({task.get('start')})")
                
                try:
                    parent, properties, icon = process_entry(task)
                    if existing_page_id:
                        notion_helper.update_page(page_id=existing_page_id, properties=properties, icon=icon)
                    else:
                        notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                except Exception as e:
                    utils.log(f"Error processing task {task.get('id')}: {e}")
        
        if current_start <= start_date:
            break
        
        # Prepare for next iteration
        current_end = current_start.subtract(seconds=1)
        
    return True

def insert_to_notion():
    now = pendulum.now("Asia/Shanghai")
    
    # 1. Check latest entry in Notion (Forward Sync Anchor)
    sorts_desc = [{"property": "时间", "direction": "descending"}]
    response = notion_helper.query(
        data_source_id=notion_helper.time_data_source_id, sorts=sorts_desc, page_size=1
    )
    
    latest_end = None
    if len(response.get("results")) > 0:
        latest_page = response.get("results")[0]
        properties = latest_page.get("properties", {})
        date_prop = properties.get("时间", {}).get("date")
        if date_prop and date_prop.get("end"):
             latest_end = pendulum.parse(date_prop.get("end")).in_timezone("Asia/Shanghai")
        elif date_prop and date_prop.get("start"):
             latest_end = pendulum.parse(date_prop.get("start")).in_timezone("Asia/Shanghai")

    # 2. Check earliest entry in Notion (Backward Gap Check)
    sorts_asc = [{"property": "时间", "direction": "ascending"}]
    response_asc = notion_helper.query(
        data_source_id=notion_helper.time_data_source_id, sorts=sorts_asc, page_size=1
    )
    
    earliest_start = None
    if len(response_asc.get("results")) > 0:
        earliest_page = response_asc.get("results")[0]
        props_early = earliest_page.get("properties", {})
        date_prop_early = props_early.get("时间", {}).get("date")
        if date_prop_early and date_prop_early.get("start"):
            earliest_start = pendulum.parse(date_prop_early.get("start")).in_timezone("Asia/Shanghai")
            utils.log(f"🔍 Found earliest entry in Notion: {date_prop_early.get('start')}")

    # Track API v9 returns all entries for the user
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("No workspaces found or API error.")
        return
    workspace_ids = [ws["id"] for ws in workspaces if ws.get("id") is not None]
    for ws in workspaces:
        load_workspace_cache(ws["id"])

    # 3. Strategy Execution
    account_created_at = get_created_at().in_timezone("Asia/Shanghai")
    gap_threshold_days = 7
    
    # Phase A: Incremental Forward Sync (Latest -> Now)
    # Ensure we cover at least the last 24h even if latest_end is very recent
    if latest_end:
        incremental_start = latest_end.subtract(days=1) 
        utils.log(f"🔄 Starting Incremental Sync from: {incremental_start.to_datetime_string()}")
        sync_data_range(incremental_start, now, workspace_ids)
    else:
        # Notion is empty, full sync will handle it
        incremental_start = account_created_at
        utils.log(f"🚀 Notion is empty. Starting initial full import.")
        sync_data_range(incremental_start, now, workspace_ids)
        return # Initial sync done

    # Phase B: Historical Backfill (Gap Fill: Account Created -> Earliest Entry)
    if earliest_start and (earliest_start - account_created_at).days > gap_threshold_days:
        utils.log(f"⚠️ Missing history detected! Gap between registration ({account_created_at.to_date_string()}) and earliest entry ({earliest_start.to_date_string()}).")
        utils.log(f"🚀 Triggering GAP BACKFILL (Reports API).")
        
        # Sync from Created At -> Earliest Start
        # We stop at earliest_start because we assume data from there onwards exists
        sync_success = sync_data_range(
            account_created_at,
            earliest_start.subtract(seconds=1),
            workspace_ids,
            force_reports_api=True,
        )
        
        if not sync_success:
            utils.log("⚠️ Backfill stopped early due to API limit or error.")
            
    else:
        utils.log(f"✅ History continuity checked. No significant gaps found.")
    
    # After forward sync, perform reverse sync for entries created in Notion
    # Note: Reverse sync is relatively cheap (queries Notion for missing IDs)
    reverse_sync_notion_to_toggl()

def main():
    with sync_notification("Toggl") as notification:
        if init():
            insert_to_notion()
            notification.set_summary("Toggl 数据同步完成")


if __name__ == "__main__":
    main()
