"""UniFi Network: which devices are on the Wi-Fi right now (used by the TV dashboard to see that a phone left home).

The official Network Integration API (Network 9.x, key made under Settings, Control Plane, Integrations) is tried first. If the console does not have
it (HTTP 404), the older controller path is tried with the same key. The key is the Network application's own: a Protect key does not open it.
"""
import re

import requests

from synchub import http, settings

BASE = "/proxy/network"
PAGE = 200


class NetworkError(Exception):
    """Something the person can act on; the message is meant for them."""


def configured():
    return bool(settings.get("unifi_net_host").strip() and settings.get("unifi_net_api_key").strip())


def mac_key(mac):
    """aa:bb:cc... in lower case with colons, however it was typed; '' when it is not a MAC address."""
    digits = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)) if len(digits) == 12 else ""


def clean_client(raw):
    """One client in the shape the dashboard uses, from either API."""
    mac = mac_key(raw.get("macAddress") or raw.get("mac"))
    if not mac:
        return None
    kind = str(raw.get("type") or "").upper()
    wired = kind == "WIRED" or raw.get("is_wired") is True
    return {"mac": mac, "name": raw.get("name") or raw.get("hostname") or raw.get("displayName") or "", "ip": raw.get("ipAddress") or raw.get("ip") or "",
            "wired": wired, "since": raw.get("connectedAt") or raw.get("first_seen") or ""}


class NetworkClient:
    def __init__(self, host=None, api_key=None, verify=None, timeout=10):
        self.host = re.sub(r"^[a-z]+://", "", (host if host is not None else settings.get("unifi_net_host")).strip(), flags=re.I).strip("/")
        self.key = (api_key if api_key is not None else settings.get("unifi_net_api_key")).strip()
        if not self.host or not self.key:
            raise NetworkError("The UniFi Network address or API key is not set.")
        self.verify = (settings.get("unifi_net_verify_tls") == "1") if verify is None else bool(verify)
        self.timeout = timeout
        if not self.verify:
            from synchub.protect import _quiet_tls_warning
            _quiet_tls_warning()

    def _get(self, path, event, **params):
        try:
            res = http.request("unifi-network", event, "GET", f"https://{self.host}{BASE}{path}", headers={"X-API-KEY": self.key, "Accept": "application/json"},
                               params=params or None, verify=self.verify, timeout=self.timeout)
        except requests.exceptions.SSLError:
            raise NetworkError("The console's certificate is not trusted. Switch off Check the console's certificate in the UniFi Network settings.")
        except requests.RequestException as exc:
            raise NetworkError(f"Could not reach UniFi Network at {self.host}: {exc.__class__.__name__}")
        if res.status_code in (401, 403):
            raise NetworkError("UniFi Network rejected the API key. Make a key on the Network application's own Integrations page (a Protect key does not work here).")
        return res

    def sites(self):
        res = self._get("/integration/v1/sites", "sites")
        if res.status_code == 404:
            return None                     # no Integration API on this console
        if res.status_code >= 400:
            raise NetworkError(f"UniFi Network sites failed: HTTP {res.status_code}")
        body = res.json()
        return [s for s in (body.get("data", body) if isinstance(body, dict) else body) if isinstance(s, dict) and s.get("id")]

    def clients(self):
        """[{"mac", "name", "ip", "wired", "since"}], Wi-Fi first. Raises NetworkError when neither API answers."""
        found = self.sites()
        raw = []
        if found is None:
            res = self._get("/api/s/default/stat/sta", "clients (older API)")
            if res.status_code == 404:
                raise NetworkError("This console has no UniFi Network API at that address. Is Network 9 or newer installed, and is this the console's own address?")
            if res.status_code >= 400:
                raise NetworkError(f"UniFi Network clients failed: HTTP {res.status_code}")
            body = res.json()
            raw = body.get("data", []) if isinstance(body, dict) else body
        else:
            if not found:
                raise NetworkError("UniFi Network lists no sites for that key.")
            site = next((s for s in found if (s.get("internalReference") or s.get("name") or "").lower() == "default"), found[0])
            offset = 0
            while True:
                res = self._get(f"/integration/v1/sites/{site['id']}/clients", "clients", limit=PAGE, offset=offset)
                if res.status_code >= 400:
                    raise NetworkError(f"UniFi Network clients failed: HTTP {res.status_code}")
                body = res.json()
                chunk = body.get("data", []) if isinstance(body, dict) else body
                raw.extend(chunk)
                total = body.get("totalCount") if isinstance(body, dict) else None
                offset += PAGE
                if not chunk or total is None or offset >= total or offset > 5000:
                    break
        out = [c for c in (clean_client(r) for r in raw if isinstance(r, dict)) if c]
        return sorted(out, key=lambda c: (c["wired"], c["name"].lower() or "~", c["mac"]))

    def online_macs(self):
        return {c["mac"] for c in self.clients()}
