-- Per-profile rules (size by bitrate, Dolby Vision, audio, names, extra copy): only the overrides are stored, as JSON; everything else follows the Options page.
ALTER TABLE conversion_profile ADD COLUMN rules_json TEXT;
-- The profile chosen when a file was scanned: its suggestion is built from that profile until a person picks another at approval.
ALTER TABLE library_file ADD COLUMN scan_profile_id INTEGER REFERENCES conversion_profile (id) ON DELETE SET NULL;

-- "Move only" must not inherit any compression, naming or audio rule from the Options page.
UPDATE conversion_profile SET rules_json = '{"audio_mode": "keep", "clean_names": false, "dv_profile_7_8": "leave", "extra_copy": false, "size_policy": false, "tv_size_policy": false}' WHERE id = 3;
-- Three profiles that show what the rules are for. Nothing uses them until a person picks one.
INSERT INTO conversion_profile (id, name, description, video_codec, crf, preset, ten_bit, audio_kbps, subtitles, move_only, builtin, rules_json) VALUES
    (7, 'Direct play', 'Keeps the picture. Dolby Vision 7 becomes 8.1 without re-encoding, a compatible audio track is added, and big films get a 1080p copy.',
        'keep', NULL, NULL, 0, 192, 'remove_non_english', 0, 1,
        '{"audio_mode": "add_compatible", "dv_profile_7_8": "remux", "extra_copy": true, "size_policy": false}'),
    (8, 'Space saver', 'Compresses films and episodes above a bitrate ceiling to HEVC 10-bit, keeps only a compatible audio track and makes no extra copy.',
        'keep', NULL, NULL, 0, 192, 'remove_non_english', 0, 1,
        '{"audio_mode": "compatible_only", "cap_1080": 6, "cap_2160": 14, "dv_profile_7_8": "hdr10", "extra_copy": false, "size_policy": true, "tv_size_policy": true}'),
    (9, 'Archive', 'Keeps the picture, every audio track and the Dolby Vision layer as they are; only the name is tidied.',
        'keep', NULL, NULL, 0, 192, 'keep_all', 0, 1,
        '{"audio_mode": "keep", "dv_profile_7_8": "leave", "extra_copy": false, "size_policy": false, "tv_size_policy": false}');
