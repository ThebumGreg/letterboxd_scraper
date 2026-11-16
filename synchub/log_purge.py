"""Keeps the log tables from growing for ever: the API payload log, and the logs and command lines of finished jobs, by age (Settings, Logging).
The Jellyfin event log and the notification history have their own age settings (jellyfin_events.prune, notifications.store.prune)."""
import logging

from synchub import db, settings

logger = logging.getLogger(__name__)


def _days(key, default):
    try:
        return max(int(settings.get(key)), 0)
    except (TypeError, ValueError):
        return default


def purge(conn=None):
    """Deletes what is older than the settings say (0 keeps everything). Returns {what: rows removed}."""
    def run(c):
        removed = {}
        days = _days("logs_keep_days", 30)
        if days:
            removed["api_payloads"] = c.execute("DELETE FROM api_payload_log WHERE created_at < datetime('now', ?)", (f"-{days} days",)).rowcount
        days = _days("job_logs_keep_days", 30)
        if days:
            cutoff = f"-{days} days"
            removed["job_logs"] = c.execute("DELETE FROM job_log WHERE job_id IN (SELECT id FROM job WHERE finished_at IS NOT NULL AND finished_at < datetime('now', ?))", (cutoff,)).rowcount
            removed["job_attempt_logs"] = c.execute("UPDATE job_attempt SET log_tail = NULL WHERE log_tail IS NOT NULL AND finished_at IS NOT NULL AND finished_at < datetime('now', ?)", (cutoff,)).rowcount
        return removed
    if conn is not None:
        return run(conn)
    with db.connection() as c:
        return run(c)


def purge_and_report():
    removed = purge()
    if any(removed.values()):
        logger.info("Log purge removed " + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in removed.items() if n) + ".")
    return removed
