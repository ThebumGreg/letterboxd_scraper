"""How much room SyncHub itself takes, and how that changes: the database, preview clips, backups, logs, posters, and the work folders on the workers.

Everything here is read from inside the container, so it needs nothing extra. Image and container sizes come from Docker and need its socket mounted
read-only (`/var/run/docker.sock:ro`); without it they are simply left out. One reading a day is kept for 90 days (`storage_history`) and drawn as a chart."""
import http.client
import json
import logging
import os
import shutil
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import BACKUP_DIR, DATA_DIR, LOGS_DIR, POSTER_DIR
from synchub import db, dbinfo, previews, settings, workdrive

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
CACHE_SECONDS = 60
DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_TIMEOUT = 8

_lock = threading.Lock()
_cached = {"at": 0.0, "key": None, "value": None}


def human(n):
    """12345678 -> '12.3 MB' (decimal units, as Unraid and the disks show them)."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def folder_bytes(path):
    """Total size of the files under a folder (symbolic links are not followed); 0 when it does not exist."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    full = os.path.join(root, name)
                    if not os.path.islink(full):
                        total += os.path.getsize(full)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _file_bytes(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _database():
    path = db.DB_FILE
    wal = _file_bytes(path + "-wal")
    shm = _file_bytes(path + "-shm")
    out = {"file": _file_bytes(path), "wal": wal, "shm": shm, "reclaimable": 0, "tables": [], "how": ""}
    try:
        info = dbinfo.sizes(top=8)
        out.update(reclaimable=int(info["reclaimable_mb"] * 1e6), tables=info["tables"], how=info["how"])
    except Exception as exc:
        logger.warning(f"[storage] table sizes could not be read: {exc.__class__.__name__}: {exc}")
    out["total"] = out["file"] + wal + shm
    return out


def _previews(conn):
    root = previews.root_folder()
    count = 0
    if os.path.isdir(root):
        count = sum(1 for name in os.listdir(root) if os.path.isdir(os.path.join(root, name)))
    waiting = conn.execute("SELECT COUNT(*) FROM preview WHERE status = 'ready' AND decision IS NULL").fetchone()[0]
    return {"count": count, "bytes": folder_bytes(root), "waiting": waiting, "keep_days": previews.keep_days(conn)}


def _backups(directory=None):
    folder = Path(directory or BACKUP_DIR)
    files = [p for p in folder.glob("*") if p.is_file()] if folder.is_dir() else []
    copies = [p for p in files if p.suffix == ".db"]
    newest = max(copies, key=lambda p: p.stat().st_mtime, default=None)
    return {"count": len(copies), "bytes": sum(p.stat().st_size for p in files), "newest_name": newest.name if newest else None,
            "newest_bytes": newest.stat().st_size if newest else 0,
            "newest_at": datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if newest else None}


def _work(conn):
    """What each worker reported for its work folder (the Workers page's Work drive panel): [{id, name, bytes, jobs, originals, at}]."""
    out = []
    for w in conn.execute("SELECT id, name FROM worker WHERE revoked_at IS NULL ORDER BY id").fetchall():
        try:
            usage = json.loads(settings.get(workdrive.USAGE_KEY.format(w["id"]), "", conn=conn) or "{}")
        except ValueError:
            usage = {}
        if not usage:
            continue
        used = usage.get("used_bytes")
        if used is None:
            used = sum(int(usage.get(k) or 0) for k in ("jobs_bytes", "originals_bytes", "logs_bytes", "other_bytes"))
        out.append({"id": w["id"], "name": w["name"], "bytes": int(used or 0), "jobs": int(usage.get("jobs_bytes") or 0),
                    "originals": int(usage.get("originals_bytes") or 0), "at": usage.get("at") or ""})
    return out


def _volume():
    try:
        usage = shutil.disk_usage(DATA_DIR)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def measure(conn, force=False):
    """The picture now. Walking the folders costs a moment, so the answer is reused for a minute."""
    key = db.DB_FILE
    with _lock:
        held = _cached
        if not force and held["value"] is not None and held["key"] == key and time.monotonic() - held["at"] < CACHE_SECONDS:
            return held["value"]
    database = _database()
    prev, backups = _previews(conn), _backups()
    logs, posters = folder_bytes(LOGS_DIR), folder_bytes(POSTER_DIR)
    tv = folder_bytes(Path(db.DB_FILE).parent / "tv")
    work = _work(conn)
    data_total = folder_bytes(DATA_DIR)
    counted = database["total"] + prev["bytes"] + backups["bytes"] + logs + posters + tv
    result = {"data_bytes": data_total, "database": database, "previews": prev, "backups": backups, "logs_bytes": logs, "posters_bytes": posters, "tv_bytes": tv,
              "other_bytes": max(0, data_total - counted), "work": work, "work_bytes": sum(w["bytes"] for w in work), "volume": _volume(),
              "docker_socket": os.path.exists(DOCKER_SOCKET)}
    with _lock:
        _cached.update(at=time.monotonic(), key=key, value=result)
    return result


def forget():
    with _lock:
        _cached.update(at=0.0, key=None, value=None)


# ---------------------------------------------------------------- the daily history
def record(conn, day=None, reading=None):
    """Writes (or replaces) today's reading and drops what is older than 90 days. Returns the reading."""
    day = day or date.today().isoformat()
    r = reading or measure(conn, force=True)
    conn.execute("INSERT OR REPLACE INTO storage_history (day, data_bytes, db_bytes, previews_bytes, backups_bytes, logs_bytes, work_bytes, free_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (day, r["data_bytes"], r["database"]["total"], r["previews"]["bytes"], r["backups"]["bytes"], r["logs_bytes"], r["work_bytes"], r["volume"]["free"]))
    cutoff = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
    conn.execute("DELETE FROM storage_history WHERE day < ?", (cutoff,))
    return r


def record_today():
    """The daily job. A failure is logged, never raised: the history is a convenience."""
    try:
        with db.connection() as conn:
            record(conn)
    except Exception as exc:
        logger.warning(f"[storage] the daily size reading failed: {exc.__class__.__name__}: {exc}")


def history(conn, days=KEEP_DAYS):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [dict(r) for r in conn.execute("SELECT * FROM storage_history WHERE day >= ? ORDER BY day", (cutoff,))]


def ensure_today(conn):
    """The first visit of the day takes today's reading if the daily job has not yet (a fresh install, or a container that was off at the job's time)."""
    today = date.today().isoformat()
    if not conn.execute("SELECT 1 FROM storage_history WHERE day = ?", (today,)).fetchone():
        record(conn, today)


def growth(rows, key="data_bytes"):
    """(bytes gained, days spanned) between the first and last reading, or None with fewer than two."""
    rows = [r for r in rows if r.get(key) is not None]
    if len(rows) < 2:
        return None
    days = (date.fromisoformat(rows[-1]["day"]) - date.fromisoformat(rows[0]["day"])).days
    return (rows[-1][key] - rows[0][key], days) if days > 0 else None


def chart_points(rows, key, width=640, height=120, pad=6):
    """Points of a polyline for one series, scaled to the box: 'x,y x,y ...' (empty with fewer than two readings)."""
    values = [(r["day"], r.get(key)) for r in rows if r.get(key) is not None]
    if len(values) < 2:
        return ""
    first = date.fromisoformat(values[0][0])
    span = max(1, (date.fromisoformat(values[-1][0]) - first).days)
    low, high = min(v for _, v in values), max(v for _, v in values)
    rng = (high - low) or 1
    pts = []
    for day, v in values:
        x = pad + (width - 2 * pad) * (date.fromisoformat(day) - first).days / span
        y = height - pad - (height - 2 * pad) * (v - low) / rng
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


# ---------------------------------------------------------------- previews
def clear_old_previews():
    """Deletes the clips of previews whose time is up or that were answered (the same sweep as the six-hourly one). Returns (previews cleared, bytes freed)."""
    before = folder_bytes(previews.root_folder())
    cleared = previews.purge()
    freed = max(0, before - folder_bytes(previews.root_folder()))
    forget()
    return cleared, freed


# ---------------------------------------------------------------- Docker (optional)
class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=DOCKER_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _docker_get(path, socket_path=DOCKER_SOCKET):
    conn = _UnixConnection(socket_path)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"Docker answered {response.status}")
        return json.loads(body)
    finally:
        conn.close()


def docker_sizes(socket_path=DOCKER_SOCKET, hostname=None):
    """This container's image and writable-layer size, and what all images on the host take, read through the Docker socket. Read-only (GET only).
    {'available': False, 'reason': ...} when the socket is not mounted or Docker does not answer."""
    if not os.path.exists(socket_path):
        return {"available": False, "reason": "The Docker socket is not mounted, so image and container sizes are not shown. Add /var/run/docker.sock as a read-only path to the container to see them."}
    try:
        me = _docker_get(f"/containers/{hostname or socket.gethostname()}/json?size=1", socket_path)
        image = _docker_get(f"/images/{me.get('Image', '')}/json", socket_path)
        images = _docker_get("/images/json", socket_path)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return {"available": False, "reason": f"Docker did not answer: {exc}"}
    return {"available": True, "container": (me.get("Name") or "").lstrip("/"), "image_tags": image.get("RepoTags") or [], "image_bytes": int(image.get("Size") or 0),
            "writable_bytes": int(me.get("SizeRw") or 0), "root_bytes": int(me.get("SizeRootFs") or 0),
            "images_count": len(images), "images_bytes": sum(int(i.get("Size") or 0) for i in images)}
