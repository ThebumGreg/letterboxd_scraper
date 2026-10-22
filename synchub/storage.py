"""Storage roots: the shares the workers read from and write to, named once so every job and every worker agrees.

A job never carries a machine's path. It says "media: Movies/X.mkv" and each worker mounts the root `media` at the same mount point.
SyncHub cannot mount anything on a worker PC, but it holds the definitions and writes the files that make the mount happen
(a Docker compose file, or `/etc/fstab` lines). A user name and password for a share never enter SyncHub: they go in the worker's own `.env`.
"""
import hashlib
import ipaddress
import json
import re

MARKER = ".synchub-root"          # a file in the top folder of each root holding the root's name: proof that the right share is mounted
PROTOCOLS = {"smb": "SMB (Windows file sharing)", "nfs": "NFS", "local": "Already mounted on the worker PC"}
DEFAULT_OPTIONS = {"smb": "vers=3.0", "nfs": "nfsvers=4,hard", "local": ""}
WORK_MOUNT = "/work"              # inside the worker's container
SETUP_ORDER = (".env", "docker-compose.yml", "synchub-agent.ini", "fstab")      # the order the page shows the files in (JSON objects have none)

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SMB_SHARE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_PATH = re.compile(r"^/[A-Za-z0-9 ._/-]*$")
_MOUNT = re.compile(r"^/[A-Za-z0-9._/-]+$")
_OPTIONS = re.compile(r"^[A-Za-z0-9=,._:-]*$")
_SECRET_OPTION = re.compile(r"(^|,)\s*(user|username|password|pass|passwd|credentials|cred)\s*=", re.I)
_APP_PATH = re.compile(r"^/[^\x00-\x1f\\]*$")


class StorageError(Exception):
    """A storage setting that cannot be used; the message is meant for the user."""


def clean_rel(rel):
    """A path inside a root: relative, forward slashes, no '..'. Raises StorageError."""
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts) or any(ord(c) < 32 for c in rel):
        raise StorageError("Give a path inside the share, such as Movies/Heat (1995)/Heat (1995).mkv. It cannot contain '..' or empty parts.")
    if len(rel) > 1000:
        raise StorageError("That path is too long.")
    return "/".join(parts)


def _path(value, label, pattern, absolute_hint):
    value = str(value or "").strip()
    if not pattern.match(value) or any(p == ".." for p in value.split("/")):
        raise StorageError(f"{label} must be {absolute_hint}.")
    return value.rstrip("/") or "/"


def validate(data):
    """Checks one root's settings and returns them cleaned. Raises StorageError."""
    data = data if isinstance(data, dict) else {}
    name = str(data.get("name") or "").strip().lower()
    if not _NAME.match(name):
        raise StorageError("The name is what jobs call this share: lowercase letters, numbers, - and _ (for example media or tfiles).")
    protocol = str(data.get("protocol") or "").strip().lower()
    if protocol not in PROTOCOLS:
        raise StorageError("Choose SMB, NFS or already mounted.")
    label = " ".join(str(data.get("label") or "").split())[:80] or None
    host = str(data.get("host") or "").strip()
    if protocol in ("smb", "nfs"):
        if not _HOST.match(host):
            raise StorageError("Enter the Unraid server's address, such as 192.168.1.10 or tower.")
    else:
        host = ""
    if protocol == "smb":
        share = str(data.get("share") or "").strip().strip("/")
        if not _SMB_SHARE.match(share):
            raise StorageError("The share name is the name shown under Shares in Unraid, such as Media (no slashes).")
    elif protocol == "nfs":
        share = _path(data.get("share"), "The export path", _PATH, "the path Unraid exports, such as /mnt/user/Media")
    else:
        share = _path(data.get("share"), "The folder", _PATH, "the folder already mounted on the worker PC, such as /mnt/tower/Media")
    mount_point = _path(data.get("mount_point"), "The mount point", _MOUNT, "where workers see it, such as /mnt/tower/Media (letters, numbers, . _ - and /)")
    if mount_point == "/":
        raise StorageError("The mount point cannot be the top folder.")
    options = str(data.get("options") if data.get("options") is not None else "").strip().strip(",")
    if not options:
        options = DEFAULT_OPTIONS[protocol]
    if protocol == "local":
        options = ""
    if _SECRET_OPTION.search(options):
        raise StorageError("Do not put a user name or password in the options. They go in the worker's .env file, so SyncHub never stores them.")
    if not _OPTIONS.match(options):
        raise StorageError("Mount options are letters, numbers and = , . _ : - only.")
    paths = {}
    for key, label_text in (("jellyfin_path", "Jellyfin"), ("radarr_path", "Radarr"), ("sonarr_path", "Sonarr")):
        value = str(data.get(key) or "").strip()
        if value and not _APP_PATH.match(value):
            raise StorageError(f"The path {label_text} sees must start with / (for example /data/media).")
        paths[key] = value.rstrip("/") or None if value else None
    return {"name": name, "label": label, "protocol": protocol, "host": host or None, "share": share, "options": options, "mount_point": mount_point, **paths}


# ---------------------------------------------------------------- managing roots
def get(conn, root_id):
    r = conn.execute("SELECT * FROM storage_root WHERE id = ?", (root_id,)).fetchone()
    return dict(r) if r else None


def by_name(conn, name):
    r = conn.execute("SELECT * FROM storage_root WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


def list_all(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM storage_root ORDER BY name")]


def _check_unique(conn, cleaned, root_id=None):
    for other in list_all(conn):
        if other["id"] == root_id:
            continue
        if other["name"] == cleaned["name"]:
            raise StorageError(f"There is already a share called '{cleaned['name']}'.")
        a, b = cleaned["mount_point"], other["mount_point"]
        if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
            raise StorageError(f"The mount point {a} overlaps {other['name']}'s ({b}). Every share needs its own folder, not one inside another.")


def create(conn, data):
    cleaned = validate(data)
    _check_unique(conn, cleaned)
    columns = list(cleaned)
    return conn.execute(f"INSERT INTO storage_root ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", [cleaned[c] for c in columns]).lastrowid


def _in_use(conn, name):
    return conn.execute("SELECT COUNT(*) FROM job WHERE status IN ('queued', 'running', 'needs_review') AND (source_root = ? OR dest_root = ?)", (name, name)).fetchone()[0]


def update(conn, root_id, data):
    current = get(conn, root_id)
    if current is None:
        raise StorageError("That share no longer exists.")
    cleaned = validate({**current, **(data if isinstance(data, dict) else {})})
    _check_unique(conn, cleaned, root_id)
    if cleaned["name"] != current["name"] and _in_use(conn, current["name"]):
        raise StorageError(f"Jobs are waiting or running on '{current['name']}'. Rename it when they have finished.")
    conn.execute(f"UPDATE storage_root SET {', '.join(f'{c} = ?' for c in cleaned)} WHERE id = ?", [*cleaned.values(), root_id])


def delete(conn, root_id):
    current = get(conn, root_id)
    if current is None:
        raise StorageError("That share no longer exists.")
    waiting = _in_use(conn, current["name"])
    if waiting:
        raise StorageError(f"{waiting} job(s) are waiting or running on '{current['name']}'. Cancel them first.")
    conn.execute("DELETE FROM storage_root WHERE id = ?", (root_id,))


def for_agent(conn):
    """What a worker is told at registration: each root, where to find it, and the marker file that proves it is the right share."""
    return [{"name": r["name"], "mount_point": r["mount_point"], "marker": MARKER, "marker_text": r["name"]} for r in list_all(conn)]


def display_path(root, rel=""):
    """A path as people know it: //server/share/folder for SMB, server:/export/folder for NFS, the folder itself for one already mounted."""
    rel = str(rel or "").strip("/")
    if root["protocol"] == "smb":
        base = f"//{root['host']}/{root['share']}"
    elif root["protocol"] == "nfs":
        base = f"{root['host']}:{root['share']}"
    else:
        base = root["share"]
    return f"{base}/{rel}" if rel else base


def roots_hash(conn):
    """A short fingerprint of what for_agent returns, so an agent can tell when the list of shares changed."""
    return hashlib.sha1(json.dumps(for_agent(conn), sort_keys=True).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- jobs on roots
def queue_test_copy(conn, source_root, source_rel, dest_root, dest_rel, worker_id=None):
    """A plain verified copy between two roots: the quickest way to prove a worker's mounts, permissions and speed. Returns the job id."""
    from synchub import jobs
    roots = {r["name"] for r in list_all(conn)}
    for name in (source_root, dest_root):
        if name not in roots:
            raise StorageError(f"There is no share called '{name}'. Add it on the Storage page first.")
    source_rel, dest_rel = clean_rel(source_rel), clean_rel(dest_rel)
    if (source_root, source_rel) == (dest_root, dest_rel):
        raise StorageError("The copy would overwrite its own source. Choose a different destination.")
    if worker_id is not None and not conn.execute("SELECT 1 FROM worker WHERE id = ? AND revoked_at IS NULL", (worker_id,)).fetchone():
        raise StorageError("That worker no longer exists.")
    return jobs.create(conn, "library", title=f"Test copy: {source_rel.rsplit('/', 1)[-1]}", ref=("test", None), source=(source_root, source_rel),
                       dest=(dest_root, dest_rel), plan={"op": "copy"}, pinned_worker_id=worker_id)


# ---------------------------------------------------------------- setup files for a worker
def _q(text):
    """A string for YAML: a JSON string is a valid double-quoted YAML scalar."""
    return json.dumps(str(text))


def _fstab_escape(text):
    return str(text).replace("\\", "\\134").replace(" ", "\\040")


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _volume_name(root):
    return "root_" + root["name"].replace("-", "_")


def compose_file(worker_name, roots):
    """The worker's docker-compose.yml. Docker mounts each share itself, so if one cannot be mounted the container does not start
    (instead of writing into an empty local folder)."""
    lines = [f"# docker-compose.yml for the SyncHub worker {worker_name!r}. Generated by SyncHub; generate it again after changing a share.",
             "# Put it next to the .env file and the 'agent' folder from the SyncHub repository, then run:  docker compose up -d --build",
             "services:", "  worker:", "    build: ./agent", "    image: synchub-worker", "    container_name: synchub-worker", "    init: true",
             "    restart: unless-stopped", "    stop_grace_period: 90s", "    environment:",
             "      SYNCHUB_URL: ${SYNCHUB_URL:?set SYNCHUB_URL in .env}", "      SYNCHUB_TOKEN: ${SYNCHUB_TOKEN:?set SYNCHUB_TOKEN in .env}",
             "      SYNCHUB_NAME: ${SYNCHUB_NAME:-}", f"      SYNCHUB_WORK: {WORK_MOUNT}      # the share login in .env is used by Docker to mount the shares and is not passed into the container",
             "    volumes:",
             f"      - {_q('${WORK_DIR:-./work}:' + WORK_MOUNT)}      # job folders, kept originals and logs: use a big local disk"]
    declared = []
    for root in roots:
        if root["protocol"] == "local":
            lines.append(f"      - {_q(root['share'] + ':' + root['mount_point'])}      # {root['name']}: already mounted on this PC")
        else:
            lines.append(f"      - {_q(_volume_name(root) + ':' + root['mount_point'])}      # {root['name']}")
            declared.append(root)
    if declared:
        lines += ["", "volumes:"]
    for root in declared:
        if root["protocol"] == "smb" and not _is_ip(root["host"]):
            lines.append(f"  # {root['name']}: Docker hands this address to the kernel as written and only looks up names for NFS. If this mount fails, use the server's IP address.")
        options = [f"addr={root['host']}"]
        if root["protocol"] == "smb":
            options += ["username=${SMB_USER}", "password=${SMB_PASSWORD}"]
        if root["options"]:
            options.append(root["options"])
        device = f"//{root['host']}/{root['share']}" if root["protocol"] == "smb" else f":{root['share']}"
        lines += [f"  {_volume_name(root)}:", "    driver: local", "    driver_opts:", f"      type: {'cifs' if root['protocol'] == 'smb' else 'nfs'}",
                  f"      o: {_q(','.join(options))}", f"      device: {_q(device)}"]
    return "\n".join(lines) + "\n"


def env_file(worker_name, url, token, roots):
    lines = ["# .env for the SyncHub worker: the address, the token and (for SMB) the login for the Unraid shares.",
             f"SYNCHUB_URL={url}", f"SYNCHUB_TOKEN={token}", f"SYNCHUB_NAME={worker_name}", f"SYNCHUB_WORK={WORK_MOUNT}",
             "# A big local disk for job folders and the original files kept for the retention period:", "WORK_DIR=./work"]
    if any(r["protocol"] == "smb" for r in roots):
        lines += ["", "# The Unraid user that may read and write the shares. These stay on this PC; SyncHub never stores them.",
                  "# In the password write $ as $$ and a comma as two commas. Keep this file private:  chmod 600 .env", "SMB_USER=", "SMB_PASSWORD="]
    return "\n".join(lines) + "\n"


def agent_ini(worker_name, url, token):
    return "\n".join(["# synchub-agent.ini: for a worker that runs without Docker (python3 synchub_agent.py --config synchub-agent.ini)",
                      "[synchub]", f"url = {url}", f"token = {token}", f"name = {worker_name}", "",
                      "[worker]", "work_dir = /var/lib/synchub-agent", "",
                      "# Only needed when a share is mounted somewhere other than the Storage page says:", "# [roots]", "# media = /mnt/other/Media", ""])


def fstab_lines(roots):
    """For a worker without Docker: the /etc/fstab lines that mount the shares. 'nofail' lets the PC boot without the server; the agent
    refuses to use a root that lacks its marker file, so an unmounted folder is never written to."""
    out = ["# Add to /etc/fstab, then:  sudo mkdir -p <each mount point>  &&  sudo mount -a",
           "# SMB needs a credentials file (sudo nano /etc/synchub-smb.cred; sudo chmod 600 /etc/synchub-smb.cred) containing two lines:  username=...  and  password=..."]
    for root in roots:
        opts = [o for o in root["options"].split(",") if o]
        if root["protocol"] == "smb":
            out.append(f"//{root['host']}/{_fstab_escape(root['share'])}  {root['mount_point']}  cifs  {','.join(['credentials=/etc/synchub-smb.cred', *opts, '_netdev', 'nofail'])}  0  0")
        elif root["protocol"] == "nfs":
            out.append(f"{root['host']}:{_fstab_escape(root['share'])}  {root['mount_point']}  nfs  {','.join([*opts, '_netdev', 'nofail'])}  0  0")
        else:
            out.append(f"# {root['name']}: {root['mount_point']} is already mounted on the PC, nothing to add")
    return "\n".join(out) + "\n"


def setup_files(worker_name, url, token, roots):
    """Everything to set up one worker, as {file name: text}. `token` is the real token only right after it was made."""
    return {".env": env_file(worker_name, url, token, roots), "docker-compose.yml": compose_file(worker_name, roots),
            "synchub-agent.ini": agent_ini(worker_name, url, token), "fstab": fstab_lines(roots)}

