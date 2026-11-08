-- Greg's choice: keep Jellyfin webhook events 7 days. A saved 365 (the old default) moves to 7; any other value a person chose is left alone.
UPDATE app_setting SET value = '7' WHERE key = 'jellyfin_events_keep_days' AND value = '365';
