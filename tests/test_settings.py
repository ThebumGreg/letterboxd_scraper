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
