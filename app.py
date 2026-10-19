# app.py
import csv
import io
import json
import os
import re
import secrets
import queue
import hashlib
from collections import deque
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

from database import get_setting, set_setting, get_ddns_history
from ddns_updater import run_ddns_update, get_dynu_credentials, fetch_and_cache_dynu_metadata

from automation import run_radarr_automation_worker, run_sonarr_automation_worker
from config import DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE, POSTER_DIR
from database import (
    get_media_catalog,
    get_all_settings,
    get_setting,
    get_film_by_slug,
    get_all_task_statuses,
    get_tv_show_details,
    get_movie_details_by_slug,
    init_db,
    set_setting,
    update_film_identifiers,
    get_fuzzy_matches,
    get_ddns_cache,
)
from jellyfin_integration import (
    fetch_and_cache_poster,
    sync_jellyfin_playlist,
)
from ddns_updater import run_ddns_update, get_dynu_credentials
from notifications import send_notification

from scheduler import (
    init_scheduler,
    reconfigure_scheduler,
    start_async_task,
    run_letterboxd_scraper_task,
    run_serializd_scraper_task,
    run_radarr_task,
    run_sonarr_task,
    run_jellyfin_movies_task,
    run_jellyfin_tv_task,
    run_all_sequential_pipeline,
    trigger_async_task,
)

#from projects.letterboxd_scraper.letterboxd_scraper import run_all_scrapers
from export_to_csv import generate_consolidated_csv_buffer
from disk_scanner import scan_local_disk_directory as run_disk_scan

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

# SSE Message Announcer Pattern
class MessageAnnouncer:
    def __init__(self):
        self.listeners = []
        
    def listen(self):
        q = queue.Queue(maxsize=5)
        self.listeners.append(q)
        return q
        
    def announce(self, msg):
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
            msg = messages.get()
            yield msg
    return Response(stream(), mimetype="text/event-stream")

@app.route("/")
def index():
    films = get_media_catalog()
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

    history = get_ddns_history(limit=10)

    return render_template(
        "ddns.html",
        dynu_hostname=creds["domain"],
        dynu_api_key=creds["api_key"],
        dynu_enabled=creds["enabled"],
        dynu_created_on=creds["created_on"],
        dynu_remote_updated_on=creds["remote_updated_on"],
        dynu_domain_state=creds["state"],
        dynu_ttl=creds["ttl"],
        dynu_dns_id=creds["dns_id"],
        dynu_record_id=creds["record_id"],
        ddns_status=ddns_status,
        ddns_history=history
    )

@app.route("/ddns/trigger-update", methods=["POST"])
def ddns_trigger_update():
    success, message = run_ddns_update(force=True)
    if success:
        flash(message, "success")
    else:
        flash(message, "danger")
    return redirect(url_for("ddns_page"))

@app.route("/resolution")
def resolution_center():
    suggestions = get_fuzzy_matches()
    unmatched_films = [f for f in get_media_catalog() if not f.get("tmdb_id") and not f.get("tvdb_id")]
    return render_template("resolution.html", suggestions=suggestions, unmatched_films=unmatched_films)

@app.route("/api/resolution/merge", methods=["POST"])
def merge_resolution():
    data = request.get_json(silent=True) or {}
    slug = data.get("slug")
    action = data.get("action")
    tmdb_id = data.get("tmdb_id")
    tvdb_id = data.get("tvdb_id")

    if not slug or not action:
        return jsonify({"ok": False, "message": "Missing slug or action payload"}), 400

    if action == "approve":
        update_film_identifiers(slug, tmdb_id=tmdb_id, tvdb_id=tvdb_id, set_ignore=False)
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Successfully linked IDs to {slug}."})
        
    elif action == "dismiss":
        update_film_identifiers(slug, set_ignore=True)
        return jsonify({"ok": True, "message": f"Successfully ignored match for {slug}."})

    return jsonify({"ok": False, "message": "Invalid action."}), 400


@app.route("/poster/<slug>")
def get_poster(slug):
    poster_filename = f"{slug}.jpg"
    if (POSTER_DIR / poster_filename).exists():
        return send_from_directory(POSTER_DIR, poster_filename, max_age=86400)

    jellyfin_id = None
    tmdb_id = None
    
    catalog = get_media_catalog()
    film = next((f for f in catalog if f["movie_slug"] == slug), None)
    if film:
        jellyfin_id = film.get("jellyfin_id")
        tmdb_id = film.get("tmdb_id")

    fetch_and_cache_poster(slug, jellyfin_id, tmdb_id)
    if (POSTER_DIR / poster_filename).exists():
        return send_from_directory(POSTER_DIR, poster_filename, max_age=86400)

    if os.path.exists(os.path.join(app.static_folder, "placeholder.jpg")):
        return send_from_directory(app.static_folder, "placeholder.jpg")

    return ("Poster not found", 404)

@app.route("/tools")
def tools():
    settings = get_all_settings()
    job_status = json.loads(settings.get("job_status", "{}"))
    task_statuses = get_all_task_statuses()
    unmatched_films = [f for f in get_media_catalog() if not f.get("tmdb_id") and not f.get("tvdb_id")]

    return render_template(
        "tools.html",
        settings=settings,
        job_status=job_status,
        task_statuses=task_statuses,
        unmatched_films=unmatched_films,
    )

@app.route("/settings")
def settings_page():
    settings = get_all_settings()
    return render_template("settings.html", settings=settings)

@app.route("/logs")
def logs_page():
    return render_template("logs.html")

SECRET_FIELDS = ("tmdb_api_key", "jellyfin_api_key", "radarr_api_key", "sonarr_api_key", "apprise_urls", "dynu_password")
_TOKEN_RE = re.compile(r"(?i)\b(api_?key|token|apikey|password)=([^&\s]+)")

def _known_secrets():
    found = set()
    for key in SECRET_FIELDS:
        value = get_setting(key, "") or ""
        for piece in [value] + re.split(r"[,\s]+", value):
            if len(piece) >= 6:
                found.add(piece)
    return sorted(found, key=len, reverse=True)

def _redact(line, secret_values):
    for secret in secret_values:
        line = line.replace(secret, "***")
    return _TOKEN_RE.sub(r"\1=***", line)

@app.route("/api/logs")
def api_logs():
    try:
        tail = max(1, min(int(request.args.get("tail", 300)), 5000))
    except ValueError:
        tail = 300
    log_file = os.path.join("logs", "app.log")
    lines = []
    if os.path.exists(log_file):
        try:
            secret_values = _known_secrets()
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
    import requests
    fields = (request.get_json(silent=True) or {}).get("fields", {})

    def val(name):
        return (fields.get(name) or "").strip() or (get_setting(name, "") or "")

    def result(ok, message):
        return jsonify(ok=ok, message=message)

    try:
        if service == "tmdb":
            key = val("tmdb_api_key")
            if not key: return result(False, "No TMDB API key set.")
            headers, params = {}, {}
            if key.startswith("eyJ"): headers["Authorization"] = f"Bearer {key}"
            else: params["api_key"] = key
            r = requests.get("https://api.themoviedb.org/3/configuration", headers=headers, params=params, timeout=8)
            return result(r.ok, "TMDB key accepted." if r.ok else f"TMDB rejected the key (HTTP {r.status_code}).")

        if service in ("jellyfin", "radarr", "sonarr"):
            url = _http_url(val(f"{service}_url"))
            key = val(f"{service}_api_key")
            if not url: return result(False, "Enter a valid http(s) URL.")
            if not key: return result(False, "No API key set.")

            if service == "jellyfin":
                r = requests.get(f"{url}/System/Info", headers={"X-Emby-Token": key}, timeout=8)
            else:
                r = requests.get(f"{url}/api/v3/system/status", headers={"X-Api-Key": key}, timeout=8)

            if r.status_code in (401, 403): return result(False, "The server rejected the API key.")
            if not r.ok: return result(False, f"Unexpected response (HTTP {r.status_code}).")
            info = r.json()
            name = info.get("ServerName") or info.get("appName") or service.title()
            return result(True, f"Connected to {name} {info.get('Version') or info.get('version') or ''}".strip())

        if service == "notifications":
            urls = [u for u in re.split(r"[,\s]+", val("apprise_urls")) if u]
            if not urls: return result(False, "No Apprise URLs set.")
            try:
                import apprise
            except ImportError:
                ok = send_notification("SyncHub Test", "Test notification from SyncHub.")
                return result(bool(ok), "Sent using the saved settings." if ok else "Sending failed.")

            sender = apprise.Apprise()
            if not sender.add(urls): return result(False, "None of the URLs are valid Apprise URLs.")
            ok = sender.notify(title="SyncHub Test", body="Test notification from SyncHub.")
            return result(bool(ok), "Notification sent." if ok else "Sending failed. Check the URLs.")

        return result(False, "Unknown service."), 404
    except requests.RequestException as exc:
        return result(False, f"Could not connect: {exc.__class__.__name__}")
    except ValueError:
        return result(False, "The server returned an unexpected response.")

@app.route("/tools/fix-match", methods=["POST"])
def fix_match():
    slug = (request.form.get("movie_slug") or "").strip()
    tmdb_id = (request.form.get("tmdb_id") or "").strip()
    imdb_id = (request.form.get("imdb_id") or "").strip()
    tvdb_id = (request.form.get("tvdb_id") or "").strip()
    error = None

    if not slug: error = "Choose an item first."
    elif not (tmdb_id or imdb_id or tvdb_id): error = "Enter at least one ID (TMDB, IMDb or TVDB)."
    elif (tmdb_id and not tmdb_id.isdigit()) or (tvdb_id and not tvdb_id.isdigit()): error = "TMDB and TVDB IDs must be numbers."
    elif imdb_id and not re.fullmatch(r"tt\d{6,}", imdb_id): error = "IMDb IDs look like tt0092099."

    if error:
        flash(error, "danger")
    else:
        update_film_identifiers(slug, tmdb_id, imdb_id, tvdb_id)
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        flash(f"Updated metadata IDs for {slug}", "success")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-radarr-sync", methods=["POST"])
def trigger_radarr_sync():
    start_async_task(run_radarr_task)
    flash("Radarr movie automation search initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-sonarr-sync", methods=["POST"])
def trigger_sonarr_sync():
    start_async_task(run_sonarr_task)
    flash("Sonarr TV automation search initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-jellyfin-movies", methods=["POST"])
def trigger_jellyfin_movies():
    start_async_task(run_jellyfin_movies_task)
    flash("Jellyfin movie catalog sync initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-jellyfin-tv", methods=["POST"])
def trigger_jellyfin_tv():
    start_async_task(run_jellyfin_tv_task)
    flash("Jellyfin TV show catalog sync initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-all", methods=["POST"])
def trigger_all():
    start_async_task(run_all_sequential_pipeline)
    flash("Sequential pipeline ('Run All') initiated. Tasks are queued.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/test-notification", methods=["POST"])
def test_notification():
    success = send_notification("SyncHub Test", "Test notification from Letterboxd & Serializd SyncHub!")
    if success: flash("Push notification dispatched successfully!", "success")
    else: flash("Failed to send notification. Verify Apprise/Pushover settings.", "danger")
    return redirect(url_for("tools"))

SETTING_DEFAULTS = {
    "letterboxd_interval_hours": "6",
    "serializd_interval_hours": "6",
    "scraper_interval_hours": "6",
    "radarr_interval_hours": "6",
    "sonarr_interval_hours": "6",
    "jellyfin_interval_hours": "6",
    "request_delay_min": str(DEFAULT_REQUEST_DELAY_RANGE[0]),
    "request_delay_max": str(DEFAULT_REQUEST_DELAY_RANGE[1]),
    "user_delay_min": str(DEFAULT_USER_DELAY_RANGE[0]),
    "user_delay_max": str(DEFAULT_USER_DELAY_RANGE[1]),
    "disable_jellyfin_connection": "0",
    "jellyfin_url": "",
    "radarr_enable_library_sync": "1",
    "radarr_enable_queue_addition": "0",
    "radarr_url": "",
    "radarr_min_user_count": "1",
    "sonarr_enable_library_sync": "1",
    "sonarr_enable_queue_addition": "0",
    "sonarr_url": "",
    "sonarr_min_user_count": "1",
    "enable_notifications": "0",
}

def _clean_username_list(raw):
    raw = (raw or "").strip()
    names = None
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list): names = [str(n).strip() for n in parsed]
        except ValueError:
            pass
    if names is None: names = [n.strip() for n in re.split(r"[\n,]+", raw)]
    names = list(dict.fromkeys(n for n in names if n))
    bad = next((n for n in names if not re.fullmatch(r"[\w.\-]+", n)), None)
    if bad: return None, f'"{bad}" is not a valid username.'
    return json.dumps(names), None

@app.route("/tools/update-settings", methods=["POST"])
def update_settings():
    problems = []
    for key in ("target_usernames", "target_serializd_usernames"):
        cleaned, error = _clean_username_list(request.form.get(key, "[]"))
        if error: problems.append(f"{key}: {error}")
        else: set_setting(key, cleaned)

    for key, default in SETTING_DEFAULTS.items():
        set_setting(key, (request.form.get(key, default) or default).strip())

    for key in SECRET_FIELDS:
        value = (request.form.get(key) or "").strip()
        if value: set_setting(key, value)

    reconfigure_scheduler()
    if problems: flash("Some settings were not saved. " + " ".join(problems), "danger")
    else: flash("Settings updated successfully.", "success")
    return redirect(url_for("settings_page"))

@app.route("/api/job-status")
def api_job_status():
    status_data = json.loads(get_setting("job_status", "{}"))
    return jsonify(status_data)

@app.route("/tools/sync-playlist", methods=["POST"])
def trigger_playlist_sync():
    playlist_name = request.form.get("playlist_name", "Letterboxd Community Watchlist")
    active_films = get_media_catalog()
    matching_item_ids = [film["jellyfin_id"] for film in active_films if film.get("in_jellyfin") and film.get("jellyfin_id")]
    matching_item_ids = list(dict.fromkeys(matching_item_ids))

    success, message = sync_jellyfin_playlist(playlist_name, matching_item_ids)
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
def trigger_disk_scan():
    try:
        run_disk_scan()
        flash("Disk scan completed successfully.", "success")
    except Exception as e:
        flash(f"Disk scan failed: {e}", "danger")
    return redirect(url_for("tools"))

@app.route("/api/webhook/<service>", methods=["POST"])
def api_webhook(service):
    payload = request.get_json(silent=True) or {}
    event_type = payload.get('eventType') or payload.get('NotificationType') or 'unknown'
    app.logger.info(f"Webhook received from {service}: {event_type}")
    
    if service == "jellyfin":
        from jellyfin_integration import handle_playback_reporting
        handle_playback_reporting(payload)
    
    if service in ("radarr", "sonarr") and event_type in ("Grab", "Download"):
        series = payload.get("series", {})
        movie = payload.get("movie", {})
        
        sse_data = {
            "service": service,
            "event": event_type,
            "tvdb_id": series.get("tvdbId"),
            "tmdb_id": movie.get("tmdbId"),
            "queue_status": "Downloading" if event_type == "Grab" else "Downloaded"
        }
        announcer.announce(format_sse(json.dumps(sse_data)))

    return jsonify({"status": "received", "service": service}), 200

@app.route("/api/request-seasons", methods=["POST"])
def api_request_seasons():
    data = request.get_json(silent=True) or {}
    tvdb_id = data.get("tvdb_id")
    seasons = data.get("seasons", [])
    
    if not tvdb_id or not isinstance(seasons, list):
        return jsonify({"ok": False, "message": "Missing tvdb_id or seasons array."}), 400
        
    from automation import request_tv_seasons
    res = request_tv_seasons(tvdb_id, seasons)
    
    if res.get("status") in ("added", "updated"):
        from database import reconcile_master_catalog
        reconcile_master_catalog()
        return jsonify({"ok": True, "message": f"Monitored seasons {seasons} in Sonarr."})
    else:
        return jsonify({"ok": False, "message": res.get("message", "Sonarr request failed.")}), 500

@app.route("/settings/profiles/save", methods=["POST"])
def save_profile():
    profile_name = request.form.get("profile_name", "").strip()
    letterboxd = request.form.get("letterboxd_username", "").strip()
    serializd = request.form.get("serializd_username", "").strip()
    jellyfin_id = request.form.get("jellyfin_user_id", "").strip()
    
    if profile_name:
        from database import save_user_profile_mapping
        save_user_profile_mapping(profile_name, letterboxd, serializd, jellyfin_id)
        flash(f"Profile mapping for '{profile_name}' saved.", "success")
    else:
        flash("Profile name is required.", "danger")
    return redirect(url_for("settings_page"))

@app.route("/settings/profiles/delete/<int:profile_id>", methods=["POST"])
def delete_profile(profile_id):
    from database import delete_user_profile_mapping
    delete_user_profile_mapping(profile_id)
    flash("Profile mapping removed.", "info")
    return redirect(url_for("settings_page"))

@app.route("/tools/trigger-letterboxd-scraper", methods=["POST"])
def trigger_letterboxd_scraper():
    start_async_task(run_letterboxd_scraper_task)
    flash("Letterboxd scraper initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/tools/trigger-serializd-scraper", methods=["POST"])
def trigger_serializd_scraper():
    start_async_task(run_serializd_scraper_task)
    flash("Serializd scraper initiated in background.", "info")
    return redirect(url_for("tools"))

@app.route("/movie/<slug>")
def movie_details(slug):
    movie = get_movie_details_by_slug(slug)
    if not movie:
        flash("Movie not found in the catalog.", "warning")
        return redirect(url_for('index'))
        
    return render_template("movie_details.html", movie=movie)

@app.route("/show/<tvdb_id>")
def show_details(tvdb_id):
    show, seasons = get_tv_show_details(tvdb_id)
    if not show:
        flash("TV Show not found in the catalog.", "warning")
        return redirect(url_for('index'))
        
    return render_template("show_details.html", show=show, seasons=seasons)

@app.route("/ddns/update-settings", methods=["POST"])
def ddns_update_settings():
    domain = (request.form.get("dynu_hostname") or "").strip()
    apikey = (request.form.get("dynu_api_key") or "").strip()
    enabled = "1" if request.form.get("dynu_enabled") == "1" else "0"

    old_domain = get_setting("dynu_domain")
    old_key = get_setting("dynu_api_key")

    set_setting("dynu_domain", domain)
    set_setting("dynu_api_key", apikey)
    set_setting("dynu_enabled", enabled)

    # Invalidate cached IDs if domain or API key changed
    if domain != old_domain or apikey != old_key:
        set_setting("dynu_dns_id", "")
        set_setting("dynu_record_id", "")
        if domain and apikey:
            fetch_and_cache_dynu_metadata(domain, apikey)

    flash("DDNS configuration saved successfully.", "success")
    return redirect(url_for("ddns_page"))


if csrf:
    csrf.exempt(api_webhook)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")