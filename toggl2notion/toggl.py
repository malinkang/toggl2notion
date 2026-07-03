import os
import json
import re
import time
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

GAP_THRESHOLD_DAYS = 7
MAX_MIDDLE_GAPS_PER_RUN = 5
DEFAULT_INCREMENTAL_LOOKBACK_DAYS = 7
DEFAULT_INITIAL_IMPORT_DAYS = 0
STATE_SCHEMA_VERSION = 1
DEFAULT_SYNC_DELETIONS = True
REPORTS_PAGE_SIZE = 100
TIME_ENTRIES_LIMIT_GUARD = 1000


class SyncStats:
    def __init__(self):
        self.processed = 0
        self.created = 0
        self.updated = 0
        self.archived = 0
        self.failed = 0
        self.failures = []

    def add_success(self, was_update):
        if was_update:
            self.updated += 1
        else:
            self.created += 1

    def add_archive(self):
        self.archived += 1

    def add_failure(self, scope, identifier, message):
        self.failed += 1
        detail = f"{scope} {identifier}: {message}"
        self.failures.append(detail)
        utils.log(f"❌ {detail}")

    def summary(self):
        parts = [f"新增 {self.created}", f"更新 {self.updated}"]
        if self.archived:
            parts.append(f"归档 {self.archived}")
        parts.append(f"失败 {self.failed}")
        return "，".join(parts)

    def failure_summary(self, limit=5):
        if not self.failures:
            return ""
        visible = self.failures[:limit]
        more = len(self.failures) - len(visible)
        suffix = f"；另有 {more} 个错误" if more > 0 else ""
        return "；".join(visible) + suffix


def parse_int_env(name, default, minimum=None, maximum=None):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        utils.log(f"⚠️ Invalid {name}={raw!r}; using default {default}.")
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def parse_bool_env(name, default=False):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_optional_date_env(name):
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return pendulum.parse(raw, tz="Asia/Shanghai").in_timezone("Asia/Shanghai")
    except Exception as e:
        raise ValueError(f"{name} 不是有效日期：{raw}") from e


def get_state_key():
    raw = (
        os.getenv("ACTIVATION_CODE")
        or os.getenv("USER_ID")
        or os.getenv("NOTION_PAGE")
        or "default"
    )
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")
    return key[:80] or "default"


def get_state_path():
    state_dir = os.getenv("TOGGL_SYNC_STATE_DIR") or os.path.join(os.getcwd(), "state")
    return os.path.join(state_dir, f"{get_state_key()}.json")


class SyncState:
    def __init__(self, path=None):
        self.path = path or get_state_path()
        self.data = {
            "schema_version": STATE_SCHEMA_VERSION,
            "checked_empty_gaps": {},
        }
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("checked_empty_gaps", {})
        except Exception as e:
            utils.log(f"⚠️ Failed to load sync state {self.path}: {e}")

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)

    def gap_key(self, start_date, end_date):
        return f"{start_date.to_iso8601_string()}::{end_date.to_iso8601_string()}"

    def is_empty_gap_checked(self, start_date, end_date):
        return self.gap_key(start_date, end_date) in self.data.get("checked_empty_gaps", {})

    def mark_empty_gap_checked(self, start_date, end_date):
        key = self.gap_key(start_date, end_date)
        self.data.setdefault("checked_empty_gaps", {})[key] = {
            "start": start_date.to_iso8601_string(),
            "end": end_date.to_iso8601_string(),
            "checked_at": pendulum.now("Asia/Shanghai").to_iso8601_string(),
        }
        self.save()


def get_time_boundary(page, prefer_end=False):
    if not page:
        return None
    date_prop = page.get("properties", {}).get("时间", {}).get("date")
    if not date_prop:
        return None
    value = date_prop.get("end") if prefer_end else date_prop.get("start")
    value = value or date_prop.get("start") or date_prop.get("end")
    return pendulum.parse(value).in_timezone("Asia/Shanghai") if value else None

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
    """Find explicitly marked Notion entries without Toggl IDs and create them in Toggl."""
    utils.log("🔄 Checking for Notion entries explicitly marked to sync back to Toggl...")
    notion_helper.ensure_time_id_property()
    missing_entries = notion_helper.query_entries_marked_for_toggl_sync()
    if not missing_entries:
        utils.log("No Notion entries marked for Toggl reverse sync.")
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
    item["时间"] = {"start": start_ts, "end": stop_ts}
    
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
        
    properties = notion_helper.build_properties(notion_helper.time_data_source_id, item, mandatory_properties=["标题", "Id"])
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
        "page": 1,
        "page_size": REPORTS_PAGE_SIZE,
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
            per_page = data.get("per_page") or params["page_size"]
            total_count = data.get("total_count")
            total_suffix = f" / total {total_count}" if total_count is not None else ""

            utils.log(
                f"Fetched Reports page {params['page']} "
                f"({len(entries)} entries, per_page={per_page}{total_suffix})..."
            )
            
            if len(entries) < per_page:
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


def get_time_range_from_page(page):
    date_prop = page.get("properties", {}).get("时间", {}).get("date")
    if not date_prop or not date_prop.get("start"):
        return None
    start = pendulum.parse(date_prop.get("start")).in_timezone("Asia/Shanghai")
    end = pendulum.parse(date_prop.get("end") or date_prop.get("start")).in_timezone("Asia/Shanghai")
    return start, end


def find_time_gaps(ranges, threshold_days=GAP_THRESHOLD_DAYS, max_gaps=MAX_MIDDLE_GAPS_PER_RUN):
    """Find gaps between sorted time ranges that are large enough to backfill."""
    normalized = sorted(
        [(start, end) for start, end in ranges if start and end],
        key=lambda item: item[0],
    )
    gaps = []
    previous_end = None
    for start, end in normalized:
        if previous_end and start > previous_end:
            gap_days = (start.int_timestamp - previous_end.int_timestamp) / 86400
            if gap_days > threshold_days:
                gaps.append((previous_end.add(seconds=1), start.subtract(seconds=1)))
                if max_gaps is not None and len(gaps) >= max_gaps:
                    break
        if previous_end is None or end > previous_end:
            previous_end = end
    return gaps


def find_middle_gaps(max_gaps=MAX_MIDDLE_GAPS_PER_RUN, state=None):
    pages = notion_helper.query_time_entries_sorted_by_time(toggl_only=True)
    ranges = []
    for page in pages:
        time_range = get_time_range_from_page(page)
        if time_range:
            ranges.append(time_range)
    gaps = find_time_gaps(ranges, max_gaps=None)
    if state:
        skipped = [gap for gap in gaps if state.is_empty_gap_checked(*gap)]
        gaps = [gap for gap in gaps if not state.is_empty_gap_checked(*gap)]
        if skipped:
            utils.log(f"✅ Skipping {len(skipped)} checked empty middle gap(s).")
    gaps = gaps[:max_gaps]
    if gaps:
        utils.log(f"⚠️ Found {len(gaps)} middle history gap(s) larger than {GAP_THRESHOLD_DAYS} days.")
    else:
        utils.log("✅ No middle history gaps found.")
    return gaps


def sync_middle_gaps(workspace_ids, stats, progress=None, state=None):
    gaps = find_middle_gaps(state=state)
    for start_date, end_date in gaps:
        utils.log(
            f"🚀 Backfilling middle gap from {start_date.to_date_string()} to {end_date.to_date_string()} via Reports API."
        )
        processed_before = stats.processed
        sync_success = sync_data_range(
            start_date,
            end_date,
            workspace_ids,
            force_reports_api=True,
            progress=progress,
            stats=stats,
        )
        if sync_success and state and stats.processed == processed_before:
            utils.log("✅ Reports API returned no entries for this gap; marking it as checked.")
            state.mark_empty_gap_checked(start_date, end_date)


def get_entry_toggl_id(entry):
    entry_id = entry.get("id") if entry else None
    if entry_id is None:
        return None
    try:
        return str(int(entry_id))
    except (TypeError, ValueError):
        return str(entry_id)


def sync_deleted_notion_entries(start_date, end_date, source_entries, stats, progress=None):
    """Archive Notion Time pages that disappeared from Toggl within a confirmed range."""
    source_ids = {
        entry_id
        for entry_id in (
            get_entry_toggl_id(entry)
            for entry in (source_entries or [])
            if not entry.get("server_deleted_at")
        )
        if entry_id
    }
    try:
        pages = notion_helper.query_toggl_entries_by_time_range(start_date, end_date)
    except Exception as e:
        stats.add_failure(
            "delete-check",
            f"{start_date.to_date_string()}-{end_date.to_date_string()}",
            str(e),
        )
        return

    archived = 0
    for page in pages:
        notion_toggl_id = notion_helper.get_toggl_id_from_time_page(page)
        notion_toggl_id = str(int(notion_toggl_id)) if notion_toggl_id is not None else None
        if not notion_toggl_id or notion_toggl_id in source_ids:
            continue
        page_id = page.get("id")
        if not page_id:
            continue
        try:
            notion_helper.archive_page(page_id)
            archived += 1
            stats.add_archive()
            utils.log(f"🗑️ Archived Notion time entry deleted from Toggl: {notion_toggl_id}")
            if progress:
                progress.add(f"Toggl 删除记录 {notion_toggl_id}", page_id=page_id, status="已归档")
        except Exception as e:
            stats.add_failure("delete", notion_toggl_id, str(e))
    if archived:
        utils.log(f"✅ Archived {archived} Notion entries deleted from Toggl in this range.")


def sync_data_range(start_date, end_date, workspace_ids, force_reports_api=False, progress=None, stats=None, sync_deletions=False):
    """Sync data for a specific date range."""
    stats = stats or SyncStats()
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
                 stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", "Toggl API returned 402")
                 return False # Stop sync
            elif status_code != 200:
                 stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", f"Toggl API returned {status_code}")
                 return False
            elif entries is not None and len(entries) >= TIME_ENTRIES_LIMIT_GUARD:
                 utils.log(
                     f"⚠️ Standard API returned {len(entries)} entries for "
                     f"{current_start.to_date_string()}-{current_end.to_date_string()}, "
                     f"near the API limit. Retrying with Reports API to avoid missing data..."
                 )
                 use_reports_api = True
                 entries = None

        if use_reports_api:
            entries, status_code = get_historical_entries(workspace_ids, current_start, current_end)
            
            if status_code == 402:
                # Special handling for Free Tier limit on historical reports
                utils.log(f"🛑 Payment Required (402) for range {current_start.to_date_string()} - {current_end.to_date_string()}.")
                utils.log(f"⚠️ Likely reached the limit of historical data access for Free Plan (approx 1 year).")
                utils.log(f"🛑 Stoping backfill to avoid further errors.")
                stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", "Reports API returned 402")
                return False # Stop sync completely for deeper history
            
            if status_code != 200:
                utils.log(f"🛑 Reports API failed with {status_code}. Stopping sync for this chunk.")
                stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", f"Reports API returned {status_code}")
                return False

        if entries:
            utils.log(f"Found {len(entries)} entries from {current_start.to_date_string()} to {current_end.to_date_string()}. Processing...")
            # Sort newest first
            entries.sort(key=lambda x: pendulum.parse(x['start']), reverse=True)
            
            for task in entries:
                if task.get("server_deleted_at"):
                    continue
                
                toggl_id = task.get('id')
                description_display = task.get('description') or '无描述'
                stats.processed += 1
                
                try:
                    existing_page_id = notion_helper.get_page_by_toggl_id(toggl_id)
                    action = "Updating" if existing_page_id else "Syncing"
                    utils.log(f"📝 {action}: [{description_display}] ({task.get('start')})")
                    parent, properties, icon = process_entry(task)
                    if existing_page_id:
                        notion_helper.update_page(page_id=existing_page_id, properties=properties, icon=icon)
                        page_id = existing_page_id
                    else:
                        page = notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                        page_id = page.get("id")
                    stats.add_success(was_update=bool(existing_page_id))
                    if progress:
                        status = "已更新" if existing_page_id else "已新增"
                        progress.add(description_display, page_id=page_id, status=status)
                except Exception as e:
                    stats.add_failure("entry", task.get("id"), str(e))

        if sync_deletions:
            sync_deleted_notion_entries(current_start, current_end, entries or [], stats, progress=progress)
        
        if current_start <= start_date:
            break
        
        # Prepare for next iteration
        current_end = current_start.subtract(seconds=1)
        
    return True

def insert_to_notion(progress=None):
    stats = SyncStats()
    now = pendulum.now("Asia/Shanghai")
    notion_helper.ensure_time_id_property()
    state = SyncState()
    
    # 1. Check Toggl-linked entries only; manual Notion records must not move sync anchors.
    latest_page = notion_helper.query_time_entry_boundary(direction="descending", toggl_only=True)
    latest_end = get_time_boundary(latest_page, prefer_end=True)

    # 2. Check earliest Toggl-linked entry in Notion (Backward Gap Check)
    earliest_page = notion_helper.query_time_entry_boundary(direction="ascending", toggl_only=True)
    earliest_start = get_time_boundary(earliest_page)
    if earliest_start:
        utils.log(f"🔍 Found earliest Toggl-linked entry in Notion: {earliest_start.to_iso8601_string()}")

    # Track API v9 returns all entries for the user
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("No workspaces found or API error.")
        stats.add_failure("workspace", "all", "No workspaces found or Toggl API error")
        return stats
    workspace_ids = [ws["id"] for ws in workspaces if ws.get("id") is not None]
    for ws in workspaces:
        load_workspace_cache(ws["id"])

    sync_deletions = parse_bool_env("TOGGL_SYNC_DELETIONS", DEFAULT_SYNC_DELETIONS)
    manual_backfill_start = parse_optional_date_env("TOGGL_BACKFILL_START")
    manual_backfill_end = parse_optional_date_env("TOGGL_BACKFILL_END")
    if manual_backfill_start or manual_backfill_end:
        if not (manual_backfill_start and manual_backfill_end):
            stats.add_failure("backfill", "manual", "TOGGL_BACKFILL_START 和 TOGGL_BACKFILL_END 必须同时设置")
            return stats
        if manual_backfill_end <= manual_backfill_start:
            stats.add_failure("backfill", "manual", "TOGGL_BACKFILL_END 必须晚于 TOGGL_BACKFILL_START")
            return stats
        utils.log(
            f"🚀 Manual backfill from {manual_backfill_start.to_iso8601_string()} "
            f"to {manual_backfill_end.to_iso8601_string()} via Reports API."
        )
        sync_data_range(
            manual_backfill_start,
            manual_backfill_end,
            workspace_ids,
            force_reports_api=True,
            progress=progress,
            stats=stats,
            sync_deletions=parse_bool_env("TOGGL_BACKFILL_SYNC_DELETIONS", False),
        )
        return stats

    # 3. Strategy Execution
    account_created_at = get_created_at().in_timezone("Asia/Shanghai")
    # Phase A: Incremental Forward Sync (Latest -> Now)
    # Re-scan a configurable recent window so older edits are not missed immediately.
    lookback_days = parse_int_env(
        "TOGGL_INCREMENTAL_LOOKBACK_DAYS",
        DEFAULT_INCREMENTAL_LOOKBACK_DAYS,
        minimum=1,
        maximum=90,
    )
    if latest_end:
        incremental_start = latest_end.subtract(days=lookback_days)
        utils.log(
            f"🔄 Starting Incremental Sync from: {incremental_start.to_datetime_string()} "
            f"(lookback {lookback_days} day(s))"
        )
        sync_data_range(
            incremental_start,
            now,
            workspace_ids,
            progress=progress,
            stats=stats,
            sync_deletions=sync_deletions,
        )
    else:
        initial_import_days = parse_int_env(
            "TOGGL_INITIAL_IMPORT_DAYS",
            DEFAULT_INITIAL_IMPORT_DAYS,
            minimum=0,
        )
        incremental_start = (
            max(account_created_at, now.subtract(days=initial_import_days))
            if initial_import_days > 0
            else account_created_at
        )
        utils.log(f"🚀 Notion has no Toggl-linked entries. Starting initial import.")
        sync_data_range(incremental_start, now, workspace_ids, progress=progress, stats=stats)
        return stats # Initial sync done

    # Phase B: Historical Backfill (Gap Fill: Account Created -> Earliest Entry)
    if earliest_start and ((earliest_start.int_timestamp - account_created_at.int_timestamp) / 86400) > GAP_THRESHOLD_DAYS:
        utils.log(f"⚠️ Missing history detected! Gap between registration ({account_created_at.to_date_string()}) and earliest entry ({earliest_start.to_date_string()}).")
        utils.log(f"🚀 Triggering GAP BACKFILL (Reports API).")
        
        # Sync from Created At -> Earliest Start
        # We stop at earliest_start because we assume data from there onwards exists
        sync_success = sync_data_range(
            account_created_at,
            earliest_start.subtract(seconds=1),
            workspace_ids,
            force_reports_api=True,
            progress=progress,
            stats=stats,
        )
        
        if not sync_success:
            utils.log("⚠️ Backfill stopped early due to API limit or error.")
            
    else:
        utils.log(f"✅ History continuity checked. No significant gaps found.")

    sync_middle_gaps(workspace_ids, stats, progress=progress, state=state)
    
    # After forward sync, perform reverse sync for entries created in Notion
    # Note: Reverse sync is relatively cheap (queries Notion for missing IDs)
    reverse_sync_notion_to_toggl()
    return stats

def main():
    with sync_notification("Toggl") as notification:
        if init():
            progress = notification.progress("同步", batch_size=10)
            stats = insert_to_notion(progress=progress)
            progress.flush()
            if stats and stats.failed:
                summary = f"Toggl 数据同步部分失败：{stats.summary()}"
                notification.set_summary(summary)
                raise RuntimeError(f"{summary}。{stats.failure_summary()}")
            summary = f"Toggl 数据同步完成：{stats.summary() if stats else '无数据变更'}"
            notification.set_summary(summary)


if __name__ == "__main__":
    main()
