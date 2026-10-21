import pytest

from settings_validation import validate_setting


@pytest.mark.parametrize("key,value", [
    ("scraper_interval_hours", "abc"), ("scraper_interval_hours", ""), ("scraper_interval_hours", "-1"),
    ("scraper_interval_hours", "1.5"), ("radarr_min_user_count", "0"),
    ("radarr_quality_profile_id", "Loading..."), ("sonarr_quality_profile_id", "0"),
    ("request_delay_min", "-2"), ("request_delay_min", "fast"),
    ("jellyfin_url", "ftp://host"), ("radarr_url", "not a url"),
    ("radarr_root_folder", "Loading..."), ("sonarr_root_folder", ""),
    ("tv_metadata_provider", "bogus"), ("radarr_dry_run", "2"),
])
def test_invalid_values_are_rejected(key, value):
    ok, message = validate_setting(key, value)
    assert not ok and key in message


@pytest.mark.parametrize("key,value,expected", [
    ("scraper_interval_hours", " 0 ", "0"), ("radarr_interval_hours", "6", "6"),
    ("radarr_min_user_count", "2", "2"), ("radarr_quality_profile_id", "4", "4"),
    ("request_delay_max", "4.5", "4.5"), ("jellyfin_url", "http://192.168.1.10:8096", "http://192.168.1.10:8096"),
    ("sonarr_url", "", ""), ("radarr_root_folder", "D:\\Movies", "D:\\Movies"),
    ("tv_metadata_provider", "sonarr", "sonarr"), ("radarr_dry_run", "1", "1"),
    ("some_unlisted_key", " x ", "x"),
])
def test_valid_values_are_cleaned_and_accepted(key, value, expected):
    assert validate_setting(key, value) == (True, expected)


def test_update_settings_saves_valid_and_skips_invalid(client, db):
    db.set_setting("scraper_interval_hours", "24")
    response = client.post("/tools/update-settings", data={
        "scraper_interval_hours": "abc",
        "radarr_interval_hours": "12",
        "radarr_quality_profile_id": "Loading...",
        "radarr_min_user_count": "3",
    }, follow_redirects=True)

    assert response.status_code == 200
    assert db.get_setting("scraper_interval_hours") == "24"       # invalid value did not overwrite the stored one
    assert db.get_setting("radarr_quality_profile_id", "unset") == "unset"
    assert db.get_setting("radarr_interval_hours") == "12"
    assert db.get_setting("radarr_min_user_count") == "3"
    page = response.get_data(as_text=True)
    assert "Some values were not saved" in page and "scraper_interval_hours" in page


def test_update_settings_reports_success_when_all_valid(client, db):
    response = client.post("/tools/update-settings", data={"sonarr_interval_hours": "8"}, follow_redirects=True)
    assert "Settings updated successfully." in response.get_data(as_text=True)
    assert db.get_setting("sonarr_interval_hours") == "8"


def test_update_settings_keeps_secret_when_blank(client, db):
    db.set_setting("tmdb_api_key", "keepme-123456")
    client.post("/tools/update-settings", data={"tmdb_api_key": ""})
    assert db.get_setting("tmdb_api_key") == "keepme-123456"


def test_logs_endpoint_reads_configured_log_file(client, tmp_path, monkeypatch):
    import app as app_module
    log = tmp_path / "custom.log"
    log.write_text("line one\nline two\n")
    monkeypatch.setattr(app_module, "LOG_FILE_PATH", log)
    assert client.get("/api/logs").get_json()["logs"] == ["line one", "line two"]
