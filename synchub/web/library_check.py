"""The Library check page: where Radarr, Sonarr, Jellyfin and the files disagree (synchub/library/arr_check.py), with the few fixes that are safe to do from here."""
import csv
import io
import logging
import re

from flask import Response, jsonify, render_template, request

from synchub import db
from synchub.clients import ApiError
from synchub.library import arr_check

logger = logging.getLogger(__name__)
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]+')


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def new_folder(movie):
    """Radarr's own folder name for the film under its root folder, e.g. /movies/Heat (1995). Always a folder that has no other film's files in it."""
    root = (movie.get("rootFolderPath") or "").rstrip("/")
    if not root:
        root = (movie.get("path") or "").rstrip("/").rsplit("/", 1)[0]
    name = _UNSAFE_NAME.sub(" ", f"{movie.get('title') or 'Film'} ({movie.get('year')})" if movie.get("year") else str(movie.get("title") or "Film")).strip()
    return f"{root}/{name}"


def repoint_radarr(client, radarr_id):
    """Points the Radarr film at a new empty folder without moving or deleting anything, then rescans so Radarr drops the file it wrongly claimed.
    Returns the new folder. The caller has already checked the file is really another film's."""
    movie = client.movie(radarr_id)
    old = movie.get("path")
    folder = new_folder(movie)
    if not folder or folder == old:
        raise ApiError("Radarr's folder for this film is already the standard one, so repointing would change nothing.")
    movie["path"] = folder
    client.update_movie(radarr_id, movie, move_files=False)
    client.rescan_movie(radarr_id)
    return folder


def register(app):
    @app.route("/library/check")
    def library_check_page():
        with db.connection() as conn:
            findings = arr_check.run(conn)
        return render_template("library_check.html", findings=findings, summary=arr_check.summary(findings), labels=arr_check.LABELS, help=arr_check.HELP)

    @app.route("/library/check.csv")
    def library_check_csv():
        with db.connection() as conn:
            findings = arr_check.run(conn)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["problem", "title", "year", "what is wrong", "what to do"])
        for f in findings:
            writer.writerow([f["label"], f["title"], f["year"] or "", f["summary"], f["fix"]])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=library-check.csv"})

    @app.route("/api/library/check/scan-jellyfin", methods=["POST"])
    def api_library_check_scan_jellyfin():
        from synchub.clients.jellyfin import JellyfinClient
        try:
            JellyfinClient().refresh_library()
        except ApiError as exc:
            return _fail(str(exc), 502)
        return jsonify(ok=True, message="Jellyfin is scanning its libraries. Give it a few minutes, then run the check again.")

    @app.route("/api/library/check/radarr-repoint", methods=["POST"])
    def api_library_check_radarr_repoint():
        from synchub.clients.arr import RadarrClient
        data = request.get_json(silent=True) or {}
        radarr_id = str(data.get("radarr_id") or "")
        with db.connection() as conn:                                  # look again now: only a film still found to hold another film's file is changed
            current = [f for f in arr_check.movie_findings(conn) if f["kind"] == arr_check.WRONG_FILE and str(f.get("radarr_id")) == radarr_id]
        if not current:
            return _fail("That film no longer looks wrong, so nothing was changed. Reload the page.")
        try:
            folder = repoint_radarr(RadarrClient(), radarr_id)
        except ApiError as exc:
            return _fail(str(exc), 502)
        logger.info(f"Library check: Radarr film #{radarr_id} ({current[0]['title']}) pointed at {folder}; its file was not touched")
        return jsonify(ok=True, message=f"Radarr now looks for {current[0]['title']} in {folder}. The file was not moved or deleted. Run a Radarr sync, then search for the film in Radarr.")
