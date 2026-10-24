"""In-memory ring buffer of everything the app sends, shared with the UI and a log file."""
import json
import logging
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

MAX_RESPONSE = 4000


class LogBook:
    def __init__(self, maxlen=2000, path=None):
        self._lock = threading.Lock()
        self._entries = deque(maxlen=maxlen)
        self._next = 1
        self._poll_ok = {}
        self._file = None
        if path:
            handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._file = logging.getLogger(f"synchub.tv.file.{id(self)}")
            self._file.setLevel(logging.INFO)
            self._file.propagate = False
            self._file.addHandler(handler)

    def add(self, kind, device, action, request="", status=None, ok=None, ms=None, response=""):
        """kind: adb | roku | jellyfin | system; device: device id or None."""
        response = str(response or "")
        if len(response) > MAX_RESPONSE:
            response = response[:MAX_RESPONSE] + f"\n... ({len(response) - MAX_RESPONSE} more chars)"
        now = time.time()
        with self._lock:
            entry = {
                "id": self._next,
                "ts": time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}",
                "epoch": now,
                "kind": kind,
                "device": device,
                "action": action,
                "request": request,
                "status": status,
                "ok": ok,
                "ms": None if ms is None else round(ms),
                "response": response,
            }
            self._next += 1
            self._entries.append(entry)
        if self._file:
            self._file.info(json.dumps(entry))
        return entry

    def since(self, last_id=0, limit=500):
        with self._lock:
            return [e for e in self._entries if e["id"] > last_id][-limit:]

    def clear(self):
        with self._lock:
            self._entries.clear()

    def dump(self):
        with self._lock:
            return list(self._entries)

    # Background polls would flood the log, so only record when a poll starts
    # failing and when it recovers.
    def poll_ok(self, key, kind, device, action):
        with self._lock:
            was_ok = self._poll_ok.get(key, True)
            self._poll_ok[key] = True
        if not was_ok:
            self.add(kind, device, f"{action} recovered", ok=True)

    def poll_failed(self, key, kind, device, action, request="", status=None, response=""):
        with self._lock:
            was_ok = self._poll_ok.get(key, True)
            self._poll_ok[key] = False
        if was_ok:
            self.add(kind, device, f"{action} failing", request, status, False, None, response)
