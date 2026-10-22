"""Links source records (Jellyfin, Radarr, Sonarr, Letterboxd, Serializd) to media rows.

Evidence is used in this order, and every link stores the method and a confidence:
  TMDB ID (1.0)  >  TVDB/IMDb ID (1.0 if already known locally, 0.95 via TMDB /find)  >  title + year (0.9, single hit)
Anything weaker becomes a pending match_review item for a human. Two records that carry different IDs of the
same kind are never silently merged: the TMDB ID decides, and the disagreement is raised for review.
"""
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from synchub import db, metadata
from synchub.text import clean_id, norm_title, split_title_year

logger = logging.getLogger(__name__)

AUTO_THRESHOLD = 0.9     # at or above: link automatically
SKIP_THRESHOLD = 0.95    # an existing link at or above this is trusted and not re-evaluated
SEARCH_RETRY_DAYS = 7


class Unavailable(Exception):
    """TMDB is not configured or did not answer; the record is retried on the next run."""


@dataclass
class Match:
    media_id: int
    method: str
    confidence: float


def _within_a_day(a, b):
    try:
        return abs(datetime.strptime(a[:10], "%Y-%m-%d") - datetime.strptime(b[:10], "%Y-%m-%d")) <= timedelta(days=1)
    except (TypeError, ValueError):
        return False


class Matcher:
    def __init__(self, conn, tmdb=None, now=None):
        self.conn, self.tmdb, self.now = conn, tmdb, now or db.now()
        self.stats = Counter()
        self._find_cache = {}
        self._titles = {}

    # ------------------------------------------------------------ local lookups
    def _media_by(self, kind, column, value):
        row = self.conn.execute(f"SELECT id FROM media WHERE kind = ? AND {column} = ?", (kind, value)).fetchone()
        return row["id"] if row else None

    def _title_index(self, kind):
        if kind not in self._titles:
            index = defaultdict(list)
            for row in self.conn.execute("SELECT id, title, original_title, year, tmdb_id, tvdb_id, imdb_id FROM media WHERE kind = ?", (kind,)):
                for name in {norm_title(row["title"]), norm_title(row["original_title"])} - {""}:
                    index[name].append(row)
            self._titles[kind] = index
        return self._titles[kind]

    def _forget_titles(self):
        self._titles = {}

    # ------------------------------------------------------------ TMDB lookups
    def _find(self, external_id, source, kind):
        """Returns TMDB's first result for an external ID, None if TMDB does not know it. Raises Unavailable on failure."""
        if self.tmdb is None:
            raise Unavailable("TMDB is not configured")
        key = (external_id, source, kind)
        if key not in self._find_cache:
            try:
                result = self.tmdb.find(external_id, source)
            except Exception as exc:
                raise Unavailable(str(exc))
            hits = result.get("movie_results" if kind == "movie" else "tv_results") or []
            self._find_cache[key] = hits[0] if hits else None
        return self._find_cache[key]

    def _from_find(self, hit, kind, rec):
        title = hit.get("title") or hit.get("name") or rec.get("title")
        year = metadata._year(hit.get("release_date") or hit.get("first_air_date")) or rec.get("year")
        return metadata.ensure_media(self.conn, kind, hit["id"], title, year, rec.get("tvdb_id"), rec.get("imdb_id"))

    # ------------------------------------------------------------ matching
    def match(self, rec):
        """Returns (Match or None, [review candidates])."""
        kind = rec["kind"]
        tmdb_id, tvdb_id, imdb_id = (clean_id(rec.get(k)) for k in ("tmdb_id", "tvdb_id", "imdb_id"))
        rec = {**rec, "tmdb_id": tmdb_id, "tvdb_id": tvdb_id, "imdb_id": imdb_id}

        if tmdb_id:
            media_id = metadata.ensure_media(self.conn, kind, tmdb_id, rec.get("title"), rec.get("year"), tvdb_id, imdb_id)
            self._forget_titles()
            return Match(media_id, "tmdb_id", 1.0), self._conflicts(media_id, rec)

        try:
            if tvdb_id and kind == "show":
                local = self._media_by(kind, "tvdb_id", tvdb_id)
                if local:
                    return Match(local, "tvdb_id", 1.0), []
                hit = self._find(tvdb_id, "tvdb_id", kind)
                if hit:
                    self._forget_titles()
                    return Match(self._from_find(hit, kind, rec), "tvdb_via_tmdb_find", 0.95), []
            if imdb_id:
                local = self._media_by(kind, "imdb_id", imdb_id)
                if local:
                    return Match(local, "imdb_id", 0.98), []
                hit = self._find(imdb_id, "imdb_id", kind)
                if hit:
                    self._forget_titles()
                    return Match(self._from_find(hit, kind, rec), "imdb_via_tmdb_find", 0.95), []
        except Unavailable as exc:
            self.stats["tmdb_unavailable"] += 1
            logger.info(f"{rec['source']} {rec['key']}: {exc}; will retry.")
            return None, []

        return self._by_title(rec)

    def _conflicts(self, media_id, rec):
        row = self.conn.execute("SELECT tvdb_id, imdb_id, tmdb_id FROM media WHERE id = ?", (media_id,)).fetchone()
        reviews = []
        if rec.get("tvdb_id") and row["tvdb_id"] and rec["tvdb_id"] != row["tvdb_id"] and rec["kind"] == "show":
            reviews.append({"candidate_media_id": media_id, "candidate_tmdb_id": row["tmdb_id"], "confidence": 0.5,
                            "reason": f"TVDB ID {rec['tvdb_id']} on the source differs from {row['tvdb_id']} on the linked show."})
        return reviews

    @staticmethod
    def _ids_disagree(rec, row):
        return any(rec.get(k) and row[k] and str(rec[k]) != str(row[k]) for k in ("tmdb_id", "tvdb_id", "imdb_id"))

    def _by_title(self, rec):
        kind, key = rec["kind"], norm_title(rec.get("title"))
        year = rec.get("year")
        if not key:
            return None, []
        candidates = [c for c in self._title_index(kind).get(key, []) if not self._ids_disagree(rec, c)]
        same_year = [c for c in candidates if year and c["year"] == year]
        if len(same_year) == 1:      # namesakes from other years (remakes) do not matter: the year pins this one down
            return Match(same_year[0]["id"], "title_year", 0.9), []

        reviews, same_year_ids = [], {c["id"] for c in same_year}
        for c in candidates:
            if year and c["year"] and abs(c["year"] - year) == 1:
                reviews.append({"candidate_media_id": c["id"], "candidate_tmdb_id": c["tmdb_id"], "confidence": 0.7, "reason": "Same title, year differs by one."})
            elif c["id"] in same_year_ids:
                reviews.append({"candidate_media_id": c["id"], "candidate_tmdb_id": c["tmdb_id"], "confidence": 0.7, "reason": "Several media share this title and year."})
            elif not year:
                reviews.append({"candidate_media_id": c["id"], "candidate_tmdb_id": c["tmdb_id"], "confidence": 0.6, "reason": "Same title; the source has no year."})
        if reviews or candidates:
            return None, reviews
        return None, self._search(rec, key)

    def _search(self, rec, key):
        """TMDB title search for a record nothing local matches. Results are only ever suggestions."""
        if self.tmdb is None or not rec.get("title"):
            return []
        attempted = self.conn.execute("SELECT attempted_at FROM link_attempt WHERE source = ? AND source_key = ?", (rec["source"], rec["key"])).fetchone()
        if attempted and datetime.strptime(self.now, "%Y-%m-%d %H:%M:%S") - datetime.strptime(attempted["attempted_at"], "%Y-%m-%d %H:%M:%S") < timedelta(days=SEARCH_RETRY_DAYS):
            return []
        try:
            results = self.tmdb.search_movie(rec["title"], rec.get("year")) if rec["kind"] == "movie" else self.tmdb.search_tv(rec["title"], rec.get("year"))
        except Exception as exc:
            self.stats["tmdb_unavailable"] += 1
            logger.info(f"TMDB search for '{rec['title']}' failed: {exc}")
            return []
        self.conn.execute("INSERT INTO link_attempt (source, source_key, attempted_at) VALUES (?, ?, ?) ON CONFLICT(source, source_key) DO UPDATE SET attempted_at = excluded.attempted_at",
                          (rec["source"], rec["key"], self.now))
        year, reviews = rec.get("year"), []
        for r in results:
            names = {norm_title(r.get("title") or r.get("name")), norm_title(r.get("original_title") or r.get("original_name"))}
            if key not in names:
                continue
            r_year = metadata._year(r.get("release_date") or r.get("first_air_date"))
            if year and r_year == year:
                confidence, reason = 0.85, "TMDB search: same title and year."
            elif year and r_year and abs(r_year - year) == 1:
                confidence, reason = 0.7, "TMDB search: same title, year differs by one."
            elif not year:
                confidence, reason = 0.6, "TMDB search: same title; the source has no year."
            else:
                continue
            reviews.append({"candidate_media_id": None, "candidate_tmdb_id": str(r["id"]), "confidence": confidence, "reason": reason})
        return reviews

    # ------------------------------------------------------------ source records
    def records(self):
        c = self.conn
        for r in c.execute("SELECT * FROM src_jellyfin_item WHERE type IN ('movie', 'series') AND removed_at IS NULL"):
            yield {"source": "jellyfin", "key": r["item_id"], "kind": "movie" if r["type"] == "movie" else "show", "title": r["name"], "year": r["year"],
                   "tmdb_id": r["tmdb_id"], "tvdb_id": r["tvdb_id"], "imdb_id": r["imdb_id"]}
        for r in c.execute("SELECT * FROM src_radarr_movie WHERE removed_at IS NULL"):
            yield {"source": "radarr", "key": r["radarr_id"], "kind": "movie", "title": r["title"], "year": r["year"], "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"]}
        for r in c.execute("SELECT * FROM src_sonarr_series WHERE removed_at IS NULL"):
            title, year = split_title_year(r["title"])      # a year in the name ("Doctor Who (2023)") disambiguates; prefer it
            year = year or r["year"]
            yield {"source": "sonarr", "key": r["sonarr_id"], "kind": "show", "title": title, "year": year, "tmdb_id": r["tmdb_id"], "tvdb_id": r["tvdb_id"], "imdb_id": r["imdb_id"]}
        # Letterboxd/Serializd entries are kept even after removal from the list, so they stay linked and visible.
        for r in c.execute("""SELECT slug, MAX(title) AS title, MAX(year) AS year, MAX(tmdb_id) AS tmdb_id, MAX(imdb_id) AS imdb_id
                              FROM src_letterboxd_entry GROUP BY slug"""):
            yield {"source": "letterboxd", "key": r["slug"], "kind": "movie", "title": r["title"], "year": r["year"], "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"]}
        for r in c.execute("""SELECT serializd_show_id AS sid, MAX(title) AS title, MAX(year) AS year, MAX(tmdb_id) AS tmdb_id,
                              MAX(tvdb_id) AS tvdb_id, MAX(imdb_id) AS imdb_id FROM src_serializd_entry GROUP BY serializd_show_id"""):
            yield {"source": "serializd", "key": r["sid"], "kind": "show", "title": r["title"], "year": r["year"], "tmdb_id": r["tmdb_id"], "tvdb_id": r["tvdb_id"], "imdb_id": r["imdb_id"]}

    def _save_link(self, rec, match, existing):
        entity = "media"
        if existing is None:
            self.conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence, status) VALUES (?, ?, ?, ?, ?, ?, 'auto')",
                              (entity, match.media_id, rec["source"], rec["key"], match.method, match.confidence))
            self.stats["linked"] += 1
        elif match.confidence > existing["confidence"] or existing["entity_id"] != match.media_id:
            self.conn.execute("UPDATE link SET entity_id = ?, method = ?, confidence = ?, updated_at = ? WHERE id = ?",
                              (match.media_id, match.method, match.confidence, self.now, existing["id"]))
            self.stats["relinked"] += 1

    def _add_reviews(self, rec, reviews):
        for r in reviews:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO match_review (source, source_key, source_title, source_year, kind, candidate_media_id, candidate_tmdb_id, confidence, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["source"], rec["key"], rec.get("title"), rec.get("year"), rec["kind"], r["candidate_media_id"], r["candidate_tmdb_id"], r["confidence"], r["reason"]))
            self.stats["review_added"] += cur.rowcount

    def resolve_all(self, progress=None):
        progress = progress or (lambda message, force=False: None)
        records = list(self.records())
        for number, rec in enumerate(records, 1):
            if number % 25 == 0:
                progress(f"Matching {number} of {len(records)} source records")
            existing = self.conn.execute("SELECT * FROM link WHERE source = ? AND source_key = ? AND entity_type = 'media'", (rec["source"], rec["key"])).fetchone()
            if existing and (existing["status"] == "manual" or existing["confidence"] >= SKIP_THRESHOLD):
                continue
            match, reviews = self.match(rec)
            if match and match.confidence >= AUTO_THRESHOLD:
                self._save_link(rec, match, existing)
                if match.method == "title_year":
                    reviews = []
            elif existing is None and match is None and not reviews:
                self.stats["unresolved"] += 1
            self._add_reviews(rec, reviews)
            # Commit as we go: a record can mean a TMDB call, and the write lock must not be held across slow network calls.
            self.conn.commit()
        return dict(self.stats)


# ---------------------------------------------------------------- human decisions
def link_manually(conn, source, source_key, kind, tmdb_id, title=None, year=None, now=None):
    """Links a source record to the media for a TMDB ID, creating it if needed. Manual links are never overwritten."""
    media_id = metadata.ensure_media(conn, kind, tmdb_id, title, year)
    conn.execute(
        """INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence, status)
           VALUES ('media', ?, ?, ?, 'manual', 1.0, 'manual')
           ON CONFLICT(source, source_key) DO UPDATE SET entity_id = excluded.entity_id, method = 'manual', confidence = 1.0, status = 'manual', updated_at = ?""",
        (media_id, source, source_key, now or db.now()))
    conn.execute("UPDATE match_review SET status = CASE WHEN candidate_tmdb_id = ? THEN 'approved' ELSE 'dismissed' END WHERE source = ? AND source_key = ? AND status = 'pending'",
                 (str(tmdb_id), source, source_key))
    return media_id


def approve_review(conn, review_id):
    review = conn.execute("SELECT * FROM match_review WHERE id = ? AND status = 'pending'", (review_id,)).fetchone()
    if not review:
        return None
    kind = review["kind"] or ("show" if review["source"] in ("serializd", "sonarr") else "movie")
    return link_manually(conn, review["source"], review["source_key"], kind, review["candidate_tmdb_id"], review["source_title"], review["source_year"])


def dismiss_review(conn, review_id):
    cur = conn.execute("UPDATE match_review SET status = 'dismissed' WHERE id = ? AND status = 'pending'", (review_id,))
    return cur.rowcount == 1
