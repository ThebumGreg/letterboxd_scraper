-- A counter that goes up whenever anything the Library's recommendations are worked out from changes (files, quality results, profiles, settings, shows; only shows count from the media table, as they decide the show folder names).
-- The Library pages keep the worked-out result in memory and reuse it until this number moves, instead of recomputing it for every page view and refresh.
CREATE TABLE change_counter (name TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
INSERT INTO change_counter (name, n) VALUES ('library', 0);

CREATE TRIGGER change_library_file_i AFTER INSERT ON library_file BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_library_file_u AFTER UPDATE ON library_file BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_library_file_d AFTER DELETE ON library_file BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_check_i AFTER INSERT ON quality_check BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_check_u AFTER UPDATE ON quality_check BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_check_d AFTER DELETE ON quality_check BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_accept_i AFTER INSERT ON quality_accept BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_accept_u AFTER UPDATE ON quality_accept BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_quality_accept_d AFTER DELETE ON quality_accept BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_profile_i AFTER INSERT ON conversion_profile BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_profile_u AFTER UPDATE ON conversion_profile BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_profile_d AFTER DELETE ON conversion_profile BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_setting_i AFTER INSERT ON app_setting BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_setting_u AFTER UPDATE ON app_setting BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_setting_d AFTER DELETE ON app_setting BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_media_i AFTER INSERT ON media WHEN NEW.kind = 'show' BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_media_u AFTER UPDATE ON media WHEN NEW.kind = 'show' OR OLD.kind = 'show' BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
CREATE TRIGGER change_media_d AFTER DELETE ON media WHEN OLD.kind = 'show' BEGIN UPDATE change_counter SET n = n + 1 WHERE name = 'library'; END;
