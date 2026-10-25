"""The things SyncHub can tell someone about. An event type is an entry here: a code, who it is for, and how to turn what happened into a message."""
from dataclasses import dataclass
from typing import Callable

from synchub.notifications.providers.base import Message

USER, ADMIN = "user", "admin"


@dataclass(frozen=True)
class EventType:
    code: str
    label: str
    audience: str                       # USER: the people it is about. ADMIN: the profiles the administrator chose.
    render: Callable[[dict], Message]   # the payload recorded with the event -> the message
    expires_hours: int = 48             # undelivered after this long it is no longer news, and is dropped instead of sent


REGISTRY = {}


def register(event):
    REGISTRY[event.code] = event
    return event


def get(code):
    try:
        return REGISTRY[code]
    except KeyError:
        raise KeyError(f"There is no notification event called '{code}'.")


register(EventType(
    "test", "Test notification", USER,
    render=lambda p: Message("SyncHub test", p.get("text") or "This is a test notification from SyncHub. If you can read this, it works.", ttl_seconds=3600),
    expires_hours=1,
))
