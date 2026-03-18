import os

from notionhub.client import NotionHelperBase, TARGET_ICON_URL, TAG_ICON_URL, USER_ICON_URL, BOOKMARK_ICON_URL
from notionhub.utils import get_icon, get_relation, get_title, get_date, format_date
from notionhub.log import log


class NotionHelper(NotionHelperBase):
    database_id_dict = {}
    image_dict = {}

    def __init__(self):
        super().__init__()

        self.time_database_id = os.getenv("TIME_DATABASE_NAME")
        self.day_database_id = os.getenv("DAY_DATABASE_ID")
        self.week_database_id = os.getenv("WEEK_DATABASE_ID")
        self.month_database_id = os.getenv("MONTH_DATABASE_ID")
        self.year_database_id = os.getenv("YEAR_DATABASE_ID")
        self.all_database_id = os.getenv("ALL_DATABASE_ID")
        self.client_database_id = os.getenv("CLIENT_DATABASE_ID")
        self.project_database_id = os.getenv("PROJECT_DATABASE_ID")
        self.tag_database_id = os.getenv("TAG_DATABASE_ID")
        self.heatmap_block_id = os.getenv("HEATMAP_BLOCK_ID")

        if self.time_database_id:
            self.write_database_id(self.time_database_id)

    # --- Unique methods ---

    def write_database_id(self, database_id):
        env_file = os.getenv('GITHUB_ENV')
        if env_file:
            with open(env_file, "a") as file:
                file.write(f"DATABASE_ID={database_id}\n")

    def get_page_by_toggl_id(self, toggl_id):
        """Find the Notion page ID for a given Toggl ID."""
        filter = {"property": "Id", "number": {"equals": int(toggl_id)}}
        try:
            response = self.query(
                database_id=self.time_database_id, filter=filter, page_size=1
            )
            results = response.get("results")
            return results[0].get("id") if results else None
        except Exception as e:
            error_str = str(e).lower()
            if "id" in error_str and ("property" in error_str or "exists" in error_str):
                return None
            raise e

    def query_missing_toggl_id(self):
        """Query entries in Time database that are missing a Toggl ID."""
        filter = {"property": "Id", "number": {"is_empty": True}}
        try:
            return self.query_all_by_filter(database_id=self.time_database_id, filter=filter)
        except Exception as e:
            error_str = str(e).lower()
            if "id" in error_str and ("property" in error_str or "exists" in error_str):
                return []
            raise e

    def get_remote_id_from_page(self, page_id):
        """Retrieve the 'Id' (Toggl ID) from a Notion page (Project/Client)."""
        try:
            page = self.client.pages.retrieve(page_id=page_id)
            props = page.get("properties", {})
            id_prop = props.get("Id", {})
            return id_prop.get("number")
        except Exception:
            return None

    # Override get_relation_id to support remote_id param - KEEP THIS OVERRIDE
    def get_relation_id(self, name, id, icon, properties=None, remote_id=None):
        if properties is None:
            properties = {}
        fetch_key = f"{id}{remote_id if remote_id else name}"
        if fetch_key in self._NotionHelperBase__cache:
            return self._NotionHelperBase__cache.get(fetch_key)

        page_id = None
        results = []

        # 1. Try to find by remote_id if provided
        if remote_id:
            filter = {"property": "Id", "number": {"equals": int(remote_id)}}
            try:
                response = self.query(database_id=id, filter=filter)
                results = response.get("results")
                if results:
                    page_id = results[0].get("id")
                    existing_name = results[0].get("properties", {}).get("标题", {}).get("title", [])
                    existing_name = existing_name[0].get("plain_text") if existing_name else ""
                    if existing_name != name:
                        log(f"Updating name for ID {remote_id}: '{existing_name}' -> '{name}'")
                        properties["标题"] = get_title(name)
                        self.update_page(page_id, properties, icon)
            except Exception as e:
                error_str = str(e).lower()
                if "id" in error_str and ("property" in error_str or "exists" in error_str):
                    log(f"Property 'Id' missing in database {id}. Falling back to name-based lookup for '{name}'.")
                else:
                    log(f"Failed to query database {id} by remote_id: {e}")
                    raise e

        # 2. Fallback to name-based lookup if not found by ID or ID not provided
        if not page_id:
            filter = {"property": "标题", "title": {"equals": name}}
            try:
                response = self.query(database_id=id, filter=filter)
                results = response.get("results")
            except Exception as e:
                log(f"Failed to query database {id} for name '{name}': {e}")
                raise e

            if results:
                page_id = results[0].get("id")
                if remote_id:
                    try:
                        properties["Id"] = {"number": int(remote_id)}
                        self.update_page(page_id, properties, icon)
                    except Exception as e:
                        error_str = str(e).lower()
                        if "id" in error_str and ("property" in error_str or "exists" in error_str):
                            log(f"Could not write 'Id' to database {id}: Property missing.")
                        else:
                            log(f"Error writing 'Id' to database {id}: {e}")

        # 3. Create if still not found
        if not page_id:
            parent = {"database_id": id, "type": "database_id"}
            properties["标题"] = get_title(name)
            if remote_id:
                properties["Id"] = {"number": int(remote_id)}

            try:
                page_id = self.create_page(
                    parent=parent, properties=properties, icon=icon
                ).get("id")
            except Exception as e:
                error_str = str(e).lower()
                if "id" in error_str and ("property" in error_str or "exists" in error_str) and "Id" in properties:
                    log(f"Retrying page creation for '{name}' without 'Id' property...")
                    new_props = {k: v for k, v in properties.items() if k != "Id"}
                    page_id = self.create_page(
                        parent=parent, properties=new_props, icon=icon
                    ).get("id")
                else:
                    raise e

        self._NotionHelperBase__cache[fetch_key] = page_id
        return page_id

    # Override get_day_relation_id to include year/month/week in day properties
    def get_day_relation_id(self, date):
        new_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day = new_date.strftime("%Y年%m月%d日")
        properties = {
            "日期": get_date(format_date(date)),
        }
        properties["年"] = get_relation([self.get_year_relation_id(new_date)])
        properties["月"] = get_relation([self.get_month_relation_id(new_date)])
        properties["周"] = get_relation([self.get_week_relation_id(new_date)])
        return self.get_relation_id(
            day, self.day_database_id, get_icon(TARGET_ICON_URL), properties
        )

    # Override date relation methods to use get_icon(TARGET_ICON_URL) instead of date icon
    def get_week_relation_id(self, date):
        from notionhub.utils import get_first_and_last_day_of_week
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            week, self.week_database_id, get_icon(TARGET_ICON_URL), properties
        )

    def get_month_relation_id(self, date):
        from notionhub.utils import get_first_and_last_day_of_month
        month = date.strftime("%Y年%-m月")
        start, end = get_first_and_last_day_of_month(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            month, self.month_database_id, get_icon(TARGET_ICON_URL), properties
        )

    def get_year_relation_id(self, date):
        from notionhub.utils import get_first_and_last_day_of_year
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            year, self.year_database_id, get_icon(TARGET_ICON_URL), properties
        )

    # Override get_date_relation to include 全部
    def get_date_relation(self, properties, date, include_day=True):
        properties["年"] = get_relation([self.get_year_relation_id(date)])
        properties["月"] = get_relation([self.get_month_relation_id(date)])
        properties["周"] = get_relation([self.get_week_relation_id(date)])
        properties["日"] = get_relation([self.get_day_relation_id(date)])
        properties["全部"] = get_relation(
            [self.get_relation_id("全部", id=self.all_database_id, icon=get_icon(TARGET_ICON_URL))]
        )

    # Override update_page to support icon parameter
    def update_page(self, page_id, properties, icon=None, cover=None):
        kwargs = {"page_id": page_id, "properties": properties}
        if icon:
            kwargs["icon"] = icon
        try:
            return self.client.pages.update(**kwargs)
        except Exception as e:
            error_str = str(e).lower()
            if "id" in error_str and ("property" in error_str or "exists" in error_str) and "Id" in properties:
                log(f"Property 'Id' missing in database. Updating without 'Id'.")
                new_props = {k: v for k, v in properties.items() if k != "Id"}
                kwargs["properties"] = new_props
                return self.client.pages.update(**kwargs)
            raise e

    # Override create_page to handle Id property errors
    def create_page(self, parent, properties, icon=None, cover=None):
        parent = self.normalize_parent(parent)
        try:
            return self.client.pages.create(parent=parent, properties=properties, icon=icon)
        except Exception as e:
            error_str = str(e).lower()
            if "id" in error_str and ("property" in error_str or "exists" in error_str) and "Id" in properties:
                log(f"Property 'Id' missing in main database. Retrying without 'Id'.")
                new_props = {k: v for k, v in properties.items() if k != "Id"}
                return self.client.pages.create(parent=parent, properties=new_props, icon=icon)
            raise e

    def query_all_by_book(self, database_id, filter):
        return self.query_all_by_filter(database_id, filter)
