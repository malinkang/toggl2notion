import hashlib
import os
import re
import requests
import emoji
import pendulum
import time

from notionhub.utils import (
    get_title,
    get_rich_text,
    get_url,
    get_file,
    get_multi_select,
    get_relation,
    get_date,
    get_icon,
    get_select,
    get_number,
    get_heading,
    get_table_of_contents,
    get_embed,
    get_quote,
    get_rich_text_from_result,
    get_number_from_result,
    format_date,
    format_time,
    timestamp_to_date,
    str_to_timestamp,
    url_to_md5,
    download_image,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_year,
    get_properties,
    get_property_value,
    MAX_LENGTH,
    RICH_TEXT,
    URL,
    RELATION,
    NUMBER,
    DATE,
    FILES,
    STATUS,
    TITLE,
    SELECT,
    MULTI_SELECT,
)
from notionhub.log import log, timeit

# --- Script-specific functions ---


def split_emoji_from_string(s):
    # 检查第一个字符是否是emoji
    l = list(filter(lambda x: x.get("match_start") == 0, emoji.emoji_list(s)))
    if len(l) > 0:
        return l[0].get("emoji"), s[l[0].get("match_end"):]
    else:
        return '⏰', s


upload_url = "https://toggl.notionhub.app/upload-svg"


def upload_image(activation_code, file_path, upload_name=None):
    upload_name = upload_name or os.path.basename(file_path)
    with open(file_path, "rb") as file:
        files = {
            "svgFile": (upload_name, file, "image/svg+xml")
        }
        data = {
            "activationCode": activation_code
        }
        headers = {
            "Accept": "application/json"
        }
        response = requests.post(upload_url, files=files, data=data, headers=headers, timeout=30)
    if response.status_code == 200:
        log(f"File uploaded successfully. {response.text}")
        return response.json().get("svgUrl")
    else:
        log(f"Failed to upload file. Status code: {response.status_code}")
        return None


def upload_cover(url):
    cover_file = download_image(url)
    return upload_image("cover", cover_file)
