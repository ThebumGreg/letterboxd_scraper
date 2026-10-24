"""Where the time goes between "the IP changed" and "the name points at it": the path from this machine to the services, and what Dynu's name
servers and public resolvers say about the name. Nothing here changes any record."""
import logging
import random
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PUBLIC_RESOLVERS = (("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"), ("Quad9", "9.9.9.9"))
SLOW_LOOKUP_SECONDS, SLOW_STEP_SECONDS = 1.0, 2.0
WATCH_SECONDS, WATCH_EVERY = 180, 3


# ---------------------------------------------------------------- a small DNS client (UDP, A and NS records)
class DnsError(Exception):
    pass


def _encode_name(name):
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("idna")
        if not raw or len(raw) > 63:
            raise DnsError(f"'{name}' is not a valid name")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _read_name(msg, pos):
    labels, jumped, end = [], False, pos
    for _ in range(128):
        if pos >= len(msg):
            raise DnsError("the answer was cut short")
        length = msg[pos]
        if length == 0:
            return ".".join(labels), (pos + 1 if not jumped else end)
        if length & 0xC0 == 0xC0:
            if not jumped:
                end = pos + 2
            pos, jumped = ((length & 0x3F) << 8) | msg[pos + 1], True
            continue
        labels.append(msg[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length
    raise DnsError("the answer loops")


def build_query(name, qtype, recurse=True, query_id=None):
    return struct.pack(">HHHHHH", query_id if query_id is not None else random.randrange(65536), 0x0100 if recurse else 0, 1, 0, 0, 0) + _encode_name(name) + struct.pack(">HH", qtype, 1)


def parse_response(msg, query_id=None):
    """{'rcode', 'answers': [{'type', 'ttl', 'data'}]} from a DNS message. A (1) gives the address, NS (2) and CNAME (5) the name."""
    if len(msg) < 12:
        raise DnsError("the answer was too short")
    got_id, flags, qd, an, _, _ = struct.unpack(">HHHHHH", msg[:12])
    if query_id is not None and got_id != query_id:
        raise DnsError("the answer was for a different question")
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(msg, pos)
        pos += 4
    answers = []
    for _ in range(an):
        _, pos = _read_name(msg, pos)
        if pos + 10 > len(msg):
            raise DnsError("the answer was cut short")
        rtype, _, ttl, rdlength = struct.unpack(">HHIH", msg[pos:pos + 10])
        pos += 10
        data = None
        if rtype == 1 and rdlength == 4:
            data = socket.inet_ntoa(msg[pos:pos + 4])
        elif rtype in (2, 5):
            data = _read_name(msg, pos)[0]
        answers.append({"type": rtype, "ttl": ttl, "data": data})
        pos += rdlength
    return {"rcode": flags & 0xF, "answers": answers}


def query(server, name, qtype=1, recurse=True, timeout=2.0, port=53):
    """Asks one DNS server. Returns {'ms', 'rcode', 'answers'} or {'ms', 'error'}; never raises."""
    query_id = random.randrange(65536)
    started = time.monotonic()
    try:
        packet = build_query(name, qtype, recurse, query_id)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, port))
            reply, _ = sock.recvfrom(4096)
        result = parse_response(reply, query_id)
    except socket.timeout:
        return {"ms": int((time.monotonic() - started) * 1000), "error": f"no answer within {timeout:g} s"}
    except (OSError, DnsError) as exc:
        return {"ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def _addresses(result):
    return [a["data"] for a in result.get("answers", []) if a["type"] == 1 and a["data"]]


def zone_nameservers(domain, resolver, limit=3, **kw):
    """The name servers that answer for `domain` (looked up through `resolver`), as [(name, address)]. Walks up from the name: a host's zone is usually its parent's."""
    labels = domain.strip(".").split(".")
    for start in range(len(labels) - 1):
        got = query(resolver, ".".join(labels[start:]), 2, **kw)
        names = [a["data"] for a in got.get("answers", []) if a["type"] == 2 and a["data"]]
        if names:
            servers = []
            for name in names[:limit]:
                addresses = _addresses(query(resolver, name, 1, **kw))
                if addresses:
                    servers.append((name, addresses[0]))
            return servers
    return []


# ---------------------------------------------------------------- what the servers say about the name
def system_answer(domain):
    started = time.monotonic()
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except OSError as exc:
        return {"ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
    return {"ms": int((time.monotonic() - started) * 1000), "ips": addresses, "ttl": None}


def snapshot(domain, expected_ip=None, resolvers=PUBLIC_RESOLVERS, **kw):
    """The name as seen from Dynu's own name servers, the big public resolvers and this machine.
    Each row: label, server, ips, ttl (what is left of the record's life in that cache), ms, current (shows `expected_ip`) or error."""
    rows = []

    def add(group, label, server, result):
        row = {"group": group, "label": label, "server": server, "ms": result.get("ms")}
        if "error" in result:
            row["error"] = result["error"]
        else:
            ips = result.get("ips") if "ips" in result else _addresses(result)
            ttls = [a["ttl"] for a in result.get("answers", []) if a["type"] == 1]
            row.update(ips=ips, ttl=(min(ttls) if ttls else result.get("ttl")), current=(expected_ip in ips) if expected_ip else None)
            if not ips:
                row["error"] = "no address in the answer"
        rows.append(row)

    servers = zone_nameservers(domain, resolvers[0][1], **kw) if resolvers else []
    for name, address in servers:
        add("authoritative", name, address, query(address, domain, 1, recurse=False, **kw))
    if not servers:
        rows.append({"group": "authoritative", "label": "Dynu's name servers", "server": None, "ms": None, "error": "could not find the name servers for this domain"})
    for label, address in resolvers:
        add("resolver", label, address, query(address, domain, 1, **kw))
    add("resolver", "This computer", None, system_answer(domain))
    return rows


# ---------------------------------------------------------------- the path from here to a service
def timed_request(url, headers=None, timeout=8.0):
    """One GET broken into DNS lookup, connect, TLS and first byte (seconds). Never raises: a failure is reported with the steps that did finish."""
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    steps = {"url": url}
    sock = None
    try:
        started = time.monotonic()
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        steps["dns"] = time.monotonic() - started
        v4 = [i for i in infos if i[0] == socket.AF_INET]
        steps["addresses"] = {"ipv4": len(v4), "ipv6": len(infos) - len(v4)}
        family, _, _, _, address = (v4 or infos)[0]
        started = time.monotonic()
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(address)
        steps["connect"] = time.monotonic() - started
        if secure:
            started = time.monotonic()
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
            steps["tls"] = time.monotonic() - started
        request = f"GET {parts.path or '/'}{'?' + parts.query if parts.query else ''} HTTP/1.1\r\nHost: {parts.netloc}\r\nUser-Agent: SyncHub-check\r\nConnection: close\r\n"
        request += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n"
        started = time.monotonic()
        sock.sendall(request.encode())
        first = sock.recv(256)
        steps["first_byte"] = time.monotonic() - started
        steps["status"] = int(first.split(b" ", 2)[1]) if first.startswith(b"HTTP/") else None
    except (OSError, ValueError, IndexError) as exc:
        steps["error"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    steps["total"] = sum(steps.get(k, 0) for k in ("dns", "connect", "tls", "first_byte"))
    return steps


# ---------------------------------------------------------------- plain-words conclusions
def findings(timings, rows, dynu=None, expected_ip=None):
    """[(level, text)] saying what the numbers mean. Levels: ok, note, problem."""
    out = []
    for t in timings:
        host = urlsplit(t["url"]).hostname
        if "error" in t:
            out.append(("problem", f"{host}: could not be reached from this machine ({t['error']})."))
            continue
        if t.get("dns", 0) >= SLOW_LOOKUP_SECONDS:
            out.append(("problem", f"Looking up {host} took {t['dns']:.1f} s on this machine, before any request is sent. This happens for every update and comes from the DNS resolver this machine uses (router, Docker, WSL, VPN), not from SyncHub or Dynu."))
        slow = [(name, t[key]) for key, name in (("connect", "connecting"), ("tls", "the secure handshake"), ("first_byte", "the first answer")) if t.get(key, 0) >= SLOW_STEP_SECONDS]
        for name, seconds in slow:
            out.append(("problem", f"{host}: {name} took {seconds:.1f} s. That is the network path or the service being slow."))
    authoritative = [r for r in rows if r["group"] == "authoritative" and "error" not in r]
    resolvers = [r for r in rows if r["group"] == "resolver" and "error" not in r]
    ttls = [r["ttl"] for r in authoritative if r.get("ttl")]
    if ttls:
        out.append(("note", f"The record's life (TTL) is {max(ttls)} s. After the IP changes, any resolver that already holds the old answer keeps serving it for up to that long, so waits of up to {max(ttls)} s (about {max(ttls) // 2} s on average) are normal DNS behaviour even when Dynu and SyncHub are instant."))
    if expected_ip and authoritative:
        behind = [r for r in authoritative if not r.get("current")]
        if behind:
            out.append(("problem", f"Dynu's own name server{'s' if len(behind) > 1 else ''} ({', '.join(r['label'] for r in behind)}) still answer{'' if len(behind) > 1 else 's'} with {', '.join(sorted({i for r in behind for i in r.get('ips', [])})) or 'no address'}, not {expected_ip}. The change has not reached Dynu's servers yet: that is on Dynu's side (or the update did not go through)."))
        elif any(not r.get("current") for r in resolvers):
            stale = [r for r in resolvers if not r.get("current")]
            left = [f"{r['label']} for up to {r['ttl']} s more" if r.get("ttl") else r["label"] for r in stale]
            out.append(("note", f"Dynu's name servers already answer {expected_ip}. {', '.join(left)} still hold{'s' if len(stale) == 1 else ''} the old address in cache. That is ordinary DNS caching; it clears by itself and no setting in SyncHub speeds it up (a lower TTL at Dynu shortens it)."))
        else:
            out.append(("ok", f"Dynu's name servers and every resolver asked answer {expected_ip}."))
    if dynu and dynu.get("ipv4Address") and expected_ip and dynu["ipv4Address"] != expected_ip:
        out.append(("problem", f"Dynu's record says {dynu['ipv4Address']} but this connection's public address is {expected_ip}: SyncHub's last update has not (yet) taken effect."))
    return out


# ---------------------------------------------------------------- watching one change spread
def watch(domain, new_ip, started_at, record, sleep=time.sleep, clock=time.monotonic, seconds=WATCH_SECONDS, every=WATCH_EVERY, snapshot_fn=None, **kw):
    """After an update, asks every server about the name every few seconds and notes when each first shows `new_ip` (seconds after the update was accepted).
    `record` is called with the finished timeline, once. Returns the timeline."""
    take = snapshot_fn or snapshot
    seen, last = {}, []
    while True:
        last = take(domain, new_ip, **kw)
        elapsed = clock() - started_at
        for row in last:
            key = (row["group"], row["label"])
            if row.get("current") and key not in seen:
                seen[key] = round(elapsed, 1)
        waiting = [r for r in last if "error" not in r and not r.get("current")]        # a server that cannot be asked is not waited for
        if not waiting or elapsed >= seconds:
            break
        sleep(every)
    timeline = {"ip": new_ip, "domain": domain, "watched_seconds": round(clock() - started_at, 1),
                "servers": [{"group": r["group"], "label": r["label"], "seconds": seen.get((r["group"], r["label"])), "error": r.get("error")} for r in last]}
    record(timeline)
    return timeline


def watch_in_background(domain, new_ip, record, **kw):
    thread = threading.Thread(target=lambda: _safe_watch(domain, new_ip, record, **kw), daemon=True, name="ddns-watch")
    thread.start()
    return thread


def _safe_watch(domain, new_ip, record, **kw):
    try:
        watch(domain, new_ip, time.monotonic(), record, **kw)
    except Exception as exc:
        logger.warning(f"DDNS: could not watch the change spread: {exc}")
