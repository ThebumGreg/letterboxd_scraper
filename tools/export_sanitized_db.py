#!/usr/bin/env python3
"""Make a safe-to-share copy of the SyncHub database for UI review.

Usage (on Unraid):
  docker exec synchub python /app/tools/export_sanitized_db.py /data/synchub.db /data/sample.db
  Then take sample.db from your SyncHub data folder (appdata/synchub) and upload it to the project.
The live database is only READ (SQLite backup API); nothing in it changes.

The copy has secrets, tokens, PIN hashes, notification channels, raw API payloads,
Jellyfin events, logs and your IP addresses/host names removed. Library, jobs,
workers, shares, media, episodes and watch state are kept so the pages show real shapes.
"""
import os, re, sqlite3, sys

src, dst = sys.argv[1], sys.argv[2]
if os.path.exists(dst):
    os.remove(dst)      # start clean, e.g. after a failed earlier run
a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
b = sqlite3.connect(dst)
a.backup(b)
a.close()

tables = {r[0] for r in b.execute("select name from sqlite_master where type='table'")}
cols = lambda t: [r[1] for r in b.execute(f'pragma table_info("{t}")')]

def wipe(t):
    if t in tables:
        b.execute(f'delete from "{t}"')

def blank(t, names, value=None):
    """Erase a column. Tries NULL, then a unique placeholder, for columns with NOT NULL / CHECK / UNIQUE rules."""
    if t not in tables:
        return
    have = cols(t)
    for c in names:
        if c not in have:
            continue
        for expr in ("NULL", "'x'||rowid"):
            try:
                b.execute(f'update "{t}" set "{c}"={expr}')
                break
            except sqlite3.IntegrityError:
                continue

# Gone entirely
for t in ("app_secret", "api_payload_log", "notification_channel", "notification", "notification_event",
          "jellyfin_event", "job_log", "worker_log", "job_attempt", "library_event", "ddns_history"):
    wipe(t)

# Blank sensitive columns
blank("worker", ["token_hash", "token_hint", "hostname"])
blank("profile", ["pin_hash", "letterboxd_username", "serializd_username", "jellyfin_user_id"])
blank("storage_root", ["host", "options"])
if "worker" in tables:
    b.execute("update worker set name='worker-'||id where name is not null")

# Settings: keep harmless ones only
if "app_setting" in tables:
    bad = re.compile(r"key|token|secret|password|pass|url|host|address|user|webhook|ddns|domain|email", re.I)
    for k, in b.execute("select key from app_setting").fetchall():
        if bad.search(k):
            b.execute("delete from app_setting where key=?", (k,))

# Anything left that looks like an IP address or URL in remaining text columns
ip = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|https?://\S+")
for t in tables:
    for c in cols(t):
        for rowid, val in b.execute(f'select rowid, "{c}" from "{t}" where typeof("{c}")=\'text\' and ("{c}" like \'%http%\' or "{c}" like \'%.%.%.%\')').fetchall():
            new = ip.sub("x.x.x.x", val)
            if new != val:
                b.execute(f'update "{t}" set "{c}"=? where rowid=?', (new, rowid))

b.commit()
b.execute("vacuum")
b.close()
print("Wrote", dst, "- check it with: sqlite3", dst, "'select count(*) from media'")
