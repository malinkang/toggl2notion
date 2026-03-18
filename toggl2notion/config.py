from notionhub.utils import RICH_TEXT, URL, RELATION, NUMBER, DATE, FILES, STATUS, TITLE, SELECT, MULTI_SELECT
from notionhub.client import TAG_ICON_URL, USER_ICON_URL, BOOKMARK_ICON_URL

BOOK_ICON_URL = "https://www.notion.so/icons/book_gray.svg"

time_properties_type_dict = {
    "标题": TITLE,
    "时间": DATE,
    "Id": NUMBER,
    "备注": RICH_TEXT,
    "标签": RELATION,
    "Project": RELATION,
    "Client": RELATION,
}
