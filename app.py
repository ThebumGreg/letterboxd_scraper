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

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-this")

init_db()
init_scheduler()


@app.route("/")
def index():
    films = get_aggregated_active_films()
    jf_cache = get_external_library_cache("jellyfin")
    radarr_cache = get_external_library_cache("radarr")
    sonarr_cache = get_external_library_cache("sonarr")

    # Lookups indexed by TMDB ID / TVDB ID
    jellyfin_tmdb_lookup = {str(item["tmdb_id"]): item for item in jf_cache if item.get("tmdb_id")}
    jellyfin_tvdb_lookup = {str(item["tvdb_id"]): item for item in jf_cache if item.get("tvdb_id")}
    
    radarr_lookup = {str(item["tmdb_id"]): item for item in radarr_cache if item.get("tmdb_id")}
    sonarr_lookup = {str(item["tvdb_id"]): item for item in sonarr_cache if item.get("tvdb_id")}

    user_agent = request.headers.get("User-Agent", "")
    all_users = set()
    matched_in_jellyfin_count = 0
    movie_count = 0
    tv_count = 0

    # Track identifiers already present from Letterboxd watchlists
    known_tmdb_ids = set()
    known_tvdb_ids = set()

    for film in films:
        users = [u.strip() for u in film["added_by_users"].split(",") if u.strip()]
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

        if jf_match and get_setting("disable_jellyfin_connection", "0") != "1":
            film["in_jellyfin"] = True
            film["jellyfin_id"] = jf_match.get("external_item_id")
            links = get_jellyfin_item_link(jf_match.get("external_item_id"), user_agent)
            film["jellyfin_link"] = links["primary_url"]
            matched_in_jellyfin_count += 1
        else:
            film["in_jellyfin"] = False
            film["jellyfin_id"] = None
            film["jellyfin_link"] = None

    # --- Append Items Present in Jellyfin But Missing From Watchlists ---
    all_users.add("Jellyfin")

    for jf_item in jf_cache:
        tmdb_id = str(jf_item.get("tmdb_id")) if jf_item.get("tmdb_id") else None
        tvdb_id = str(jf_item.get("tvdb_id")) if jf_item.get("tvdb_id") else None

        # Skip if already represented by a Letterboxd / user film entry
        if (tmdb_id and tmdb_id in known_tmdb_ids) or (tvdb_id and tvdb_id in known_tvdb_ids):
            continue

        item_id = jf_item.get("external_item_id")
        links = get_jellyfin_item_link(item_id, user_agent)
        
        # Determine media type based on identifiers present
        media_type = "tv" if tvdb_id and not tmdb_id else "movie"
        if media_type == "tv":
            tv_count += 1
            known_tvdb_ids.add(tvdb_id)
        else:
            movie_count += 1
            if tmdb_id:
                known_tmdb_ids.add(tmdb_id)

        matched_in_jellyfin_count += 1

        # Synthesize entry for UI grid rendering
        title = jf_item.get("details_json") or f"Jellyfin Item ({tmdb_id or tvdb_id or item_id})"
        slug = f"jf-{item_id}"

        virtual_film = {
            "movie_slug": slug,
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
        }
        films.append(virtual_film)

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

    return render_template("index.html", films=films, all_users=sorted(list(all_users)), stats=stats)


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


@app.route("/tools/export-csv")
def export_csv():
    films = get_aggregated_active_films()
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Title", "Display Name", "Slug", "Media Type", "TMDB ID", "IMDb ID", "TVDB ID",
        "Added By Users", "User Count", "Letterboxd URL"
    ])

    for f in films:
        writer.writerow([
            f["movie_title"],
            f["full_display_name"],
            f["movie_slug"],
            f.get("media_type", "movie"),
            f.get("tmdb_id", ""),
            f.get("imdb_id", ""),
            f.get("tvdb_id", ""),
            f["added_by_users"],
            f["user_count"],
            f["movie_url"],
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=watchlist_export.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)