import os
import time
from urllib.parse import urlencode

from .notion_helper import NotionHelper
from .utils import log
from notionhub.internal_api import InternalApiError, NotionHubInternalClient


def normalize_optional_value(value):
    normalized = str(value or "").strip()
    if normalized in {"", "undefined", "[undefined]", "null", "[null]", "None"}:
        return None
    return normalized


def get_heatmap_base_url():
    return os.getenv("TOGGL_HEATMAP_BASE_URL", "https://togglapi.notionhub.app").rstrip("/")


def build_heatmap_url():
    if NotionHubInternalClient.is_available():
        try:
            return NotionHubInternalClient.from_env().public_heatmap_url(
                {"format": "html", "v": str(int(time.time()))}
            )
        except InternalApiError as error:
            if not error.fallback_allowed:
                raise
            log("内部热力图链接接口暂不可用，回退旧链接。")
    query = {"v": str(int(time.time()))}
    activation_code = normalize_optional_value(os.getenv("ACTIVATION_CODE"))
    user_id = normalize_optional_value(os.getenv("USER_ID"))
    if activation_code:
        query["activationCode"] = activation_code
    elif user_id:
        query["userId"] = user_id
    return f"{get_heatmap_base_url()}/toggl/heatmap?{urlencode(query)}"


def main():
    notion_helper = NotionHelper()
    url = build_heatmap_url()
    if not notion_helper.heatmap_block_id:
        log("跳过 Toggl 热力图更新: 未找到 heatmap block id")
        return
    notion_helper.update_heatmap(block_id=notion_helper.heatmap_block_id, url=url)
    log("更新 Toggl 热力图成功。")


if __name__ == "__main__":
    main()
