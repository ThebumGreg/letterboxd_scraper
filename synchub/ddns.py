"""Dynu dynamic DNS updater (ported from the original app; the update payload still needs a live check)."""
import datetime
import ipaddress
import json
import logging
import time

from synchub import db, http, settings

logger = logging.getLogger(__name__)
API_BASE = "https://api.dynu.com/v2"
IP_SOURCES = ("https://api.ipify.org", "https://ipv4.icanhazip.com")        # the second is only asked when the first fails
IP_TIMEOUT = (4, 6)                                                           # connect, read: a service that does not answer must not hold the update up
WATCH_AFTER_UPDATE = True                                                     # note how long each real IP change takes to reach the resolvers


def credentials():
    get = settings.get
    return {
        "domain": get("dynu_domain") or get("dynu_hostname"), "api_key": get("dynu_api_key"), "enabled": get("dynu_enabled", "0") == "1",
        "last_ip": get("ddns_last_ip"), "dns_id": get("dynu_dns_id"), "record_id": get("dynu_record_id") or "Root Domain",
        "created_on": get("dynu_created_on") or "N/A", "remote_updated_on": get("dynu_remote_updated_on") or "N/A",
        "state": get("dynu_domain_state") or "Unknown", "ttl": get("dynu_ttl") or "60",
    }


def status():
    return {"last_update_status": settings.get("ddns_last_status", "UNKNOWN"), "message": settings.get("ddns_last_message"),
            "last_run": settings.get("ddns_last_run"), "last_ip": settings.get("ddns_last_ip")}


def history(limit=10):
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT old_ip, new_ip, status, message, created_at FROM ddns_history ORDER BY id DESC LIMIT ?", (limit,))]


def _log(old_ip, new_ip, state, message):
    with db.connection() as conn:
        conn.execute("INSERT INTO ddns_history (old_ip, new_ip, status, message) VALUES (?, ?, ?, ?)", (old_ip or "N/A", new_ip or "N/A", state, message))


def _result(ok_state, message):
    settings.set_value("ddns_last_status", ok_state)
    settings.set_value("ddns_last_message", message)
    return ok_state == "SUCCESS", message


def fetch_metadata(domain, api_key):
    """Finds the domain's DNS ID on the Dynu account and caches its details. Returns the ID or None."""
    try:
        res = http.request("dynu", "GetDNS", "GET", f"{API_BASE}/dns", headers={"API-Key": api_key, "Accept": "application/json"}, timeout=10)
        res.raise_for_status()
        target = next((d for d in res.json().get("domains", []) if d.get("name", "").lower() == domain.lower()), None)
        if not target:
            return None
        dns_id = str(target.get("id"))
        for key, value in (("dynu_dns_id", dns_id), ("dynu_record_id", "Root Domain"), ("dynu_domain_state", target.get("state", "Complete")),
                           ("dynu_ttl", str(target.get("ttl", 60))), ("dynu_created_on", target.get("createdOn", "")), ("dynu_remote_updated_on", target.get("updatedOn", ""))):
            settings.set_value(key, value)
        if target.get("ipv4Address"):
            settings.set_value("ddns_last_ip", target["ipv4Address"])
        return dns_id
    except Exception as exc:
        logger.error(f"DDNS: could not read Dynu metadata: {exc}")
        return None


def _ipv4(text):
    """The text if it is an IPv4 address (an error page from a proxy must never be sent to Dynu as one), else None."""
    try:
        return str(ipaddress.IPv4Address((text or "").strip()))
    except ValueError:
        return None


def current_ip():
    for source in IP_SOURCES:
        try:
            res = http.request("dynu", "GetPublicIP", "GET", source, timeout=IP_TIMEOUT)
            res.raise_for_status()
            address = _ipv4(res.text)
            if address:
                return address
            logger.warning(f"DDNS: {source} did not answer with an IPv4 address.")
        except Exception as exc:
            logger.warning(f"DDNS: could not get the public IP from {source}: {exc}")
    logger.error("DDNS: could not get the public IP from any source.")
    return None


def read_record(dns_id, api_key):
    """What Dynu holds for the record (address, TTL, state, last change), or None when it cannot be read."""
    try:
        res = http.request("dynu", "GetRecord", "GET", f"{API_BASE}/dns/{dns_id}", headers={"API-Key": api_key, "Accept": "application/json"}, timeout=10)
        res.raise_for_status()
        return res.json()
    except Exception as exc:
        logger.warning(f"DDNS: could not read the Dynu record: {exc}")
        return None


def interval_minutes():
    """How often the public address is checked. Only a change (or a forced run) calls Dynu, so a short interval costs one small request."""
    try:
        minutes = int(float(settings.get("ddns_interval_minutes")))
    except (TypeError, ValueError):
        return 1
    return minutes if 1 <= minutes <= 60 else 1


def _took(steps):
    return f"Took {sum(steps.values()):.1f} s: " + ", ".join(f"{name} {seconds:.1f} s" for name, seconds in steps.items()) + "."


def _then_took(message, steps):
    """A failure message followed by the timing sentence, whatever the message ends with."""
    return f"{message.rstrip(' .')}. {_took(steps)}"


def _record_timeline(timeline):
    settings.set_value("ddns_last_timeline", json.dumps(timeline))


def last_timeline():
    try:
        return json.loads(settings.get("ddns_last_timeline") or "null")
    except ValueError:
        return None


def run_update(force=False):
    """Returns (ok, message). Skips quietly when disabled or when the IP has not changed (unless forced). The message says where the time went."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    settings.set_value("ddns_last_run", now)
    creds = credentials()
    if not creds["enabled"] and not force:
        return _result("SKIPPED", "DDNS update skipped: disabled in settings.")
    if not creds["domain"] or not creds["api_key"]:
        return _result("FAILED", "DDNS update skipped: domain or API key missing.")
    steps = {}

    def timed(name, fn, *args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            steps[name] = steps.get(name, 0) + time.monotonic() - started

    ip = timed("public IP", current_ip)
    if not ip:
        _log(creds["last_ip"], None, "FAILED", "Public IP check failed.")
        return _result("FAILED", f"DDNS update failed: public IP check failed. {_took(steps)}")
    dns_id = creds["dns_id"] or timed("Dynu record lookup", fetch_metadata, creds["domain"], creds["api_key"])
    if not dns_id:
        _log(creds["last_ip"], ip, "FAILED", "Could not resolve the DNS ID.")
        return _result("FAILED", f"DDNS update failed: could not resolve the DNS ID. {_took(steps)}")
    if ip == creds["last_ip"] and not force:
        return _result("SUCCESS", f"No IP change detected ({ip}). Update skipped. {_took(steps)}")
    headers = {"API-Key": creds["api_key"], "Content-Type": "application/json", "Accept": "application/json"}
    try:
        res = timed("Dynu update", http.request, "dynu", "UpdateIP", "POST", f"{API_BASE}/dns/{dns_id}", headers=headers, json={"name": creds["domain"], "ipv4Address": ip, "ipv4": True}, timeout=15)
    except Exception as exc:
        message = f"Error connecting to Dynu: {exc}"
        _log(creds["last_ip"], ip, "FAILED", _then_took(message, steps))
        return _result("FAILED", "DDNS " + _then_took(message, steps))
    if res.status_code == 200:
        message = f"Dynu DDNS successfully updated to {ip}. {_took(steps)}"
        settings.set_value("ddns_last_ip", ip)
        settings.set_value("dynu_remote_updated_on", now)
        _log(creds["last_ip"], ip, "SUCCESS", message)
        if WATCH_AFTER_UPDATE and ip != creds["last_ip"]:
            from synchub import ddns_check
            ddns_check.watch_in_background(creds["domain"], ip, _record_timeline)
        return _result("SUCCESS", message)
    message = f"Dynu update failed (HTTP {res.status_code}): {res.text[:200]}"
    _log(creds["last_ip"], ip, "FAILED", _then_took(message, steps))
    return _result("FAILED", _then_took(message, steps))


def save_settings(domain, api_key, enabled, interval=None):
    """Stores the DDNS form. A blank key keeps the saved one; a changed domain or key forgets the cached DNS IDs. Returns an error message or None."""
    if interval is not None and str(interval).strip():
        from settings_validation import validate_setting
        ok, value = validate_setting("ddns_interval_minutes", interval)
        if not ok:
            return value
        settings.set_value("ddns_interval_minutes", value)
    old_domain, old_key = settings.get("dynu_domain"), settings.get("dynu_api_key")
    api_key = (api_key or "").strip() or old_key
    settings.set_value("dynu_domain", (domain or "").strip())
    settings.set_value("dynu_api_key", api_key)
    settings.set_value("dynu_enabled", "1" if enabled else "0")
    if (domain or "").strip() != old_domain or api_key != old_key:
        settings.set_value("dynu_dns_id", "")
        settings.set_value("dynu_record_id", "")
        if domain and api_key:
            fetch_metadata(domain.strip(), api_key)
    return None
