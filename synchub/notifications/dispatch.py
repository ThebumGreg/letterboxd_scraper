"""Sends what is waiting in the outbox.

Real notifications go out from one scheduler job (so `SYNCHUB_SCHEDULER=0` silences them) and only while the administrator's switch is on; a
test a person asked for goes out at once. A send happens with no database connection open. A temporary failure is tried again with a growing
wait; a refused message is final; a recipient the provider says is invalid is switched off after three such answers; a used-up allowance or
a rejected application token puts the whole provider on hold, so nothing hammers a service that has said no (Pushover blocks addresses that do)."""
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from synchub import db, settings
from synchub.notifications import emit, providers, store
from synchub.notifications.providers.base import BAD_CHANNEL, BAD_SETUP, QUOTA, RETRY, Message, ProviderError, Result

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (30, 120, 600, 3600)      # the wait after the 1st, 2nd, 3rd and 4th failed attempt (Pushover asks for 5 seconds at least)
LEASE_SECONDS = 120                          # a send under way holds its row this long, so nothing sends it twice and a crash only delays it
SETUP_PAUSE_SECONDS = 900
FAILURES_BEFORE_OFF = 3
BATCH = 50
_run_lock = threading.Lock()


def outcome(ok, status, message):
    return {"ok": ok, "status": status, "message": message}


# ---------------------------------------------------------------- providers on hold
def _holds(conn):
    try:
        got = json.loads(settings.get("notify_paused", conn=conn) or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def paused(conn, provider_key, now=None):
    """Why a provider is on hold, or None."""
    hold = _holds(conn).get(provider_key)
    return (hold.get("reason") or "On hold.") if hold and str(hold.get("until")) > (now or db.now()) else None


def pause(conn, provider_key, until, reason):
    now = db.now()
    holds = {k: v for k, v in _holds(conn).items() if str(v.get("until")) > now}
    holds[provider_key] = {"until": until, "reason": reason}
    settings.set_value("notify_paused", json.dumps(holds), conn=conn)


def clear_pause(provider_key):
    with db.connection() as conn:
        holds = _holds(conn)
        if holds.pop(provider_key, None) is not None:
            settings.set_value("notify_paused", json.dumps(holds), conn=conn)


# ---------------------------------------------------------------- one notification
def _epoch(text):
    return int(datetime.strptime(text, store.FORMAT).replace(tzinfo=timezone.utc).timestamp())


def _drop(conn, row, why):
    conn.execute("UPDATE notification SET status = 'dropped', error = ?, next_attempt_at = NULL WHERE id = ?", (why, row["id"]))
    return {"outcome": outcome(False, "dropped", why)}


def _claim(conn, notification_id, now):
    """Decides whether this notification can go now and, if so, takes a short lease on it. {'row': ...} to send, else {'outcome': ...}."""
    row = conn.execute("""SELECT n.*, c.provider AS provider, c.config AS config, c.enabled AS channel_enabled
                          FROM notification n LEFT JOIN notification_channel c ON c.id = n.channel_id WHERE n.id = ?""", (notification_id,)).fetchone()
    if row is None:
        return {"outcome": outcome(False, "gone", "That notification no longer exists.")}
    if row["status"] != "pending":
        return {"outcome": outcome(row["status"] == "sent", row["status"], f"Already {row['status']}.")}
    if row["next_attempt_at"] and row["next_attempt_at"] > now:
        return {"outcome": outcome(False, "pending", "Not due yet.")}
    if row["expires_at"] and row["expires_at"] <= now:
        return _drop(conn, row, "Too old to be worth sending.")
    if row["channel_id"] is None:
        return _drop(conn, row, "That delivery method was removed.")
    if not row["channel_enabled"]:
        return _drop(conn, row, "That delivery method is switched off.")
    why = paused(conn, row["provider"], now)
    if why:
        return {"outcome": outcome(False, "pending", f"On hold: {why}")}
    taken = conn.execute("UPDATE notification SET next_attempt_at = ? WHERE id = ? AND status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                         (store.later(now, LEASE_SECONDS), notification_id, now)).rowcount
    if not taken:
        return {"outcome": outcome(False, "pending", "Another sender has it.")}
    return {"row": dict(row)}


def _send(row):
    """Hands the message to its provider. Runs with no database connection open."""
    try:
        provider = providers.get(row["provider"])
        message = Message(row["title"], row["body"], row["url"], row["url_title"], row["priority"], row["ttl_seconds"], _epoch(row["created_at"]))
        return provider.send(json.loads(row["config"] or "{}"), message)
    except ProviderError as exc:
        return Result(BAD_CHANNEL, str(exc))
    except Exception as exc:
        logger.error(f"Notification {row['id']} could not be sent: {exc.__class__.__name__}")
        return Result(RETRY, "Sending failed unexpectedly (see the log).")


def _finish(conn, row, status, attempts, error, next_attempt_at=None):
    conn.execute("UPDATE notification SET status = ?, attempts = ?, error = ?, next_attempt_at = ? WHERE id = ?", (status, attempts, error, next_attempt_at, row["id"]))


def _record(conn, row, result, now, retry):
    attempts = row["attempts"] + 1
    detail = result.detail or "Sending failed."
    if result.ok:
        conn.execute("UPDATE notification SET status = 'sent', sent_at = ?, attempts = ?, provider_ref = ?, error = NULL, next_attempt_at = NULL WHERE id = ?",
                     (now, attempts, result.ref, row["id"]))
        conn.execute("UPDATE notification_channel SET last_ok_at = ?, last_error = NULL, failures = 0 WHERE id = ?", (now, row["channel_id"]))
        return outcome(True, "sent", "Sent.")
    if result.outcome in (QUOTA, BAD_SETUP):
        until = store.later(now, result.retry_after or 3600) if result.outcome == QUOTA else store.later(now, SETUP_PAUSE_SECONDS)
        pause(conn, row["provider"], until, detail)
        if retry:
            _finish(conn, row, "pending", row["attempts"], detail, until)           # not the message's fault: it keeps its place and its attempts
            return outcome(False, "pending", f"{detail} It will be tried again later.")
    elif result.outcome == BAD_CHANNEL:
        known = conn.execute("SELECT failures FROM notification_channel WHERE id = ?", (row["channel_id"],)).fetchone()
        if known is not None:                                                        # (the person may have removed it while this was being sent)
            failures = known["failures"] + 1
            off = failures >= FAILURES_BEFORE_OFF
            conn.execute("UPDATE notification_channel SET failures = ?, last_error = ?, enabled = CASE WHEN ? THEN 0 ELSE enabled END WHERE id = ?",
                         (failures, f"Switched off after repeated failures: {detail}" if off else detail, 1 if off else 0, row["channel_id"]))
    elif result.outcome == RETRY and retry and attempts < MAX_ATTEMPTS:
        _finish(conn, row, "pending", attempts, detail, store.later(now, BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]))
        return outcome(False, "pending", f"{detail} It will be tried again.")
    _finish(conn, row, "failed", attempts, detail)
    return outcome(False, "failed", detail)


def deliver(notification_id, now=None, retry=True):
    """Sends one queued notification now. Returns {'ok', 'status', 'message'}. With retry=False a failure is final (a test someone is waiting on)."""
    now = now or db.now()
    with db.connection() as conn:
        claimed = _claim(conn, notification_id, now)
    if "row" not in claimed:
        return claimed["outcome"]
    result = _send(claimed["row"])
    with db.connection() as conn:
        return _record(conn, claimed["row"], result, now, retry)


def send_test(profile_id, channel_id, now=None):
    """Sends a test message through one of a person's channels, whatever the administrator's switch says. Raises LookupError if it is not theirs."""
    with db.connection() as conn:
        channel = store.get_channel(conn, channel_id, profile_id)
        if channel is None:
            raise LookupError("That delivery method no longer exists.")
        ids = emit.enqueue(conn, "test", uuid.uuid4().hex, {}, [channel], now) if channel["enabled"] else []
    if not ids:
        return outcome(False, "dropped", "Switch this delivery method on first.")
    return deliver(ids[0], now=now, retry=False)


# ---------------------------------------------------------------- the scheduled jobs
def run_due(now=None, limit=BATCH):
    """Sends what is due, oldest first. Does nothing while the administrator's switch is off."""
    counts = {"sent": 0, "failed": 0, "pending": 0, "dropped": 0}
    if settings.get("enable_notifications") != "1":
        return counts
    if not _run_lock.acquire(blocking=False):
        return counts
    try:
        at = now or db.now()
        with db.connection() as conn:
            due = conn.execute("""SELECT n.id, c.provider FROM notification n LEFT JOIN notification_channel c ON c.id = n.channel_id
                                  WHERE n.status = 'pending' AND (n.next_attempt_at IS NULL OR n.next_attempt_at <= ?) ORDER BY n.id LIMIT ?""", (at, limit)).fetchall()
            held = {key for key in _holds(conn) if paused(conn, key, at)}
        for row in due:
            if row["provider"] in held:
                continue
            try:
                got = deliver(row["id"], now=now)
            except Exception as exc:                                  # one bad row must not stop the rest
                logger.error(f"Notification {row['id']} could not be delivered: {exc.__class__.__name__}: {exc}")
                continue
            counts[got["status"]] = counts.get(got["status"], 0) + 1
        return counts
    finally:
        _run_lock.release()


def prune_history():
    with db.connection() as conn:
        removed = store.prune(conn)
    if removed:
        logger.info(f"Removed {removed} notification event(s) older than the retention setting.")
    return removed


def state(now=None):
    """What a page needs to say about sending as a whole: the switch, the application token, anything on hold, the Pushover allowance."""
    from synchub.notifications.providers import pushover
    with db.connection() as conn:
        hold = paused(conn, "pushover", now)
    left = pushover.quota()
    if left and left.get("reset"):
        left = {**left, "reset_label": datetime.fromtimestamp(left["reset"], timezone.utc).strftime("%b %d").replace(" 0", " ")}
    return {"enabled": settings.get("enable_notifications") == "1", "pushover_ready": bool(pushover.app_token()), "paused": hold, "quota": left}
