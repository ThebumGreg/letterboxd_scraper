-- Compression by bitrate only: a film is re-encoded when its video is above the "re-encode above" bitrate of its picture size and aimed at the "aim for" bitrate.
-- 4K 50 -> 35 Mbps, 1080p 25 -> 12 Mbps, 720p and smaller are left alone; the GB rules are off; the extra 1080p copy is offered for every 4K film;
-- Dolby Vision profile 7 is converted to 8.1. Only values that were already saved are changed (a fresh database takes the new defaults).
UPDATE app_setting SET value = '35' WHERE key = 'cap_mbps_2160';
UPDATE app_setting SET value = '12' WHERE key = 'cap_mbps_1080';
UPDATE app_setting SET value = '0'  WHERE key IN ('safety_gb_2160', 'safety_gb_1080');
UPDATE app_setting SET value = '0'  WHERE key = 'variant_min_gb';
UPDATE app_setting SET value = 'remux' WHERE key = 'dolby_vision_profile_7_8';
-- Space saver used to compress above 1.5 x its own ceilings (14 and 6 Mbps); the new rule names that bitrate, so it keeps the same behaviour.
UPDATE conversion_profile SET rules_json = '{"audio_mode": "compatible_only", "cap_1080": 6, "cap_2160": 14, "dv_profile_7_8": "hdr10", "extra_copy": false, "size_policy": true, "tv_size_policy": true, "trigger_1080": 9, "trigger_2160": 21, "trigger_720": 6, "trigger_sd": 3}'
    WHERE id = 8 AND builtin = 1 AND name = 'Space saver';
