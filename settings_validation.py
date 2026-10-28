# settings_validation.py
from urllib.parse import urlparse

_SWITCH_KEYS = {
    "disable_jellyfin_connection", "radarr_enable_library_sync", "radarr_enable_queue_addition", "radarr_dry_run",
    "sonarr_enable_library_sync", "sonarr_enable_queue_addition", "sonarr_dry_run", "enable_notifications",
    "payload_logging_enabled", "dvr_enabled", "size_policy", "tv_size_policy", "gpu_encoding", "preview_auto", "log_service_jellyfin", "log_service_radarr", "log_service_sonarr",
    "log_service_tmdb", "log_service_dynu",
}
_INT_RANGES = {
    "scraper_interval_hours": (0, 8760), "letterboxd_interval_hours": (0, 8760), "serializd_interval_hours": (0, 8760),
    "radarr_interval_hours": (0, 8760), "sonarr_interval_hours": (0, 8760), "jellyfin_interval_hours": (0, 8760),
    "radarr_min_user_count": (1, 1000), "sonarr_min_user_count": (1, 1000),
    "radarr_quality_profile_id": (1, 1_000_000), "sonarr_quality_profile_id": (1, 1_000_000),
    "payload_retention_limit": (1, 100_000), "metadata_refresh_hours": (1, 8760), "queue_poll_seconds": (0, 3600), "dvr_poll_seconds": (0, 86400),
    "library_tail_seconds": (5, 600), "jellyfin_events_keep_days": (0, 36500), "notify_history_keep_days": (0, 36500), "ddns_interval_minutes": (1, 60), "access_session_days": (1, 365), "backup_keep": (0, 365), "size_crf": (14, 35), "trust_after": (1, 50), "preview_seconds": (30, 60), "preview_keep_days": (1, 365), "originals_retention_days": (0, 3650),
    "library_default_profile": (1, 1_000_000),
}
_FLOAT_KEYS = {"request_delay_min", "request_delay_max", "user_delay_min", "user_delay_max", "movie_max_gb", "movie_target_gb", "tv_max_gb_per_hour", "tv_target_gb_per_hour"}
_URL_KEYS = {"jellyfin_url", "radarr_url", "sonarr_url", "synchub_url"}
_PATH_KEYS = {"radarr_root_folder", "sonarr_root_folder"}
_CHOICES = {"tv_metadata_provider": {"tmdb", "sonarr"}, "library_integrity_mode": {"metadata", "tail", "full"}, "download_pipeline": {"off", "review", "auto"}, "vmaf_mode": {"off", "warn", "hold"}, "size_preset": {"fast", "medium", "slow", "slower"}, "review_first": {"always", "until_trusted", "low_confidence_only", "never"}, "dolby_vision_profile_5": {"review", "hdr10"}, "dolby_vision_profile_7_8": {"review", "hdr10"},
            "source_delete_after_days": {"0", "7", "14", "30"}, "arr_after_migration": {"none", "update_location", "unmonitor", "remove"}, "arr_apply_when": {"after_copy", "with_source_delete"}}
_FOLDER_NAME_KEYS = {"library_tv_folder", "library_movie_folder"}

# The Servarr dropdowns submit this text if loading their options never finished.
_PLACEHOLDER = "Loading..."


def validate_setting(key, value):
    """Returns (ok, cleaned_value_or_error_message) for a submitted setting."""
    value = (value or "").strip()

    if key == "notify_admin_profiles":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return (True, ",".join(parts)) if all(p.isdigit() for p in parts) else (False, "Choose administrators from the list.")

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

    if key in _FOLDER_NAME_KEYS:
        bad = set('<>:"/\\|?*') & set(value)
        if not value or bad or value != value.strip() or value.endswith(".") or any(ord(c) < 32 for c in value) or len(value) > 80:
            return False, f"{key} must be a folder name without / \\ : * ? \" < > | and not ending in a dot."
        return True, value

    if key == "library_destination_root":
        import re
        value = value.lower()
        return (True, value) if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value) else (False, "The destination share is one of the names on the Storage page (lowercase letters, numbers, - and _).")

    if key == "library_exclude_folders":
        lines = [line.strip() for line in value.replace("\r", "").split("\n") if line.strip()]
        if any(len(line) > 200 or "\x00" in line for line in lines):
            return False, "Each excluded folder must be a name, a wildcard or share:folder, at most 200 characters."
        return True, "\n".join(lines)

    if key == "dvr_path_prefixes":
        parts = [p.strip().rstrip("/\\") for p in value.replace("\r", "").replace("\n", ",").split(",") if p.strip()]
        if any(not p.startswith("/") or "\x00" in p or len(p) > 200 for p in parts):
            return False, "dvr_path_prefixes must be folder paths as Jellyfin sees them, starting with /, separated by commas."
        return True, ",".join(parts)

    if key in _CHOICES:
        return (True, value) if value in _CHOICES[key] else (False, f"{key} must be one of {sorted(_CHOICES[key])}.")

    return True, value
