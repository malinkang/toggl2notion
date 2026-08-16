import argparse

import pendulum

from notionhub.utils import get_property_value

from .notion_helper import NotionHelper
from . import utils


def get_page_time_start(page):
    value = get_property_value(page.get("properties", {}).get("时间", {}))
    if isinstance(value, dict):
        return value.get("start")
    return value


def repair_date_relations(apply=False, limit=None):
    notion_helper = NotionHelper()
    pages = notion_helper.query_time_entries_sorted_by_time(toggl_only=False)
    checked = 0
    updated = 0
    skipped = 0

    for page in pages:
        if limit is not None and checked >= limit:
            break
        checked += 1
        start_ts = get_page_time_start(page)
        if not start_ts:
            skipped += 1
            continue

        properties = {}
        notion_helper.get_date_relation(
            properties,
            pendulum.from_timestamp(start_ts, tz="Asia/Shanghai"),
        )
        if apply:
            notion_helper.update_page(page_id=page.get("id"), properties=properties)
        updated += 1

    mode = "apply" if apply else "dry-run"
    utils.log(f"日期关系修复完成 ({mode})：检查 {checked}，可更新 {updated}，跳过 {skipped}")
    return checked, updated, skipped


def main():
    parser = argparse.ArgumentParser(description="Repair Toggl date relations from Time entries.")
    parser.add_argument("--apply", action="store_true", help="write repaired relations to Notion")
    parser.add_argument("--limit", type=int, default=None, help="limit pages for smoke tests")
    args = parser.parse_args()
    repair_date_relations(apply=args.apply, limit=args.limit)


if __name__ == "__main__":
    main()
