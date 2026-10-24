"""The Dynamic DNS delay tools: a small DNS client, the path timings, the plain-words findings, the watcher, and the instrumented updater."""
import http.server
import json
import socket
import struct
import threading

import pytest

from synchub import ddns, ddns_check as dc
from synchub import scheduler, settings


# ---------------------------------------------------------------- a fake DNS server on loopback addresses
def _name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".")) + b"\0"


def _decode_question(packet):
    labels, pos = [], 12
    while packet[pos]:
        labels.append(packet[pos + 1:pos + 1 + packet[pos]].decode())
        pos += 1 + packet[pos]
    qtype = struct.unpack(">H", packet[pos + 1:pos + 3])[0]
    return ".".join(labels), qtype, packet[:12], packet[12:pos + 5], struct.unpack(">H", packet[2:4])[0]


def answer_packet(packet, records, rcode=0):
    """A reply to `packet`: `records` is [(type, ttl, data)]; names in the answers use a compression pointer back to the question."""
    name, qtype, header, question, _ = _decode_question(packet)
    out = struct.pack(">H", struct.unpack(">H", header[:2])[0]) + struct.pack(">H", 0x8180 | rcode) + struct.pack(">HHHH", 1, len(records), 0, 0) + question
    for rtype, ttl, data in records:
        rdata = socket.inet_aton(data) if rtype == 1 else _name(data)
        out += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return out


class FakeDns:
    """Answers from a table {(name, qtype): [(rtype, ttl, data)]}. Also notes whether each question asked for recursion."""
    def __init__(self, host, port, table, silent=False):
        self.table, self.silent, self.asked = table, silent, []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while self.running:
            try:
                packet, peer = self.sock.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            name, qtype, _, _, flags = _decode_question(packet)
            self.asked.append((name, qtype, bool(flags & 0x0100)))
            if not self.silent:
                self.sock.sendto(answer_packet(packet, self.table.get((name, qtype), [])), peer)

    def close(self):
        self.running = False
        self.thread.join(1)
        self.sock.close()


@pytest.fixture
def dns():
    """A resolver (127.0.0.1) and Dynu-like name servers (127.0.0.2, 127.0.0.3) on one port. Returns (port, servers by address)."""
    first = FakeDns("127.0.0.1", 0, {})
    port, servers = first.port, {"127.0.0.1": first}
    for address in ("127.0.0.2", "127.0.0.3", "127.0.0.4"):
        servers[address] = FakeDns(address, port, {})
    resolver_table = {("home.dynu.net", 2): [], ("dynu.net", 2): [(2, 3600, "ns1.dynu.test"), (2, 3600, "ns2.dynu.test")],
                      ("ns1.dynu.test", 1): [(1, 3600, "127.0.0.2")], ("ns2.dynu.test", 1): [(1, 3600, "127.0.0.3")]}
    servers["127.0.0.1"].table = resolver_table
    yield port, servers
    for server in servers.values():
        server.close()


def point(servers, domain, ip, authoritative_ttl=90, resolver_ip=None, resolver_ttl=40):
    for address in ("127.0.0.2", "127.0.0.3"):
        servers[address].table = {(domain, 1): [(1, authoritative_ttl, ip)]}
    servers["127.0.0.1"].table[(domain, 1)] = [(1, resolver_ttl, resolver_ip or ip)]
    servers["127.0.0.4"].table = {(domain, 1): [(1, resolver_ttl, resolver_ip or ip)]}


# ---------------------------------------------------------------- the DNS client
def test_a_query_is_encoded_and_a_reply_with_compressed_names_is_read(dns):
    port, servers = dns
    servers["127.0.0.1"].table[("a.example.test", 1)] = [(1, 77, "10.1.2.3"), (1, 60, "10.1.2.4")]
    got = dc.query("127.0.0.1", "a.example.test", 1, timeout=1, port=port)
    assert got["rcode"] == 0 and [(a["ttl"], a["data"]) for a in got["answers"]] == [(77, "10.1.2.3"), (60, "10.1.2.4")] and got["ms"] >= 0
    assert servers["127.0.0.1"].asked[-1] == ("a.example.test", 1, True)                                   # asked to look it up for us
    dc.query("127.0.0.1", "a.example.test", 1, recurse=False, timeout=1, port=port)
    assert servers["127.0.0.1"].asked[-1][2] is False                                                      # the name servers are asked for what they hold themselves


def test_ns_answers_name_the_servers_through_compression(dns):
    port, servers = dns
    got = dc.query("127.0.0.1", "dynu.net", 2, timeout=1, port=port)
    assert [a["data"] for a in got["answers"]] == ["ns1.dynu.test", "ns2.dynu.test"]


def test_a_silent_server_and_a_wrong_reply_are_errors_not_exceptions(dns):
    port, servers = dns
    servers["127.0.0.4"].silent = True
    quiet = dc.query("127.0.0.4", "x.test", 1, timeout=0.3, port=port)
    assert "no answer within 0.3 s" in quiet["error"] and "answers" not in quiet
    assert "error" in dc.query("127.0.0.1", "bad..name", 1, timeout=0.3, port=port)                       # an invalid name is refused before sending
    assert dc.query("999.999.999.999", "x.test", 1, timeout=0.3)["error"]


@pytest.mark.parametrize("message, text", [(b"short", "too short"), (struct.pack(">HHHHHH", 5, 0x8180, 0, 1, 0, 0) + b"\xc0\x0c", "loops|cut short"),
                                           (struct.pack(">HHHHHH", 5, 0x8180, 0, 1, 0, 0) + b"\x03abc", "cut short")])
def test_malformed_replies_are_refused_clearly(message, text):
    with pytest.raises(dc.DnsError, match=text):
        dc.parse_response(message)


def test_a_reply_to_another_question_is_refused():
    packet = dc.build_query("x.test", 1, query_id=7)
    reply = answer_packet(packet, [(1, 5, "1.2.3.4")])
    assert dc.parse_response(reply, 7)["answers"][0]["data"] == "1.2.3.4"
    with pytest.raises(dc.DnsError, match="different question"):
        dc.parse_response(reply, 8)


def test_name_servers_are_found_by_walking_up_from_the_host_name(dns):
    port, servers = dns
    found = dc.zone_nameservers("home.dynu.net", "127.0.0.1", timeout=1, port=port)
    assert found == [("ns1.dynu.test", "127.0.0.2"), ("ns2.dynu.test", "127.0.0.3")]
    assert [q[:2] for q in servers["127.0.0.1"].asked if q[1] == 2] == [("home.dynu.net", 2), ("dynu.net", 2)]      # the host itself has none: its parent does
    assert dc.zone_nameservers("nothing.invalid", "127.0.0.1", timeout=0.5, port=port) == []
    assert len(dc.zone_nameservers("home.dynu.net", "127.0.0.1", limit=1, timeout=1, port=port)) == 1


# ---------------------------------------------------------------- what the servers say
RESOLVERS = (("Fake A", "127.0.0.1"), ("Fake B", "127.0.0.4"))


def test_a_snapshot_asks_dynus_servers_the_resolvers_and_this_computer(dns, monkeypatch):
    port, servers = dns
    point(servers, "home.dynu.net", "5.6.7.8")
    monkeypatch.setattr(dc, "system_answer", lambda domain: {"ms": 3, "ips": ["5.6.7.8"], "ttl": None})
    rows = dc.snapshot("home.dynu.net", "5.6.7.8", resolvers=RESOLVERS, timeout=1, port=port)
    assert [(r["group"], r["label"]) for r in rows] == [("authoritative", "ns1.dynu.test"), ("authoritative", "ns2.dynu.test"), ("resolver", "Fake A"), ("resolver", "Fake B"), ("resolver", "This computer")]
    assert all(r["current"] and r["ips"] == ["5.6.7.8"] for r in rows)
    assert [r["ttl"] for r in rows] == [90, 90, 40, 40, None]


def test_a_resolver_that_still_serves_the_old_address_is_marked_not_current(dns, monkeypatch):
    port, servers = dns
    point(servers, "home.dynu.net", "5.6.7.8", resolver_ip="1.1.1.9", resolver_ttl=33)
    monkeypatch.setattr(dc, "system_answer", lambda domain: {"ms": 1, "error": "Name or service not known"})
    rows = {r["label"]: r for r in dc.snapshot("home.dynu.net", "5.6.7.8", resolvers=RESOLVERS, timeout=1, port=port)}
    assert rows["ns1.dynu.test"]["current"] is True and rows["Fake A"]["current"] is False and rows["Fake A"]["ips"] == ["1.1.1.9"] and rows["Fake A"]["ttl"] == 33
    assert "Name or service not known" in rows["This computer"]["error"]


def test_a_snapshot_survives_a_domain_with_no_name_servers_and_silent_resolvers(dns, monkeypatch):
    port, servers = dns
    monkeypatch.setattr(dc, "system_answer", lambda domain: {"ms": 1, "ips": [], "ttl": None})
    rows = dc.snapshot("nothing.invalid", "1.2.3.4", resolvers=RESOLVERS, timeout=0.3, port=port)
    assert rows[0]["group"] == "authoritative" and "could not find the name servers" in rows[0]["error"]
    assert all("error" in r for r in rows)                                                                  # nothing to compare, nothing claimed


def test_this_computers_answer_is_timed(monkeypatch):
    got = dc.system_answer("localhost")
    assert got["ips"] == ["127.0.0.1"] and got["ms"] >= 0
    assert "error" in dc.system_answer("no-such-host.invalid")


# ---------------------------------------------------------------- the path to a service
class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers)))
        self.send_response(401 if "denied" in self.path else 200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture
def web():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()


def test_a_request_is_timed_in_steps_and_sends_the_headers(web):
    base, server = web
    got = dc.timed_request(base + "/v2/dns?x=1", headers={"API-Key": "K"})
    assert got["status"] == 200 and set(got) >= {"dns", "connect", "first_byte", "total", "addresses"} and "tls" not in got and "error" not in got
    assert got["total"] == pytest.approx(got["dns"] + got["connect"] + got["first_byte"]) and got["addresses"]["ipv4"] == 1
    path, headers = server.seen[0]
    assert path == "/v2/dns?x=1" and headers["API-Key"] == "K" and headers["User-Agent"] == "SyncHub-check"
    assert dc.timed_request(base + "/denied")["status"] == 401                                              # a refusal is still a measurement


def test_an_unreachable_service_reports_the_steps_that_did_finish():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    got = dc.timed_request(f"http://127.0.0.1:{port}/")
    assert "ConnectionRefusedError" in got["error"] and "dns" in got and "connect" not in got
    assert "error" in dc.timed_request("http://no-such-host.invalid/")


# ---------------------------------------------------------------- what it means, in words
def timing(host="api.dynu.com", **kw):
    return {"url": f"https://{host}/v2/dns", "dns": 0.02, "connect": 0.03, "tls": 0.1, "first_byte": 0.2, "total": 0.35, **kw}


def row(group, label, ips, ttl=90, current=True):
    return {"group": group, "label": label, "server": "x", "ms": 5, "ips": ips, "ttl": ttl, "current": current}


def said(items, level):
    return [t for lv, t in items if lv == level]


def test_a_slow_name_lookup_is_put_on_this_machines_resolver():
    out = dc.findings([timing(dns=4.9), timing("api.ipify.org")], [], None, None)
    [problem] = said(out, "problem")
    assert "api.dynu.com took 4.9 s" in problem and "before any request is sent" in problem and "not from SyncHub or Dynu" in problem


def test_slow_connect_handshake_and_answer_and_unreachable_services_are_each_named():
    out = said(dc.findings([timing(connect=2.5, tls=3.0, first_byte=9.0), timing("api.ipify.org", error="TimeoutError: timed out")], [], None, None), "problem")
    assert any("connecting took 2.5 s" in t for t in out) and any("secure handshake took 3.0 s" in t for t in out) and any("first answer took 9.0 s" in t for t in out)
    assert any("api.ipify.org: could not be reached" in t and "timed out" in t for t in out)
    assert dc.findings([timing()], [], None, None) == []                                                     # fast: nothing to say


def test_when_dynus_servers_are_behind_it_is_dynus_side():
    rows = [row("authoritative", "ns1", ["1.1.1.1"], current=False), row("authoritative", "ns2", ["2.2.2.2"]), row("resolver", "Google", ["2.2.2.2"])]
    out = dc.findings([], rows, None, "2.2.2.2")
    [problem] = said(out, "problem")
    assert "ns1" in problem and "1.1.1.1" in problem and "not 2.2.2.2" in problem and "on Dynu's side" in problem and "ns2" not in problem


def test_when_only_resolvers_are_behind_it_is_ordinary_caching_with_the_time_left():
    rows = [row("authoritative", "ns1", ["2.2.2.2"], ttl=90), row("resolver", "Google", ["1.1.1.1"], ttl=61, current=False), row("resolver", "Cloudflare", ["2.2.2.2"], ttl=12)]
    notes = said(dc.findings([], rows, None, "2.2.2.2"), "note")
    assert any("TTL) is 90 s" in t and "about 45 s on average" in t for t in notes)
    caching = next(t for t in notes if "cache" in t and "Google" in t)
    assert "for up to 61 s more" in caching and "Cloudflare" not in caching and "ordinary DNS caching" in caching


def test_when_everything_agrees_it_says_so_and_a_stale_dynu_record_is_flagged():
    rows = [row("authoritative", "ns1", ["2.2.2.2"]), row("resolver", "Google", ["2.2.2.2"])]
    assert any("every resolver asked answer 2.2.2.2" in t for t in said(dc.findings([], rows, None, "2.2.2.2"), "ok"))
    stale = said(dc.findings([], rows, {"ipv4Address": "3.3.3.3"}, "2.2.2.2"), "problem")
    assert len(stale) == 1 and "3.3.3.3" in stale[0] and "2.2.2.2" in stale[0] and "has not (yet) taken effect" in stale[0]
    assert dc.findings([], [{"group": "authoritative", "label": "Dynu's name servers", "error": "could not find"}], None, "2.2.2.2") == []


# ---------------------------------------------------------------- watching one change spread
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_the_watcher_notes_when_each_server_first_showed_the_new_address():
    clock, calls, recorded = Clock(), [], []

    def take(domain, ip):
        calls.append(clock.now)
        step = len(calls)
        return [{"group": "authoritative", "label": "ns1", "current": True, "ips": [ip]},
                {"group": "resolver", "label": "Google", "current": step >= 3, "ips": [ip]},
                {"group": "resolver", "label": "This computer", "current": step >= 4, "ips": [ip]},
                {"group": "resolver", "label": "Broken", "error": "no answer", "current": None}]
    timeline = dc.watch("home.dynu.net", "2.2.2.2", 100.0, recorded.append, sleep=clock.sleep, clock=clock, every=3, snapshot_fn=take)
    assert len(calls) == 4                                                                                  # stopped once everyone who could answer had
    assert {s["label"]: s["seconds"] for s in timeline["servers"]} == {"ns1": 0.0, "Google": 6.0, "This computer": 9.0, "Broken": None}
    assert timeline["servers"][-1]["error"] == "no answer" and timeline["ip"] == "2.2.2.2" and timeline["domain"] == "home.dynu.net" and timeline["watched_seconds"] == 9.0
    assert recorded == [timeline]


def test_the_watcher_gives_up_after_its_time_and_says_who_never_showed_it():
    clock, recorded = Clock(), []
    never = lambda domain, ip: [{"group": "resolver", "label": "Stuck", "current": False, "ips": ["1.1.1.1"]}]
    timeline = dc.watch("d", "2.2.2.2", 100.0, recorded.append, sleep=clock.sleep, clock=clock, seconds=30, every=10, snapshot_fn=never)
    assert timeline["servers"] == [{"group": "resolver", "label": "Stuck", "seconds": None, "error": None}] and timeline["watched_seconds"] == 30.0 and len(recorded) == 1


def test_a_watcher_that_breaks_is_logged_not_raised(caplog):
    dc._safe_watch("d", "1.1.1.1", lambda t: None, snapshot_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")), sleep=lambda s: None)
    assert "could not watch the change spread: boom" in caplog.text


# ---------------------------------------------------------------- the updater says where its time went
class Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code, self.text, self._body = status, text, body

    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_http(monkeypatch, handler):
    calls = []

    def request(service, event, method, url, **kw):
        calls.append((event, url, kw.get("timeout")))
        return handler(event, url)
    monkeypatch.setattr(ddns.http, "request", request)
    return calls


def configure(**extra):
    for k, v in {"dynu_domain": "home.dynu.net", "dynu_api_key": "KEY", "dynu_enabled": "1", "dynu_dns_id": "42", "ddns_last_ip": "1.1.1.1", **extra}.items():
        settings.set_value(k, v)


def test_every_update_reports_how_long_each_step_took(v2, monkeypatch):
    configure()
    fake_http(monkeypatch, lambda event, url: Resp(text="2.2.2.2") if event == "GetPublicIP" else Resp())
    ok, message = ddns.run_update()
    assert ok and message.startswith("Dynu DDNS successfully updated to 2.2.2.2. Took ") and "public IP " in message and "Dynu update " in message and message.endswith(" s.")
    assert ddns.history()[0]["message"] == message                                                           # the history shows it too
    configure(ddns_last_ip="2.2.2.2")
    assert "No IP change detected (2.2.2.2). Update skipped. Took " in ddns.run_update()[1]


def test_the_public_address_is_asked_quickly_and_a_second_source_is_used_when_the_first_fails(v2, monkeypatch):
    configure()
    seen = {"https://api.ipify.org": Resp(status=502), "https://ipv4.icanhazip.com": Resp(text="9.9.9.9\n")}
    calls = fake_http(monkeypatch, lambda event, url: seen[url] if event == "GetPublicIP" else Resp())
    assert ddns.current_ip() == "9.9.9.9"
    assert [c[1] for c in calls] == ["https://api.ipify.org", "https://ipv4.icanhazip.com"] and all(c[2] == (4, 6) for c in calls)      # connect 4 s, read 6 s
    seen["https://api.ipify.org"] = Resp(text="8.8.8.8")
    assert ddns.current_ip() == "8.8.8.8" and len(calls) == 3                                                # the second is not asked when the first answered


@pytest.mark.parametrize("text", ["<html>proxy error</html>", "", "999.1.1.1", "1.2.3", "2001:db8::1", "1.1.1.1 extra"])
def test_something_that_is_not_an_ipv4_address_is_never_sent_to_dynu(v2, monkeypatch, text):
    configure()
    calls = fake_http(monkeypatch, lambda event, url: Resp(text=text) if event == "GetPublicIP" else Resp())
    assert ddns.current_ip() is None and [c[0] for c in calls] == ["GetPublicIP", "GetPublicIP"]
    ok, message = ddns.run_update()
    assert not ok and "public IP check failed" in message and not any(c[0] == "UpdateIP" for c in calls)


def test_an_address_with_stray_spaces_is_cleaned(v2):
    assert ddns._ipv4(" 10.0.0.1\n") == "10.0.0.1" and ddns._ipv4("010.0.0.1") is None


def test_a_failed_update_still_says_how_long_it_took(v2, monkeypatch):
    configure()
    fake_http(monkeypatch, lambda event, url: Resp(text="2.2.2.2") if event == "GetPublicIP" else Resp(status=500, text="boom"))
    ok, message = ddns.run_update()
    assert not ok and "(HTTP 500): boom. Took " in message and "Dynu update" in message


# ---------------------------------------------------------------- the watcher after a real change
def test_a_real_address_change_starts_the_watcher_and_its_timeline_is_kept(v2, monkeypatch):
    configure()
    fake_http(monkeypatch, lambda event, url: Resp(text="2.2.2.2") if event == "GetPublicIP" else Resp())
    started = []
    monkeypatch.setattr(ddns, "WATCH_AFTER_UPDATE", True)
    monkeypatch.setattr(dc, "watch_in_background", lambda domain, ip, record, **kw: started.append((domain, ip, record)))
    assert ddns.run_update()[0] and [(d, i) for d, i, _ in started] == [("home.dynu.net", "2.2.2.2")]
    started[0][2]({"ip": "2.2.2.2", "domain": "home.dynu.net", "watched_seconds": 12.0, "servers": []})
    assert ddns.last_timeline()["watched_seconds"] == 12.0
    ddns.run_update(force=True)                                                                              # forced with the same address: nothing changed, nothing to watch
    assert len(started) == 1


def test_nothing_is_watched_after_a_failure_or_when_there_is_no_change(v2, monkeypatch):
    configure()
    started = []
    monkeypatch.setattr(ddns, "WATCH_AFTER_UPDATE", True)
    monkeypatch.setattr(dc, "watch_in_background", lambda *a, **k: started.append(a))
    fake_http(monkeypatch, lambda event, url: Resp(text="2.2.2.2") if event == "GetPublicIP" else Resp(status=500, text="no"))
    assert not ddns.run_update()[0]
    configure(ddns_last_ip="2.2.2.2")
    fake_http(monkeypatch, lambda event, url: Resp(text="2.2.2.2"))
    ddns.run_update()
    assert started == []


def test_a_damaged_timeline_is_ignored(v2):
    assert ddns.last_timeline() is None
    settings.set_value("ddns_last_timeline", "{not json")
    assert ddns.last_timeline() is None


# ---------------------------------------------------------------- how often the address is checked
def test_the_check_interval_defaults_to_a_minute_and_survives_bad_values(v2):
    assert ddns.interval_minutes() == 1
    for stored, expected in (("5", 5), ("60", 60), ("0", 1), ("61", 1), ("-3", 1), ("abc", 1), ("2.9", 2)):
        settings.set_value("ddns_interval_minutes", stored)
        assert ddns.interval_minutes() == expected, stored


def test_the_scheduler_uses_the_interval_and_the_form_saves_and_applies_it(client, v2, monkeypatch):
    monkeypatch.setattr(ddns, "fetch_metadata", lambda d, k: None)
    res = client.post("/ddns/update-settings", data={"dynu_hostname": "home.dynu.net", "dynu_api_key": "", "dynu_enabled": "1", "ddns_interval_minutes": "7"}, follow_redirects=True)
    assert "saved successfully" in res.get_data(as_text=True) and settings.get("ddns_interval_minutes") == "7"
    assert scheduler.scheduler.get_job("job_ddns").trigger.interval.total_seconds() == 7 * 60
    bad = client.post("/ddns/update-settings", data={"dynu_hostname": "home.dynu.net", "dynu_api_key": "", "ddns_interval_minutes": "0"}, follow_redirects=True)
    assert "ddns_interval_minutes" in bad.get_data(as_text=True) and settings.get("ddns_interval_minutes") == "7"      # refused, and the old value stays
    scheduler.scheduler.remove_all_jobs()


def test_the_ddns_page_shows_the_interval_the_panel_and_the_last_timeline(client, v2):
    page = client.get("/ddns").get_data(as_text=True)
    assert 'name="ddns_interval_minutes" value="1"' in page and "Where does the delay come from?" in page and 'id="delayCheckBtn"' in page and "every 5 minutes" not in page
    assert 'id="delayTimeline" class="mt-3" hidden' in page
    settings.set_value("ddns_last_timeline", json.dumps({"ip": "2.2.2.2", "domain": "home.dynu.net", "watched_seconds": 61.5, "servers": [
        {"group": "authoritative", "label": "ns1.dynu.com", "seconds": 0.0, "error": None}, {"group": "resolver", "label": "Google", "seconds": 44.0, "error": None},
        {"group": "resolver", "label": "Quad9", "seconds": None, "error": None}, {"group": "resolver", "label": "<b>x</b>", "seconds": None, "error": "no answer"}]}))
    page = client.get("/ddns").get_data(as_text=True)
    assert "home.dynu.net changed to <code>2.2.2.2</code>" in page and "<strong>Google</strong>: 44.0 s" in page and "not within 61.5 s" in page and "could not be asked (no answer)" in page
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


# ---------------------------------------------------------------- the check button
def test_the_check_runs_every_part_and_changes_nothing(client, v2, monkeypatch):
    configure(dynu_domain="home.dynu.net", dynu_api_key="SECRETKEY")
    seen = []
    monkeypatch.setattr(ddns, "current_ip", lambda: "2.2.2.2")
    monkeypatch.setattr(ddns, "read_record", lambda dns_id, key: seen.append(("record", dns_id, key)) or {"ipv4Address": "2.2.2.2", "ttl": 90, "state": "Complete", "updatedOn": "t", "extra": "dropped"})
    monkeypatch.setattr(dc, "timed_request", lambda url, headers=None, timeout=8.0: seen.append(("timed", url, headers)) or timing(url.split("/")[2], url=url))
    monkeypatch.setattr(dc, "snapshot", lambda domain, ip, **kw: [row("authoritative", "ns1.dynu.com", ["2.2.2.2"]), row("resolver", "Google", ["1.1.1.1"], ttl=30, current=False)])
    before = settings.get_all()
    data = client.post("/api/ddns/check").get_json()
    assert data["ok"] and data["ip"] == "2.2.2.2" and data["domain"] == "home.dynu.net" and data["dynu"] == {"ipv4Address": "2.2.2.2", "ttl": 90, "state": "Complete", "updatedOn": "t"}
    assert [t["name"] for t in data["timings"]] == ["Public address service", "Dynu API"] and len(data["rows"]) == 2 and data["took"] >= 0
    assert any("for up to 30 s more" in f["text"] for f in data["findings"]) and {f["level"] for f in data["findings"]} <= {"ok", "note", "problem"}
    assert ("timed", "https://api.ipify.org", None) in seen and ("timed", f"{ddns.API_BASE}/dns", {"API-Key": "SECRETKEY", "Accept": "application/json"}) in seen and ("record", "42", "SECRETKEY") in seen
    assert settings.get_all() == before                                                                      # looking changed no setting and no record


def test_the_check_copes_with_nothing_configured(client, v2, monkeypatch):
    monkeypatch.setattr(ddns, "current_ip", lambda: None)
    monkeypatch.setattr(dc, "timed_request", lambda url, headers=None, timeout=8.0: {"url": url, "error": "offline", "total": 0})
    monkeypatch.setattr(dc, "snapshot", lambda *a, **k: pytest.fail("no domain, so nothing to ask about"))
    data = client.post("/api/ddns/check").get_json()
    assert data["ok"] and data["rows"] == [] and data["dynu"] is None and data["ip"] is None
    assert any("could not be reached" in f["text"] for f in data["findings"])
    assert client.get("/api/ddns/timeline").get_json() == {"ok": True, "timeline": None}
