import logging
import os
import re

from notion_client import Client
from retrying import retry
from dotenv import load_dotenv
load_dotenv()
from .utils import (
    format_date,
    get_date,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_year,
    get_icon,
    get_relation,
    get_title,
    log,
)

TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"
BOOKMARK_ICON_URL = "https://www.notion.so/icons/bookmark_gray.svg"


class NotionHelper:

    database_id_dict = {}
    image_dict = {}
    def __init__(self):
        self.client = Client(auth=os.getenv("NOTION_TOKEN"), log_level=logging.ERROR)
        self.__cache = {}
        # self.page_id = self.extract_page_id(os.getenv("NOTION_PAGE"))
        # self.search_database(self.page_id)

        
        # Directly get IDs from environment variables using the names defined in database_name_dict
        # Assumption: The environment variables passed to the script match the VALUES in database_name_dict 
        # (e.g., if TIME_DATABASE_NAME="时间记录", we expect an env var "时间记录" containing the ID)
        # OR, more likely, the script expects explicit ID env vars if name lookup fails.
        # But based on the request "from environment variables", we'll trust os.getenv(name) works if set.
        
        self.time_database_id = os.getenv("TIME_DATABASE_NAME")
        self.day_database_id = os.getenv("DAY_DATABASE_ID")
        self.week_database_id = os.getenv("WEEK_DATABASE_ID")
        self.month_database_id = os.getenv("MONTH_DATABASE_ID")
        self.year_database_id = os.getenv("YEAR_DATABASE_ID")
        self.all_database_id = os.getenv("ALL_DATABASE_ID")
        self.client_database_id = os.getenv("CLIENT_DATABASE_ID")
        self.project_database_id = os.getenv("PROJECT_DATABASE_ID")
        self.tag_database_id = os.getenv("TAG_DATABASE_ID")
        
        # Heatmap Block ID from env
        self.heatmap_block_id = os.getenv("HEATMAP_BLOCK_ID")

        if self.time_database_id:
            self.write_database_id(self.time_database_id)

    def write_database_id(self, database_id):
        env_file = os.getenv('GITHUB_ENV')
        if env_file:
            # 将值写入环境文件
            with open(env_file, "a") as file:
                file.write(f"DATABASE_ID={database_id}\n")

    def extract_page_id(self, notion_url):
        # 正则表达式匹配 32 个字符的 Notion page_id
        match = re.search(
            r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            notion_url,
        )
        if match:
            return match.group(0)
        else:
            raise Exception(f"获取NotionID失败，请检查输入的Url是否正确")


    # def search_database(self, block_id):
    #     children = self.client.blocks.children.list(block_id=block_id)["results"]
    #     # 遍历子块
    #     for child in children:
    #         # 检查子块的类型

    #         if child["type"] == "child_database":
    #             self.database_id_dict[
    #                 child.get("child_database").get("title")
    #             ] = child.get("id")
    #         elif child["type"] == "embed" and child.get("embed").get("url"):
    #             if child.get("embed").get("url").startswith("https://heatmap.malinkang.com/"):
    #                 self.heatmap_block_id = child.get("id")
    #         # 如果子块有子块，递归调用函数
    #         if "has_children" in child and child["has_children"]:
    #             self.search_database(child["id"])
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_heatmap(self, block_id, url):
        # 更新 image block 的链接
        return self.client.blocks.update(block_id=block_id, embed={"url": url})


    def get_week_relation_id(self, date):
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            week, self.week_database_id, get_icon(TARGET_ICON_URL), properties
        )

    def get_month_relation_id(self, date):
        month = date.strftime("%Y年%-m月")
        start, end = get_first_and_last_day_of_month(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            month, self.month_database_id, get_icon(TARGET_ICON_URL), properties
        )

    def get_year_relation_id(self, date):
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            year, self.year_database_id, get_icon(TARGET_ICON_URL), properties
        )

    def get_day_relation_id(self, date):
        new_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day = new_date.strftime("%Y年%m月%d日")
        properties = {
            "日期": get_date(format_date(date)),
        }
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(new_date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(new_date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(new_date),
            ]
        )
        return self.get_relation_id(
            day, self.day_database_id, get_icon(TARGET_ICON_URL), properties
        )
    
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_relation_id(self, name, id, icon, properties={}, remote_id=None):
        fetch_key = f"{id}{remote_id if remote_id else name}"
        if fetch_key in self.__cache:
            return self.__cache.get(fetch_key)
        
        page_id = None
        results = []
        
        # 1. Try to find by remote_id if provided
        if remote_id:
            filter = {"property": "Id", "number": {"equals": int(remote_id)}}
            response = self.client.databases.query(database_id=id, filter=filter)
            results = response.get("results")
            if results:
                page_id = results[0].get("id")
                # Update name if changed
                existing_name = results[0].get("properties", {}).get("标题", {}).get("title", [])
                existing_name = existing_name[0].get("plain_text") if existing_name else ""
                if existing_name != name:
                    log(f"🔄 Updating name for ID {remote_id}: '{existing_name}' -> '{name}'")
                    properties["标题"] = get_title(name)
                    self.update_page(page_id, properties, icon)
        
        # 2. Fallback to name-based lookup if not found by ID or ID not provided
        if not page_id:
            filter = {"property": "标题", "title": {"equals": name}}
            try:
                response = self.client.databases.query(database_id=id, filter=filter)
                results = response.get("results")
            except Exception as e:
                log(f"Failed to query database {id} for name '{name}': {e}")
                raise e
            
            if results:
                page_id = results[0].get("id")
                if remote_id: # Link the ID if it was missing in Notion
                    properties["Id"] = {"number": int(remote_id)}
                    self.update_page(page_id, properties, icon)

        # 3. Create if still not found
        if not page_id:
            parent = {"database_id": id, "type": "database_id"}
            properties["标题"] = get_title(name)
            if remote_id:
                properties["Id"] = {"number": int(remote_id)}
            page_id = self.client.pages.create(
                parent=parent, properties=properties, icon=icon
            ).get("id")
            
        self.__cache[fetch_key] = page_id
        return page_id



    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_book_page(self, page_id, properties):
        return self.client.pages.update(page_id=page_id, properties=properties)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_page(self, page_id, properties, icon=None):
        kwargs = {"page_id": page_id, "properties": properties}
        if icon:
            kwargs["icon"] = icon
        return self.client.pages.update(**kwargs)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page(self, parent, properties, icon):
        return self.client.pages.create(parent=parent, properties=properties, icon=icon)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query(self, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v}
        return self.client.databases.query(**kwargs)

    def get_page_by_toggl_id(self, toggl_id):
        """Find the Notion page ID for a given Toggl ID."""
        filter = {"property": "Id", "number": {"equals": int(toggl_id)}}
        response = self.client.databases.query(
            database_id=self.time_database_id, filter=filter, page_size=1
        )
        results = response.get("results")
        return results[0].get("id") if results else None

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_block_children(self, id):
        response = self.client.blocks.children.list(id)
        return response.get("results")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks(self, block_id, children):
        return self.client.blocks.children.append(block_id=block_id, children=children)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks_after(self, block_id, children, after):
        return self.client.blocks.children.append(
            block_id=block_id, children=children, after=after
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def delete_block(self, block_id):
        return self.client.blocks.delete(block_id=block_id)


    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all_by_book(self, database_id, filter):
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                filter=filter,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all(self, database_id):
        """获取database中所有的数据"""
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    def get_date_relation(self, properties, date):
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(date),
            ]
        )
        properties["日"] = get_relation(
            [
                self.get_day_relation_id(date),
            ]
        )
        properties["全部"] = get_relation(
            [
                self.get_relation_id("全部",id=self.all_database_id,icon=get_icon(TARGET_ICON_URL)),
            ]
        )
