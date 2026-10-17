import os
import re
from typing import List, Dict
from database import get_setting, save_disk_file_cache
from logger import logger

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts"}

def scan_local_disk_directory() -> List[Dict]:
    is_enabled = get_setting("enable_disk_scanner", "0") == "1"
    folder_path = get_setting("disk_scanner_path", "/media/movies")

    if not is_enabled or not os.path.exists(folder_path):
        return []

    logger.info(f"Scanning disk directory: {folder_path}")
    scanned_files = []

    try:
        for root, _, files in os.walk(folder_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    full_path = os.path.join(root, file)
                    clean_name = re.sub(r"[._]", " ", os.path.splitext(file)[0])
                    scanned_files.append({
                        "file_name": file,
                        "file_path": full_path,
                        "parsed_title": clean_name.strip(),
                        "file_size": os.path.getsize(full_path)
                    })
        save_disk_file_cache(scanned_files)
        logger.info(f"Disk scan completed. Indexed {len(scanned_files)} video files.")
        return scanned_files
    except Exception as e:
        logger.error(f"Error scanning disk directory: {e}")
        return []