"""Push notifications through Apprise (URLs come from settings)."""
import logging
import re

from synchub import settings

logger = logging.getLogger(__name__)


def urls():
    return [u for u in re.split(r"[,\s]+", settings.get("apprise_urls")) if u]


def send(title, body, force=False):
    """True if at least one target accepted the message. `force` ignores the on/off switch (used by the test button)."""
    if not force and settings.get("enable_notifications") != "1":
        return False
    targets = urls()
    if not targets:
        return False
    import apprise
    notifier = apprise.Apprise()
    for url in targets:
        notifier.add(url)
    try:
        return bool(notifier.notify(title=title, body=body))
    except Exception as exc:
        logger.error(f"Notification failed: {exc}")
        return False
