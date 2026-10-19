# ddns_updater.py
import datetime
import logging
import requests
from database import get_setting, set_setting, log_ddns_history

logger = logging.getLogger(__name__)

DYNU_API_BASE = "https://api.dynu.com/v2"

def get_dynu_credentials():
    """Retrieves full Dynu configuration and cached metadata."""
    return {
        "domain": get_setting("dynu_domain") or get_setting("dynu_hostname") or "",
        "api_key": get_setting("dynu_api_key") or "",
        "enabled": get_setting("dynu_enabled", "0") == "1",
        "last_ip": get_setting("ddns_last_ip") or "",
        "dns_id": get_setting("dynu_dns_id") or "",
        "record_id": get_setting("dynu_record_id") or "Root Domain",
        "created_on": get_setting("dynu_created_on") or "N/A",
        "remote_updated_on": get_setting("dynu_remote_updated_on") or "N/A",
        "state": get_setting("dynu_domain_state") or "Unknown",
        "ttl": get_setting("dynu_ttl") or "60"
    }

def fetch_and_cache_dynu_metadata(domain, api_key):
    """Queries Dynu v2 REST API to resolve DNS_ID, createdOn, and updatedOn."""
    headers = {"API-Key": api_key, "Accept": "application/json"}

    try:
        # Step 1: Query domain list from Dynu
        resp = requests.get(f"{DYNU_API_BASE}/dns", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        domains = data.get("domains", [])

        target_domain = next((d for d in domains if d.get("name", "").lower() == domain.lower()), None)
        if not target_domain:
            logger.error(f"DDNS Error: Domain '{domain}' not found in Dynu account.")
            return None

        dns_id = str(target_domain.get("id"))
        set_setting("dynu_dns_id", dns_id)
        set_setting("dynu_record_id", "Root Domain")
        set_setting("dynu_domain_state", target_domain.get("state", "Complete"))
        set_setting("dynu_ttl", str(target_domain.get("ttl", 60)))
        set_setting("dynu_created_on", target_domain.get("createdOn", ""))
        set_setting("dynu_remote_updated_on", target_domain.get("updatedOn", ""))
        
        # Cache existing IP if set on Dynu
        if target_domain.get("ipv4Address"):
            set_setting("ddns_last_ip", target_domain.get("ipv4Address"))

        return dns_id

    except Exception as e:
        logger.error(f"DDNS Error: Failed fetching Dynu metadata: {e}")
        return None

def get_current_ip():
    """Fetches public IP address via ipify."""
    try:
        response = requests.get("https://api.ipify.org", timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except Exception as e:
        logger.error(f"DDNS Error: Failed getting public IP: {e}")
        return None

def run_ddns_update(force=False):
    """Executes DDNS IP update against Dynu REST API v2 Domain endpoint."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_setting("ddns_last_run", now_str)

    creds = get_dynu_credentials()

    if not creds["enabled"] and not force:
        msg = "DDNS update skipped: Disabled in settings."
        set_setting("ddns_last_status", "SKIPPED")
        set_setting("ddns_last_message", msg)
        return False, msg

    domain = creds["domain"]
    api_key = creds["api_key"]

    if not domain or not api_key:
        msg = "DDNS update skipped: Domain or API Key missing."
        set_setting("ddns_last_status", "FAILED")
        set_setting("ddns_last_message", msg)
        return False, msg

    current_ip = get_current_ip()
    if not current_ip:
        msg = "DDNS update failed: Public IP check failed."
        set_setting("ddns_last_status", "FAILED")
        set_setting("ddns_last_message", msg)
        log_ddns_history(creds["last_ip"], "N/A", "FAILED", msg)
        return False, msg

    # Resolve or fetch DNS_ID if missing
    dns_id = creds["dns_id"]
    if not dns_id:
        dns_id = fetch_and_cache_dynu_metadata(domain, api_key)
        if not dns_id:
            msg = "DDNS update failed: Could not resolve DNS_ID from Dynu API."
            set_setting("ddns_last_status", "FAILED")
            set_setting("ddns_last_message", msg)
            log_ddns_history(creds["last_ip"], current_ip, "FAILED", msg)
            return False, msg

    # Skip if IP has not changed and force is False
    if current_ip == creds["last_ip"] and not force:
        msg = f"No IP change detected ({current_ip}). Update skipped."
        set_setting("ddns_last_status", "SUCCESS")
        set_setting("ddns_last_message", msg)
        return True, msg

    # Execute update directly via POST /v2/dns/{DNS_ID}
    headers = {
        "API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "name": domain,
        "ipv4Address": current_ip,
        "ipv4": True
    }

    url = f"{DYNU_API_BASE}/dns/{dns_id}"

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            msg = f"Dynu DDNS successfully updated to {current_ip}."
            set_setting("ddns_last_ip", current_ip)
            set_setting("ddns_last_status", "SUCCESS")
            set_setting("ddns_last_message", msg)
            set_setting("dynu_remote_updated_on", now_str)
            log_ddns_history(creds["last_ip"], current_ip, "SUCCESS", msg)
            return True, msg
        else:
            msg = f"Dynu update failed (HTTP {response.status_code}): {response.text}"
            set_setting("ddns_last_status", "FAILED")
            set_setting("ddns_last_message", msg)
            log_ddns_history(creds["last_ip"], current_ip, "FAILED", msg)
            return False, msg

    except Exception as e:
        msg = f"DDNS Error connecting to Dynu API: {e}"
        set_setting("ddns_last_status", "FAILED")
        set_setting("ddns_last_message", msg)
        log_ddns_history(creds["last_ip"], current_ip, "FAILED", msg)
        return False, msg

if __name__ == "__main__":
    run_ddns_update()