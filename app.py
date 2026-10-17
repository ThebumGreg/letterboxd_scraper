import json
import os
import threading
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from curl_cffi import requests as cffi_requests

from database import (
    get_aggregated_active_films,
    get_all_settings,
    get_setting,
    init_db,
    set_setting,
)
from export_to_csv import generate_consolidated_csv_buffer
from jellyfin_client import fetch_jellyfin_catalog, fetch_jellyfin_library_titles
from logger import LOG_FILE_PATH, logger
from radarr_client import add_movie_to_radarr, fetch_radarr_library_titles, normalize_title, sync_missing_to_radarr
from sonarr_client import add_series_to_sonarr, fetch_sonarr_library_titles
from rss_feed import generate_radarr_rss_xml
from scraper import run_scraper_job

app = Flask(__name__)
init_db()

POSTER_DIR = Path(app.root_path) / "static" / "posters"
POSTER_DIR.mkdir(parents=True, exist_ok=True)

scheduler = BackgroundScheduler(daemon=True)

def update_scheduler_interval():
    try:
        hours = int(get_setting("scrape_interval_hours", "24"))
        scheduler.remove_all_jobs()
        scheduler.add_job(run_scraper_job, "interval", hours=hours, id="scrape_job")
    except Exception as e:
        logger.error(f"Failed to update scheduler interval: {e}")

update_scheduler_interval()
scheduler.start()

def cache_poster_locally(movie_slug: str, film_identifier: str) -> str:
    """Safely retrieves or caches poster images locally with a strict 2-second timeout."""
    if not film_identifier:
        return "/static/favicon.svg"
    
    local_path = POSTER_DIR / f"{movie_slug}.jpg"
    relative_url = f"/static/posters/{movie_slug}.jpg"

    if local_path.exists():
        return relative_url

    # Construct Letterboxd standard poster CDN URL
    id_parts = "/".join(list(film_identifier))
    remote_url = f"https://a.ltrbxd.com/resized/film-poster/{id_parts}/{film_identifier}-0-230-0-345-crop.jpg"

    try:
        res = cffi_requests.get(remote_url, timeout=2)
        if res.status_code == 200:
            with open(local_path, "wb") as f:
                f.write(res.content)
            return relative_url
    except Exception as e:
        logger.warning(f"Failed or timed out fetching poster for {movie_slug}: {e}")

    # Immediate fallback to local SVG icon on failure or timeout
    return "/static/favicon.svg"

@app.get('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.svg',
        mimetype='image/svg+xml'
    )

@app.get("/")
def dashboard():
    active_films = get_aggregated_active_films()
    
    # Safely fetch API libraries with fast execution boundaries
    try:
        radarr_owned = fetch_radarr_library_titles()
    except Exception as e:
        logger.error(f"Radarr library fetch error: {e}")
        radarr_owned = set()

    try:
        jellyfin_catalog = fetch_jellyfin_catalog(item_type="Movie")
    except Exception as e:
        logger.error(f"Jellyfin library fetch error: {e}")
        jellyfin_catalog = []

    jellyfin_owned = {item["normalized_title"] for item in jellyfin_catalog}
    jellyfin_lookup = {item["normalized_title"]: item for item in jellyfin_catalog}

    total_active = len(active_films)
    multi_user_duplicates = sum(1 for f in active_films if f["user_count"] > 1)

    processed_films = []
    in_radarr_count = 0
    in_jellyfin_count = 0

    for film in active_films:
        display_title = film["full_display_name"] or film["movie_title"]
        norm = normalize_title(display_title)
        
        has_radarr = norm in radarr_owned
        has_jellyfin = norm in jellyfin_owned

        if has_radarr: in_radarr_count += 1
        if has_jellyfin: in_jellyfin_count += 1

        poster_url = cache_poster_locally(film["movie_slug"], film["film_identifier"])
        jelly_item = jellyfin_lookup.get(norm, {})

        processed_films.append({
            "title": display_title,
            "slug": film["movie_slug"],
            "url": film["movie_url"],
            "poster_url": poster_url,
            "user_count": film["user_count"],
            "added_by": film["added_by_users"].split(",") if film["added_by_users"] else [],
            "in_radarr": has_radarr,
            "in_jellyfin": has_jellyfin,
            "jellyfin_web_url": jelly_item.get("web_url", ""),
        })

    settings_data = get_all_settings()
    target_users = json.loads(settings_data.get("target_usernames", "[]"))

    return render_template(
        "index.html",
        films=processed_films,
        target_users=target_users,
        total_active=total_active,
        in_radarr_count=in_radarr_count,
        in_jellyfin_count=in_jellyfin_count,
        duplicates_count=multi_user_duplicates,
        last_run=settings_data.get("last_run_timestamp", "Never"),
        is_scraping=settings_data.get("is_scraping_active", "0") == "1",
    )

@app.get("/settings")
def settings_page():
    all_settings = get_all_settings()
    usernames_list = json.loads(all_settings.get("target_usernames", '["tristanisepic"]'))
    all_settings["target_usernames_str"] = ", ".join(usernames_list)
    return render_template("settings.html", settings=all_settings)

@app.post("/settings")
def save_settings():
    form = request.form
    usernames_raw = form.get("target_usernames", "")
    usernames_array = [u.strip() for u in usernames_raw.split(",") if u.strip()]

    set_setting("target_usernames", json.dumps(usernames_array))
    set_setting("scrape_interval_hours", form.get("scrape_interval_hours", "24"))
    set_setting("request_delay_min", form.get("request_delay_min", "2.5"))
    set_setting("request_delay_max", form.get("request_delay_max", "4.5"))
    set_setting("user_delay_min", form.get("user_delay_min", "10.0"))
    set_setting("user_delay_max", form.get("user_delay_max", "20.0"))
    set_setting("radarr_url", form.get("radarr_url", "http://localhost:7878"))
    set_setting("radarr_api_key", form.get("radarr_api_key", ""))
    set_setting("radarr_root_folder", form.get("radarr_root_folder", "/movies"))
    set_setting("radarr_quality_profile_id", form.get("radarr_quality_profile_id", "1"))
    set_setting("auto_sync_radarr", "1" if form.get("auto_sync_radarr") else "0")
    set_setting("sonarr_url", form.get("sonarr_url", "http://localhost:8989"))
    set_setting("sonarr_api_key", form.get("sonarr_api_key", ""))
    set_setting("sonarr_root_folder", form.get("sonarr_root_folder", "/tv"))
    set_setting("sonarr_quality_profile_id", form.get("sonarr_quality_profile_id", "1"))
    set_setting("auto_sync_sonarr", "1" if form.get("auto_sync_sonarr") else "0")
    set_setting("jellyfin_url", form.get("jellyfin_url", "http://localhost:8096"))
    set_setting("jellyfin_api_key", form.get("jellyfin_api_key", ""))
    set_setting("enable_disk_scanner", "1" if form.get("enable_disk_scanner") else "0")
    set_setting("disk_scanner_path", form.get("disk_scanner_path", "/media/movies"))

    update_scheduler_interval()
    return jsonify({"status": "success", "message": "Settings updated successfully!"})

@app.get("/logs")
def logs_page():
    return render_template("logs.html")

@app.get("/api/logs")
def get_logs_json():
    logs_content = ""
    if LOG_FILE_PATH.exists():
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
            logs_content = f.read()[-12000:]
    return jsonify({"logs": logs_content})

@app.get("/api/export/csv")
def download_csv():
    csv_buffer = generate_consolidated_csv_buffer()
    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=letterboxd_export.csv"}
    )

@app.post("/api/scraper/run")
def trigger_scrape():
    if get_setting("is_scraping_active", "0") == "1":
        return jsonify({"status": "error", "message": "A scrape job is already in progress."}), 400

    thread = threading.Thread(target=run_scraper_job)
    thread.start()
    return jsonify({"status": "success", "message": "Scraper execution started in background!"})

@app.post("/api/radarr/add")
def radarr_add():
    data = request.json or {}
    title = data.get("title")
    if not title:
        return jsonify({"status": "error", "message": "Movie title required."}), 400

    result = add_movie_to_radarr(title)
    return jsonify(result)

@app.post("/api/sonarr/add")
def sonarr_add():
    data = request.json or {}
    title = data.get("title")
    if not title:
        return jsonify({"status": "error", "message": "TV Show title required."}), 400

    result = add_series_to_sonarr(title)
    return jsonify(result)

@app.post("/api/radarr/sync-all")
def radarr_sync_all():
    thread = threading.Thread(target=sync_missing_to_radarr)
    thread.start()
    return jsonify({"status": "success", "message": "Bulk Radarr API sync started in background!"})

@app.get("/rss/radarr")
def get_radarr_rss():
    list_type = request.args.get("list_type")
    exclude_owned = request.args.get("exclude_owned", "true").lower() == "true"
    xml_payload = generate_radarr_rss_xml(list_type=list_type, exclude_owned=exclude_owned)
    return Response(xml_payload, mimetype="application/rss+xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)