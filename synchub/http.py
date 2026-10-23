"""One place for outgoing HTTP so every call can be (optionally) logged with secrets masked."""
import json
import logging
import sqlite3
import time
from urllib.parse import urlsplit

import requests

from synchub import db, settings

logger = logging.getLogger(__name__)
SLOW_SECONDS = 5

_SENSITIVE_HEADERS = {"x-api-key", "authorization", "api-key", "x-emby-token", "x-mediabrowser-token"}
_SENSITIVE_PARAMS = {"api_key", "apikey", "token"}
DEFAULT_TIMEOUT = 15


def _mask_headers(headers):
    return {k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v) for k, v in dict(headers or {}).items()}


def _mask_params(params):
    if not isinstance(params, dict):
        return params
    return {k: ("<redacted>" if k.lower() in _SENSITIVE_PARAMS else v) for k, v in params.items()}


def _log(direction, service, event, url, status, duration_ms, headers, payload, response):
    if settings.get("payload_logging_enabled") != "1" or settings.get(f"log_service_{service}") != "1":
        return
    limit = int(settings.get("payload_retention_limit") or 500)

    def dump(value):
        return value if isinstance(value, str) or value is None else json.dumps(value, default=str)

    try:
        # Best effort and short wait: a busy database must never make the real request fail.
        with db.connection(timeout=2) as conn:
            conn.execute(
                "INSERT INTO api_payload_log (direction, service, event_type, endpoint, status_code, duration_ms, headers_json, payload_json, response_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (direction, service, event, url, status, duration_ms, dump(_mask_headers(headers)), dump(payload), dump(response)),
            )
            conn.execute("DELETE FROM api_payload_log WHERE id NOT IN (SELECT id FROM api_payload_log ORDER BY id DESC LIMIT ?)", (limit,))
    except sqlite3.OperationalError as exc:
        logger.warning(f"Could not store the {service} {event} payload log entry: {exc}")


def request(service, event, method, url, **kwargs):
    """requests.request with a default timeout and optional payload logging."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    db.assert_not_in_write_transaction(f"HTTP {method} to {service} ({event})")
    started = time.time()
    payload = _mask_params(kwargs.get("params")) or kwargs.get("json")
    where = f"{service} {event} {method} {urlsplit(url).netloc}{urlsplit(url).path}"
    try:
        response = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        logger.warning(f"{where} failed after {time.time() - started:.1f}s: {exc.__class__.__name__}")
        _log("OUT", service, event, url, 0, int((time.time() - started) * 1000), kwargs.get("headers"), payload, str(exc))
        raise
    elapsed = time.time() - started
    (logger.warning if elapsed >= SLOW_SECONDS else logger.debug)(f"{where} -> HTTP {response.status_code} in {elapsed:.1f}s")
    try:
        body = response.json()
    except Exception:
        body = (response.text or "")[:2000]
    _log("OUT", service, event, url, response.status_code, int((time.time() - started) * 1000), kwargs.get("headers"), payload, body)
    return response
