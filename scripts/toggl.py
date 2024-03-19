import os
from requests.auth import HTTPBasicAuth
import pendulum
from retrying import retry
import requests
from notion_helper import NotionHelper
import utils

from config import time_properties_type_dict, TAG_ICON_URL
from utils import get_icon, split_emoji_from_string


def get_time_entries():
    # 获取当前UTC时间
    now = pendulum.now("UTC")
    # toggl只支持90天的数据
    end = now.to_iso8601_string()
    start = now.subtract(days=90).start_of("day")
    # 格式化时间
    start = start.to_iso8601_string()
    params = {"start_date": start, "end_date": end}
    response = requests.get(
        "https://api.track.toggl.com/api/v9/me/time_entries", params=params, auth=auth
    )
    time_entries = response.json()
    return time_entries


def insert_to_notion():
    # 获取当前UTC时间
    now = pendulum.now("Asia/Shanghai")
    # toggl只支持90天的数据
    end = now.to_iso8601_string()
    start = now.subtract(days=90).start_of("day")
    # 格式化时间
    start = start.to_iso8601_string()
    sorts = [{"property": "时间", "direction": "descending"}]
    page_size = 1
    response = notion_helper.query(
        database_id=notion_helper.time_database_id, sorts=sorts, page_size=page_size
    )
    if len(response.get("results")) > 0:
        start = (
            response.get("results")[0]
            .get("properties")
            .get("时间")
            .get("date")
            .get("end")
        )
    params = {"start_date": start, "end_date": end}
    response = requests.get(
        "https://api.track.toggl.com/api/v9/me/time_entries", params=params, auth=auth
    )
    time_entries = response.json()
    time_entries.sort(key=lambda x: x["start"], reverse=False)

    for task in time_entries:
        if task.get("pid") is not None and task.get("stop") is not None:
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
            project_id = task.get("project_id")
            if project_id:
                workspace_id = task.get("workspace_id")
                start = pendulum.parse(task.get("start"))
                stop = pendulum.parse(task.get("stop"))
                start = start.in_timezone("Asia/Shanghai").int_timestamp
                stop = stop.in_timezone("Asia/Shanghai").int_timestamp
                item["时间"] = (start, stop)
                response = requests.get(
                    f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects/{project_id}",
                    auth=auth,
                )
                project = response.json().get("name")
                emoji, project = split_emoji_from_string(project)
                item["标题"] = project
                client_id = response.json().get("cid")
                #默认金币设置为1
                project_properties = {"金币":{"number": 1}}
                if client_id:
                    response = requests.get(
                        f"https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/clients/{client_id}",
                        auth=auth,
                    )
                    client = response.json().get("name")
                    client_emoji, client = split_emoji_from_string(client)
                    item["Client"] = [
                        notion_helper.get_relation_id(
                            client,
                            notion_helper.client_database_id,
                            {"type": "emoji", "emoji": client_emoji},
                        )
                    ]
                    project_properties["Client"] = {
                        "relation": [{"id": id} for id in item.get("Client")]
                    }
                item["Project"] = [
                    notion_helper.get_relation_id(
                        project,
                        notion_helper.project_database_id,
                        {"type": "emoji", "emoji": emoji},
                        properties=project_properties,
                    )
                ]
            if task.get("description") is not None:
                item["备注"] = task.get("description")
            properties = utils.get_properties(item, time_properties_type_dict)
            parent = {
                "database_id": notion_helper.time_database_id,
                "type": "database_id",
            }
            notion_helper.get_date_relation(
                properties, pendulum.from_timestamp(stop, tz="Asia/Shanghai")
            )
            icon = {"type": "emoji", "emoji": emoji}
            notion_helper.create_page(parent=parent, properties=properties, icon=icon)


if __name__ == "__main__":
    notion_helper = NotionHelper()
    auth = HTTPBasicAuth(f"{os.getenv('EMAIL').strip()}", f"{os.getenv('PASSWORD').strip()}")
    insert_to_notion()
