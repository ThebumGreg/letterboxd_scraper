"""Worker PCs: who they are, how they prove it (a token each), and what they report about themselves.

A worker never opens SyncHub's database; it calls the worker API with its token. Only a hash of the token is stored, so a
lost token is replaced (rotated), never recovered.
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from synchub import db, settings
from synchub.notifications import emit

PROTOCOL = 1                  # bumped when the worker API changes incompatibly; older agents are refused
ONLINE_SECONDS = 60           # a worker that has not polled for this long shows as offline
MAX_SLOTS = 16
MAX_STATUS_BYTES = 8192
MAX_CAPABILITIES_BYTES = 32768   # what an agent says about itself once (the hardware details make this larger than a status summary)
MAX_LOG_CHARS = 1_000_000        # the tail of a worker's own log that is kept


class WorkerError(Exception):
    """A change that cannot be made; the message is meant for the user."""


class ProtocolError(WorkerError):
    """The agent speaks a protocol version SyncHub does not support."""


def new_token():
    return "sw_" + secrets.token_hex(24)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tags(value):
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    out = []
    for tag in value or []:
        tag = str(tag).strip().lower()
        if tag and tag not in out:
            if len(tag) > 32 or not all(c.isalnum() or c in "-_." for c in tag):
                raise WorkerError(f"'{tag}' is not a usable tag (letters, numbers, - _ . and at most 32 characters).")
            out.append(tag)
    return out


def _slots(value):
    try:
        slots = int(value)
    except (TypeError, ValueError):
        raise WorkerError("Slots must be a whole number.")
    if not 1 <= slots <= MAX_SLOTS:
        raise WorkerError(f"Slots must be between 1 and {MAX_SLOTS}.")
    return slots


def _name(value):
    name = " ".join((value or "").split())
    if not name:
        raise WorkerError("A worker needs a name.")
    if len(name) > 60:
        raise WorkerError("The name can be at most 60 characters.")
    return name


def _load(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def row(r, now=None):
    """A worker as a dict, with the JSON columns decoded and whether it is online."""
    w = dict(r)
    for key, default in (("tags", []), ("capabilities", {}), ("status", {}), ("originals", [])):
        w[key] = _load(w.get(key), default)
    w["settings"] = _load(w.get("settings"), {})
    w["token_hash"] = bool(w["token_hash"])            # never hand the hash around
    w["revoked"] = bool(w.get("revoked_at"))
    w["online"] = is_online(w.get("last_seen"), now)
    return w


def is_online(last_seen, now=None):
    if not last_seen:
        return False
    try:
        seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)
    return (current - seen) < timedelta(seconds=ONLINE_SECONDS)


# ---------------------------------------------------------------- managing workers (the web UI)
# What a worker's owner chooses in SyncHub instead of in the PC's .env: field -> (kind, default, low, high or choices).
# 'agent' fields are sent to the agent in its register and poll answers; 'host' fields are read by agent/auto-update.sh on the PC.
SETTINGS = {
    "encoder": ("agent", "choice", "auto", ("auto", "cpu", "nvenc", "qsv", "vaapi")),
    "gpu_slots": ("agent", "int", 1, (1, 16)),                 # GPU encodes at once; a job over the limit uses the CPU instead of waiting
    "max_copy_mb_s": ("agent", "float", 0, (0, 100000)),       # 0 = no limit
    "scan_threads": ("agent", "int", 2, (1, 16)),
    "log_level": ("agent", "choice", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
    "auto_update": ("host", "bool", True, None),
    "update_window": ("host", "window", "", None),              # HH:MM-HH:MM local time on the PC, empty = only when idle
}
_WINDOW = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$")


def clean_settings(given, current=None):
    """The worker's settings with what `given` changes: unknown fields are ignored, a bad value raises WorkerError naming the field."""
    out = {k: v for k, v in (current or {}).items() if k in SETTINGS}
    for key, (_, kind, default, rule) in SETTINGS.items():
        if not isinstance(given, dict) or key not in given:
            continue
        value = given[key]
        if value in (None, ""):
            out.pop(key, None)                                    # blank means the default
            continue
        try:
            if kind == "choice":
                value = str(value).strip()
                value = value.upper() if key == "log_level" else value.lower()
                if value not in rule:
                    raise ValueError
            elif kind in ("int", "float"):
                value = int(value) if kind == "int" else float(value)
                if isinstance(given[key], bool) or not rule[0] <= value <= rule[1]:
                    raise ValueError
            elif kind == "bool":
                if value not in (True, False, 1, 0, "1", "0", "true", "false", "on", "off"):
                    raise ValueError
                value = value in (True, 1, "1", "true", "on")
            elif kind == "window":
                value = str(value).strip()
                if not _WINDOW.match(value):
                    raise ValueError
        except ValueError:
            raise WorkerError(f"The setting '{key.replace('_', ' ')}' is not valid.")
        if value == default:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def setting(w, key):
    """One setting of a worker (the dict from get/list_all), or its default."""
    return w["settings"].get(key, SETTINGS[key][2])


def agent_settings(w):
    """What the agent is told in its register and poll answers: slots (kept in step with SyncHub's own limit) and the 'agent' settings that were chosen.
    A setting left at its default is not sent: the worker then keeps what its own .env or ini says."""
    out = {key: w["settings"][key] for key, (where, *_rest) in SETTINGS.items() if where == "agent" and key in w["settings"]}
    out["slots"] = w["slots"]
    return out


def create(conn, name, tags=(), slots=1, settings=None):
    """Adds a worker. Returns (worker_id, token); the token is shown once and cannot be read again."""
    name, tags, slots = _name(name), _tags(tags), _slots(slots)
    chosen = clean_settings(settings)
    if conn.execute("SELECT 1 FROM worker WHERE name = ?", (name,)).fetchone():
        raise WorkerError(f"There is already a worker called '{name}'.")
    token = new_token()
    worker_id = conn.execute("INSERT INTO worker (name, token_hash, token_hint, tags, slots, settings) VALUES (?, ?, ?, ?, ?, ?)",
                             (name, hash_token(token), token[-4:], json.dumps(tags), slots, json.dumps(chosen))).lastrowid
    return worker_id, token


def get(conn, worker_id, now=None):
    r = conn.execute("SELECT * FROM worker WHERE id = ?", (worker_id,)).fetchone()
    return row(r, now) if r else None


def list_all(conn, now=None):
    workers = [row(r, now) for r in conn.execute("SELECT * FROM worker ORDER BY name COLLATE NOCASE")]
    running = {r["worker_id"]: r["n"] for r in conn.execute("SELECT worker_id, COUNT(*) AS n FROM job WHERE status = 'running' GROUP BY worker_id")}
    for w in workers:
        w["running"] = running.get(w["id"], 0)
    return workers


def update(conn, worker_id, name=None, tags=None, slots=None, settings=None):
    w = get(conn, worker_id)
    if not w:
        raise WorkerError("That worker no longer exists.")
    new_name = _name(name) if name is not None else w["name"]
    if new_name != w["name"] and conn.execute("SELECT 1 FROM worker WHERE name = ? AND id <> ?", (new_name, worker_id)).fetchone():
        raise WorkerError(f"There is already a worker called '{new_name}'.")
    conn.execute("UPDATE worker SET name = ?, tags = ?, slots = ?, settings = ? WHERE id = ?",
                 (new_name, json.dumps(_tags(tags) if tags is not None else w["tags"]), _slots(slots) if slots is not None else w["slots"],
                  json.dumps(clean_settings(settings, w["settings"])) if settings is not None else json.dumps(w["settings"]), worker_id))


def set_flag(conn, worker_id, flag, value):
    """flag: enabled, paused or draining."""
    if flag not in ("enabled", "paused", "draining"):
        raise WorkerError("Unknown setting.")
    if conn.execute(f"UPDATE worker SET {flag} = ? WHERE id = ?", (1 if value else 0, worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


def rotate_token(conn, worker_id):
    """Replaces the token (the old one stops working at once). Returns the new token, shown once."""
    token = new_token()
    if conn.execute("UPDATE worker SET token_hash = ?, token_hint = ?, revoked_at = NULL WHERE id = ?", (hash_token(token), token[-4:], worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")
    return token


def revoke(conn, worker_id):
    """Stops the token working. The worker's name, history and jobs stay; rotate the token to let it back in."""
    now = db.now()
    if conn.execute("UPDATE worker SET token_hash = NULL, token_hint = NULL, revoked_at = ? WHERE id = ?", (now, worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


def delete(conn, worker_id):
    if conn.execute("SELECT 1 FROM job WHERE worker_id = ? AND status = 'running'", (worker_id,)).fetchone():
        raise WorkerError("This worker is running a job. Cancel the job or drain the worker first.")
    if conn.execute("DELETE FROM worker WHERE id = ?", (worker_id,)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


# ---------------------------------------------------------------- what a worker does (the worker API)
def authenticate(conn, token):
    """(worker, problem): the worker for a token, or None and a reason ('missing', 'unknown' or 'disabled')."""
    token = (token or "").strip()
    if not token:
        return None, "missing"
    r = conn.execute("SELECT * FROM worker WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if r is None:
        return None, "unknown"
    if not r["enabled"]:
        return None, "disabled"
    return row(r), None


def register(conn, worker_id, info, now=None):
    """Records what the agent says about itself. Raises ProtocolError for an unsupported protocol version."""
    info = info if isinstance(info, dict) else {}
    if info.get("protocol") != PROTOCOL:
        raise ProtocolError(f"This SyncHub speaks worker protocol {PROTOCOL}; the agent sent {info.get('protocol')!r}. Update the agent.")
    capabilities = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
    capabilities["agent_slots"] = info.get("slots")
    text = json.dumps(capabilities)
    if len(text) > MAX_CAPABILITIES_BYTES:                  # cutting the text would leave invalid JSON; drop the bulky part instead
        capabilities.pop("host", None)
        text = json.dumps(capabilities)[:MAX_CAPABILITIES_BYTES]
    conn.execute("UPDATE worker SET protocol = ?, version = ?, os = ?, hostname = ?, capabilities = ?, last_seen = ? WHERE id = ?",
                 (PROTOCOL, str(info.get("version") or "")[:40], str(info.get("os") or "")[:80], str(info.get("hostname") or "")[:80],
                  text, now or db.now(), worker_id))


MAX_ORIGINALS = 400
MAX_ORIGINALS_BYTES = 262144
ORIGINAL_FIELDS = ("id", "kind", "root", "rel", "name", "size", "saved_at", "expires_at", "retention_days", "replaced_with")


def store_originals(conn, worker_id, listing):
    """Keeps the list of originals a worker says it holds (only known fields, a bounded amount)."""
    if not isinstance(listing, list):
        return
    clean = [{k: (v if isinstance(v, (int, float)) or v is None else str(v)[:400]) for k, v in item.items() if k in ORIGINAL_FIELDS} for item in listing[:MAX_ORIGINALS] if isinstance(item, dict) and item.get("id")]
    text = json.dumps(clean)
    if len(text) <= MAX_ORIGINALS_BYTES:
        conn.execute("UPDATE worker SET originals = ? WHERE id = ?", (text, worker_id))


def touch(conn, worker_id, status=None, now=None):
    """Marks the worker as seen, and stores its latest status summary when it sent one."""
    if isinstance(status, dict):
        text = json.dumps(status)
        if len(text) <= MAX_STATUS_BYTES:
            conn.execute("UPDATE worker SET last_seen = ?, status = ? WHERE id = ?", (now or db.now(), text, worker_id))
            return
    conn.execute("UPDATE worker SET last_seen = ? WHERE id = ?", (now or db.now(), worker_id))


# ---------------------------------------------------------------- the worker's own log ("Get logs")
def request_log(conn, worker_id):
    """Asks a worker for the tail of its own log. It answers with its next poll."""
    if conn.execute("UPDATE worker SET log_requested = 1 WHERE id = ? AND revoked_at IS NULL", (worker_id,)).rowcount != 1:
        raise WorkerError("That worker no longer exists." if get(conn, worker_id) is None else "That worker's token is revoked, so it cannot answer. Make a new token first.")


def store_log(conn, worker_id, text, truncated=False, now=None):
    """Keeps the log a worker uploaded (the latest only) and closes the request."""
    text = str(text if text is not None else "")
    if len(text) > MAX_LOG_CHARS:
        text, truncated = "[earlier lines left out]\n" + text[-MAX_LOG_CHARS:], True
    conn.execute("INSERT INTO worker_log (worker_id, received_at, truncated, content) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(worker_id) DO UPDATE SET received_at = excluded.received_at, truncated = excluded.truncated, content = excluded.content",
                 (worker_id, now or db.now(), 1 if truncated else 0, text))
    conn.execute("UPDATE worker SET log_requested = 0 WHERE id = ?", (worker_id,))


def stored_log(conn, worker_id):
    r = conn.execute("SELECT * FROM worker_log WHERE worker_id = ?", (worker_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- the automatic update (agent/auto-update.sh on the worker PC)
UPDATE_KEY = "worker_update:{}"
UPDATE_FIELDS = ("status", "version", "previous", "commit", "restored", "error", "log", "at")


HOST_REQUESTS = ("update", "restart")
HOST_SEEN_KEY = "worker_host_seen:{}"
HOST_SEEN_SECONDS = 300           # the PC's timer asks every minute; this long without it and the buttons say it is not installed


def request_host(conn, worker_id, what):
    """Asks the worker's PC (agent/auto-update.sh) to update or restart the worker. An update waits until the worker is idle."""
    if what not in HOST_REQUESTS:
        raise WorkerError("Unknown request.")
    if conn.execute("UPDATE worker SET host_request = ? WHERE id = ? AND revoked_at IS NULL", (what, worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists or its token is revoked.")


def host_check(conn, worker_id, now=None):
    """The PC's timer asked: what it should know. A request is handed over once (an update only when nothing is running) and cleared."""
    now = now or db.now()
    settings.set_value(HOST_SEEN_KEY.format(worker_id), now, conn=conn)
    w = get(conn, worker_id)
    running = idle(conn, worker_id)
    request = w["host_request"] if w else None
    if request == "update" and running:
        request = None                                              # it stays requested until the worker is idle
    if request:
        conn.execute("UPDATE worker SET host_request = NULL WHERE id = ? AND host_request = ?", (worker_id, request))
    return {"idle": running == 0, "running": running, "auto_update": bool(setting(w, "auto_update")), "window": setting(w, "update_window"), "request": request}


def host_seen(conn, worker_id, now=None):
    """True when the PC's timer has asked recently."""
    seen = settings.get(HOST_SEEN_KEY.format(worker_id), "", conn=conn)
    return is_online(seen, now) if seen else False


def idle(conn, worker_id):
    """How many jobs the worker is running right now (0 = nothing would be cut off by a restart)."""
    return conn.execute("SELECT COUNT(*) FROM job WHERE worker_id = ? AND status = 'running'", (worker_id,)).fetchone()[0]


def report_update(conn, worker_id, info, now=None):
    """Records what the automatic update did on a worker and tells the administrators. `info` is the PC's own report: status 'ok' or 'failed'.

    Returns the stored report. Raises ValueError for a status that is neither."""
    info = info if isinstance(info, dict) else {}
    status = info.get("status")
    if status not in ("ok", "failed", "current", "problem"):
        raise ValueError("The update report needs a status of 'ok', 'failed', 'current' or 'problem'.")
    report = {k: str(info.get(k) or "")[:{"error": 2000, "log": 6000}.get(k, 80)] for k in UPDATE_FIELDS if k != "at"}
    report["status"], report["at"] = status, now or db.now()
    settings.set_value(UPDATE_KEY.format(worker_id), json.dumps(report), conn=conn)
    if status == "current":                                          # asked to update and there was nothing new: shown on the page, nothing to tell anyone
        return report
    w = get(conn, worker_id)
    name = (w or {}).get("name") or f"worker {worker_id}"
    kind = "worker_update_done" if status == "ok" else "worker_update_failed"
    same = f"{report['at']}:{report['commit']}" if status != "problem" else "problem:" + hashlib.sha1(report["error"].encode()).hexdigest()[:12]      # the same problem is told once
    emit.notify(conn, kind, f"worker:{worker_id}:{kind}:{same}", {"worker": name, **{k: v for k, v in report.items() if k != "log"}}, now=report["at"])
    return report


def last_update(conn, worker_id):
    """The last update report of a worker (a dict, see UPDATE_FIELDS), or None."""
    text = settings.get(UPDATE_KEY.format(worker_id), "", conn=conn)
    report = _load(text, {})
    return report or None

# ---------------------------------------------------------------- the benchmark ("Run benchmark")
BENCHMARK_KEEP = 10


def request_benchmark(conn, worker_id):
    """Asks a worker to benchmark its encoders. It starts with its next poll when it has no job running."""
    w = get(conn, worker_id)
    if w is None:
        raise WorkerError("That worker no longer exists.")
    if w["revoked"]:
        raise WorkerError("That worker's token is revoked, so it cannot answer. Make a new token first.")
    if "benchmark" not in _items_of(w["capabilities"].get("features")):
        raise WorkerError("This worker's agent is too old to run a benchmark. Update it first (docs/UPGRADE.md).")
    conn.execute("UPDATE worker SET benchmark_requested = 1 WHERE id = ?", (worker_id,))


def _items_of(value):
    return value if isinstance(value, list) else []


def _summary(report):
    done = [r for r in report.get("results") or [] if r.get("ok")]
    if not done:
        return "No encoder worked."
    parts = [f"{r['label'].split(' (')[0]} {r['speed']}x" + (f", VMAF {r['vmaf']}" if r.get("vmaf") is not None else "") for r in done if r.get("speed")]
    return "; ".join(parts) or "Finished."


def store_benchmark(conn, worker_id, report, now=None):
    """Keeps the report a worker uploaded and closes the request. Only the fields the page shows are kept, trimmed."""
    if not isinstance(report, dict):
        raise WorkerError("The benchmark report is not readable.")
    clean = {k: report.get(k) for k in ("agent", "when", "seconds", "cpu", "cores", "gpus", "source", "ok", "took_seconds")}
    clean["health"] = [{"name": str(h.get("name"))[:40], "ok": bool(h.get("ok")), "detail": str(h.get("detail") or "")[:300]} for h in (report.get("health") or [])[:10] if isinstance(h, dict)]
    clean["results"] = [{k: r.get(k) for k in ("encoder", "label", "ok", "reason", "seconds", "speed", "fps", "size_mb", "vmaf", "vmaf_floor", "passed")}
                        for r in (report.get("results") or [])[:8] if isinstance(r, dict)]
    clean["ok"] = bool(clean["ok"])
    conn.execute("INSERT INTO worker_benchmark (worker_id, created_at, ok, summary, report) VALUES (?, ?, ?, ?, ?)",
                 (worker_id, now or db.now(), 1 if clean["ok"] else 0, _summary(clean), json.dumps(clean)))
    conn.execute("DELETE FROM worker_benchmark WHERE worker_id = ? AND id NOT IN (SELECT id FROM worker_benchmark WHERE worker_id = ? ORDER BY id DESC LIMIT ?)", (worker_id, worker_id, BENCHMARK_KEEP))
    conn.execute("UPDATE worker SET benchmark_requested = 0 WHERE id = ?", (worker_id,))


def last_benchmark(conn, worker_id):
    r = conn.execute("SELECT * FROM worker_benchmark WHERE worker_id = ? ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["report"] = json.loads(out["report"])
    return out
