# settings_validation.py
from urllib.parse import urlparse

_SWITCH_KEYS = {
    "disable_jellyfin_connection", "radarr_enable_library_sync", "radarr_enable_queue_addition", "radarr_dry_run",
    "sonarr_enable_library_sync", "sonarr_enable_queue_addition", "sonarr_dry_run", "enable_notifications",
    "payload_logging_enabled", "log_service_jellyfin", "log_service_radarr", "log_service_sonarr",
    "log_service_tmdb", "log_service_dynu",
}
_INT_RANGES = {
    "scraper_interval_hours": (0, 8760), "letterboxd_interval_hours": (0, 8760), "serializd_interval_hours": (0, 8760),
    "radarr_interval_hours": (0, 8760), "sonarr_interval_hours": (0, 8760), "jellyfin_interval_hours": (0, 8760),
    "radarr_min_user_count": (1, 1000), "sonarr_min_user_count": (1, 1000),
    "radarr_quality_profile_id": (1, 1_000_000), "sonarr_quality_profile_id": (1, 1_000_000),
    "payload_retention_limit": (1, 100_000), "metadata_refresh_hours": (1, 8760),
}
_FLOAT_KEYS = {"request_delay_min", "request_delay_max", "user_delay_min", "user_delay_max"}
_URL_KEYS = {"jellyfin_url", "radarr_url", "sonarr_url"}
_PATH_KEYS = {"radarr_root_folder", "sonarr_root_folder"}
_CHOICES = {"tv_metadata_provider": {"tmdb", "sonarr"}}

# The Servarr dropdowns submit this text if loading their options never finished.
_PLACEHOLDER = "Loading..."


def validate_setting(key, value):
    """Returns (ok, cleaned_value_or_error_message) for a submitted setting."""
    value = (value or "").strip()

    if key in _SWITCH_KEYS:
        return (True, value) if value in ("0", "1") else (False, f"{key} must be 0 or 1.")

    if key in _INT_RANGES:
        low, high = _INT_RANGES[key]
        try:
            number = int(value)
        except ValueError:
            return False, f"{key} must be a whole number."
        if not low <= number <= high:
            return False, f"{key} must be between {low} and {high}."
        return True, str(number)

    if key in _FLOAT_KEYS:
        try:
            number = float(value)
        except ValueError:
            return False, f"{key} must be a number."
        if number < 0:
            return False, f"{key} cannot be negative."
        return True, value

    if key in _URL_KEYS:
        if not value:
            return True, ""
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return True, value
        return False, f"{key} must be a valid http(s) URL."

    if key in _PATH_KEYS:
        if not value or value == _PLACEHOLDER:
            return False, f"{key} must be a folder path."
        return True, value

    if key in _CHOICES:
        return (True, value) if value in _CHOICES[key] else (False, f"{key} must be one of {sorted(_CHOICES[key])}.")

    return True, value
