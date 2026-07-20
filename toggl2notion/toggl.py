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
from notionhub.sync_policy import current_sync_policy
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


def is_full_sync_requested():
    return os.getenv("SYNC_MODE", "").strip().lower() == "full" or os.getenv("TOGGL_FORCE_FULL_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}


def get_service_options():
    raw = os.getenv("SERVICE_OPTIONS") or os.getenv("TOGGL_SERVICE_OPTIONS") or "{}"
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def allow_reverse_sync():
    return get_service_options().get("allowReverseSync", True) is not False


def is_blocking_notion_config_error(message):
    message = str(message).lower()
    return (
        "could not find data_source" in message
        or "could not find database" in message
        or ("object_not_found" in message and ("data_source" in message or "database" in message))
    )


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
        scope_label = {
            "range": "时间范围",
            "entry": "时间记录",
            "delete": "删除记录",
            "workspace": "工作区",
            "backfill": "历史回填",
        }.get(scope, scope)
        identifier_label = {"all": "全部", "manual": "手动"}.get(identifier, identifier)
        detail = f"{scope_label} {identifier_label}: {message}"
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
        utils.log(f"配置 {name}={raw!r} 无效，将使用默认值 {default}")
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
            utils.log(f"读取同步状态失败 {self.path}: {e}")

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
        utils.log("缺少 TOGGL_TOKEN 环境变量")
        return False
    auth = HTTPBasicAuth(f"{toggl_token}", "api_token")
    return True


def get_created_at():
    response = requests.get("https://api.track.toggl.com/api/v9/me", auth=auth, timeout=15)
    if response.ok:
        data = response.json()
        if data.get("created_at"):
            return pendulum.parse(data["created_at"])
    options = get_service_options()
    stored_created_at = options.get("accountCreatedAt")
    if stored_created_at:
        utils.log("Toggl 用户信息接口暂时不可用，使用已保存的注册时间")
        return pendulum.parse(stored_created_at)
    raise RuntimeError("无法获取 Toggl 注册时间，请重新连接 Toggl 后再试")


def effective_sync_start(account_created_at, options=None):
    options = options if isinstance(options, dict) else get_service_options()
    timezone = str(options.get("timezone") or "Asia/Shanghai")
    registered = account_created_at.in_timezone(timezone)
    configured = options.get("syncStartDate")
    if not configured:
        return registered
    custom = pendulum.parse(str(configured), tz=timezone).start_of("day")
    return max(registered, custom)

def get_workspaces():
    response = requests.get(
        "https://api.track.toggl.com/api/v9/me/workspaces", auth=auth, timeout=15
    )
    if response.ok:
        return response.json()
    else:
        utils.log(f"获取 Toggl 工作区失败: {response.text}")
        return []

def normalize_cache_name(name):
    return (name or "").strip().lower()


def load_workspace_cache(workspace_id):
    global project_cache, client_cache, project_name_cache, client_name_cache
    # Load Clients
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients", auth=auth, timeout=15)
    if response.ok:
        clients = response.json()
        utils.log(f"已从工作区 {workspace_id} 加载 {len(clients)} 个客户")
        for c in clients:
            client_cache[c["id"]] = c["name"]
            client_name_cache[(workspace_id, normalize_cache_name(c.get("name")))] = c["id"]
    else:
        utils.log(f"加载工作区 {workspace_id} 的客户失败: {response.status_code} {response.text}")
    
    # Load Projects
    response = requests.get(f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects", auth=auth, timeout=15)
    if response.ok:
        projects = response.json()
        utils.log(f"已从工作区 {workspace_id} 加载 {len(projects)} 个项目")
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
        utils.log(f"加载工作区 {workspace_id} 的项目失败: {response.status_code} {response.text}")

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
        utils.log(f"获取 {start_date.to_date_string()} 至 {end_date.to_date_string()} 的时间记录失败: {response.status_code} {response.text}")
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
        utils.log(f"已创建 Toggl 时间记录: [{description}] (ID: {entry['id']})")
        return entry.get("id")
    else:
        utils.log(f"创建 Toggl 时间记录失败: {response.status_code} {response.text}")
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
        utils.log(f"创建 Toggl 客户“{clean_name}”失败: {response.status_code} {response.text}")
        return None
    client = response.json()
    client_id = client.get("id")
    if client_id:
        client_cache[client_id] = client.get("name") or clean_name
        client_name_cache[cache_key] = client_id
        utils.log(f"已创建 Toggl 客户: [{clean_name}] (ID: {client_id})")
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
        utils.log(f"创建 Toggl 项目“{clean_name}”失败: {response.status_code} {response.text}")
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
        utils.log(f"已创建 Toggl 项目: [{clean_name}] (ID: {project_id})")
    return project_id


def ensure_remote_client(client_page_id, workspace_id):
    if not client_page_id:
        return None
    remote_id = notion_helper.get_remote_id_from_page(client_page_id)
    if remote_id:
        return int(remote_id)

    client_name, _ = notion_helper.get_page_title(client_page_id)
    if not client_name:
        utils.log(f"Notion 客户页面 {client_page_id} 没有标题，跳过客户同步")
        return None

    client_id = create_toggl_client(workspace_id, client_name)
    if client_id:
        notion_helper.update_page(client_page_id, {"Id": {"number": int(client_id)}})
        utils.log(f"已将 Notion 客户“{client_name}”关联到 Toggl ID {client_id}")
    return client_id


def ensure_remote_project(project_page_id, workspace_id, client_page_id_override=None):
    if not project_page_id:
        return None
    remote_id = notion_helper.get_remote_id_from_page(project_page_id)
    if remote_id:
        return int(remote_id)

    project_name, project_page = notion_helper.get_page_title(project_page_id)
    if not project_name:
        utils.log(f"Notion 项目页面 {project_page_id} 没有标题，跳过项目同步")
        return None

    client_page_id = (
        notion_helper.get_relation_page(project_page, ["Client", "客户", "客户端"])
        or client_page_id_override
    )
    client_id = ensure_remote_client(client_page_id, workspace_id)
    project_id = create_toggl_project(workspace_id, project_name, client_id)
    if project_id:
        notion_helper.update_page(project_page_id, {"Id": {"number": int(project_id)}})
        utils.log(f"已将 Notion 项目“{project_name}”关联到 Toggl ID {project_id}")
    return project_id


def reverse_sync_notion_to_toggl():
    """Find explicitly marked Notion entries without Toggl IDs and create them in Toggl."""
    utils.log("正在检查明确标记为需要回写到 Toggl 的 Notion 记录")
    notion_helper.ensure_time_id_property()
    missing_entries = notion_helper.query_entries_marked_for_toggl_sync()
    if not missing_entries:
        utils.log("没有标记为需要反向同步到 Toggl 的 Notion 记录")
        return

    # Use the first workspace as default for new entries
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("无法执行反向同步: 未找到 Toggl 工作区")
        return
    fallback_workspace_id = workspaces[0]["id"]

    for page in missing_entries:
        props = page.get("properties", {})
        title = notion_helper.get_title_from_page(page) or "无描述"
        
        date_prop = props.get("时间", {}).get("date", {})
        if not date_prop or not date_prop.get("start"):
            utils.log(f"跳过 Notion 页面 {page.get('id')}: 缺少开始时间")
            continue
            
        start_time = date_prop.get("start")
        end_time = date_prop.get("end")
        
        # Calculate duration in seconds
        start_p = pendulum.parse(start_time)
        if end_time:
            end_p = pendulum.parse(end_time)
            duration = (end_p - start_p).total_seconds()
        else:
            utils.log(f"跳过 Notion 页面 {page.get('id')}: 缺少结束时间")
            continue
        if duration <= 0:
            utils.log(f"跳过 Notion 页面 {page.get('id')}: 时长必须大于零")
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
                utils.log(f"Notion 中“{title}”的项目没有 Toggl ID，将不关联项目并继续创建")
            elif pid in project_cache:
                workspace_id = project_cache[pid].get("workspace_id", fallback_workspace_id)
            else:
                utils.log(
                    f"⚠️ Project ID {pid} not found in Toggl cache. Creating '{title}' without Project."
                )
                pid = None
        else:
            if client_page_id:
                utils.log(f"“{title}”已关联客户但未关联项目，Toggl 时间记录只能通过项目关联客户")

        # Create in Toggl
        new_toggl_id = create_toggl_entry(workspace_id, title, start_time, duration, pid)
        
        # Write ID back to Notion
        if new_toggl_id:
            try:
                notion_helper.update_page(page["id"], {"Id": {"number": int(new_toggl_id)}})
                utils.log(f"已将 Notion 页面 {page['id']} 关联到 Toggl ID {new_toggl_id}")
            except Exception as e:
                utils.log(f"将新 Toggl ID 写回 Notion 失败: {e}")

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
        project_properties = {"金币":{"number": 0}}
        
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
             utils.log(f"缓存中未找到项目 ID {pid}，改用描述作为标题")
        item["标题"] = description or "无描述"
        
    if description:
        item["备注"] = description
        
    properties = notion_helper.build_properties(notion_helper.time_data_source_id, item, mandatory_properties=["标题", "Id"])
    parent = {
        "data_source_id": notion_helper.time_data_source_id,
        "type": "data_source_id",
    }
    # 时间记录按开始时间归属日/月/周/年，避免跨天记录被挂到结束日期。
    notion_helper.get_date_relation(
        properties, pendulum.from_timestamp(start_ts, tz="Asia/Shanghai")
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
                utils.log(f"报表 API 触发限流（{rate_limit_retries}/{max_rate_limit_retries}），等待 2 秒后重试")
                time.sleep(2)
                continue
                
            if not response.ok:
                utils.log(f"获取详细报表失败: {response.status_code} {response.text}")
                return None, response.status_code
            
            data = response.json()
            entries = data.get("data", [])
            all_entries.extend(entries)
            per_page = data.get("per_page") or params["page_size"]
            total_count = data.get("total_count")
            total_suffix = f"，共 {total_count} 条" if total_count is not None else ""

            utils.log(
                f"已获取报表第 {params['page']} 页"
                f"（本页 {len(entries)} 条，每页 {per_page} 条{total_suffix}）"
            )
            
            if len(entries) < per_page:
                break
                
            params["page"] += 1
            time.sleep(1.1)  # Rate limiting (conservative)
            
        except Exception as e:
            utils.log(f"获取报表时发生异常: {e}")
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
        utils.log(f"正在获取工作区 {workspace_id} 的历史记录")
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


def find_middle_gaps(max_gaps=MAX_MIDDLE_GAPS_PER_RUN, state=None, lower_bound=None):
    pages = notion_helper.query_time_entries_sorted_by_time(toggl_only=True)
    ranges = []
    for page in pages:
        time_range = get_time_range_from_page(page)
        if time_range:
            ranges.append(time_range)
    gaps = find_time_gaps(ranges, max_gaps=None)
    if lower_bound:
        gaps = [
            (max(start, lower_bound), end)
            for start, end in gaps
            if end >= lower_bound
        ]
    if state:
        skipped = [gap for gap in gaps if state.is_empty_gap_checked(*gap)]
        gaps = [gap for gap in gaps if not state.is_empty_gap_checked(*gap)]
        if skipped:
            utils.log(f"已跳过 {len(skipped)} 个确认无数据的历史记录缺口")
    gaps = gaps[:max_gaps]
    if gaps:
        utils.log(f"发现 {len(gaps)} 个超过 {GAP_THRESHOLD_DAYS} 天的历史记录缺口")
    else:
        utils.log("未发现历史记录缺口")
    return gaps


def sync_middle_gaps(workspace_ids, stats, progress=None, state=None, lower_bound=None):
    gaps = find_middle_gaps(state=state, lower_bound=lower_bound)
    for start_date, end_date in gaps:
        utils.log(f"正在通过报表 API 回填 {start_date.to_date_string()} 至 {end_date.to_date_string()} 的历史缺口")
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
            utils.log("报表 API 未返回该缺口内的记录，已标记为检查完成")
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
            utils.log(f"已归档从 Toggl 删除的 Notion 时间记录: {notion_toggl_id}")
            if progress:
                progress.add(f"Toggl 删除记录 {notion_toggl_id}", page_id=page_id, status="已归档")
        except Exception as e:
            stats.add_failure("delete", notion_toggl_id, str(e))
    if archived:
        utils.log(f"已归档当前范围内从 Toggl 删除的 {archived} 条 Notion 记录")


def sync_data_range(start_date, end_date, workspace_ids, force_reports_api=False, progress=None, stats=None, sync_deletions=False):
    """Sync data for a specific date range."""
    stats = stats or SyncStats()
    sync_policy = current_sync_policy()
    notion_helper.ensure_time_id_property()
    utils.log(f"正在同步 {start_date.to_iso8601_string()} 至 {end_date.to_iso8601_string()} 的记录")
    
    current_end = end_date
    while current_end > start_date:
        if sync_policy.is_trial and sync_policy.remaining("time_entries") <= 0:
            utils.log("免费体验时间记录额度已用完，停止继续读取历史数据")
            return True
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
                 utils.log("标准 API 返回 400，可能超出历史数据范围，改用报表 API 重试")
                 use_reports_api = True
                 status_code = 200 # Reset for retry
            elif status_code == 402:
                 utils.log("Toggl API 返回 402，已停止同步")
                 stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", "Toggl API 返回 402")
                 return False # Stop sync
            elif status_code != 200:
                 stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", f"Toggl API 返回 {status_code}")
                 return False
            elif entries is not None and len(entries) >= TIME_ENTRIES_LIMIT_GUARD:
                 utils.log(
                     f"标准 API 为 {current_start.to_date_string()} 至 {current_end.to_date_string()} "
                     f"返回 {len(entries)} 条记录，接近接口上限，改用报表 API 重试以避免遗漏"
                 )
                 use_reports_api = True
                 entries = None

        if use_reports_api:
            entries, status_code = get_historical_entries(workspace_ids, current_start, current_end)
            
            if status_code == 402:
                # Special handling for Free Tier limit on historical reports
                utils.log(f"报表 API 为 {current_start.to_date_string()} 至 {current_end.to_date_string()} 返回 402")
                utils.log("可能已达到免费套餐的历史数据访问范围，通常约为 1 年")
                utils.log("已停止历史回填，避免产生更多错误")
                stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", "报表 API 返回 402")
                return False # Stop sync completely for deeper history
            
            if status_code != 200:
                utils.log(f"报表 API 请求失败，状态码 {status_code}，停止同步当前批次")
                stats.add_failure("range", f"{current_start.to_date_string()}-{current_end.to_date_string()}", f"报表 API 返回 {status_code}")
                return False

        if entries:
            utils.log(f"找到 {current_start.to_date_string()} 至 {current_end.to_date_string()} 的 {len(entries)} 条记录，正在处理")
            # Sort newest first
            entries.sort(key=lambda x: pendulum.parse(x['start']), reverse=True)
            
            for task in entries:
                if sync_policy.is_trial and sync_policy.remaining("time_entries") <= 0:
                    utils.log("免费体验时间记录额度已用完，停止继续读取历史数据")
                    return True
                if task.get("server_deleted_at"):
                    continue
                
                toggl_id = task.get('id')
                description_display = task.get('description') or '无描述'
                stats.processed += 1
                
                try:
                    existing_page_id = notion_helper.get_page_by_toggl_id(toggl_id)
                    action = "正在更新" if existing_page_id else "正在同步"
                    utils.log(f"{action}: [{description_display}] ({task.get('start')})")
                    parent, properties, icon = process_entry(task)
                    if existing_page_id:
                        notion_helper.update_page(page_id=existing_page_id, properties=properties, icon=icon)
                        page_id = existing_page_id
                    else:
                        if not sync_policy.can_create("time_entries", toggl_id):
                            continue
                        page = notion_helper.create_page(parent=parent, properties=properties, icon=icon)
                        page_id = page.get("id")
                        sync_policy.record_success(
                            "time_entries",
                            toggl_id,
                            occurred_at=task.get("start"),
                            heatmap_value=max(1, round(int(task.get("duration") or 0) / 60)),
                            created=True,
                        )
                    stats.add_success(was_update=bool(existing_page_id))
                    if progress:
                        status = "已更新" if existing_page_id else "已新增"
                        progress.add(description_display, page_id=page_id, status=status)
                except Exception as e:
                    error_message = str(e)
                    stats.add_failure("entry", task.get("id"), error_message)
                    if is_blocking_notion_config_error(error_message):
                        utils.log("检测到 Notion 模板数据库不可访问，停止本次同步以避免长时间重复失败")
                        return False

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
        utils.log(f"Notion 中最早关联 Toggl 的记录: {earliest_start.to_iso8601_string()}")

    # Track API v9 returns all entries for the user
    workspaces = get_workspaces()
    if not workspaces:
        utils.log("未找到工作区，或 API 请求失败")
        stats.add_failure("workspace", "all", "未找到工作区，或 Toggl API 请求失败")
        return stats
    workspace_ids = [ws["id"] for ws in workspaces if ws.get("id") is not None]
    for ws in workspaces:
        load_workspace_cache(ws["id"])

    sync_deletions = (
        parse_bool_env("TOGGL_SYNC_DELETIONS", DEFAULT_SYNC_DELETIONS)
        if not current_sync_policy().is_trial
        else False
    )
    account_created_at = get_created_at().in_timezone("Asia/Shanghai")
    sync_start = effective_sync_start(account_created_at).in_timezone("Asia/Shanghai")
    manual_backfill_start = parse_optional_date_env("TOGGL_BACKFILL_START")
    manual_backfill_end = parse_optional_date_env("TOGGL_BACKFILL_END")
    if current_sync_policy().is_trial:
        manual_backfill_start = None
        manual_backfill_end = None
    if manual_backfill_start or manual_backfill_end:
        if not (manual_backfill_start and manual_backfill_end):
            stats.add_failure("backfill", "manual", "TOGGL_BACKFILL_START 和 TOGGL_BACKFILL_END 必须同时设置")
            return stats
        manual_backfill_start = max(manual_backfill_start, sync_start)
        if manual_backfill_end <= manual_backfill_start:
            stats.add_failure("backfill", "manual", "TOGGL_BACKFILL_END 必须晚于 TOGGL_BACKFILL_START")
            return stats
        utils.log(
            f"正在通过报表 API 手动回填 {manual_backfill_start.to_iso8601_string()} "
            f"至 {manual_backfill_end.to_iso8601_string()} 的历史记录"
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
    if is_full_sync_requested() and not current_sync_policy().is_trial:
        utils.log(
            f"开始全量同步：从 {sync_start.to_datetime_string()} "
            f"同步到 {now.to_datetime_string()}"
        )
        sync_data_range(
            sync_start,
            now,
            workspace_ids,
            force_reports_api=True,
            progress=progress,
            stats=stats,
            sync_deletions=False,
        )
        return stats

    # Phase A: Incremental Forward Sync (Latest -> Now)
    # Re-scan a configurable recent window so older edits are not missed immediately.
    lookback_days = parse_int_env(
        "TOGGL_INCREMENTAL_LOOKBACK_DAYS",
        DEFAULT_INCREMENTAL_LOOKBACK_DAYS,
        minimum=1,
        maximum=90,
    )
    if latest_end:
        incremental_start = max(latest_end.subtract(days=lookback_days), sync_start)
        utils.log(
            f"开始增量同步：{incremental_start.to_datetime_string()}，"
            f"回看 {lookback_days} 天"
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
            max(sync_start, now.subtract(days=initial_import_days))
            if initial_import_days > 0
            else sync_start
        )
        utils.log("Notion 中没有关联 Toggl 的记录，开始首次导入")
        sync_data_range(incremental_start, now, workspace_ids, progress=progress, stats=stats)
        return stats # Initial sync done

    # Phase B: Historical Backfill (Gap Fill: Account Created -> Earliest Entry)
    if earliest_start and ((earliest_start.int_timestamp - sync_start.int_timestamp) / 86400) > GAP_THRESHOLD_DAYS:
        utils.log(f"检测到历史记录缺口: {sync_start.to_date_string()} 至最早记录 {earliest_start.to_date_string()}")
        utils.log("开始通过报表 API 回填历史记录缺口")
        
        # Sync from Created At -> Earliest Start
        # We stop at earliest_start because we assume data from there onwards exists
        sync_success = sync_data_range(
            sync_start,
            earliest_start.subtract(seconds=1),
            workspace_ids,
            force_reports_api=True,
            progress=progress,
            stats=stats,
        )
        
        if not sync_success:
            utils.log("历史回填因 API 限制或错误提前停止")
            
    else:
        utils.log("历史记录连续性检查完成，未发现明显缺口")

    sync_middle_gaps(workspace_ids, stats, progress=progress, state=state, lower_bound=sync_start)
    
    # After forward sync, perform reverse sync for entries created in Notion
    # Note: Reverse sync is relatively cheap (queries Notion for missing IDs)
    if current_sync_policy().allows("reverse_sync") and allow_reverse_sync():
        reverse_sync_notion_to_toggl()
    elif not allow_reverse_sync():
        utils.log("已关闭 Notion 到 Toggl 的反向同步")
    return stats

def main():
    sync_policy = current_sync_policy()
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
            sync_policy.write_report(status="success")


if __name__ == "__main__":
    main()
