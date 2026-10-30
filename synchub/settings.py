"""Key/value settings stored in app_setting, with validation shared with the UI."""
from config import DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE
from settings_validation import validate_setting
from synchub import db

SECRET_FIELDS = ("tmdb_api_key", "jellyfin_api_key", "radarr_api_key", "sonarr_api_key", "apprise_urls", "dynu_api_key", "pushover_app_token")

DEFAULTS = {
    "scraper_interval_hours": "24", "radarr_interval_hours": "6", "sonarr_interval_hours": "6", "jellyfin_interval_hours": "6",
    "metadata_refresh_hours": "24", "queue_poll_seconds": "30", "synchub_url": "", "jellyfin_events_keep_days": "365", "ddns_interval_minutes": "1", "access_guest_permissions": "dashboard,tv", "guest_max_rating": "1", "access_session_days": "30", "backup_keep": "7",
    "request_delay_min": str(DEFAULT_REQUEST_DELAY_RANGE[0]), "request_delay_max": str(DEFAULT_REQUEST_DELAY_RANGE[1]),
    "user_delay_min": str(DEFAULT_USER_DELAY_RANGE[0]), "user_delay_max": str(DEFAULT_USER_DELAY_RANGE[1]),
    "disable_jellyfin_connection": "0", "jellyfin_url": "", "jellyfin_server_id": "", "radarr_url": "", "sonarr_url": "",
    "radarr_enable_queue_addition": "0", "radarr_dry_run": "0",
    "radarr_min_user_count": "1", "radarr_quality_profile_id": "1", "radarr_root_folder": "/movies",
    "sonarr_enable_queue_addition": "0", "sonarr_dry_run": "0",
    "sonarr_min_user_count": "1", "sonarr_quality_profile_id": "1", "sonarr_root_folder": "/tv",
    "dvr_enabled": "0", "dvr_poll_seconds": "300", "dvr_path_prefixes": "/DVR", "dvr_counts_as_library": "1",
    "enable_notifications": "0", "notify_admin_profiles": "", "notify_history_keep_days": "90", "payload_logging_enabled": "0", "payload_retention_limit": "500",
    "log_service_jellyfin": "0", "log_service_radarr": "0", "log_service_sonarr": "0", "log_service_tmdb": "0", "log_service_dynu": "0",
    # the Library: the one share everything is brought into, how it is laid out, and how scans look at files
    "library_destination_root": "media", "library_tv_folder": "TV Shows", "library_movie_folder": "Movies",
    "library_integrity_mode": "tail", "library_tail_seconds": "30",
    # downloads Radarr/Sonarr import: off, review (a person approves each one), auto (high-confidence plans run on their own); the original is kept on the worker this long
    "size_policy": "0", "movie_max_gb": "30", "movie_target_gb": "25", "tv_size_policy": "0", "tv_max_gb_per_hour": "4", "tv_target_gb_per_hour": "2.5", "gpu_encoding": "0", "vmaf_mode": "warn", "size_crf": "22", "size_preset": "medium",
    "dolby_vision_profile_5": "review", "dolby_vision_profile_7_8": "hdr10",
    # extra versions of a film (a smaller copy beside the original, shown by Jellyfin as another version) and Dolby Vision profile 7 to 8.1 (docs/DESIGN_ENCODING.md 13)
    "versions_on": "1", "variant_min_gb": "30", "variant_height": "1080", "variant_label": "1080p", "variant_crf": "23", "variant_maxrate_kbps": "8000", "variant_audio_kbps": "192",
    "variant_stereo": "1", "dv7_remux": "1",
    # compression by video bitrate (Mbps) per picture height, how far over the ceiling a file must be before it is touched, a size that is compressed whatever the bitrate,
    # whether names are cleaned while files are brought in, and what happens to audio (keep, add_compatible, compatible_only); a conversion profile can override all of them
    "size_mode": "bitrate", "cap_mbps_2160": "18", "cap_mbps_1080": "8", "cap_mbps_720": "4", "cap_mbps_sd": "2", "tv_cap_share": "0.5", "size_trigger_factor": "1.5", "safety_gb_2160": "45", "safety_gb_1080": "15",
    "clean_names": "1", "audio_mode": "add_compatible", "naming_style": "jellyfin", "naming_tmdb_ids": "0",
    "review_first": "always", "trust_after": "3", "preview_seconds": "45", "preview_keep_days": "7", "preview_auto": "1",
    "download_pipeline": "off", "originals_retention_days": "30",
    # conversion profile used when none is chosen for a file; the source file is deleted this many days after a verified migration (0 = never);
    # what Radarr/Sonarr are told about the move (none, update_location, unmonitor, remove) and when (right after the copy, or only when the source is deleted)
    "library_default_profile": "1", "source_delete_after_days": "0", "arr_after_migration": "none", "arr_apply_when": "after_copy",
    "library_exclude_folders": "@eaDir\n#recycle\n.Recycle.Bin\n$RECYCLE.BIN\nSystem Volume Information\nlost+found\n.Trash-*",
}


def get(key, default=None, conn=None):
    """Returns the stored value, else the supplied default, else the built-in default, else ''."""
    fallback = default if default is not None else DEFAULTS.get(key, "")

    def _read(c):
        row = c.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else fallback

    if conn is not None:
        return _read(conn)
    with db.connection() as c:
        return _read(c)


def set_value(key, value, conn=None):
    sql = "INSERT INTO app_setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    if conn is not None:
        conn.execute(sql, (key, str(value)))
        return
    with db.connection() as c:
        c.execute(sql, (key, str(value)))


def get_all():
    with db.connection() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM app_setting")}


def save_form(form):
    """Validates and stores submitted settings. Blank secrets keep the stored value. Returns a list of error messages."""
    errors = []
    for key in DEFAULTS:
        raw = form.get(key)
        if raw is None:
            continue
        ok, result = validate_setting(key, raw)
        if ok:
            set_value(key, result)
        else:
            errors.append(result)
    for key in SECRET_FIELDS:
        value = (form.get(key) or "").strip()
        if value:
            set_value(key, value)
    return errors
