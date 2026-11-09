-- Migration 041's counter went up on every settings write, including the ones made all the time in the background (a worker PC checking in, every Jellyfin
-- webhook event, DDNS and notification bookkeeping), so the Library and dashboard caches were thrown away constantly. These keys are never read by the
-- recommendations, so writing them no longer moves the counter.
DROP TRIGGER change_setting_i;
DROP TRIGGER change_setting_u;
DROP TRIGGER change_setting_d;
CREATE TRIGGER change_setting_i AFTER INSERT ON app_setting
  WHEN NEW.key NOT LIKE 'worker_host_seen:%' AND NEW.key NOT LIKE 'worker_updater_log%' AND NEW.key NOT LIKE 'worker_update:%' AND NEW.key NOT LIKE 'webhook_last_%'
   AND NEW.key NOT LIKE 'ddns_last_%' AND NEW.key NOT LIKE 'dynu_remote_updated_on' AND NEW.key NOT IN ('pushover_quota', 'notify_paused')
BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_setting_u AFTER UPDATE ON app_setting
  WHEN NEW.key NOT LIKE 'worker_host_seen:%' AND NEW.key NOT LIKE 'worker_updater_log%' AND NEW.key NOT LIKE 'worker_update:%' AND NEW.key NOT LIKE 'webhook_last_%'
   AND NEW.key NOT LIKE 'ddns_last_%' AND NEW.key NOT LIKE 'dynu_remote_updated_on' AND NEW.key NOT IN ('pushover_quota', 'notify_paused')
BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_setting_d AFTER DELETE ON app_setting
  WHEN OLD.key NOT LIKE 'worker_host_seen:%' AND OLD.key NOT LIKE 'worker_updater_log%' AND OLD.key NOT LIKE 'worker_update:%' AND OLD.key NOT LIKE 'webhook_last_%'
   AND OLD.key NOT LIKE 'ddns_last_%' AND OLD.key NOT LIKE 'dynu_remote_updated_on' AND OLD.key NOT IN ('pushover_quota', 'notify_paused')
BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
