# ddns_updater.py
import os
import json
import logging
import hashlib
import re
from datetime import datetime, timezone, timedelta
import requests
from database import get_connection, get_setting, set_setting

logger = logging.getLogger(__name__)

IP_RESOLVER_URL = "https://api.ipify.org?format=json"
DYNU_UPDATE_URL = "https://api.dynu.com/nic/update"

def get_dynu_credentials():
    hostname = get_setting("dynu_hostname", "") or os.getenv("DYNU_HOSTNAME", "")
    raw_password = get_setting("dynu_password", "") or os.getenv("DYNU_PASSWORD", "")
    enabled = get_setting("dynu_enabled", "1") == "1"

    if not raw_password:
        return hostname, "", enabled

    # Check if raw_password is already a 32-character hex string (MD5)
    if re.fullmatch(r"[a-fA-F0-9]{32}", raw_password):
        password_md5 = raw_password
    else:
        # Convert plaintext password to MD5 hash automatically
        password_md5 = hashlib.md5(raw_password.encode("utf-8")).hexdigest()

    return hostname, password_md5, enabled

def _get_public_ip():
    try:
        response = requests.get(IP_RESOLVER_URL, timeout=10)
        response.raise_for_status()
        return response.json().get("ip")
    except requests.RequestException as e:
        logger.error(f"DDNS: Failed to resolve public IP: {e}")
        return None

def run_ddns_update(force=False):
    """Background or manually triggered task to synchronize public IP with Dynu DDNS."""
    hostname, password_md5, enabled = get_dynu_credentials()

    if not enabled and not force:
        logger.debug("DDNS: Module is disabled in settings. Skipping update.")
        return False, "DDNS module is disabled."

    if not hostname or not password_md5:
        logger.debug("DDNS: Configuration missing (Hostname or Password). Skipping.")
        return False, "DDNS configuration missing (Hostname or Password)."

    current_ip = _get_public_ip()
    if not current_ip:
        return False, "Could not resolve public IP address."

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM ddns_cache ORDER BY id DESC LIMIT 1").fetchone()
        
        last_ip = row["last_known_ip"] if row else None
        last_sync = row["last_sync_timestamp"] if row else None
        
        # Check heartbeat threshold (14 days)
        heartbeat_reached = False
        if last_sync:
            try:
                last_sync_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_sync_dt > timedelta(days=14):
                    heartbeat_reached = True
            except ValueError:
                heartbeat_reached = True

        if current_ip == last_ip and not heartbeat_reached and not force:
            logger.debug(f"DDNS: IP ({current_ip}) unchanged and within 14-day window. Skipping API call.")
            return True, f"IP ({current_ip}) unchanged. Sync not required."

        logger.info(f"DDNS: Update triggered. Target IP: {current_ip} (Force={force})")

        params = {
            "hostname": hostname,
            "myip": current_ip,
            "password": password_md5
        }

        status = "ERROR"
        raw_payload = "{}"
        message = ""

        try:
            response = requests.get(DYNU_UPDATE_URL, params=params, timeout=15)
            raw_payload = response.text
            
            if response.status_code == 200 and ("good" in raw_payload or "nochg" in raw_payload):
                status = "SUCCESS"
                message = f"Successfully synchronized {hostname} to {current_ip}"
                logger.info(f"DDNS: {message}")
            else:
                status = "API_REJECTED"
                message = f"Dynu API rejected update: {raw_payload}"
                logger.warning(f"DDNS: {message}")
                
        except requests.RequestException as e:
            status = "NETWORK_ERROR"
            raw_payload = str(e)
            message = f"Network error during Dynu update: {e}"
            logger.error(f"DDNS: {message}")

        # Update Local Cache
        conn.execute("DELETE FROM ddns_cache")
        conn.execute("""
            INSERT INTO ddns_cache (last_known_ip, last_update_status, raw_dynu_json_payload, last_sync_timestamp)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (current_ip, status, json.dumps({"response": raw_payload})))

        return status == "SUCCESS", message