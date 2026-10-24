"""Jellyfin Sessions API client. Every call is recorded in the log book."""
import re
import time

import requests


class JellyfinError(Exception):
    pass


def summarize_session(s):
    now = s.get("NowPlayingItem") or {}
    return {
        "Id": s.get("Id"),
        "Client": s.get("Client"),
        "DeviceName": s.get("DeviceName"),
        "DeviceId": s.get("DeviceId"),
        "Version": s.get("ApplicationVersion"),
        "UserName": s.get("UserName"),
        "SupportsRemoteControl": s.get("SupportsRemoteControl"),
        "SupportedCommands": s.get("SupportedCommands") or [],
        "LastActivityDate": s.get("LastActivityDate"),
        "NowPlaying": summarize_item(now) if now else None,
        "PlayState": s.get("PlayState") or {},
    }


def summarize_item(i):
    return {
        "Id": i.get("Id"),
        "Name": i.get("Name"),
        "Type": i.get("Type"),
        "Year": i.get("ProductionYear"),
        "SeriesName": i.get("SeriesName"),
        "Season": i.get("ParentIndexNumber"),
        "Episode": i.get("IndexNumber"),
        "RunTimeTicks": i.get("RunTimeTicks"),
        "ResumeTicks": (i.get("UserData") or {}).get("PlaybackPositionTicks"),
    }


_VERSION = re.compile(r"\bv?\d+(?:\.\d+)+\b")


def _norm(text):
    """Lowercase and drop version numbers, so "Jellyfin Roku 3.2.4" and "Jellyfin Roku" are the same client."""
    return re.sub(r"\s+", " ", _VERSION.sub("", (text or "").lower())).strip()


def _contains(wanted, actual):
    return (_norm(wanted) or wanted.lower()) in _norm(actual)


def match_session(rule, session):
    """rule: {"client": substr, "device": substr, "device_id": exact}; all given parts must match."""
    if not rule:
        return False
    if rule.get("client") and not _contains(rule["client"], session.get("Client")):
        return False
    if rule.get("device") and not _contains(rule["device"], session.get("DeviceName")):
        return False
    if rule.get("device_id") and rule["device_id"] != session.get("DeviceId"):
        return False
    return True


class JellyfinClient:
    def __init__(self, url, api_key, logbook, user_id="", timeout=10):
        self.url = url.rstrip("/")
        self.key = api_key
        self.logbook = logbook
        self.user_id = user_id
        self.timeout = timeout

    def _headers(self):
        return {
            "Accept": "application/json",
            "Authorization": (
                'MediaBrowser Client="WholeHouseRemote", Device="Web", '
                f'DeviceId="whole-house-remote-01", Version="1.0.0", Token="{self.key}"'
            ),
        }

    def request(self, method, path, quiet=False, **kw):
        """quiet: don't log (background polling); errors still raise."""
        t0 = time.perf_counter()
        desc = f"{method} {path}"
        if kw.get("params"):
            desc += f" params={kw['params']}"
        if kw.get("json") is not None:
            desc += f" json={kw['json']}"
        try:
            r = requests.request(method, f"{self.url}{path}", headers=self._headers(), timeout=self.timeout, **kw)
        except requests.RequestException as e:
            ms = (time.perf_counter() - t0) * 1000
            if not quiet:
                self.logbook.add("jellyfin", None, path.split("?")[0], desc, 0, False, ms, str(e))
            raise JellyfinError(f"Could not reach Jellyfin: {e}")
        ms = (time.perf_counter() - t0) * 1000
        if not quiet:
            self.logbook.add("jellyfin", None, path.split("?")[0], desc, r.status_code, r.ok, ms, r.text[:1500])
        return r

    def sessions(self, quiet=True):
        r = self.request("GET", "/Sessions", quiet=quiet)
        if not r.ok:
            raise JellyfinError(f"GET /Sessions returned HTTP {r.status_code}")
        return r.json()

    def lookup_user_id(self):
        if not self.user_id:
            users = self.request("GET", "/Users").json()
            admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), None)
            self.user_id = (admin or users[0])["Id"]
        return self.user_id

    def search(self, q):
        r = self.request("GET", "/Items", params={
            "userId": self.lookup_user_id(), "searchTerm": q, "recursive": "true",
            "includeItemTypes": "Movie,Series,Episode", "limit": 25, "fields": "ProductionYear"})
        return [summarize_item(i) for i in r.json().get("Items", [])]

    def episodes(self, series_id):
        r = self.request("GET", f"/Shows/{series_id}/Episodes", params={"userId": self.lookup_user_id()})
        return [summarize_item(i) for i in r.json().get("Items", [])]

    def item(self, item_id):
        r = self.request("GET", f"/Items/{item_id}", params={"userId": self.lookup_user_id()})
        return r.json() if r.ok else {}

    def play(self, sid, params):
        return self.request("POST", f"/Sessions/{sid}/Playing", params=params)

    def playstate(self, sid, command, params=None):
        return self.request("POST", f"/Sessions/{sid}/Playing/{command}", params=params or {})

    def command(self, sid, payload):
        return self.request("POST", f"/Sessions/{sid}/Command", json=payload)

    def viewing(self, sid, params):
        return self.request("POST", f"/Sessions/{sid}/Viewing", params=params)

    def message(self, sid, payload):
        return self.request("POST", f"/Sessions/{sid}/Message", json=payload)
