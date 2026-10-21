# app.py
import csv
import io
import json
import os
import re
import secrets
import queue
import threading
import time
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from config import DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE, POSTER_DIR
from database import (
    get_media_catalog,
    get_catalog_item_by_slug,
    get_all_settings,
    get_setting,
    get_film_by_slug,
    get_all_task_statuses,
    get_tv_show_details,
    get_full_tv_show_episodes,
    get_movie_details_by_slug,
    get_user_profile_mappings,
    save_user_profile_mapping,
    delete_user_profile_mapping,
    toggle_profile_mapping,
    init_db,
    set_setting,
    update_film_identifiers,
    link_service_item_to_request,
    get_fuzzy_matches,
    get_unconfirmed_guesses,
    confirm_guessed_id,
    reject_guessed_id,
    get_ddns_cache,
    get_ddns_history,
    log_api_payload,
    logged_request,
    get_payload_logs,
    clear_payload_logs,
    get_connection
)
from jellyfin_integration import (
    _get_auth_headers,
    fetch_and_cache_poster,
    sync_jellyfin_playlist,
    get_jellyfin_users
)
from radarr_client import get_quality_profiles as radarr_quality, get_root_folders as radarr_root
from sonarr_client import get_quality_profiles as sonarr_quality, get_root_folders as sonarr_root
from ddns_updater import run_ddns_update, get_dynu_credentials, fetch_and_cache_dynu_metadata
from notifications import send_notification

from scheduler import (
    init_scheduler,
    reconfigure_scheduler,
    start_async_task,
    run_letterboxd_scraper_task,
    run_serializd_scraper_task,
    run_id_resolver_task,
    run_radarr_task,
    run_sonarr_task,
    run_jellyfin_movies_task,
    run_jellyfin_tv_task,
    run_disk_scan_task,
    run_all_sequential_pipeline,
)

from export_to_csv import generate_consolidated_csv_buffer

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

try:
    from flask_wtf import CSRFProtect
    csrf = CSRFProtect(app)
except ImportError:
    csrf = None
    app.logger.warning("flask-wtf is not installed: CSRF protection is OFF.")

init_db()
init_scheduler()

class MessageAnnouncer:
    def __init__(self):
        self.listeners = []
        self._lock = threading.Lock()
        
    def listen(self):
        q = queue.Queue(maxsize=5)
        with self._lock:
            self.listeners.append(q)
        return q
        
    def announce(self, msg):
        with self._lock:
            for i in reversed(range(len(self.listeners))):
                try:
                    self.listeners[i].put_nowait(msg)
                except queue.Full:
                    del self.listeners[i]

announcer = MessageAnnouncer()

def format_sse(data: str, event=None) -> str:
    msg = f"data: {data}\n\n"
    if event is not None:
        msg = f"event: {event}\n{msg}"
    return msg

@app.route("/api/stream/events")
def stream_events():
    def stream():
        messages = announcer.listen()
        while True:
            try:
                msg = messages.get(timeout=15)
                yield msg
            except queue.Empty:
                yield format_sse('{"status": "ping"}', event="ping")
    return Response(stream(), mimetype="text/event-stream")

@app.route("/")
def index():
    films = get_media_catalog()
    
    with get_connection() as conn:
        jf_tv_eps_count = conn.execute("SELECT COUNT(*) FROM service_cache WHERE service = 'jellyfin' AND media_type = 'episode' AND in_library = 1 AND status != 'jf_missing'").fetchone()[0]
        jf_movies_count = conn.execute("SELECT COUNT(*) FROM service_cache WHERE service = 'jellyfin' AND media_type = 'movie' AND in_library = 1 AND status != 'jf_missing'").fetchone()[0]

    movie_count = sum(1 for f in films if f["media_type"] == "movie")
    tv_count = sum(1 for f in films if f["media_type"] in ("tv", "episode"))
    matched_in_jellyfin_count = sum(1 for f in films if f["in_jellyfin"])
    radarr_count = sum(1 for f in films if f["in_radarr"])
    sonarr_count = sum(1 for f in films if f["in_sonarr"])
    
    all_users = set()
    for f in films:
        if f["in_jellyfin"] and not f.get("jellyfin_app_link"):
            f["jellyfin_app_link"] = f"jellyfin://details?id={f['jellyfin_id']}"
        for u in f["users_list"]:
            all_users.add(u)
            
    stats = {
        "total_films": len(films),
        "total_users": len(all_users),
        "jellyfin_count": matched_in_jellyfin_count,
        "missing_count": len(films) - matched_in_jellyfin_count,
        "movie_count": movie_count,
        "tv_count": tv_count,
        "radarr_count": radarr_count,
        "sonarr_count": sonarr_count,
        "jf_episodes_count": jf_tv_eps_count,
        "jf_combined_count": jf_tv_eps_count + jf_movies_count
    }
    return render_template("index.html", films=films, all_users=sorted(list(all_users)), stats=stats)

@app.route("/ddns", methods=["GET"])
def ddns_page():
    creds = get_dynu_credentials()
    ddns_status = {
        "last_update_status": get_setting("ddns_last_status", "UNKNOWN"),
        "last_ip": creds["last_ip"],
        "message": get_setting("ddns_last_message", ""),
        "last_run": get_setting("ddns_last_run", "")
    }
    return render_template(
        "ddns.html", dynu_hostname=creds["domain"], dynu_api_key=creds["api_key"],
        dynu_enabled=creds["enabled"], dynu_created_on=creds["created_on"],
        dynu_remote_updated_on=creds["remote_updated_on"], dynu_domain_state=creds["state"],
        dynu_ttl=creds["ttl"], dynu_dns_id=creds["dns_id"], dynu_record_id=creds["record_id"],
        ddns_status=ddns_status, ddns_history=get_ddns_history(limit=10)
    )

@app.route("/ddns/trigger-update", methods=["POST"])
def ddns_trigger_update():
    success, message = run_ddns_update(force=True)
    flash(message, "success" if success else "danger")
    return redirect(url_for("ddns_page"))

@app.route("/resolution")
def resolution_center():
    return render_template("resolution.html", suggestions=get_fuzzy_matches(), unconfirmed_guesses=get_unconfirmed_guesses(), unmatched_films=[f for f in get_media_catalog() if not f.get("tmdb_id") and not f.get("tvdb_id")])

@app.route("/api/resolution/merge", methods=["POST"])
def merge_resolution():
    data = request.get_json(silent=True) or {}
    slug, action = data.get("slug"), data.get("action")
    if not slug or not action: return jsonify({"ok": False, "message": "Missing slug or action"}), 400

    if action == "approve_guess":
        confirm_guessed_id(slug)
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Approved guessed ID for {slug}."})
    elif action == "reject_guess":
        reject_guessed_id(slug)
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Rejected guessed ID for {slug}."})

    ok = link_service_item_to_request(slug, action, data.get("service"), data.get("external_id"), data.get("tmdb_id"), data.get("tvdb_id"))
    if ok:
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Updated match resolution for {slug}."})
    return jsonify({"ok": False, "message": "Failed updating match resolution."}), 400

@app.route("/poster/<slug>")
def get_poster(slug):
    poster_filename = f"{slug}.jpg"
    if (POSTER_DIR / poster_filename).exists():
        return send_from_directory(POSTER_DIR, poster_filename, max_age=86400)

    film = get_catalog_item_by_slug(slug)
    if film:
        fetch_and_cache_poster(slug, film.get("jellyfin_id"), film.get("tmdb_id"), film.get("media_type", "movie"))
        if (POSTER_DIR / poster_filename).exists():
            return send_from_directory(POSTER_DIR, poster_filename, max_age=86400)

    if os.path.exists(os.path.join(app.static_folder, "placeholder.jpg")):
        return send_from_directory(app.static_folder, "placeholder.jpg")
    return ("Poster not found", 404)

@app.route("/tools")
def tools():
    return render_template(
        "tools.html", settings=get_all_settings(), job_status=json.loads(get_all_settings().get("job_status", "{}")),
        task_statuses=get_all_task_statuses(), unmatched_films=[f for f in get_media_catalog() if not f.get("tmdb_id") and not f.get("tvdb_id")],
    )

@app.route("/settings")
def settings_page():
    return render_template("settings.html", settings=get_all_settings(), profiles=get_user_profile_mappings())

@app.route("/payloads")
def payloads_page():
    return render_template("payloads.html", settings=get_all_settings())

@app.route("/logs")
def logs_page():
    return render_template("logs.html")

SECRET_FIELDS = ("tmdb_api_key", "jellyfin_api_key", "radarr_api_key", "sonarr_api_key", "apprise_urls", "dynu_password", "dynu_api_key")
_TOKEN_RE = re.compile(r"(?i)\b(api_?key|token|apikey|password)=([^&\s]+)")

def _known_secrets():
    found = set()
    for key in SECRET_FIELDS:
        value = get_setting(key, "") or ""
        for piece in [value] + re.split(r"[,\s]+", value):
            if len(piece) >= 6: found.add(piece)
    return sorted(found, key=len, reverse=True)

def _redact(line, secret_values):
    for secret in secret_values:
        line = line.replace(secret, "***")
    return _TOKEN_RE.sub(r"\1=***", line)

@app.route("/api/logs")
def api_logs():
    try: tail = max(1, min(int(request.args.get("tail", 300)), 5000))
    except ValueError: tail = 300
    log_file = os.path.join("logs", "app.log")
    lines = []
    if os.path.exists(log_file):
        try:
            secret_values = _known_secrets()
            from collections import deque
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = [_redact(line.strip(), secret_values) for line in deque(f, maxlen=tail)]
        except Exception:
            lines = ["Unable to read log file."]
    return jsonify({"logs": lines})

@app.route("/api/tasks")
def api_tasks():
    tasks = get_all_task_statuses() or {}
    return jsonify(tasks={key: (dict(value) if value else {}) for key, value in tasks.items()})

def _http_url(value):
    parsed = urlparse(value or "")
    return value.rstrip("/") if parsed.scheme in ("http", "https") and parsed.netloc else None

@app.route("/api/test-connection/<service>", methods=["POST"])
def api_test_connection(service):
    fields = (request.get_json(silent=True) or {}).get("fields", {})

    def val(name): return (fields.get(name) or "").strip() or (get_setting(name, "") or "")
    def result(ok, message): return jsonify(ok=ok, message=message)

    try:
        if service == "tmdb":
            key = val("tmdb_api_key")
            if not key: return result(False, "No TMDB API key set.")
            headers, params = {}, {}
            if key.startswith("eyJ"): headers["Authorization"] = f"Bearer {key}"
            else: params["api_key"] = key
            r = logged_request("tmdb", "TestConnection", "GET", "https://api.themoviedb.org/3/configuration", headers=headers, params=params, timeout=8)
            return result(r.ok, "TMDB key accepted." if r.ok else f"TMDB rejected key (HTTP {r.status_code}).")

        if service in ("jellyfin", "radarr", "sonarr"):
            url = _http_url(val(f"{service}_url"))
            key = val(f"{service}_api_key")
            if not url: return result(False, "Enter a valid http(s) URL.")
            if not key: return result(False, "No API key set.")
            if service == "jellyfin":
                r = logged_request("jellyfin", "TestConnection", "GET", f"{url}/System/Info", headers=_get_auth_headers(key), timeout=8)
            else:
                r = logged_request(service, "TestConnection", "GET", f"{url}/api/v3/system/status", headers={"X-Api-Key": key}, timeout=8)
            if r.status_code in (401, 403): return result(False, "Server rejected API key.")
            if not r.ok: return result(False, f"Unexpected response (HTTP {r.status_code}).")
            info = r.json()
            return result(True, f"Connected to {info.get('ServerName') or info.get('appName') or service.title()} {info.get('Version') or info.get('version') or ''}".strip())

        if service == "notifications":
            if not [u for u in re.split(r"[,\s]+", val("apprise_urls")) if u]: return result(False, "No Apprise URLs set.")
            ok = send_notification("SyncHub Test", "Test notification from SyncHub.")
            return result(bool(ok), "Notification sent." if ok else "Sending failed.")
        return result(False, "Unknown service."), 404
    except Exception as exc:
        return result(False, f"Could not connect: {exc.__class__.__name__}")

@app.route("/api/servarr/<service>/quality-profiles", methods=["POST"])
def api_servarr_quality_profiles(service):
    data = request.get_json(silent=True) or {}
    url, key = data.get("url"), data.get("api_key")
    try:
        if service == "radarr": return jsonify(radarr_quality(url, key))
        elif service == "sonarr": return jsonify(sonarr_quality(url, key))
        return jsonify([]), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/servarr/<service>/root-folders", methods=["POST"])
def api_servarr_root_folders(service):
    data = request.get_json(silent=True) or {}
    url, key = data.get("url"), data.get("api_key")
    try:
        if service == "radarr": return jsonify(radarr_root(url, key))
        elif service == "sonarr": return jsonify(sonarr_root(url, key))
        return jsonify([]), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/jellyfin/users", methods=["GET"])
def api_jellyfin_users():
    return jsonify(get_jellyfin_users())

@app.route("/tools/fix-match", methods=["POST"])
def fix_match():
    slug = (request.form.get("movie_slug") or "").strip()
    tmdb_id, imdb_id, tvdb_id = (request.form.get("tmdb_id") or "").strip(), (request.form.get("imdb_id") or "").strip(), (request.form.get("tvdb_id") or "").strip()
    error = None

    if not slug: error = "Choose an item first."
    elif not (tmdb_id or imdb_id or tvdb_id): error = "Enter at least one ID."
    elif (tmdb_id and not tmdb_id.isdigit()) or (tvdb_id and not tvdb_id.isdigit()): error = "IDs must be numbers."

    if error: flash(error, "danger")
    else:
        update_film_identifiers(slug, tmdb_id, imdb_id, tvdb_id)
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        flash(f"Updated metadata IDs for {slug}", "success")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-radarr-sync", methods=["POST"])
def trigger_radarr_sync(): start_async_task(run_radarr_task); flash("Radarr sync initiated in background.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-sonarr-sync", methods=["POST"])
def trigger_sonarr_sync(): start_async_task(run_sonarr_task); flash("Sonarr sync initiated in background.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-jellyfin-movies", methods=["POST"])
def trigger_jellyfin_movies(): start_async_task(run_jellyfin_movies_task); flash("Jellyfin movie sync initiated.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-jellyfin-tv", methods=["POST"])
def trigger_jellyfin_tv(): start_async_task(run_jellyfin_tv_task); flash("Jellyfin TV sync initiated.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-id-resolver", methods=["POST"])
def trigger_id_resolver(): start_async_task(run_id_resolver_task); flash("ID Resolver initiated.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-all", methods=["POST"])
def trigger_all(): start_async_task(run_all_sequential_pipeline); flash("Sequential pipeline initiated.", "info"); return redirect(url_for("tools"))

@app.route("/tools/test-notification", methods=["POST"])
def test_notification():
    success = send_notification("SyncHub Test", "Test notification from Letterboxd & Serializd SyncHub!")
    flash("Notification sent!" if success else "Failed to send.", "success" if success else "danger")
    return redirect(url_for("tools"))

SETTING_DEFAULTS = {
    "letterboxd_interval_hours": "6", "serializd_interval_hours": "6", "scraper_interval_hours": "24",
    "radarr_interval_hours": "6", "sonarr_interval_hours": "6", "jellyfin_interval_hours": "6",
    "request_delay_min": str(DEFAULT_REQUEST_DELAY_RANGE[0]), "request_delay_max": str(DEFAULT_REQUEST_DELAY_RANGE[1]),
    "user_delay_min": str(DEFAULT_USER_DELAY_RANGE[0]), "user_delay_max": str(DEFAULT_USER_DELAY_RANGE[1]),
    "disable_jellyfin_connection": "0", "jellyfin_url": "", "radarr_enable_library_sync": "1",
    "radarr_enable_queue_addition": "0", "radarr_url": "", "radarr_min_user_count": "1",
    "radarr_quality_profile_id": "1", "radarr_root_folder": "/movies", "radarr_dry_run": "0",
    "sonarr_enable_library_sync": "1", "sonarr_enable_queue_addition": "0", "sonarr_url": "",
    "sonarr_min_user_count": "1", "sonarr_quality_profile_id": "1", "sonarr_root_folder": "/tv",
    "sonarr_dry_run": "0", "enable_notifications": "0", "payload_logging_enabled": "0",
    "payload_retention_limit": "500", "log_service_jellyfin": "0", "log_service_radarr": "0",
    "log_service_sonarr": "0", "log_service_tmdb": "0", "log_service_dynu": "0",
    "tv_metadata_provider": "tmdb"
}

@app.route("/tools/update-settings", methods=["POST"])
def update_settings():
    for key, default in SETTING_DEFAULTS.items():
        val = request.form.get(key)
        if val is not None: set_setting(key, val.strip())

    for key in SECRET_FIELDS:
        value = (request.form.get(key) or "").strip()
        if value: set_setting(key, value)

    reconfigure_scheduler()
    flash("Settings updated successfully.", "success")
    if request.referrer and "payloads" in request.referrer: return redirect(url_for("payloads_page"))
    return redirect(url_for("settings_page"))

@app.route("/tools/sync-playlist", methods=["POST"])
def trigger_playlist_sync():
    playlist_name = request.form.get("playlist_name", "Letterboxd Community Watchlist")
    item_ids = list(dict.fromkeys([f["jellyfin_id"] for f in get_media_catalog() if f.get("in_jellyfin") and f.get("jellyfin_id")]))
    success, message = sync_jellyfin_playlist(playlist_name, item_ids)
    flash(message, "success" if success else "danger")
    return redirect(url_for("tools"))

@app.route("/tools/export-csv", methods=["POST", "GET"])
def export_csv():
    csv_buffer = generate_consolidated_csv_buffer()
    mem = io.BytesIO()
    mem.write(csv_buffer.getvalue().encode('utf-8'))
    mem.seek(0)
    csv_buffer.close()
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name='letterboxd_sync_export.csv')

@app.route("/tools/trigger-disk-scan", methods=["POST"])
def trigger_disk_scan(): start_async_task(run_disk_scan_task); flash("Disk scanner initiated.", "info"); return redirect(url_for("tools"))

@app.route("/api/webhook/<service>", methods=["POST"])
def api_webhook(service):
    start_time = time.time()
    payload = request.get_json(silent=True) or {}
    event_type = payload.get('eventType') or payload.get('NotificationType') or 'WebhookTrigger'
    
    if service == "jellyfin":
        from jellyfin_integration import handle_playback_reporting
        handle_playback_reporting(payload)
    
    if service in ("radarr", "sonarr") and event_type in ("Grab", "Download"):
        sse_data = {
            "service": service, "event": event_type,
            "tvdb_id": payload.get("series", {}).get("tvdbId"),
            "tmdb_id": payload.get("movie", {}).get("tmdbId"),
            "queue_status": "Downloading" if event_type == "Grab" else "Downloaded"
        }
        announcer.announce(format_sse(json.dumps(sse_data)))

    log_api_payload("IN", service, event_type, f"/api/webhook/{service}", 200, int((time.time() - start_time) * 1000), dict(request.headers), payload, {"status": "received"})
    return jsonify({"status": "received", "service": service}), 200

@app.route("/api/payload-logs")
def get_api_payload_logs():
    return jsonify(get_payload_logs(limit=request.args.get("limit", 100), service=request.args.get("service"), errors_only=request.args.get("errors_only") == "1"))

@app.route("/api/payload-logs/clear", methods=["POST"])
def clear_api_payload_logs(): clear_payload_logs(); return jsonify({"ok": True})

@app.route("/api/request-seasons", methods=["POST"])
def api_request_seasons():
    data = request.get_json(silent=True) or {}
    if not data.get("tvdb_id") or not isinstance(data.get("seasons", []), list):
        return jsonify({"ok": False, "message": "Missing tvdb_id or seasons array."}), 400
    from automation import request_tv_seasons
    res = request_tv_seasons(data["tvdb_id"], data["seasons"])
    if res.get("status") in ("added", "updated"):
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Monitored seasons {data['seasons']}."})
    return jsonify({"ok": False, "message": res.get("message", "Request failed.")}), 500

@app.route("/api/show/<identifier>/episodes", methods=["GET"])
def api_get_show_episodes(identifier):
    episodes = get_full_tv_show_episodes(identifier)
    return jsonify({"ok": True, "seasons": episodes})

@app.route("/api/request-episodes", methods=["POST"])
def api_request_episodes():
    data = request.get_json(silent=True) or {}
    tvdb_id = data.get("tvdb_id")
    episode_ids = data.get("episode_ids")
    episodes_spec = data.get("episodes")
    search = data.get("search", True)

    if not tvdb_id or (not episode_ids and not episodes_spec):
        return jsonify({"ok": False, "message": "Missing tvdb_id or episode selection."}), 400

    from automation import request_tv_episodes
    res = request_tv_episodes(tvdb_id, episode_ids=episode_ids, episodes_spec=episodes_spec, search=search)
    if res.get("status") == "updated":
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": res.get("message")})
    return jsonify({"ok": False, "message": res.get("message", "Request failed.")}), 500

@app.route("/settings/profiles/save", methods=["POST"])
def save_profile():
    profile_name = request.form.get("profile_name", "").strip()
    if profile_name:
        save_user_profile_mapping(profile_name, request.form.get("letterboxd_username", "").strip(), request.form.get("serializd_username", "").strip(), request.form.get("jellyfin_user_id", "").strip(), 1)
        flash(f"Profile mapping for '{profile_name}' saved.", "success")
    else: flash("Profile name is required.", "danger")
    return redirect(url_for("settings_page"))

@app.route("/api/profiles/toggle", methods=["POST"])
def api_toggle_profile():
    data = request.get_json()
    toggle_profile_mapping(data["id"], data["is_active"])
    return jsonify({"ok": True})

@app.route("/settings/profiles/delete/<int:profile_id>", methods=["POST"])
def delete_profile(profile_id):
    delete_user_profile_mapping(profile_id)
    flash("Profile mapping removed.", "info")
    return redirect(url_for("settings_page"))

@app.route("/tools/trigger-letterboxd-scraper", methods=["POST"])
def trigger_letterboxd_scraper(): start_async_task(run_letterboxd_scraper_task); flash("Letterboxd scraper initiated.", "info"); return redirect(url_for("tools"))

@app.route("/tools/trigger-serializd-scraper", methods=["POST"])
def trigger_serializd_scraper(): start_async_task(run_serializd_scraper_task); flash("Serializd scraper initiated.", "info"); return redirect(url_for("tools"))

@app.route("/movie/<slug>")
def movie_details(slug):
    movie = get_movie_details_by_slug(slug)
    if not movie: flash("Movie not found in catalog.", "warning"); return redirect(url_for('index'))
    return render_template("movie_details.html", movie=movie)

@app.route("/api/show/<identifier>/season/<int:season_num>", methods=["GET"])
def api_get_season_episodes(identifier, season_num):
    from database import get_season_episodes_lazy
    episodes = get_season_episodes_lazy(identifier, season_num)
    return jsonify({"ok": True, "season": season_num, "episodes": episodes})

@app.route("/show/<identifier>")
def show_details(identifier):
    show, seasons = get_tv_show_details(identifier)
    if not show: flash("TV Show not found in catalog.", "warning"); return redirect(url_for('index'))
    return render_template("show_details.html", show=show, seasons=seasons)

@app.route("/ddns/update-settings", methods=["POST"])
def ddns_update_settings():
    domain = (request.form.get("dynu_hostname") or "").strip()
    apikey = (request.form.get("dynu_api_key") or "").strip()
    enabled = "1" if request.form.get("dynu_enabled") == "1" else "0"
    old_domain, old_key = get_setting("dynu_domain"), get_setting("dynu_api_key")

    set_setting("dynu_domain", domain)
    set_setting("dynu_api_key", apikey)
    set_setting("dynu_enabled", enabled)

    if domain != old_domain or apikey != old_key:
        set_setting("dynu_dns_id", "")
        set_setting("dynu_record_id", "")
        if domain and apikey: fetch_and_cache_dynu_metadata(domain, apikey)

    flash("DDNS configuration saved successfully.", "success")
    return redirect(url_for("ddns_page"))

if csrf: csrf.exempt(api_webhook)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")