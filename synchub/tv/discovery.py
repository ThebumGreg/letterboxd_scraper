"""Find TVs on the local network, so they can be added from the UI instead of written into devices.json.

Three methods run side by side and their findings are merged by address:
  * SSDP        - Rokus answer a "roku:ecp" search (multicast)
  * mDNS        - Google TV / Android TV advertise _androidtvremote2._tcp (multicast, needs the `zeroconf` package)
  * range scan  - connect to the control ports of every address in a subnet (plain unicast)

Multicast does not cross every network boundary (WSL2, Docker and VMs on NAT drop it), so the range scan is what
finds TVs there; it is limited to private address ranges.
"""
import asyncio
import ipaddress
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

ROKU_PORT, ANDROID_PORT, ADB_PORT = 8060, 6466, 5555
MAX_HOSTS = 1024                        # a /22; larger ranges are refused
SSDP_GROUP = ("239.255.255.250", 1900)
MDNS_TYPE = "_androidtvremote2._tcp.local."
MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)
_scan_lock = threading.Lock()


class DiscoveryError(Exception):
    pass


def parse_ranges(values):
    """Validate user-supplied subnets. -> [IPv4Network]. Only private ranges, so this cannot be used to scan the internet."""
    nets = []
    for v in values:
        text = str(v).strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text if "/" in text else text + "/24", strict=False)
        except ValueError:
            raise DiscoveryError(f"{text!r} is not an address range such as 192.168.1.0/24")
        if net.version != 4 or not net.is_private:
            raise DiscoveryError(f"{net} is not a private IPv4 range; only home-network ranges can be scanned")
        if net.num_addresses > MAX_HOSTS:
            raise DiscoveryError(f"{net} is too large to scan; use /22 or smaller")
        nets.append(net)
    return nets


def suggest_ranges(hosts=()):
    """Ranges worth scanning: the /24 around each address we already know of, else the one this computer is on."""
    out = []
    for h in hosts:
        try:
            net = ipaddress.ip_network(f"{ipaddress.ip_address(h)}/24", strict=False)
        except ValueError:
            continue
        if net.is_private and str(net) not in out:
            out.append(str(net))
    if not out:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))        # no packet is sent; this only selects the outgoing interface
                net = ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False)
            if net.is_private:
                out.append(str(net))
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ SSDP (Roku)
def _ssdp(timeout, target=SSDP_GROUP):
    found = {}
    request = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
               "ST: roku:ecp\r\n\r\n").encode()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            for _ in range(2):
                s.sendto(request, target)
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    continue
                text = data.decode(errors="replace")
                if "roku" not in text.lower():
                    continue
                m = re.search(r"^location:\s*https?://([\d.]+)", text, re.I | re.M)
                found[m.group(1) if m else addr[0]] = {"type": "roku", "via": "SSDP"}
    except OSError:
        return found, "SSDP could not be sent from this computer"
    return found, None


# ------------------------------------------------------------------ mDNS (Google TV / Android TV)
def _mdns(timeout):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return {}, "mDNS skipped: install the zeroconf package to use it"
    found, names = {}, []

    class Listener:
        def add_service(self, zc, kind, name):
            names.append((kind, name))

        def update_service(self, zc, kind, name):
            pass

        def remove_service(self, zc, kind, name):
            pass

    try:
        zc = Zeroconf()
    except OSError as e:
        return {}, f"mDNS could not start: {e}"
    try:
        ServiceBrowser(zc, MDNS_TYPE, Listener())
        time.sleep(timeout)
        for kind, name in list(names):
            info = zc.get_service_info(kind, name, timeout=1500)
            if not info:
                continue
            label = name[: -len(kind) - 1] if name.endswith("." + kind) else name
            for addr in info.parsed_addresses():
                if ":" not in addr:
                    found[addr] = {"type": "googletv", "name": label, "via": "mDNS"}
                    break
    finally:
        zc.close()
    return found, None


# ------------------------------------------------------------------ range scan
def _open(host, port, timeout):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _sweep(nets, ports, timeout=0.6):
    """-> {host: {port, ...}} for every address in the ranges with one of the ports open."""
    hosts = [str(h) for net in nets for h in net.hosts()] if nets else []
    if not hosts:
        return {}
    hits = {}
    lock = threading.Lock()

    def check(pair):
        host, port = pair
        if _open(host, port, timeout):
            with lock:
                hits.setdefault(host, set()).add(port)

    with ThreadPoolExecutor(max_workers=96) as pool:
        list(pool.map(check, [(h, p) for h in hosts for p in ports]))
    return hits


def roku_info(host, timeout=2.5):
    """Name, model and MAC of a Roku (None if it is not one)."""
    try:
        r = requests.get(f"http://{host}:{ROKU_PORT}/query/device-info", timeout=timeout)
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        return None
    if root.tag != "device-info":
        return None
    mac = root.findtext("wifi-mac") or root.findtext("ethernet-mac") or ""
    return {"type": "roku",
            "name": root.findtext("user-device-name") or root.findtext("friendly-device-name") or root.findtext("model-name") or "Roku",
            "model": root.findtext("model-name"), "mac": mac.upper() if MAC_RE.match(mac) else None,
            "isTv": root.findtext("is-tv") == "true"}


def android_identity(host, cert_dir, timeout=4):
    """The name and MAC a Google TV reports on its pairing port (None when it will not say)."""
    try:
        from pathlib import Path

        from androidtvremote2 import AndroidTVRemote
    except ImportError:
        return None

    async def go():
        certs = Path(cert_dir)
        certs.mkdir(parents=True, exist_ok=True)
        atv = AndroidTVRemote("Whole-House Remote", str(certs / "client-cert.pem"), str(certs / "client-key.pem"), host)
        await atv.async_generate_cert_if_missing()
        return await asyncio.wait_for(atv.async_get_name_and_mac(), timeout)

    try:
        name, mac = asyncio.run(go())
    except Exception:  # any failure just means we show the address instead of a name
        return None
    return {"name": name, "mac": mac.upper() if mac and MAC_RE.match(mac) else None}


# ------------------------------------------------------------------ everything together
def discover(ranges=(), known=None, cert_dir="data/tv/certs", timeout=3.0, ssdp_target=SSDP_GROUP,
             ports=(ROKU_PORT, ANDROID_PORT, ADB_PORT)):
    """Run every method. -> {"found": [...], "notes": [...], "ranges": [...]}.

    known: {host: device id} of TVs already added, so they are marked rather than offered again."""
    if not _scan_lock.acquire(blocking=False):
        raise DiscoveryError("A scan is already running")
    try:
        nets = parse_ranges(ranges)
        notes, results, sweep_hits = [], {}, {}
        errors = []

        def run(fn, *args):
            try:
                return fn(*args)
            except Exception as e:     # one failing method must not hide what the others found
                errors.append(f"{fn.__name__.strip('_')} failed: {e}")
                return {}, None

        with ThreadPoolExecutor(max_workers=3) as pool:
            ssdp_f = pool.submit(run, _ssdp, timeout, ssdp_target)
            mdns_f = pool.submit(run, _mdns, timeout)
            sweep_f = pool.submit(_sweep, nets, ports) if nets else None
            (ssdp, ssdp_note), (mdns, mdns_note) = ssdp_f.result(), mdns_f.result()
            sweep_hits = sweep_f.result() if sweep_f else {}
        notes += [n for n in (ssdp_note, mdns_note) if n] + errors

        for host, info in list(ssdp.items()) + list(mdns.items()):
            results.setdefault(host, {"host": host, "via": []})
            results[host].update({k: v for k, v in info.items() if k != "via"})
            results[host]["via"].append(info["via"])
        for host, open_ports in sweep_hits.items():
            entry = results.setdefault(host, {"host": host, "via": []})
            if ROKU_PORT in open_ports and "type" not in entry:
                entry["type"] = "roku"
            elif ANDROID_PORT in open_ports and "type" not in entry:
                entry["type"] = "googletv"
            if "type" in entry and ("range scan" not in entry["via"]):
                entry["via"].append("range scan")
            entry["adbOpen"] = ADB_PORT in open_ports
        for host in [h for h, e in results.items() if "type" not in e]:
            results.pop(host)           # something answered on the ADB port only: not obviously a TV

        def enrich(entry):
            if entry["type"] == "roku":
                info = roku_info(entry["host"])
                if info is None:
                    return None
                entry.update({k: v for k, v in info.items() if v})
            else:
                info = android_identity(entry["host"], cert_dir)
                if info:
                    entry.update({k: v for k, v in info.items() if v and not entry.get(k)})
            entry.setdefault("name", ("Roku" if entry["type"] == "roku" else "Google TV") + " at " + entry["host"])
            return entry

        with ThreadPoolExecutor(max_workers=8) as pool:
            enriched = [e for e in pool.map(enrich, list(results.values())) if e]
        for e in enriched:
            e["added"] = (known or {}).get(e["host"])
        found = sorted(enriched, key=lambda e: (bool(e["added"]), e["name"].lower()))
        if not found:
            notes.append("No TVs were found. Check that they are switched on (a TV in deep standby does not answer), "
                         "that they are on this network, and scan the address range your TVs use.")
        if nets and not (ssdp or mdns) and not found:
            notes.append("Multicast discovery found nothing; this is normal inside WSL2, Docker or a VM, where only the range scan can work.")
        return {"found": found, "notes": notes, "ranges": [str(n) for n in nets]}
    finally:
        _scan_lock.release()
