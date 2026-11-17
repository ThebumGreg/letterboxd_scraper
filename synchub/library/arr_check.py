"""The Library check: where Radarr, Sonarr, Jellyfin and the files on disk disagree, worked out from what SyncHub already saved. Nothing here calls a service.

The model: the file on the share is the truth about what exists. Radarr and Sonarr are checked against it ("downloaded" must be a real file of that film),
Jellyfin is checked against it (every file shows up, as the right film), and Radarr/Sonarr and Jellyfin are compared on identity only (same TMDB / TVDB id).
Each finding says what is wrong, what the real thing probably is and what to do; `action` is set only when SyncHub can fix it safely.
"""
import os
import re
from collections import defaultdict

from synchub import storage
from synchub.text import norm_title

WRONG_FILE, DIFFERENT_ID, NOT_IN_JELLYFIN, NO_JELLYFIN_LIBRARY, NO_MAPPING, NO_FILE, SHARED_FILE, DUPLICATE_RADARR, MISSING_EPISODES = (
    "wrong_file", "different_id", "not_in_jellyfin", "no_jellyfin_library", "no_mapping", "no_file", "shared_file", "duplicate_radarr", "missing_episodes")

KINDS = (
    (WRONG_FILE, "Radarr has another film's file", "Radarr says it downloaded this film, but the file in its folder is a different film."),
    (DIFFERENT_ID, "Radarr/Sonarr and Jellyfin disagree which title it is", "The same file or folder is a different title in Jellyfin (different TMDB or TVDB id)."),
    (NOT_IN_JELLYFIN, "Jellyfin has not picked the file up", "The file is in a folder Jellyfin watches, but Jellyfin has no item for it yet."),
    (MISSING_EPISODES, "Episodes Jellyfin does not have", "Sonarr has the episode files, Jellyfin does not list them."),
    (NO_JELLYFIN_LIBRARY, "No Jellyfin library covers the folder", "Add the folder to a Jellyfin library, or to the share's Jellyfin paths on the Storage page."),
    (NO_MAPPING, "Share path not set up", "SyncHub cannot tell which share Radarr or Sonarr's folder is on. Fill in the Radarr/Sonarr path on the Storage page."),
    (NO_FILE, "Marked downloaded, but Radarr has no file", "The title shows as Downloaded, but Radarr has no file for it."),
    (SHARED_FILE, "One file, two Radarr films", "Two Radarr films point at the same file."),
    (DUPLICATE_RADARR, "Same film twice in Radarr", "Two Radarr films have the same TMDB id."),
)
LABELS = {k: label for k, label, _ in KINDS}
HELP = {k: text for k, _, text in KINDS}

_YEAR = re.compile(r"(?<![0-9])(19[2-9][0-9]|20[0-3][0-9])(?![0-9])")
_PAREN_YEAR = re.compile(r"\((19[2-9][0-9]|20[0-3][0-9])\)")


def guess(path):
    """(title, year) read from a file name: the text before the year, with dots and underscores as spaces. A year in brackets wins over one without.
    Either part may be None. `Blade.Runner.2049.2017.REMUX.mkv` gives ('Blade Runner 2049', 2017) because 2049 is not a plausible year."""
    stem = os.path.splitext(os.path.basename(str(path or "").replace("\\", "/")))[0]
    m = _PAREN_YEAR.search(stem) or _YEAR.search(stem)
    year = int(m.group(1)) if m else None
    head = stem[:m.start()] if m else stem
    title = re.sub(r"[._]+", " ", head).strip(" -([") or None
    return title, year


_STOP = {"and", "the", "of", "a", "an"}


def _tokens(title):
    return {t for t in norm_title(title).split() if t not in _STOP}


def _plausible_other_film(radarr_title, radarr_year, file_title, file_year):
    """True when a file's name says it is a different film from the Radarr one: the years are more than a year apart, or the titles do not overlap enough
    (the shorter one must be at least 60 % of the longer, so `The Killer` is not `The Ritual Killer` but a trimmed subtitle still passes)."""
    if file_year and radarr_year and abs(file_year - radarr_year) > 1:
        return True
    a, b = _tokens(radarr_title), _tokens(file_title)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return not short <= long_ or len(short) / len(long_) < 0.6


def _loc(roots, path, service):
    """(share, path inside the share) with the case folded, or None when no share mapping covers the path."""
    mapped = storage.from_app_path(roots, path, service) if path else None
    return (mapped[0], mapped[1].casefold()) if mapped else None


def _folder_loc(roots, path, service):
    mapped = storage.from_app_path(roots, str(path or "").rstrip("/") + "/x", service) if path else None
    return (mapped[0], mapped[1].casefold().rsplit("/x", 1)[0].rstrip("/")) if mapped else None


def _jellyfin_by_location(conn, roots, item_type):
    out = {}
    for r in conn.execute("SELECT item_id, name, year, tmdb_id, tvdb_id, path FROM src_jellyfin_item WHERE type = ? AND removed_at IS NULL AND is_dvr = 0 AND path IS NOT NULL", (item_type,)):
        loc = _loc(roots, r["path"], "jellyfin") if item_type == "movie" else _folder_loc(roots, r["path"], "jellyfin")
        if loc:
            out.setdefault(loc, []).append(dict(r))
    return out


def _finding(kind, media, title, year, summary, fix, action=None, detail=(), extra=None):
    return {"kind": kind, "label": LABELS[kind], "media_id": media, "title": title, "year": year, "summary": summary, "fix": fix, "action": action, "detail": list(detail), **(extra or {})}


def movie_findings(conn, roots=None):
    roots = roots if roots is not None else storage.list_all(conn)
    jf = _jellyfin_by_location(conn, roots, "movie")
    movies = [dict(r) for r in conn.execute(
        """SELECT r.*, l.entity_id AS media_id FROM src_radarr_movie r LEFT JOIN link l ON l.source = 'radarr' AND l.source_key = r.radarr_id AND l.entity_type = 'media'
           WHERE r.removed_at IS NULL""")]
    by_tmdb = defaultdict(list)
    by_file = defaultdict(list)
    for r in movies:
        if r["tmdb_id"]:
            by_tmdb[r["tmdb_id"]].append(r)
        if r["file_path"]:
            by_file[r["file_path"].casefold()].append(r)
    found = []
    for r in movies:
        title, year, media = r["title"], r["year"], r["media_id"]
        if not r["has_file"] or not r["file_path"]:
            continue
        loc = _loc(roots, r["file_path"], "radarr")
        items = jf.get(loc, []) if loc else []
        file_title, file_year = guess(r["file_path"])
        wrong = _plausible_other_film(title, year, file_title, file_year)
        twins = [t for t in by_file[r["file_path"].casefold()] if t["radarr_id"] != r["radarr_id"]]
        if wrong:
            real = _real_film(conn, items, file_title, file_year)
            fix = ("Point Radarr at a new empty folder and rescan, so Radarr stops claiming this file. The file itself is not touched or deleted. Then use Search in Radarr to get the right film. " + real["note"])
            found.append(_finding(WRONG_FILE, media, title, year,
                                  f"Radarr says {title} ({year}) is downloaded, but the file is “{os.path.basename(r['file_path'])}”, which looks like {file_title or 'another film'}{f' ({file_year})' if file_year else ''}.",
                                  fix, {"type": "radarr_repoint", "radarr_id": r["radarr_id"], "media_id": media, "label": "Point Radarr at a new folder (keeps the file)"} if media else None,
                                  [f"Radarr folder: {r['folder']}", f"File: {r['file_path']}"] + real["detail"], {"real": real["name"], "radarr_id": r["radarr_id"]}))
            continue
        if twins:
            found.append(_finding(SHARED_FILE, media, title, year, f"{title} ({year}) points at the same file as {', '.join(t['title'] for t in twins)}.",
                                  "Keep one: in Radarr change the path of the film that does not own the file.", None, [f"File: {r['file_path']}"]))
        if loc is None:
            found.append(_finding(NO_MAPPING, media, title, year, f"Radarr's folder {r['folder']} is not on any share SyncHub knows.",
                                  "Storage page: fill in the Radarr path of the share (the folder Radarr sees for it).", None, [f"File: {r['file_path']}"]))
            continue
        if items:
            other = [i for i in items if i["tmdb_id"] and r["tmdb_id"] and i["tmdb_id"] != r["tmdb_id"]]
            if other and not any(i["tmdb_id"] == r["tmdb_id"] for i in items):
                i = other[0]
                found.append(_finding(DIFFERENT_ID, media, title, year,
                                      f"Radarr says {title} ({year}, TMDB {r['tmdb_id']}), Jellyfin shows the same file as {i['name']} ({i['year']}, TMDB {i['tmdb_id']}).",
                                      "Decide which one is right. If Radarr is right: in Jellyfin open the film, Identify, and pick the right match. If Jellyfin is right: change the film in Radarr (Edit, TMDB id).",
                                      None, [f"File: {r['file_path']}", f"Jellyfin item: {i['item_id']}"]))
            continue
        share = next((x for x in roots if x["name"] == loc[0]), None)
        covered = bool(share and storage.to_app_path(share, loc[1], "jellyfin"))
        if covered:
            found.append(_finding(NOT_IN_JELLYFIN, media, title, year, f"{title} ({year}) has a file but Jellyfin has no item for it.",
                                  "Scan the Jellyfin library. If it is still missing after the scan, check the file name and that the folder has only this film.",
                                  {"type": "scan_jellyfin", "label": "Scan Jellyfin"}, [f"File: {r['file_path']}"]))
        else:
            found.append(_finding(NO_JELLYFIN_LIBRARY, media, title, year, f"The folder of {title} ({year}) is not in any Jellyfin library.",
                                  "Storage page: add the folder to the share's Jellyfin paths, and make sure a Jellyfin library covers it.", None, [f"File: {r['file_path']}"]))
    for tmdb, rows in by_tmdb.items():
        if len(rows) > 1:
            r = rows[0]
            found.append(_finding(DUPLICATE_RADARR, r["media_id"], r["title"], r["year"], f"Radarr has {len(rows)} entries for TMDB {tmdb}: " + ", ".join(f"#{x['radarr_id']} {x['folder']}" for x in rows) + ".",
                                  "In Radarr delete the extra entry without deleting its files.", None, []))
    for r in conn.execute(
            """SELECT m.id, m.title, m.year, r.radarr_id FROM media m JOIN item_state s ON s.target_type = 'movie' AND s.target_id = m.id AND s.state_code = 'downloaded'
               JOIN link l ON l.source = 'radarr' AND l.entity_type = 'media' AND l.entity_id = m.id JOIN src_radarr_movie r ON r.radarr_id = l.source_key AND r.removed_at IS NULL
               WHERE r.has_file = 0 AND m.kind = 'movie'"""):
        found.append(_finding(NO_FILE, r["id"], r["title"], r["year"], f"{r['title']} ({r['year']}) shows as Downloaded but Radarr has no file for it.",
                              "Check Radarr's queue and History. If nothing is coming, the state clears itself on the next sync, or search for it in Radarr.", None, []))
    return found


def _real_film(conn, items, file_title, file_year):
    """What the wrong file probably is: the Jellyfin item at the same place, else a film SyncHub knows by the name and year. Says whether Radarr has it."""
    name, detail, note, tmdb = None, [], "", None
    if items:
        i = items[0]
        name, tmdb = f"{i['name']} ({i['year']})", i["tmdb_id"]
        detail.append(f"Jellyfin shows the file as {name}.")
    elif file_title and file_year:
        for m in conn.execute("SELECT id, title, year, tmdb_id FROM media WHERE kind = 'movie' AND year = ?", (file_year,)):
            if norm_title(m["title"]) == norm_title(file_title):
                name, tmdb = f"{m['title']} ({m['year']})", m["tmdb_id"]
                break
    if name is None:
        name = f"{file_title or 'unknown'}{f' ({file_year})' if file_year else ''}"
        return {"name": name, "detail": detail, "note": f"SyncHub does not know a film called {name}; add it in Radarr if you want it managed."}
    own = conn.execute("SELECT radarr_id, has_file FROM src_radarr_movie WHERE tmdb_id = ? AND removed_at IS NULL", (tmdb,)).fetchone() if tmdb else None
    if own is None:
        note = f"{name} is not in Radarr yet: add it and point it at this folder, or Radarr will not manage this file."
    elif own["has_file"]:
        note = f"{name} is in Radarr with its own file."
    else:
        note = f"{name} is in Radarr but has no file: after repointing the wrong record, point {name} at this folder."
    detail.append(note)
    return {"name": name, "detail": detail, "note": note}


def show_findings(conn, roots=None):
    roots = roots if roots is not None else storage.list_all(conn)
    jf = _jellyfin_by_location(conn, roots, "series")
    found = []
    series = [dict(r) for r in conn.execute(
        """SELECT s.*, l.entity_id AS media_id, m.title AS media_title, m.year AS media_year FROM src_sonarr_series s JOIN link l ON l.source = 'sonarr' AND l.source_key = s.sonarr_id AND l.entity_type = 'media'
           JOIN media m ON m.id = l.entity_id WHERE s.removed_at IS NULL""")]
    for s in series:
        rows = conn.execute(
            """SELECT se.season_number, se.episode_number, se.file_path FROM src_sonarr_episode se
               JOIN link l ON l.source = 'sonarr_episode' AND l.source_key = se.sonarr_episode_id AND l.entity_type = 'episode'
               JOIN item_state st ON st.target_type = 'episode' AND st.target_id = l.entity_id AND st.state_code = 'downloaded'
               WHERE se.sonarr_id = ? AND se.has_file = 1 AND se.removed_at IS NULL ORDER BY se.season_number, se.episode_number""", (s["sonarr_id"],)).fetchall()
        if not rows:
            continue
        loc = _folder_loc(roots, s["folder"], "sonarr")
        items = jf.get(loc, []) if loc else []
        title, year, media = s["media_title"], s["media_year"], s["media_id"]
        names = ", ".join(f"S{r['season_number']:02d}E{r['episode_number']:02d}" for r in rows[:12]) + (" ..." if len(rows) > 12 else "")
        if loc is None:
            found.append(_finding(NO_MAPPING, media, title, year, f"Sonarr's folder {s['folder']} is not on any share SyncHub knows.", "Storage page: fill in the Sonarr path of the share.", None, [f"{len(rows)} episodes have files: {names}"]))
            continue
        wrong = [i for i in items if i["tvdb_id"] != s["tvdb_id"]] if items else []
        if items and wrong and not any(i["tvdb_id"] == s["tvdb_id"] for i in items):
            i = wrong[0]
            found.append(_finding(DIFFERENT_ID, media, title, year, f"Sonarr's {s['title']} (TVDB {s['tvdb_id']}) folder is shown in Jellyfin as {i['name']} ({i['year']}, TVDB {i['tvdb_id'] or 'none'}).",
                                  "In Jellyfin open that show, Identify, and pick the right match (or rename the folder so it matches). Then scan the library.", None, [f"Sonarr folder: {s['folder']}", f"{len(rows)} episodes affected: {names}"]))
            continue
        share = next((x for x in roots if x["name"] == loc[0]), None)
        if share and storage.to_app_path(share, loc[1], "jellyfin"):
            found.append(_finding(MISSING_EPISODES, media, title, year, f"Sonarr has {len(rows)} episode file{'s' if len(rows) != 1 else ''} of {title} that Jellyfin does not list: {names}.",
                                  "Scan the Jellyfin library. If the episodes are still missing, check that the file names carry the season and episode numbers (S01E02) and sit in the show's folder.",
                                  {"type": "scan_jellyfin", "label": "Scan Jellyfin"}, [f"Sonarr folder: {s['folder']}"]))
        else:
            found.append(_finding(NO_JELLYFIN_LIBRARY, media, title, year, f"The folder of {title} is not in any Jellyfin library.", "Storage page: add it to the share's Jellyfin paths.", None, [f"Sonarr folder: {s['folder']}"]))
    for f in found:
        f["show"] = True
    return found


def run(conn):
    roots = storage.list_all(conn)
    return movie_findings(conn, roots) + show_findings(conn, roots)


def summary(findings):
    """[(kind, label, help, count)] for the kinds that have findings, in the order of KINDS."""
    counts = defaultdict(int)
    for f in findings:
        counts[f["kind"]] += 1
    return [(k, label, text, counts[k]) for k, label, text in KINDS if counts[k]]
