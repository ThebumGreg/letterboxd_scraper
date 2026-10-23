"""Webhooks from Radarr, Sonarr and Jellyfin: what an incoming event means, when the last one arrived, and creating the
webhook inside Radarr/Sonarr from the Settings page."""
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from synchub import db, settings

# Events that say nothing about downloads or library contents: acknowledged and nothing more.
IGNORED_EVENTS = {"Test", "Health", "HealthIssue", "HealthRestored", "ApplicationUpdate"}
# A grab only adds a queue entry, so a look at the queue is enough (the queue poll links it, or asks for a full sync when it cannot).
QUEUE_ONLY_EVENTS = {"Grab"}
# Every notification type Jellyfin's Webhook plugin can send (its NotificationType enum). All of them are kept (jellyfin_events.py);
# only these change what is in the library, so only these make SyncHub read it again. A sender that names no type is treated as one of them.
JELLYFIN_NOTIFICATION_TYPES = (
    "ItemAdded", "ItemDeleted", "Generic", "PlaybackStart", "PlaybackProgress", "PlaybackStop", "SubtitleDownloadFailure", "AuthenticationFailure",
    "AuthenticationSuccess", "SessionStart", "PendingRestart", "TaskCompleted", "PluginInstallationCancelled", "PluginInstallationFailed", "PluginInstalled",
    "PluginInstalling", "PluginUninstalled", "PluginUpdated", "UserCreated", "UserDeleted", "UserLockedOut", "UserPasswordChanged", "UserUpdated", "UserDataSaved",
)
JELLYFIN_LIBRARY_EVENTS = {"ItemAdded", "ItemDeleted", "TaskCompleted"}
JELLYFIN_PLUGIN_ID = "71552a5a-5c5c-4350-a2ae-ebe451a30173"
JELLYFIN_ITEM_FLAGS = ("EnableMovies", "EnableEpisodes", "EnableSeries", "EnableSeasons", "EnableAlbums", "EnableSongs", "EnableVideos")

# The notification settings SyncHub switches on when it creates the webhook (only those the service supports).
EVENT_FLAGS = {
    "radarr": ("onGrab", "onDownload", "onUpgrade", "onRename", "onMovieAdded", "onMovieDelete", "onMovieFileDelete", "onMovieFileDeleteForUpgrade"),
    "sonarr": ("onGrab", "onDownload", "onUpgrade", "onRename", "onSeriesAdd", "onSeriesDelete", "onEpisodeFileDelete", "onEpisodeFileDeleteForUpgrade", "onImportComplete"),
}


class WebhookSetupError(Exception):
    """The webhook could not be created; the message is shown to the user."""


def classify(service, event):
    """'ignore', 'queue' (look at the download queue) or 'sync' (read the library)."""
    if service == "jellyfin":
        return "sync" if event in JELLYFIN_LIBRARY_EVENTS or event not in JELLYFIN_NOTIFICATION_TYPES else "ignore"
    if event in IGNORED_EVENTS:
        return "ignore"
    return "queue" if event in QUEUE_ONLY_EVENTS else "sync"


# ---------------------------------------------------------------- last event received
def record(service, event):
    settings.set_value(f"webhook_last_{service}", json.dumps({"at": db.now(), "event": str(event)}))


def ago(at, now=None):
    try:
        then = datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"


def last_received(service, now=None):
    """{'event', 'at', 'ago'} for the most recent webhook from the service, or None if none has arrived."""
    try:
        got = json.loads(settings.get(f"webhook_last_{service}") or "null")
    except ValueError:
        return None
    if not isinstance(got, dict) or not got.get("at"):
        return None
    return {"event": got.get("event") or "", "at": got["at"], "ago": ago(got["at"], now)}


def status_for_all():
    return {service: last_received(service) for service in ("radarr", "sonarr", "jellyfin")}


# ---------------------------------------------------------------- creating the webhook in Radarr / Sonarr
def webhook_url(base, service):
    return f"{base.rstrip('/')}/api/webhook/{service}"


def check_base_url(value):
    """The SyncHub address as Radarr/Sonarr will use it. Returns it without a trailing slash or raises WebhookSetupError."""
    value = (value or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise WebhookSetupError("Enter the address Radarr and Sonarr use to reach SyncHub, for example http://192.168.1.50:5001")
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        raise WebhookSetupError("localhost only means 'this machine' to whoever reads it, so Radarr/Sonarr could not use it. Use this machine's network address or container name.")
    return value


def _field(body, name):
    return next((f for f in body.get("fields") or [] if f.get("name") == name), None)


def ensure_webhook(client, url):
    """Makes sure Radarr/Sonarr has a Webhook connection posting to `url`, built from the service's own template for it.

    Returns {'status': 'exists'|'created', ...}. Nothing is changed when a webhook for that URL already exists."""
    for existing in client.notifications():
        if existing.get("implementation") == "Webhook" and (_field(existing, "url") or {}).get("value") == url:
            return {"status": "exists", "id": existing.get("id"), "name": existing.get("name")}
    template = next((s for s in client.notification_schema() if s.get("implementation") == "Webhook"), None)
    if template is None:
        raise WebhookSetupError(f"{client.label} does not offer a Webhook connection.")
    body = {k: v for k, v in template.items() if k != "id"}
    body["name"] = "SyncHub"
    body["fields"] = [dict(f) for f in template.get("fields") or []]
    url_field = _field(body, "url")
    if url_field is None:
        raise WebhookSetupError(f"{client.label}'s Webhook connection has no URL field, so it cannot be filled in automatically.")
    url_field["value"] = url
    method = _field(body, "method")
    if method is not None:
        method["value"] = 1                                   # POST
    enabled = []
    for flag in EVENT_FLAGS[client.service]:
        if flag in body and body.get("supports" + flag[0].upper() + flag[1:]) is not False:
            body[flag] = True
            enabled.append(flag)
    created = client.add_notification(body)
    return {"status": "created", "id": created.get("id"), "events": enabled}


# ---------------------------------------------------------------- creating the webhook in Jellyfin's Webhook plugin
def jellyfin_option(url, existing=None):
    """The plugin destination SyncHub wants: every notification type, every item type, every property in the body (as JSON), for every user.
    Anything else already on an existing destination (a name, other headers) is kept."""
    option = dict(existing or {})
    headers = [h for h in option.get("Headers") or [] if str(h.get("Key") or "").lower() != "content-type"]
    option.update(WebhookName=option.get("WebhookName") or "SyncHub", WebhookUri=option.get("WebhookUri") or url, NotificationTypes=list(JELLYFIN_NOTIFICATION_TYPES), SendAllProperties=True,
                  EnableWebhook=True, SkipEmptyMessageBody=False, UserFilter=[], Fields=option.get("Fields") or [],
                  Headers=[*headers, {"Key": "Content-Type", "Value": "application/json"}])
    option.update({flag: True for flag in JELLYFIN_ITEM_FLAGS})
    return option


def ensure_jellyfin_webhook(client, url):
    """Makes sure Jellyfin's Webhook plugin has a Generic destination posting everything to `url`.

    Returns {'status': 'exists'|'created'|'updated', 'types': n}. Other destinations (Discord, Slack...) and the plugin's other settings are left alone."""
    plugin = next((p for p in client.plugins() if str(p.get("Name") or "").lower() == "webhook" or str(p.get("Id") or "").lower().replace("-", "") == JELLYFIN_PLUGIN_ID.replace("-", "")), None)
    if plugin is None:
        raise WebhookSetupError("Jellyfin does not have the Webhook plugin. Install it in Jellyfin (Dashboard, Plugins, Catalog, Webhook), restart Jellyfin, then try again.")
    if str(plugin.get("Status") or "Active") != "Active":
        raise WebhookSetupError(f"Jellyfin's Webhook plugin is {plugin.get('Status')}, not active. Restart Jellyfin if it was just installed, or enable it, then try again.")
    plugin_id = plugin.get("Id") or JELLYFIN_PLUGIN_ID
    configuration = client.plugin_configuration(plugin_id) or {}
    options = list(configuration.get("GenericOptions") or [])
    index = next((i for i, o in enumerate(options) if str(o.get("WebhookUri") or "").rstrip("/") == url.rstrip("/")), None)
    wanted = jellyfin_option(url, options[index] if index is not None else None)
    if index is not None and options[index] == wanted:
        return {"status": "exists", "types": len(JELLYFIN_NOTIFICATION_TYPES)}
    if index is None:
        options.append(wanted)
    else:
        options[index] = wanted
    configuration["GenericOptions"] = options
    client.set_plugin_configuration(plugin_id, configuration)
    return {"status": "updated" if index is not None else "created", "types": len(JELLYFIN_NOTIFICATION_TYPES)}
