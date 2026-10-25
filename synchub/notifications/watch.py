"""Notices about conditions rather than one-off happenings: a worker going quiet or coming back, a share that stops working, a TV that has not
accepted SyncHub's ADB key. Run on a timer; each call compares what is true now with what was true last time and raises an event only for a change,
so a worker that has been offline for a week says so once. The first look at anything records it without saying anything (no flood on first start)."""
import json
import logging

from synchub import db, settings, workers
from synchub.notifications import emit

logger = logging.getLogger(__name__)
KEY = "notify_watch_state"


def _load(conn):
    try:
        got = json.loads(settings.get(KEY, conn=conn) or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _tv_states():
    """{tv id: (name, True when its ADB key is not accepted)} for the TVs SyncHub drives. Reads state already in memory; asks no TV anything."""
    try:
        from synchub.tv import runtime
        devices = runtime.get().devices
    except Exception as exc:
        logger.debug(f"TV state not available: {exc.__class__.__name__}")
        return {}
    found = {}
    for dev_id, d in list(devices.items()):
        adb = getattr(d, "adb", None) or d
        found[dev_id] = (getattr(d, "name", None) or dev_id, getattr(adb, "adb_auth", None) == "unauthorized")
    return found


def check(now=None):
    """One pass. Returns how many events were raised (for tests)."""
    now = now or db.now()
    raised = 0
    with db.connection() as conn:
        before = _load(conn)
        after = {}
        for w in workers.list_all(conn, now):
            if w.get("revoked"):
                continue
            name = w.get("name") or f"worker {w['id']}"
            wk = f"worker:{w['id']}"
            was = before.get(wk)
            after[wk] = bool(w["online"])
            if was is not None and was != after[wk]:
                kind = "worker_online" if after[wk] else "worker_offline"
                raised += bool(emit.notify(conn, kind, f"{wk}:{kind}:{now}", {"worker": name}, now=now))
            roots = w["status"].get("roots") if isinstance(w["status"].get("roots"), dict) else {}
            for root, info in roots.items():
                if not isinstance(info, dict) or not w["online"]:
                    if f"{wk}:root:{root}" in before:
                        after[f"{wk}:root:{root}"] = before[f"{wk}:root:{root}"]       # offline: say nothing new about its shares
                    continue
                rk = f"{wk}:root:{root}"
                ok = info.get("ok") is True
                after[rk] = ok
                if rk in before and before[rk] != ok:
                    payload = {"worker": name, "root": root, "state": str(info.get("state") or "not ready").replace("_", " ")}
                    kind = "share_recovered" if ok else "share_problem"
                    raised += bool(emit.notify(conn, kind, f"{rk}:{kind}:{now}", payload, now=now))
        for dev_id, (name, bad) in _tv_states().items():
            tk = f"tv:{dev_id}"
            after[tk] = bad
            if bad and before.get(tk) is not True:
                raised += bool(emit.notify(conn, "tv_adb_unauthorized", f"{tk}:{now}", {"tv": name}, now=now))
        settings.set_value(KEY, json.dumps(after), conn=conn)
    return raised
