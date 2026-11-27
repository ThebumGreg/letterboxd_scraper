"""Phone or browser push (Web Push, RFC 8030/8291/8292): the way an iPhone Home Screen app, an Android phone or a desktop browser is notified.

A device subscribes from the SyncHub page it is using and hands over three things: an address at its push service (Apple, Google or Mozilla), and two keys that
only that device can open. SyncHub signs each message with its own key pair (VAPID, made the first time it is needed) and posts it to that address.

A person types nothing here, so a bad address could otherwise make the server call anywhere. Only the push services' own host names are accepted.
Nothing shows an exception's text (the address of a device is private to it)."""
import base64
import json
import logging
import re
from urllib.parse import urlsplit

from synchub import db, settings
from synchub.notifications.providers import register
from synchub.notifications.providers.base import BAD_CHANNEL, BAD_SETUP, OK, REJECTED, RETRY, Provider, ProviderError, Result

logger = logging.getLogger(__name__)

# the hosts a subscription may point at: Apple (iPhone, iPad, Safari), Google (Chrome, Android, Edge), Mozilla (Firefox), Microsoft (older Edge)
ALLOWED_HOST_SUFFIXES = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com", "android.googleapis.com")
B64URL = re.compile(r"[A-Za-z0-9_-]{16,200}")
MAX_BODY = 600
NOT_SET_UP = "Set the Public HTTPS address in Settings first: it identifies SyncHub to your phone's push service."


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _host_ok(endpoint):
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.port and not parts.username and any(host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def public_url():
    return (settings.get("public_url") or "").strip().rstrip("/")


def vapid_keys():
    """(private, public) as the strings pywebpush and browsers want. Made once and kept with the other settings (the private half is masked everywhere)."""
    private, public = settings.get("webpush_vapid_private"), settings.get("webpush_vapid_public")
    if private and public:
        return private, public
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    v = Vapid02()
    v.generate_keys()
    private = _b64(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64(v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    settings.set_value("webpush_vapid_private", private)
    settings.set_value("webpush_vapid_public", public)
    return private, public


def _fit(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class WebPush(Provider):
    key = "webpush"
    label = "Phone or browser"
    fields = ()                         # nothing is typed: the page subscribes the device (see static/js/push.js)

    def validate(self, config):
        config = config or {}
        endpoint = str(config.get("endpoint") or "").strip()
        p256dh, auth = str(config.get("p256dh") or "").strip(), str(config.get("auth") or "").strip()
        if len(endpoint) > 1000 or not _host_ok(endpoint):
            raise ProviderError("That is not a push address from Apple, Google or Mozilla.")
        if not (B64URL.fullmatch(p256dh) and B64URL.fullmatch(auth)):
            raise ProviderError("The device's keys are not valid.")
        return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}

    def check(self, config):
        """Nothing to ask: a subscription can only be tried by sending to it, which Send test does."""
        try:
            self.validate(config)
        except ProviderError as exc:
            return Result(BAD_CHANNEL, str(exc))
        return Result(OK)

    def summary(self, config):
        host = urlsplit(str((config or {}).get("endpoint") or "")).hostname or ""
        return {"service": host}

    def default_label(self, config):
        return "This device"

    def send(self, config, message):
        base = public_url()
        if not base:
            return Result(BAD_SETUP, NOT_SET_UP)
        try:
            config = self.validate(config)
        except ProviderError as exc:
            return Result(BAD_CHANNEL, str(exc))
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            return Result(BAD_SETUP, "Push sending is not installed on this SyncHub (pywebpush).")
        db.assert_not_in_write_transaction("Web Push send")
        url = message.url or "/"
        url = url if url.startswith("https://") else base + (url if url.startswith("/") else "/" + url)
        payload = json.dumps({"title": _fit(message.title, 120) or "SyncHub", "body": _fit(message.body, MAX_BODY), "url": url}, ensure_ascii=False)
        private, _ = vapid_keys()
        try:
            response = webpush({"endpoint": config["endpoint"], "keys": {"p256dh": config["p256dh"], "auth": config["auth"]}}, data=payload,
                               vapid_private_key=private, vapid_claims={"sub": base}, ttl=int(message.ttl_seconds or 86400), timeout=15)
        except WebPushException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                return Result(BAD_CHANNEL, "That device is no longer subscribed (the app was removed or notifications were turned off). Add it again from the phone.")
            if code in (400, 401, 403):
                return Result(BAD_CHANNEL, f"The push service refused SyncHub's key (HTTP {code}). Remove this device and add it again.")
            if code == 413:
                return Result(REJECTED, "The message was too big for the push service.")
            if code is None or code == 429 or code >= 500:
                return Result(RETRY, "The push service did not take it" + (f" (HTTP {code})." if code else "."))
            return Result(REJECTED, f"The push service did not accept that (HTTP {code}).")
        except Exception as exc:
            logger.warning(f"Web Push send failed: {exc.__class__.__name__}")
            return Result(RETRY, "Could not reach the push service.")
        return Result(OK) if getattr(response, "status_code", 201) < 300 else Result(RETRY, f"The push service answered HTTP {response.status_code}.")


register(WebPush())
