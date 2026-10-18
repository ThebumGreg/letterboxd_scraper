import csv
import io
import json
import os

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from automation import run_radarr_automation_worker, run_sonarr_automation_worker
from database import (
    get_aggregated_active_films,
    get_all_settings,
    get_external_library_cache,
    get_setting,
    get_film_by_slug,
    get_all_task_statuses,
    init_db,
    set_setting,
    update_film_identifiers,
)
from jellyfin_integration import (
    fetch_and_cache_poster,
    get_jellyfin_item_link,
    sync_jellyfin_playlist,
)
from notifications import send_notification
from scheduler import (
    init_scheduler,
    reconfigure_scheduler,
    start_async_task,
    run_scraper_task,
    run_radarr_task,
    run_sonarr_task,
    run_jellyfin_movies_task,
    run_jellyfin_tv_task,
    run_all_sequential_pipeline,
    trigger_async_task,
)

from scraper import run_all_scrapers

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-this")

init_db()
init_scheduler()


@app.route("/")
def index():
    films = get_aggregated_active_films()
    jellyfin_disabled = get_setting("disable_jellyfin_connection", "0") == "1"

    jf_cache = [] if jellyfin_disabled else get_external_library_cache("jellyfin")
    radarr_cache = get_external_library_cache("radarr")
    sonarr_cache = get_external_library_cache("sonarr")

    # Lookups indexed by TMDB ID / TVDB ID
    jellyfin_tmdb_lookup = {str(i["tmdb_id"]): i for i in jf_cache if i.get("tmdb_id")}
    jellyfin_tvdb_lookup = {str(i["tvdb_id"]): i for i in jf_cache if i.get("tvdb_id")}
    radarr_lookup = {str(i["tmdb_id"]): i for i in radarr_cache if i.get("tmdb_id")}
    sonarr_lookup = {str(i["tvdb_id"]): i for i in sonarr_cache if i.get("tvdb_id")}

    user_agent = request.headers.get("User-Agent", "")
    all_users = set()
    matched_in_jellyfin_count = 0
    movie_count = 0
    tv_count = 0

    # Track identifiers already present from user watchlists
    known_tmdb_ids = set()
    known_tvdb_ids = set()

    for film in films:
        users = [u.strip() for u in (film.get("added_by_users") or "").split(",") if u.strip()]
        film["users_list"] = users
        all_users.update(users)

        if film.get("tmdb_id"):
            known_tmdb_ids.add(str(film["tmdb_id"]))
        if film.get("tvdb_id"):
            known_tvdb_ids.add(str(film["tvdb_id"]))

        if film.get("media_type") == "tv":
            tv_count += 1
            jf_match = jellyfin_tvdb_lookup.get(str(film.get("tvdb_id")))
        else:
            movie_count += 1
            jf_match = jellyfin_tmdb_lookup.get(str(film.get("tmdb_id")))

        if jf_match:
            film["in_jellyfin"] = True
            film["jellyfin_id"] = jf_match.get("external_item_id")
            links = get_jellyfin_item_link(film["jellyfin_id"], user_agent)
            film["jellyfin_link"] = links["primary_url"]
            matched_in_jellyfin_count += 1
        else:
            film["in_jellyfin"] = False
            film["jellyfin_id"] = None
            film["jellyfin_link"] = None

    # --- Items in Jellyfin that are on no watchlist ---
    if jf_cache:
        all_users.add("Jellyfin")

    for jf_item in jf_cache:
        # Episodes are cached for matching but would flood the grid; show shows/movies only
        try:
            if json.loads(jf_item.get("details_json") or "{}").get("type") == "Episode":
                continue
        except ValueError:
            pass

        tmdb_id = str(jf_item["tmdb_id"]) if jf_item.get("tmdb_id") else None
        tvdb_id = str(jf_item["tvdb_id"]) if jf_item.get("tvdb_id") else None

        if (tmdb_id and tmdb_id in known_tmdb_ids) or (tvdb_id and tvdb_id in known_tvdb_ids):
            continue

        item_id = jf_item.get("external_item_id")
        links = get_jellyfin_item_link(item_id, user_agent)

        media_type = "tv" if tvdb_id and not tmdb_id else "movie"
        if media_type == "tv":
            tv_count += 1
            known_tvdb_ids.add(tvdb_id)
        else:
            movie_count += 1
            if tmdb_id:
                known_tmdb_ids.add(tmdb_id)

        matched_in_jellyfin_count += 1

        title = jf_item.get("title") or f"Jellyfin Item ({tmdb_id or tvdb_id or item_id})"

        films.append({
            "movie_slug": f"jf-{item_id}",
            "movie_title": title,
            "full_display_name": title,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "imdb_id": jf_item.get("imdb_id"),
            "tvdb_id": tvdb_id,
            "poster_url": None,
            "added_by_users": "Jellyfin",
            "users_list": ["Jellyfin"],
            "user_count": 1,
            "movie_url": links["primary_url"],
            "in_jellyfin": True,
            "jellyfin_id": item_id,
            "jellyfin_link": links["primary_url"],
            "last_scraped": jf_item.get("last_updated"),
        })

    # --- Movies in Radarr that are on no watchlist and not already listed ---
    # (runs after the Jellyfin loop so anything already in Jellyfin shows as a Jellyfin entry)
    radarr_base = get_setting("radarr_url", "").rstrip("/")
    radarr_added = False

    for rd in radarr_cache:
        tmdb_id = str(rd["tmdb_id"]) if rd.get("tmdb_id") else None
        if not tmdb_id or tmdb_id in known_tmdb_ids:
            continue

        known_tmdb_ids.add(tmdb_id)
        movie_count += 1
        radarr_added = True
        title = rd.get("title") or f"Radarr Movie ({tmdb_id})"
        link = f"{radarr_base}/movie/{tmdb_id}" if radarr_base else "#"

        films.append({
            "movie_slug": f"radarr-{rd.get('external_item_id')}",
            "movie_title": title,
            "full_display_name": title,
            "media_type": "movie",
            "tmdb_id": tmdb_id,
            "imdb_id": rd.get("imdb_id"),
            "tvdb_id": None,
            "poster_url": None,
            "added_by_users": "Radarr",
            "users_list": ["Radarr"],
            "user_count": 1,
            "movie_url": link,
            "in_jellyfin": False,
            "jellyfin_id": None,
            "jellyfin_link": None,
            "last_scraped": rd.get("last_updated"),
        })

    if radarr_added:
        all_users.add("Radarr")

    # Flag everything Radarr / Sonarr already has so the template can show a badge
    for f in films:
        f["in_radarr"] = f.get("media_type") != "tv" and str(f.get("tmdb_id")) in radarr_lookup
        f["in_sonarr"] = f.get("media_type") == "tv" and str(f.get("tvdb_id")) in sonarr_lookup

    stats = {
        "total_films": len(films),
        "total_users": len(all_users),
        "jellyfin_count": matched_in_jellyfin_count,
        "missing_count": len(films) - matched_in_jellyfin_count,
        "movie_count": movie_count,
        "tv_count": tv_count,
        "radarr_count": len(radarr_lookup),
        "sonarr_count": len(sonarr_lookup),
    }

    return render_template("index.html", films=films, all_users=sorted(all_users), stats=stats)    


@app.route("/poster/<slug>")
def get_poster(slug):
    film = get_film_by_slug(slug)

    jellyfin_id = None
    tmdb_id = None

    if film:
        tmdb_id = film.get("tmdb_id")
        jf_cache = get_external_library_cache("jellyfin")
        match = next((i for i in jf_cache if i.get("tmdb_id") == tmdb_id), None)
        if match:
            jellyfin_id = match.get("external_item_id")

    fetch_and_cache_poster(slug, jellyfin_id, tmdb_id)
    poster_filename = f"{slug}.jpg"
    poster_filepath = os.path.join("static", "posters", poster_filename)

    if os.path.exists(poster_filepath):
        return send_from_directory("static/posters", poster_filename)

    if os.path.exists(os.path.join("static", "placeholder.jpg")):
        return send_from_directory("static", "placeholder.jpg")

    return ("Poster not found", 404)


@app.route("/tools")
def tools():
    settings = get_all_settings()
    job_status = json.loads(settings.get("job_status", "{}"))
    task_statuses = get_all_task_statuses()
    unmatched_films = [f for f in get_aggregated_active_films() if not f.get("tmdb_id")]
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


@app.route("/api/logs")
def api_logs():
    log_file = os.path.join("logs", "app.log")
    lines = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()[-300:]]
        except Exception:
            lines = ["Unable to read log file."]
    return jsonify({"logs": lines})


@app.route("/tools/fix-match", methods=["POST"])
def fix_match():
    slug = request.form.get("movie_slug")
    tmdb_id = request.form.get("tmdb_id")
    imdb_id = request.form.get("imdb_id")
    tvdb_id = request.form.get("tvdb_id")

    update_film_identifiers(slug, tmdb_id, imdb_id, tvdb_id)
    flash(f"Updated metadata IDs for {slug}", "success")
    return redirect(url_for("tools"))


# --- Operations Triggers ---

@app.route("/tools/trigger-scraper", methods=["POST"])
def trigger_scraper():
    start_async_task(run_scraper_task)
    flash("Watchlist scraper process initiated in background.", "info")
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
    if success:
        flash("Push notification dispatched successfully!", "success")
    else:
        flash("Failed to send notification. Verify Apprise/Pushover settings.", "danger")
    return redirect(url_for("tools"))


@app.route("/tools/update-settings", methods=["POST"])
def update_settings():
    set_setting("target_usernames", request.form.get("target_usernames", "[]"))
    set_setting("target_serializd_usernames", request.form.get("target_serializd_usernames", "[]"))
    
    set_setting("scraper_interval_hours", request.form.get("scraper_interval_hours", "24"))
    set_setting("radarr_interval_hours", request.form.get("radarr_interval_hours", "6"))
    set_setting("sonarr_interval_hours", request.form.get("sonarr_interval_hours", "6"))
    set_setting("jellyfin_interval_hours", request.form.get("jellyfin_interval_hours", "6"))

    set_setting("request_delay_min", request.form.get("request_delay_min", "1.0"))
    set_setting("request_delay_max", request.form.get("request_delay_max", "3.0"))
    set_setting("tmdb_api_key", request.form.get("tmdb_api_key", ""))

    set_setting("disable_jellyfin_connection", request.form.get("disable_jellyfin_connection", "0"))
    set_setting("jellyfin_url", request.form.get("jellyfin_url", ""))
    set_setting("jellyfin_api_key", request.form.get("jellyfin_api_key", ""))

    set_setting("enable_radarr_automation", request.form.get("enable_radarr_automation", "0"))
    set_setting("radarr_url", request.form.get("radarr_url", ""))
    set_setting("radarr_api_key", request.form.get("radarr_api_key", ""))
    set_setting("radarr_min_user_count", request.form.get("radarr_min_user_count", "1"))

    set_setting("enable_sonarr_automation", request.form.get("enable_sonarr_automation", "0"))
    set_setting("sonarr_url", request.form.get("sonarr_url", ""))
    set_setting("sonarr_api_key", request.form.get("sonarr_api_key", ""))
    set_setting("sonarr_min_user_count", request.form.get("sonarr_min_user_count", "1"))

    set_setting("enable_notifications", request.form.get("enable_notifications", "0"))
    set_setting("apprise_urls", request.form.get("apprise_urls", ""))

    reconfigure_scheduler()

    flash("Settings updated successfully.", "success")
    return redirect(url_for("settings_page"))


@app.route("/api/job-status")
def api_job_status():
    status_data = json.loads(get_setting("job_status", "{}"))
    return jsonify(status_data)


@app.route("/tools/sync-playlist", methods=["POST"])
def trigger_playlist_sync():
    playlist_name = request.form.get("playlist_name", "Letterboxd Community Watchlist")
    active_films = get_aggregated_active_films()
    jf_cache = get_external_library_cache("jellyfin")

    jf_lookup = {item["tmdb_id"]: item["external_item_id"] for item in jf_cache if item.get("tmdb_id")}
    matching_item_ids = [
        jf_lookup[film["tmdb_id"]]
        for film in active_films
        if film.get("tmdb_id") in jf_lookup
    ]

    success, message = sync_jellyfin_playlist(playlist_name, matching_item_ids)
    flash(message, "success" if success else "danger")
    return redirect(url_for("tools"))

@app.route("/tools/export-csv", methods=["POST", "GET"])
def export_csv():
    """Web UI trigger for exporting the database."""
    csv_buffer = generate_consolidated_csv_buffer()
    
    # Convert StringIO to BytesIO for Flask send_file
    mem = io.BytesIO()
    mem.write(csv_buffer.getvalue().encode('utf-8'))
    mem.seek(0)
    csv_buffer.close()
    
    return send_file(
        mem,
        mimetype='text/csv',
        as_attachment=True,
        download_name='letterboxd_sync_export.csv'
    )

@app.route("/tools/trigger-disk-scan", methods=["POST"])
def trigger_disk_scan():
    """Web UI trigger for the disk scanner."""
    try:
        run_disk_scan() 
        return jsonify({"status": "success", "message": "Disk scan completed successfully."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)