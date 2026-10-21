"""One place for outgoing HTTP so every call can be (optionally) logged with secrets masked."""
import json
import time

import requests

from synchub import db, settings

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

    with db.connection() as conn:
        conn.execute(
            "INSERT INTO api_payload_log (direction, service, event_type, endpoint, status_code, duration_ms, headers_json, payload_json, response_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (direction, service, event, url, status, duration_ms, dump(_mask_headers(headers)), dump(payload), dump(response)),
        )
        conn.execute("DELETE FROM api_payload_log WHERE id NOT IN (SELECT id FROM api_payload_log ORDER BY id DESC LIMIT ?)", (limit,))


def request(service, event, method, url, **kwargs):
    """requests.request with a default timeout and optional payload logging."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    started = time.time()
    payload = _mask_params(kwargs.get("params")) or kwargs.get("json")
    try:
        response = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        _log("OUT", service, event, url, 0, int((time.time() - started) * 1000), kwargs.get("headers"), payload, str(exc))
        raise
    try:
        body = response.json()
    except Exception:
        body = (response.text or "")[:2000]
    _log("OUT", service, event, url, response.status_code, int((time.time() - started) * 1000), kwargs.get("headers"), payload, body)
    return response
