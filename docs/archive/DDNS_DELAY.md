# Dynamic DNS: where the 30 to 45 seconds go

> **Findings note (October 2026), kept for reference.** What was added from it is in [CHANGELOG.md](../../CHANGELOG.md).

**Question.** An update takes about 30 to 45 seconds to show. Is that SyncHub, Dynu, or something else?

**Short answer.** From the code and from how DNS works, the most likely cause is **not SyncHub**: after the address changes, resolvers that already hold the old answer keep serving it until its life (the *TTL*) runs out. Dynu's API documents a default TTL of 90 seconds; a record with a 90 s TTL is stale for anything from 0 to 90 seconds at a resolver that cached it just before the change, **45 seconds on average**. That matches what was seen. It is a hypothesis: the services could not be reached from where this was written (a restricted network), so nothing below was measured against your Dynu account. The tools added to SyncHub measure it on your own network, and the next section says what each result would mean.

## What SyncHub itself does (read from `synchub/ddns.py`)

An update is three sequential HTTPS calls, none of them with any pause or retry loop:

1. the public address (`api.ipify.org`),
2. only the first time, the Dynu record lookup,
3. the Dynu update (`POST /v2/dns/<id>`).

Normally each takes well under a second. Where SyncHub *could* add time:

| Cause | How long | Notes |
|---|---|---|
| A slow **name lookup** on the machine that runs SyncHub (router, Docker, WSL, VPN, a dead first DNS server) | 5 to 30 s **per call** | `requests` timeouts do not cover the name lookup, so a slow resolver costs its full delay before any timeout starts. Three calls with a 10 s lookup each is 30 s. This is the most common "underlying" cause in WSL and Docker. |
| A service that is slow or hangs | up to 10 + 10 + 15 s (the old timeouts) | now 4 s to connect and 6 s to answer for the address check, with a second source (`ipv4.icanhazip.com`) when the first fails |
| **Noticing the change** | up to 5 minutes (the old check interval) | the job ran every 5 minutes, so on average the change was noticed 2.5 minutes late. Now every minute by default (Dynamic DNS, "Check the public address every"), since only a change calls Dynu |
| A proxy error page taken as the address | n/a | the answer is now checked to be an IPv4 address before it is ever sent to Dynu |

## What happens after SyncHub has done its part

1. **Dynu accepts the update and its name servers start serving it.** Dynu staff have described the change as happening on Dynu's name servers first and then showing at public resolvers; there have been user reports of updates that did not show until the record was saved again in Dynu's console. If this is the delay, Dynu's own name servers (`ns1` to `ns6.dynu.com`) answer with the *old* address for a while.
2. **Resolvers hold the old answer for the TTL.** Dynu's API documentation gives 90 seconds as the default when a TTL is not set; Dynu's FAQ recommends 60 to 300 seconds for dynamic DNS. A resolver (your router, your ISP, 8.8.8.8, a phone) that looked the name up a moment before the change keeps the old address until the TTL ends. Some routers and DNS filters (dnsmasq `min-cache-ttl`, Pi-hole, unbound) keep answers *longer* than the TTL.
3. **Browsers and operating systems add their own short caches.**

**What SyncHub does not know:** whether Dynu resets the record's TTL when SyncHub's update does not send one (the update body is `name`, `ipv4Address`, `ipv4`; the original app's payload "still needs a live check", as `ddns.py` says). The check's last line, "Dynu's record: ... TTL ...", shows the TTL Dynu holds; if you lower it in Dynu's console and it is back at 90 after SyncHub's next update, SyncHub should send the TTL with the update (a one-line change, not made blind because a wrong field could make updates fail).

## What was added

- **Every update says where its time went**, in the status line and the history: `Took 1.4 s: public IP 0.4 s, Dynu update 1.0 s.` If that is small, SyncHub is not the delay.
- **Dynamic DNS, Run the check** (changes nothing):
  - the path from this machine to the address service and to Dynu: name lookup, connect, secure handshake and first answer, each timed. A slow *name lookup* points at this machine's resolver.
  - what Dynu's own name servers and Cloudflare, Google and Quad9 answer for your name, the time each has left in its cache, and what this computer answers. Dynu's servers behind = Dynu's side; Dynu's servers right and a resolver behind = ordinary caching (and the TTL says how long).
  - Dynu's record: address, TTL, state and last change.
  - a plain-words reading of the numbers.
- **After every real address change SyncHub watches** (every 3 seconds, up to 3 minutes) how long each server took to show the new address and keeps the result on the same page: the actual timeline of your next change.
- **The address is checked every minute** by default (it was every 5), with timeouts that cannot stall an update and a second address source.

## What to do with the answer

| The check says | Do this |
|---|---|
| Name lookup 1 s or more for `api.dynu.com` or the address service | Fix this machine's DNS: in WSL set `generateResolvConf = false` and a working `nameserver` in `/etc/resolv.conf` (or turn off the VPN's DNS override); in Docker set `dns:` on the service. Everything SyncHub does over the network gets faster. |
| Dynu's name servers still show the old address | The wait is at Dynu. Check the update succeeded (history), try saving the record in Dynu's console once, and tell Dynu support if it is regularly slow. |
| Dynu is right, a resolver is behind, TTL 90 | This is DNS caching. Lower the TTL in Dynu's console to the smallest value Dynu allows (the wait is at most the TTL), or point clients at a resolver that honours it (Cloudflare, Google). |
| Your router or Pi-hole is the one behind for longer than the TTL | Turn off its minimum cache time, or flush its cache after an address change. |
| Everything agrees and fast | The delay was measured at a moment when something else (a browser cache, a phone) held the old answer. |
