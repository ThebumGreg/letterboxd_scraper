"""The tab rows of the System pages that belong together.

One menu entry opens a group; the pages of a group keep their own addresses and their own permission, and a person only gets the tabs they may open
(like the Media page). `GROUPS` maps a group to (key, label, endpoint that opens it, what it holds)."""
GROUPS = {
    "status": ("Status", (
        ("overview", "Overview", "status_page", "How healthy SyncHub is, the library counts and how Jellyfin's films add up"),
        ("requests", "Requested titles", "system_requests_page", "What SyncHub asked Radarr or Sonarr for by itself"),
    )),
    "data": ("SyncHub data", (
        ("storage", "Data folder", "system_storage_page", "What SyncHub keeps in its data folder and how that has grown"),
        ("previews", "Preview clips", "system_previews_page", "Every preview, what was decided and how much room it takes"),
    )),
    "logs": ("Logs", (
        ("log", "Log", "logs_page", "The app log"),
        ("diagnostics", "Diagnostics", "activity_page", "What the app is doing right now: running tasks, database connections and threads"),
        ("traffic", "API traffic", "payloads_page", "The API requests SyncHub sent and the webhooks it received"),
        ("webhooks", "Jellyfin webhooks", "jellyfin_events_page", "The events Jellyfin's Webhook plugin sent, kept for later"),
    )),
    "remote": ("Remote setup", (
        ("apps", "Apps", "tv_settings_page", "Which apps show on the remote, and what happens when a title is resumed"),
        ("tvs", "TVs", "tv_setup_devices_page", "Add, scan for and edit your TVs"),
        ("players", "Players", "players_page", "The small PCs behind your TVs"),
        ("tools", "Tools", "tv_setup_advanced_page", "Testing tools: every key, wake and sleep method, Jellyfin sessions and the TV log"),
    )),
}


def group_of(endpoint):
    """The group a page belongs to, or None."""
    for name, (_label, tabs) in GROUPS.items():
        if any(t[2] == endpoint for t in tabs):
            return name
    return None
