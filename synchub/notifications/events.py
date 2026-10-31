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
    description: str = ""               # shown beside the on/off choice
    default_on: bool = True             # what a person gets until they choose
    configurable: bool = True           # the test message is not a choice


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
    expires_hours=1, configurable=False,
))


def _name(p, key="job"):
    return p.get("title") or p.get(key) or "a job"


register(EventType(
    "job_done", "A job finished", ADMIN,
    render=lambda p: Message("Job finished", f"{_name(p)} finished.", priority=-1, ttl_seconds=86400),
    description="A conversion or copy completed on a worker. Can be chatty while a queue works through many files.", default_on=False,
))
register(EventType(
    "job_failed", "A job failed", ADMIN,
    render=lambda p: Message("Job failed", f"{_name(p)} failed: {p.get('error') or 'no reason given'}", priority=0),
    description="A job ran out of attempts, or a worker lost it.",
))
register(EventType(
    "job_review", "A job needs your decision", ADMIN,
    render=lambda p: Message("Job needs a decision", f"{_name(p)} is waiting for you: {p.get('reason') or 'see the Library page'}", priority=0),
    description="A worker found something it cannot decide, such as a file that already exists.",
))
register(EventType(
    "worker_offline", "A worker went offline", ADMIN,
    render=lambda p: Message("Worker offline", f"{p.get('worker') or 'A worker'} has stopped checking in.", priority=1, ttl_seconds=86400),
    description="A worker stopped checking in. Urgent: it comes through quiet hours.",
))
register(EventType(
    "worker_online", "A worker is back", ADMIN,
    render=lambda p: Message("Worker back online", f"{p.get('worker') or 'A worker'} is checking in again.", priority=-1, ttl_seconds=86400),
    description="A worker that was offline is back.",
))
register(EventType(
    "worker_update_done", "A worker updated itself", ADMIN,
    render=lambda p: Message("Worker updated", f"{p.get('worker') or 'A worker'} now runs {p.get('version') or 'the latest code'}"
                             + (f" ({p['commit']})" if p.get("commit") else "") + (f", up from {p['previous']}" if p.get("previous") and p.get("previous") != p.get("version") else "") + ".",
                             priority=-1, ttl_seconds=86400),
    description="The automatic update on a worker installed the newest code and the worker is running it.",
))
register(EventType(
    "worker_update_failed", "A worker's automatic update failed", ADMIN,
    render=lambda p: Message("Worker update failed", f"{p.get('worker') or 'A worker'} could not update to {p.get('commit') or 'the newest code'}"
                             f"{'; it was put back to ' + p['restored'] if p.get('restored') else ''}. {p.get('error') or 'No reason given.'}"[:1000], priority=0),
    description="The automatic update on a worker failed. The old version keeps running. The update is not tried again until there is newer code.",
))
register(EventType(
    "share_problem", "A share is not mounted", ADMIN,
    render=lambda p: Message("Share problem", f"{p.get('worker') or 'A worker'} reports that '{p.get('root')}' is {p.get('state') or 'not ready'}.", priority=1, ttl_seconds=86400),
    description="A worker cannot reach one of its shares (not mounted, or its marker file is missing). Urgent.",
))
register(EventType(
    "share_recovered", "A share is ready again", ADMIN,
    render=lambda p: Message("Share ready again", f"{p.get('worker') or 'A worker'} can reach '{p.get('root')}' again.", priority=-1, ttl_seconds=86400),
    description="A share that had a problem works again.",
))
register(EventType(
    "tv_adb_unauthorized", "A TV needs ADB authorizing", ADMIN,
    render=lambda p: Message("TV needs authorizing", f"{p.get('tv') or 'A TV'} has not accepted SyncHub's ADB key. Tick 'Always allow' on the TV, or press Reset ADB key on the TVs page.", priority=0),
    description="Apps cannot be opened on a Google TV until you accept the prompt on it.",
))


register(EventType(
    "request_available", "A title I wanted is available", USER,
    render=lambda p: Message(f"{p.get('title') or 'A title'} is ready",
                             f"{p.get('title') or 'A title'}{' (' + str(p['year']) + ')' if p.get('year') else ''} is now in the library."
                             + (" The first episode has arrived." if p.get("kind") == "show" else ""), ttl_seconds=7 * 86400),
    description="A movie on your watchlist or that you requested reaches the library, or the first episode of a show you wanted does.", expires_hours=24 * 7,
))
register(EventType(
    "request_started", "A title I requested started downloading", USER,
    render=lambda p: Message(f"Downloading: {p.get('title') or 'a title'}", f"{p.get('title') or 'A title'}{' (' + str(p['year']) + ')' if p.get('year') else ''} was found and is downloading.",
                             priority=-1, ttl_seconds=2 * 86400),
    description="Radarr or Sonarr has found something for a title you asked for and started to download it. You are told again when it reaches the library.",
    expires_hours=48, default_on=False,
))
register(EventType(
    "request_failed", "A download for a title I requested needs attention", USER,
    render=lambda p: Message(f"Problem: {p.get('title') or 'a title'}", f"The download for {p.get('title') or 'a title'}{' (' + str(p['year']) + ')' if p.get('year') else ''} needs attention"
                             + (f": {p['reason']}" if p.get("reason") else "") + ". An admin can look in Radarr or Sonarr.", priority=0, ttl_seconds=2 * 86400),
    description="Radarr or Sonarr reports that the download of a title you asked for failed or cannot be imported without a person.",
    expires_hours=48, default_on=True,
))
def _episodes(p):
    """'S2E5', 'S2E5 and S2E6' or '3 new episodes' from the episodes in a payload."""
    eps = p.get("episodes") or []
    names = [f"S{e.get('season')}E{e.get('episode')}" for e in eps]
    if len(names) <= 2:
        return " and ".join(names) or "A new episode"
    return f"{len(names)} new episodes"


register(EventType(
    "new_episode", "A new episode of a show I follow is in the library", USER,
    render=lambda p: Message(f"New: {p.get('title') or 'a show'}",
                             f"{_episodes(p)} of {p.get('title') or 'a show'} {'is' if len(p.get('episodes') or []) <= 1 else 'are'} now in the library.", ttl_seconds=7 * 86400),
    description="A show on your watchlist, or one you requested, gets another episode in the library (the first episode is the 'title I wanted' notice). Choose below which shows count and whether you also want it when the episode airs.",
    expires_hours=24 * 7,
))
register(EventType(
    "episode_aired", "A new episode of a show I follow has aired", USER,
    render=lambda p: Message(f"Aired: {p.get('title') or 'a show'}", f"{_episodes(p)} of {p.get('title') or 'a show'} aired{' on ' + p['air_date'] if p.get('air_date') else ''}."
                             + (" It is not in the library yet." if not p.get("in_library") else ""), ttl_seconds=2 * 86400),
    description="An episode of a show you follow reaches its air date. Only sent if you choose 'when it airs' or 'both' under New episodes.",
    expires_hours=48, default_on=True,
))
register(EventType(
    "daily_picks", "Daily picks: what is new and trending", USER,
    render=lambda p: Message("Today's picks", p.get("text") or "Nothing new today.", priority=-1, ttl_seconds=86400),
    description="Each morning, a short list of what is trending and coming soon, from TMDB. Nothing is sent when TMDB is not set up.",
    expires_hours=24, default_on=False,
))
register(EventType(
    "digest", "Summary of several notices", USER,
    render=lambda p: Message(f"SyncHub: {p.get('count', 0)} updates", p.get("text") or "", ttl_seconds=86400),
    expires_hours=48, configurable=False,
))
