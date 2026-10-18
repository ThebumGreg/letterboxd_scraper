import apprise
from database import get_setting
from logger import logger


def send_notification(title: str, body: str) -> bool:
    """Sends native iOS push notifications using Apprise or Pushover."""
    enabled = get_setting("enable_notifications", "0") == "1"
    apprise_urls = get_setting("apprise_urls", "").strip()

    if not enabled or not apprise_urls:
        return False

    apsobj = apprise.Apprise()

    for url in apprise_urls.split(","):
        clean_url = url.strip()
        if clean_url:
            apsobj.add(clean_url)

    try:
        return apsobj.notify(body=body, title=title)
    except Exception as e:
        logger.error(f"Notification dispatch failed: {e}")
        return False