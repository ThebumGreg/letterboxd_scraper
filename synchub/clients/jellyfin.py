import logging

from synchub import http, settings
from synchub.clients import ApiError, check, require


logger = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, url=None, api_key=None):
        self.url, self.key = require(url or settings.get("jellyfin_url"), api_key or settings.get("jellyfin_api_key"), "Jellyfin")
        self._admin_id = None

    @property
    def headers(self):
        return {
            "Accept": "application/json",
            "Authorization": f'MediaBrowser Client="SyncHub", Device="SyncHub", DeviceId="synchub", Version="2.0", Token="{self.key}"',
        }

    def system_info(self):
        res = http.request("jellyfin", "TestConnection", "GET", f"{self.url}/System/Info", headers=self.headers, timeout=8)
        if res.status_code in (401, 403):
            raise ApiError("Server rejected the API key.")
        return check(res, "Jellyfin", "connection test").json()

    def users(self):
        res = check(http.request("jellyfin", "GetUsers", "GET", f"{self.url}/Users", headers=self.headers), "Jellyfin", "user list")
        return res.json()

    def refresh_library(self):
        """Asks Jellyfin to scan its libraries now, so files just copied into a library folder appear. Returns at once; Jellyfin scans in the background."""
        res = http.request("jellyfin", "RefreshLibrary", "POST", f"{self.url}/Library/Refresh", headers=self.headers, timeout=15)
        check(res, "Jellyfin", "library refresh")

    def plugins(self):
        return check(http.request("jellyfin", "GetPlugins", "GET", f"{self.url}/Plugins", headers=self.headers), "Jellyfin", "plugin list").json()

    def plugin_configuration(self, plugin_id):
        res = http.request("jellyfin", "GetPluginConfiguration", "GET", f"{self.url}/Plugins/{plugin_id}/Configuration", headers=self.headers)
        return check(res, "Jellyfin", "plugin settings").json()

    def set_plugin_configuration(self, plugin_id, configuration):
        res = http.request("jellyfin", "SetPluginConfiguration", "POST", f"{self.url}/Plugins/{plugin_id}/Configuration", headers=self.headers, json=configuration)
        check(res, "Jellyfin", "saving the plugin settings")

    def admin_user_id(self):
        """An administrator's ID. Querying as one avoids Jellyfin filtering items by user permissions."""
        if self._admin_id is None:
            try:
                users = self.users()
            except Exception:
                return None
            admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)
            self._admin_id = admin.get("Id") if admin else None
        return self._admin_id

    def iter_items(self, types, page_size=500):
        """Yields every item of the given Jellyfin types (Movie, Series, Season, Episode). Raises on any HTTP error."""
        start = 0
        user_id = self.admin_user_id()
        while True:
            params = {"IncludeItemTypes": ",".join(types), "Recursive": "true", "Fields": "ProviderIds,ParentId,DateCreated",
                      "StartIndex": start, "Limit": page_size}
            if user_id:
                params["UserId"] = user_id
            res = check(http.request("jellyfin", "SyncLibrary", "GET", f"{self.url}/Items", headers=self.headers, params=params, timeout=60), "Jellyfin", "library sync")
            body = res.json()
            items = body.get("Items") or []
            logger.info(f"Jellyfin: fetched {min(start + len(items), body.get('TotalRecordCount') or start + len(items))} of {body.get('TotalRecordCount', '?')} {'/'.join(types)} item(s)")
            yield from items
            start += page_size
            total = body.get("TotalRecordCount")
            if not items or len(items) < page_size or (total is not None and start >= total):
                break

    def iter_user_data(self, user_id, filters, page_size=500):
        """Yields every movie and episode this Jellyfin user has played (`filters` "IsPlayed") or is part-way through ("IsResumable"), each with
        their own data (UserData: position, played, play count, last played), most recently played first. Raises on any HTTP error."""
        start = 0
        while True:
            params = {"UserId": user_id, "IncludeItemTypes": "Movie,Episode", "Recursive": "true", "Filters": filters, "SortBy": "DatePlayed",
                      "SortOrder": "Descending", "EnableUserData": "true", "StartIndex": start, "Limit": page_size}
            res = check(http.request("jellyfin", "WatchHistory", "GET", f"{self.url}/Items", headers=self.headers, params=params, timeout=60), "Jellyfin", "watch history")
            body = res.json()
            items = body.get("Items") or []
            yield from items
            start += page_size
            total = body.get("TotalRecordCount")
            if not items or len(items) < page_size or (total is not None and start >= total):
                break

    def user_item(self, user_id, item_id):
        """One item with this user's data, or None when Jellyfin has no such item."""
        res = http.request("jellyfin", "WatchItem", "GET", f"{self.url}/Items", headers=self.headers,
                           params={"UserId": user_id, "Ids": item_id, "EnableUserData": "true"}, timeout=15)
        items = check(res, "Jellyfin", "item lookup").json().get("Items") or []
        return items[0] if items else None
