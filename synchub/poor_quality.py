"""Poor quality: flag a film, an episode or a season from its own page and ask Radarr or Sonarr for a better copy.

The Library's "Request better copy" starts from a file. This starts from the title, so it also works for a film whose file was never scanned and for shows,
which the file route does not cover. What happens is the same in both services:
  * `releases`: read only. Lists what the indexers have; nothing changes until a person picks one.
  * `request`: picks the best release on offer (at least the minimum resolution), puts the release that was grabbed before on the blocklist (the service's
    "mark as failed") and sends the new one to the download client. When nothing qualifies, nothing is blocked and nothing is grabbed.
  * `grab`: the same for one release a person chose from the list.
A manual grab ignores the quality cutoff, so a title that "already meets its profile" is still replaced. The old file is never deleted here; Radarr or Sonarr
replaces it on import when it treats the new one as an upgrade.

**DVR recordings are never touched.** A film that Jellyfin only has as a recording, and an episode that is only a recording (or that Sonarr has no downloaded
file for), is refused: there is no grabbed release behind a recording, and a recording is not something Radarr or Sonarr may replace.

Every request is kept in `media_quality_request` (who, why, what was asked) and shown on the title's page.

The Sonarr calls (history/series, history/failed, release) are Sonarr v3's documented ones but have not been run against a live Sonarr from here, so every step
reports what happened instead of assuming.
"""
import logging

from synchub import better_copy, db, quality
from synchub.clients import ApiError, ConfigError

logger = logging.getLogger(__name__)

NOTE_MAX = 300


class PoorQualityError(ValueError):
    """Something that stops the request; the message is meant for the page."""


# ---------------------------------------------------------------- what the title is, and what may be asked for it
def _media(conn, media_id):
    row = conn.execute("SELECT id, kind, title, year, tmdb_id FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise PoorQualityError("That title is not in SyncHub.")
    return dict(row)


def _service_id(conn, media_id, source, table, key):
    row = conn.execute(f"""SELECT l.source_key FROM link l JOIN {table} t ON t.{key} = l.source_key
                           WHERE l.source = ? AND l.entity_type = 'media' AND l.entity_id = ? AND t.removed_at IS NULL LIMIT 1""", (source, media_id)).fetchone()
    return row["source_key"] if row else None


def radarr_id(conn, media_id):
    return _service_id(conn, media_id, "radarr", "src_radarr_movie", "radarr_id")


def sonarr_id(conn, media_id):
    return _service_id(conn, media_id, "sonarr", "src_sonarr_series", "sonarr_id")


def movie_is_recording(conn, media_id):
    """True when every copy Jellyfin has of the film is a DVR recording."""
    rows = conn.execute("""SELECT j.is_dvr FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                           WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? AND j.removed_at IS NULL""", (media_id,)).fetchall()
    return bool(rows) and all(r["is_dvr"] for r in rows)


def episodes(conn, media_id, season_number=None, episode_id=None):
    """The show's episodes with what decides whether a better copy may be asked for: `sonarr_episode_id`, `sonarr_season`, `has_file` (Sonarr downloaded it) and
    `recording` (every copy Jellyfin has is a DVR recording). `can_flag` is true for a Sonarr download that is not a recording."""
    where, args = "e.media_id = ?", [media_id]
    if episode_id is not None:
        where, args = where + " AND e.id = ?", args + [episode_id]
    elif season_number is not None:
        where, args = where + " AND e.season_number = ?", args + [season_number]
    eps = {r["id"]: dict(r, sonarr_episode_id=None, sonarr_season=None, has_file=False, recording=False, can_flag=False)
           for r in conn.execute(f"SELECT e.id, e.season_number, e.episode_number, e.title FROM episode e WHERE {where} ORDER BY e.season_number, e.episode_number", args)}
    ids = list(eps)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"""SELECT l.entity_id AS eid, s.sonarr_episode_id, s.season_number, s.has_file FROM link l JOIN src_sonarr_episode s ON s.sonarr_episode_id = l.source_key
                                  WHERE l.source = 'sonarr_episode' AND l.entity_type = 'episode' AND s.removed_at IS NULL AND l.entity_id IN ({marks})""", chunk):
            ep = eps[r["eid"]]
            if r["has_file"] or ep["sonarr_episode_id"] is None:
                ep.update(sonarr_episode_id=r["sonarr_episode_id"], sonarr_season=r["season_number"], has_file=bool(r["has_file"]) or ep["has_file"])
        seen = {}
        for r in conn.execute(f"""SELECT l.entity_id AS eid, j.is_dvr FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                                  WHERE l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND j.removed_at IS NULL AND l.entity_id IN ({marks})""", chunk):
            seen.setdefault(r["eid"], []).append(bool(r["is_dvr"]))
        for eid, flags in seen.items():
            eps[eid]["recording"] = all(flags)
    for ep in eps.values():
        ep["can_flag"] = bool(ep["has_file"] and ep["sonarr_episode_id"] and not ep["recording"])
    return list(eps.values())


def latest(conn, media_id, limit=5):
    """The newest requests for the title, for its page."""
    return [dict(r) for r in conn.execute("SELECT * FROM media_quality_request WHERE media_id = ? ORDER BY id DESC LIMIT ?", (media_id, limit))]


def page_state(conn, media_id):
    """What the title's page needs: whether the buttons show (`available`), why not (`why`), the seasons that can be flagged (shows) and the newest requests."""
    media = _media(conn, media_id)
    out = {"kind": media["kind"], "available": False, "why": "", "seasons": [], "requests": latest(conn, media_id)}
    if media["kind"] == "movie":
        if movie_is_recording(conn, media_id):
            out["why"] = "This film is a DVR recording, so a better copy cannot be requested for it."
        elif not radarr_id(conn, media_id) and not media["tmdb_id"]:
            out["why"] = "Radarr does not have this film and it has no TMDB ID, so it cannot be requested."
        else:
            out["available"] = True
        return out
    if not sonarr_id(conn, media_id):
        out["why"] = "Sonarr does not have this show, so there is no download to replace."
        return out
    by_season = {}
    for ep in episodes(conn, media_id):
        if ep["can_flag"]:
            by_season[ep["season_number"]] = by_season.get(ep["season_number"], 0) + 1
    out["seasons"] = [{"number": n, "label": "Specials" if n == 0 else f"Season {n}", "episodes": count} for n, count in sorted(by_season.items())]
    out["available"] = bool(out["seasons"])
    if not out["available"]:
        out["why"] = "No episode of this show is a Sonarr download (DVR recordings are left alone), so there is nothing to replace."
    return out


# ---------------------------------------------------------------- the target of one request
class _Target:
    """What one request is about, worked out from the database before any service is asked."""

    def __init__(self, conn, media_id, season_number=None, episode_id=None):
        self.media = _media(conn, media_id)
        self.media_id, self.season_number, self.episode_id = media_id, None, None
        self.file_id, self.eps = None, []
        if self.media["kind"] == "movie":
            self.service, self.scope, self.label = "radarr", "movie", self.media["title"]
            if movie_is_recording(conn, media_id):
                raise PoorQualityError("This film is a DVR recording. DVR recordings are left alone, so no better copy is requested for it.")
            self.service_id = radarr_id(conn, media_id)
            main = quality.copies(conn, media_id).get("main")
            self.file_id = main["id"] if main else None
            rec = conn.execute("SELECT * FROM library_file WHERE id = ?", (self.file_id,)).fetchone() if self.file_id else None
            self.wanted = better_copy.wanted_height(conn, dict(rec)) if rec else quality.min_height(conn)
            return
        self.service, self.wanted = "sonarr", quality.min_height(conn)
        self.service_id = sonarr_id(conn, media_id)
        if not self.service_id:
            raise PoorQualityError("Sonarr does not have this show, so there is no download to replace. Request it first.")
        if episode_id is not None:
            found = episodes(conn, media_id, episode_id=episode_id)
            if not found:
                raise PoorQualityError("That episode is not part of this show.")
            ep = found[0]
            if ep["recording"]:
                raise PoorQualityError("This episode is a DVR recording. DVR recordings are left alone, so no better copy is requested for it.")
            if not ep["can_flag"]:
                raise PoorQualityError("Sonarr has no downloaded file for this episode, so there is nothing to replace. Use Request instead.")
            self.scope, self.episode_id, self.season_number, self.eps = "episode", episode_id, ep["season_number"], [ep]
            self.label = f"{self.media['title']} S{ep['season_number']:02d}E{ep['episode_number']:02d}"
            return
        if season_number is None:
            raise PoorQualityError("Choose a season or an episode.")
        self.eps = [ep for ep in episodes(conn, media_id, season_number=season_number) if ep["can_flag"]]
        if not self.eps:
            raise PoorQualityError("No episode of that season is a Sonarr download (DVR recordings are left alone), so there is nothing to replace.")
        seasons = {ep["sonarr_season"] for ep in self.eps}
        if len(seasons) != 1:
            raise PoorQualityError("Sonarr numbers this season's episodes across more than one season, so it cannot be replaced as a whole. Use Poor quality on the episodes instead.")
        self.scope, self.season_number, self.sonarr_season = "season", season_number, seasons.pop()
        self.label = f"{self.media['title']} " + ("Specials" if season_number == 0 else f"Season {season_number}")


def _client(service, client=None):
    if client is not None:
        return client
    from synchub.clients.arr import RadarrClient, SonarrClient
    return RadarrClient() if service == "radarr" else SonarrClient()


def _grabs(target, client):
    """[(history id, release title)] of the newest grab behind what is being replaced: one for a film or an episode, one per different release for a season."""
    if target.service == "radarr":
        history_id, title = better_copy.bad_release(client, target.service_id)
        return [(history_id, title)] if history_id is not None else []
    wanted = {str(ep["sonarr_episode_id"]) for ep in target.eps}
    events = [e for e in client.series_history(target.service_id, target.eps[0]["sonarr_season"]) or []
              if isinstance(e, dict) and str(e.get("eventType", "")).lower() == "grabbed" and str(e.get("episodeId")) in wanted]
    events.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    newest, out, titles = set(), [], set()
    for e in events:
        if str(e.get("episodeId")) in newest:
            continue
        newest.add(str(e.get("episodeId")))
        if e.get("sourceTitle") not in titles:                 # a season pack shows once per episode; block it once
            titles.add(e.get("sourceTitle"))
            out.append((e.get("id"), e.get("sourceTitle")))
    return out


def _offers(target, client, grabs, pick=False):
    """The releases worth offering, best first. A film and a season's automatic choice keep to the minimum resolution; a list a person picks from shows everything for a show,
    because an old show may not exist any better."""
    bad = {title for _, title in grabs if title}
    if target.service == "radarr":
        found = client.releases(target.service_id)
    elif target.scope == "episode":
        found = [r for r in client.releases(episode_id=target.eps[0]["sonarr_episode_id"]) or [] if isinstance(r, dict) and not r.get("fullSeason")]
    else:
        found = [r for r in client.releases(series_id=target.service_id, season_number=target.sonarr_season) or [] if isinstance(r, dict) and r.get("fullSeason")]
    found = [r for r in found or [] if isinstance(r, dict) and r.get("title") not in bad]
    wanted = 0 if (pick and target.service == "sonarr") else target.wanted
    return better_copy.candidates(found, None, wanted)


def _nothing(target):
    what = "season pack" if target.scope == "season" else "release"
    size = f" of at least {target.wanted}p" if target.wanted else ""
    extra = " Try Poor quality on the episodes, or Show releases." if target.scope == "season" else " Try again later, or use Show releases to look."
    return f"The indexers have no other {what}{size} right now, so nothing was grabbed and nothing was blocked.{extra}"


def _block(client, grabs):
    """(how many releases were blocklisted, the titles, a sentence about any that could not be). Never raises: a failed block is reported and the request carries on."""
    done, titles, problems = 0, [], []
    for history_id, title in grabs:
        try:
            client.mark_history_failed(history_id)
            done += 1
            titles.append(title)
        except (ApiError, ConfigError) as exc:
            problems.append(f"“{title}” could not be blocked ({exc}).")
    return done, titles, " ".join(problems)


def _log(target, mode, note, status, message, by, blocked=0, chosen=None):
    with db.connection() as conn:
        return conn.execute(
            """INSERT INTO media_quality_request (media_id, service, scope, season_number, episode_id, mode, note, blocked, chosen_release, status, message, requested_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (target.media_id, target.service, target.scope, target.season_number, target.episode_id, mode, (note or "").strip()[:NOTE_MAX] or None, blocked, chosen, status, message[:600], by)).lastrowid


def _fail(target, mode, note, by, message):
    try:
        _log(target, mode, note, "failed", message, by)
    except Exception as exc:                                    # the log is a nicety; the person still gets the real message
        logger.debug(f"[quality] could not log the failed request: {exc}")
    raise PoorQualityError(message)


def _target(media_id, season_number, episode_id):
    with db.connection() as conn:
        return _Target(conn, media_id, season_number, episode_id)


# ---------------------------------------------------------------- the three things a person can ask
def releases(media_id, season_number=None, episode_id=None, client=None):
    """Read only: what the indexers have and which release would be blocked. Nothing changes."""
    target = _target(media_id, season_number, episode_id)
    if target.service_id is None:
        raise PoorQualityError("Radarr does not have this film yet, so there is no list to show. Poor quality adds it to Radarr and searches.")
    try:
        api = _client(target.service, client)
        grabs = _grabs(target, api)
        found = _offers(target, api, grabs, pick=True)
    except (ApiError, ConfigError) as exc:
        raise PoorQualityError(str(exc))
    note = "" if found else ("The indexers have no other season pack right now. Look at the episodes instead." if target.scope == "season" else "The indexers have no other release right now.")
    return {"title": target.label, "service": target.service.title(), "bad_releases": [t for _, t in grabs if t], "wanted_height": target.wanted, "candidates": found, "message": note}


def _add_movie(target, api, note, by):
    """A film Radarr does not track: add it and let Radarr search. Nothing is blocked because nothing was grabbed."""
    from synchub import automation
    if not target.media["tmdb_id"]:
        raise PoorQualityError("Radarr does not have this film and it has no TMDB ID, so it cannot be requested.")
    try:
        title = automation._add_movie(api, target.media["tmdb_id"])
    except automation.RequestError as exc:
        raise PoorQualityError(str(exc))
    message = f"“{title}” was not in Radarr. It is added now and Radarr is searching for it. The old file is kept: delete it by hand once the new copy is in Jellyfin."
    return {"id": _log(target, "auto", note, "requested", message, by), "status": "requested", "message": message}


def _finish(target, api, chosen, grabs, mode, note, by):
    blocked, titles, problem = _block(api, grabs)
    api.grab_release(chosen["guid"], chosen["indexerId"])
    detail = " ".join(x for x in (f"{chosen['resolution']}p" if chosen.get("resolution") else "", chosen.get("quality") or "") if x)
    said = (f"Blocked {', '.join('“' + t + '”' for t in titles if t)}." if blocked and any(titles) else "") if blocked else ("" if problem else f"{target.service.title()}'s history has no grab to block.")
    owner = target.service.title()
    message = " ".join(p for p in (said, problem, f"Sent “{chosen['title']}”" + (f" ({detail})" if detail else "") + f" to the download client. The old file is kept; {owner} replaces it on import when the new one is an upgrade.") if p)
    request_id = _log(target, mode, note, "grabbed", message, by, blocked=blocked, chosen=chosen["title"])
    if target.service == "radarr" and target.file_id:           # the film's file shows the request and its download progress on Your copies
        try:
            better_copy._record(target.file_id, None, {"id": target.service_id, "title": target.media["title"]}, mode, titles[0] if titles else None,
                                grabs[0][0] if grabs else None, bool(blocked), "grabbed", message, by, chosen=chosen["title"])
        except Exception as exc:
            logger.debug(f"[quality] could not attach the request to the file: {exc}")
    logger.info(f"[quality] poor quality: {target.label} by {by}: {message}")
    return {"id": request_id, "status": "grabbed", "message": message}


def request(media_id, note="", by="someone", season_number=None, episode_id=None, client=None):
    """Flags the title and grabs the best release on offer, after blocking the one that was grabbed before. Returns {'id', 'status', 'message'}."""
    target = _target(media_id, season_number, episode_id)
    try:
        api = _client(target.service, client)
        if target.service_id is None:
            return _add_movie(target, api, note, by)
        grabs = _grabs(target, api)
        found = _offers(target, api, grabs)
        if not found:
            raise PoorQualityError(_nothing(target))
        return _finish(target, api, found[0], grabs, "auto", note, by)
    except (ApiError, ConfigError, PoorQualityError) as exc:
        _fail(target, "auto", note, by, str(exc))


def grab(media_id, guid, indexer_id, note="", by="someone", season_number=None, episode_id=None, client=None):
    """Grabs one release a person chose from `releases`. The list is read again, so only a release that is on offer now can be grabbed."""
    target = _target(media_id, season_number, episode_id)
    if target.service_id is None:
        raise PoorQualityError("Radarr does not have this film yet. Use Poor quality, which adds it to Radarr and searches.")
    try:
        api = _client(target.service, client)
        grabs = _grabs(target, api)
        chosen = next((c for c in _offers(target, api, grabs, pick=True) if c["guid"] == guid and str(c["indexerId"]) == str(indexer_id)), None)
        if chosen is None:
            raise PoorQualityError("That release is not on offer any more. Press Show releases again.")
        return _finish(target, api, chosen, grabs, "pick", note, by)
    except (ApiError, ConfigError, PoorQualityError) as exc:
        _fail(target, "pick", note, by, str(exc))
