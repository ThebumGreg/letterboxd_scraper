"""Pushover (https://pushover.net/api; the rules are summarised in docs/DESIGN_NOTIFICATIONS.md).

The administrator registers one Pushover application and pastes its token in Settings; each person adds only their own user key, which this
checks and uses to list their devices. Nothing here retries a 4xx (Pushover says it can never work, and a run of them gets the server's address
blocked) and nothing shows an exception's text (`requests` quotes the address it was calling, and the limits call carries the token in it)."""
import json
import logging
import re
import threading
import time

import requests

from synchub import db, http, settings
from synchub.notifications.providers import register
from synchub.notifications.providers.base import BAD_CHANNEL, BAD_SETUP, OK, QUOTA, REJECTED, RETRY, ConfigField, Provider, ProviderError, Result

logger = logging.getLogger(__name__)

API = "https://api.pushover.net/1"
KEY_RE = re.compile(r"[A-Za-z0-9]{30}")             # application tokens, user keys and group keys alike
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,25}")
LIMITS = {"title": 250, "message": 1024, "url": 512, "url_title": 100}
EMERGENCY_RETRY, EMERGENCY_EXPIRE = 60, 1800        # priority 2 must say how often to repeat and for how long
NOT_SET_UP = "Pushover is not set up yet: the administrator has to enter the application token in Settings."
BAD_TOKEN_SHAPE = "A Pushover application token is 30 letters and digits. Copy it from the application's page on pushover.net."
MAX_WAIT = 7 * 86400
_CONNECTIONS = threading.BoundedSemaphore(2)         # Pushover asks for no more than two connections at once


def app_token():
    return settings.get("pushover_app_token").strip()


def _fit(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _clean(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _call(method, event, path, **kwargs):
    """(response, None), or (None, a RETRY Result) when nothing came back."""
    with _CONNECTIONS:
        try:
            return http.request("pushover", event, method, f"{API}/{path}", **kwargs), None
        except requests.RequestException as exc:
            return None, Result(RETRY, f"Could not reach Pushover ({exc.__class__.__name__}).")


def _json(response):
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wait_for_reset(response):
    reset = _int(response.headers.get("X-Limit-App-Reset"))
    return min(MAX_WAIT, max(300, reset - int(time.time()))) if reset else 3600


def _classify(response, secrets=()):
    """What an answer from Pushover means. 200 with status 1 is queued; any 4xx is final; 429 is the monthly allowance; 5xx is temporary."""
    body = _json(response)
    code = response.status_code
    if code == 200 and body.get("status") == 1:
        return Result(OK, ref=body.get("request"), data=body)
    detail = _clean("; ".join(str(e) for e in body.get("errors") or [])[:300] or f"Pushover did not accept that (HTTP {code}).", secrets)
    if code == 429:
        return Result(QUOTA, "Pushover's monthly message allowance is used up. It starts again on the 1st (Central Time).", retry_after=_wait_for_reset(response))
    if code >= 500:
        return Result(RETRY, f"Pushover had a problem (HTTP {code}).")
    if body.get("token"):
        return Result(BAD_SETUP, detail)
    if body.get("user") or body.get("device"):
        return Result(BAD_CHANNEL, detail)
    return Result(REJECTED, detail)


def quota():
    """The allowance as last seen: {'limit', 'remaining', 'reset' (Unix time), 'at'}, or None."""
    try:
        got = json.loads(settings.get("pushover_quota") or "null")
    except ValueError:
        return None
    return got if isinstance(got, dict) and got.get("limit") else None


def _remember_quota(limit, remaining, reset):
    limit, remaining, reset = _int(limit), _int(remaining), _int(reset)
    if limit is None or remaining is None:
        return
    try:
        settings.set_value("pushover_quota", json.dumps({"limit": limit, "remaining": remaining, "reset": reset, "at": db.now()}))
    except Exception as exc:                          # a note about the allowance must never fail a send
        logger.debug(f"Could not store the Pushover allowance: {exc.__class__.__name__}")


def _remember_quota_headers(response):
    h = response.headers
    _remember_quota(h.get("X-Limit-App-Limit"), h.get("X-Limit-App-Remaining"), h.get("X-Limit-App-Reset"))


def check_token(token, remember_quota=True):
    """Asks Pushover about an application token (and so how much of the month's allowance is left). A Result; data has limit, remaining, reset.
    `remember_quota=False` for a token that is only being tried (it may not be the one in use)."""
    token = (token or "").strip()
    if not token:
        return Result(BAD_SETUP, "Enter the Pushover application token.")
    if not KEY_RE.fullmatch(token):
        return Result(BAD_SETUP, BAD_TOKEN_SHAPE)
    response, failure = _call("GET", "limits", "apps/limits.json", params={"token": token}, timeout=10)
    if failure:
        return failure
    result = _classify(response, (token,))
    if result.ok:
        body = result.data
        if remember_quota:
            _remember_quota(body.get("limit"), body.get("remaining"), body.get("reset"))
        result.data = {"limit": _int(body.get("limit")), "remaining": _int(body.get("remaining")), "reset": _int(body.get("reset"))}
    return result


class Pushover(Provider):
    key = "pushover"
    label = "Pushover"
    fields = (
        ConfigField("user_key", "Pushover user key", secret=True, help="At the top of the Pushover app and on pushover.net: 30 letters and digits."),
        ConfigField("device", "Device", required=False, help="Leave blank to reach all your devices."),
    )

    def validate(self, config):
        key = str((config or {}).get("user_key") or "").strip()
        if not KEY_RE.fullmatch(key):
            raise ProviderError("A Pushover user key is 30 letters and digits. Copy it from the top of the Pushover app or from pushover.net.")
        devices = [d.strip() for d in str((config or {}).get("device") or "").split(",") if d.strip()]
        if not all(DEVICE_RE.fullmatch(d) for d in devices):
            raise ProviderError("A device name uses letters, digits, - and _ (up to 25 characters).")
        return {"user_key": key, "device": ",".join(devices)}

    def _ready(self, config):
        """(token, cleaned config, None), or (None, None, the Result that says why not)."""
        token = app_token()
        if not token:
            return None, None, Result(BAD_SETUP, NOT_SET_UP)
        if not KEY_RE.fullmatch(token):
            return None, None, Result(BAD_SETUP, BAD_TOKEN_SHAPE)
        try:
            return token, self.validate(config), None
        except ProviderError as exc:
            return None, None, Result(BAD_CHANNEL, str(exc))

    def check(self, config):
        token, config, failure = self._ready(config)
        if failure:
            return failure
        data = {"token": token, "user": config["user_key"], **({"device": config["device"]} if config["device"] else {})}
        response, failure = _call("POST", "validate", "users/validate.json", data=data, timeout=10)
        if failure:
            return failure
        _remember_quota_headers(response)
        result = _classify(response, (token, config["user_key"]))
        if result.outcome == REJECTED:
            result = Result(BAD_CHANNEL, result.detail)               # for a key being added, "refused" can only mean the key is no good
        if result.ok:
            body = result.data
            result.data = {"devices": [str(d) for d in body.get("devices") or []], "licenses": [str(x) for x in body.get("licenses") or []]}
        return result

    def send(self, config, message):
        token, config, failure = self._ready(config)
        if failure:
            return failure
        data = {"token": token, "user": config["user_key"], "message": _fit(message.body, LIMITS["message"]) or "(no text)"}
        if message.title:
            data["title"] = _fit(message.title, LIMITS["title"])
        if config["device"]:
            data["device"] = config["device"]
        if message.url:
            data["url"] = _fit(message.url, LIMITS["url"])
            if message.url_title:
                data["url_title"] = _fit(message.url_title, LIMITS["url_title"])
        if message.priority:
            data["priority"] = int(message.priority)
        if message.priority == 2:
            data.update(retry=EMERGENCY_RETRY, expire=EMERGENCY_EXPIRE)
        elif message.ttl_seconds:
            data["ttl"] = int(message.ttl_seconds)                   # Pushover ignores it for emergency messages
        if message.timestamp:
            data["timestamp"] = int(message.timestamp)
        response, failure = _call("POST", "send", "messages.json", data=data, timeout=15)
        if failure:
            return failure
        _remember_quota_headers(response)
        result = _classify(response, (token, config["user_key"]))
        if result.ok:
            result.data = {"receipt": result.data.get("receipt")} if result.data.get("receipt") else {}
        return result

    def choices(self, result):
        return {"device": result.data.get("devices", [])}

    def default_label(self, config):
        device = (config or {}).get("device")
        return f"{self.label} ({device})" if device else self.label


register(Pushover())
