import argparse
import os
from .utils import get_embed, log, upload_image
from .notion_helper import NotionHelper

def get_file():
    # 设置文件夹路径
    folder_path = './OUT_FOLDER'

    # 检查文件夹是否存在
    if os.path.exists(folder_path) and os.path.isdir(folder_path):
        entries = os.listdir(folder_path)
        
        file_name = entries[0] if entries else None
        return file_name
    else:
        log("OUT_FOLDER does not exist.")
        return None
    
def main():
    notion_helper = NotionHelper()
    image_file = get_file()
    if image_file:
        activation_code = os.getenv("ACTIVATION_CODE")
        if not activation_code:
            activation_code = "default"
            
        svg_path = f"./OUT_FOLDER/{image_file}"
        # 使用 upload_image 上传
        image_url = upload_image(activation_code, svg_path)
        
        if image_url:
            heatmap_url = f"https://heatmap.malinkang.com/?image={image_url}"
            if notion_helper.heatmap_block_id:
                response = notion_helper.update_heatmap(
                    block_id=notion_helper.heatmap_block_id, url=heatmap_url
                )
                log(f"Synced heatmap to Notion. URL: {heatmap_url}")
            else:
                response = notion_helper.append_blocks(
                    block_id=notion_helper.page_id, children=[get_embed(heatmap_url)]
                )
                log(f"Appended heatmap to Notion page. URL: {heatmap_url}")
        else:
            log("Failed to upload heatmap image.")
    else:
        log("No heatmap file found.")

if __name__ == "__main__":
    main()