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
from scheduler import init_scheduler, job_sync_jellyfin, trigger_async_task

app = Flask(__name__)
app.secret_key = "super-secret-key-change-this"

init_db()
init_scheduler()


@app.route("/")
def index():
    films = get_aggregated_active_films()
    jf_cache = get_external_library_cache("jellyfin")
    radarr_cache = get_external_library_cache("radarr")
    sonarr_cache = get_external_library_cache("sonarr")

    jellyfin_lookup = {item["tmdb_id"]: item for item in jf_cache if item.get("tmdb_id")}
    radarr_lookup = {item["tmdb_id"]: item for item in radarr_cache if item.get("tmdb_id")}
    sonarr_lookup = {item["tvdb_id"]: item for item in sonarr_cache if item.get("tvdb_id")}

    user_agent = request.headers.get("User-Agent", "")
    all_users = set()
    matched_in_jellyfin_count = 0
    movie_count = 0
    tv_count = 0

    for film in films:
        users = [u.strip() for u in film["added_by_users"].split(",") if u.strip()]
        film["users_list"] = users
        all_users.update(users)

        if film.get("media_type") == "tv":
            tv_count += 1
        else:
            movie_count += 1

        jf_match = jellyfin_lookup.get(film.get("tmdb_id"))
        if jf_match and get_setting("disable_jellyfin_connection", "0") != "1":
            film["in_jellyfin"] = True
            film["jellyfin_id"] = jf_match["external_item_id"]
            links = get_jellyfin_item_link(jf_match["external_item_id"], user_agent)
            film["jellyfin_link"] = links["primary_url"]
            matched_in_jellyfin_count += 1
        else:
            film["in_jellyfin"] = False
            film["jellyfin_id"] = None
            film["jellyfin_link"] = None

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
    films = get_aggregated_active_films()
    film = next((f for f in films if f["movie_slug"] == slug), None)

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
    unmatched_films = [f for f in get_aggregated_active_films() if not f.get("tmdb_id")]
    return render_template("tools.html", settings=settings, job_status=job_status, unmatched_films=unmatched_films)


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


@app.route("/tools/trigger-sync-jellyfin", methods=["POST"])
def trigger_sync_jellyfin():
    trigger_async_task(job_sync_jellyfin)
    flash("Jellyfin library background sync initiated.", "info")
    return redirect(url_for("tools"))


@app.route("/tools/trigger-radarr-sync", methods=["POST"])
def trigger_radarr_sync():
    trigger_async_task(run_radarr_automation_worker)
    flash("Radarr movie automation search initiated.", "info")
    return redirect(url_for("tools"))


@app.route("/tools/trigger-sonarr-sync", methods=["POST"])
def trigger_sonarr_sync():
    trigger_async_task(run_sonarr_automation_worker)
    flash("Sonarr TV automation search initiated.", "info")
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
    set_setting("scrape_interval_hours", request.form.get("scrape_interval_hours", "6"))
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