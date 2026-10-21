#!/usr/bin/env python3
"""Read-only export of the SyncHub database for design review.

Writes a folder containing:
  schema.sql       every CREATE statement
  summary.json     row counts, per-column stats and ID-matching diagnostics
  tv_shows.json    a cross-source view of sample TV shows (flagged ones first)
  samples/*.csv    sample rows per table

The database is opened read-only and is never modified. Secrets in app_settings are redacted,
and api_payload_logs / ddns tables are exported as counts only. Use --anonymize-users to also
replace profile and user names. Review the output before sharing it.

Usage (from the project root):
  python scripts/export_db_sample.py
  python scripts/export_db_sample.py --anonymize-users --zip
  python scripts/export_db_sample.py --show "Breaking Bad" --show "The Office"
"""
import argparse
import csv
import json
import random
import re
import sqlite3
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "letterboxd.db"

SECRET_HINTS = ("key", "password", "token", "secret", "apprise")
MASKED_SETTINGS = {"dynu_domain", "dynu_hostname", "ddns_last_ip", "dynu_dns_id", "dynu_record_id"}
COUNTS_ONLY_TABLES = {"api_payload_logs", "ddns_history", "ddns_cache"}   # headers and IP addresses
USER_COLUMNS = {"username", "profile_name", "letterboxd_username", "serializd_username", "jellyfin_user_id"}
USER_LIST_COLUMNS = {"requested_by_users"}
EPISODE_TITLE_RE = re.compile(r"^(.*) - S\d+E\d+ \(")
_ARTICLE_RE = re.compile(r"^(the|a|an) ")


def norm(title):
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\(\d{4}\)\s*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return _ARTICLE_RE.sub("", text)


def blank(value):
    return value is None or str(value).strip() == ""


class Anonymizer:
    def __init__(self, enabled):
        self.enabled = enabled
        self.names = {}

    def name(self, value):
        if not self.enabled or blank(value):
            return value
        return self.names.setdefault(value, f"user{len(self.names) + 1}")

    def name_list(self, value):
        if not self.enabled or blank(value):
            return value
        return ",".join(self.name(p.strip()) for p in value.split(",") if p.strip())

    def row(self, row):
        if not self.enabled:
            return row
        out = dict(row)
        for col in out:
            if col in USER_COLUMNS:
                out[col] = self.name(out[col])
            elif col in USER_LIST_COLUMNS:
                out[col] = self.name_list(out[col])
        return out


def mask_setting(key, value):
    if blank(value):
        return value
    k = key.lower()
    if any(hint in k for hint in SECRET_HINTS) or k in MASKED_SETTINGS:
        return "<redacted>"
    if k.endswith("_url"):
        try:
            parsed = urlparse(value)
            if parsed.scheme:
                port = f":{parsed.port}" if parsed.port else ""
                return f"{parsed.scheme}://<host>{port}"
        except ValueError:
            pass
        return "<redacted>"
    return value


def connect(path):
    uri = f"file:{quote(Path(path).resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def rows_of(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def safe(fn, *args):
    try:
        return fn(*args)
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def table_names(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


# ---------------------------------------------------------------- schema / samples / column stats

def write_schema(conn, out):
    statements = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
    (out / "schema.sql").write_text(";\n\n".join(r["sql"] for r in statements) + ";\n", encoding="utf-8")


def column_stats(conn, table, anon):
    stats = {}
    for col in [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
        row = conn.execute(f'SELECT COUNT(*) - COUNT("{col}") AS nulls, COUNT(DISTINCT "{col}") AS distinct_values FROM "{table}"').fetchone()
        info = {"nulls": row["nulls"], "distinct": row["distinct_values"]}
        # Never list raw values for settings (secrets) or user-identifying columns.
        if 0 < row["distinct_values"] <= 12 and table != "app_settings" and col not in USER_COLUMNS | USER_LIST_COLUMNS:
            info["values"] = {str(r[0]): r[1] for r in conn.execute(f'SELECT "{col}", COUNT(*) FROM "{table}" GROUP BY "{col}" ORDER BY COUNT(*) DESC')}
        stats[col] = info
    return stats


def write_sample(conn, table, out, limit, anon):
    if table == "app_settings":
        rows = [{"key": r["key"], "value": mask_setting(r["key"], r["value"])} for r in conn.execute("SELECT key, value FROM app_settings ORDER BY key")]
    else:
        rows = [anon.row(dict(r)) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY RANDOM() LIMIT ?', (limit,))]
    if not rows:
        return 0
    with open(out / "samples" / f"{table}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- diagnostics

def id_coverage(conn, table, group_by):
    return rows_of(conn, f"""
        SELECT {group_by}, COUNT(*) AS total,
          SUM(CASE WHEN COALESCE(tmdb_id,'')!='' AND COALESCE(tvdb_id,'')!='' THEN 1 ELSE 0 END) AS both_ids,
          SUM(CASE WHEN COALESCE(tmdb_id,'')!='' AND COALESCE(tvdb_id,'')=''  THEN 1 ELSE 0 END) AS tmdb_only,
          SUM(CASE WHEN COALESCE(tmdb_id,'')=''  AND COALESCE(tvdb_id,'')!='' THEN 1 ELSE 0 END) AS tvdb_only,
          SUM(CASE WHEN COALESCE(tmdb_id,'')=''  AND COALESCE(tvdb_id,'')=''  THEN 1 ELSE 0 END) AS no_ids
        FROM {table} GROUP BY {group_by}""")


def catalog_duplicates(conn):
    result = {}
    for col in ("tvdb_id", "tmdb_id"):
        result[f"same_{col}"] = rows_of(conn, f"""
            SELECT media_type, {col}, COUNT(*) AS n, GROUP_CONCAT(title, ' | ') AS titles FROM media_catalog
            WHERE COALESCE({col},'')!='' GROUP BY media_type, {col} HAVING n > 1 LIMIT 50""")
    by_name = defaultdict(list)
    for r in conn.execute("SELECT title, release_year, media_type, tmdb_id, tvdb_id, movie_slug FROM media_catalog"):
        by_name[(r["media_type"], norm(r["title"]))].append(dict(r))
    result["same_title_different_rows"] = [
        {"media_type": k[0], "normalized_title": k[1], "rows": v} for k, v in by_name.items() if len(v) > 1 and k[1]
    ][:50]
    return result


def tv_episode_cache_diag(conn):
    return {
        "totals": rows_of(conn, """
            SELECT COUNT(*) AS total, SUM(tvdb_id='NONE') AS tvdb_none, SUM(tmdb_id='NONE') AS tmdb_none,
                   SUM(tvdb_id='NONE' AND tmdb_id='NONE') AS both_none, COUNT(DISTINCT tvdb_id || '/' || tmdb_id) AS id_pairs
            FROM tv_episodes""")[0],
        "episode_cached_under_several_id_pairs": rows_of(conn, """
            SELECT tmdb_id, season_number, episode_number, COUNT(*) AS n FROM tv_episodes
            WHERE tmdb_id != 'NONE' GROUP BY tmdb_id, season_number, episode_number HAVING n > 1 LIMIT 50"""),
    }


def jellyfin_episode_diag(conn):
    base = "FROM service_cache WHERE service='jellyfin' AND media_type='episode'"
    orphan = f"{base} AND COALESCE(tvdb_id,'')='' AND COALESCE(tmdb_id,'')=''"
    names = Counter()
    for r in conn.execute(f"SELECT title {orphan}"):
        m = EPISODE_TITLE_RE.match(r["title"] or "")
        names[m.group(1) if m else "(unparseable title)"] += 1
    return {
        "episodes_total": conn.execute(f"SELECT COUNT(*) {base}").fetchone()[0],
        "episodes_without_any_id": conn.execute(f"SELECT COUNT(*) {orphan}").fetchone()[0],
        "episodes_without_season_or_episode_number": conn.execute(f"SELECT COUNT(*) {base} AND (season_number IS NULL OR episode_number IS NULL)").fetchone()[0],
        "series_names_of_episodes_without_ids": dict(names.most_common(40)),
    }


def slug_prefixes(conn, table, column):
    counts = Counter()
    for r in conn.execute(f"SELECT {column} AS slug FROM {table}"):
        slug = r["slug"] or ""
        counts[next((p for p in ("serializd-", "jf-", "radarr-", "sonarr-") if slug.startswith(p)), "(letterboxd-style / other)" if slug else "(empty)")] += 1
    return dict(counts)


def build_diagnostics(conn, tables):
    diag = {}
    has = tables.__contains__
    if has("user_requests"):
        diag["user_requests"] = {
            "id_coverage": safe(id_coverage, conn, "user_requests", "media_type"),
            "slug_prefixes": safe(slug_prefixes, conn, "user_requests", "movie_slug"),
            "rows_by_list_and_type": safe(rows_of, conn, "SELECT media_type, letterboxd_lists, COUNT(*) AS n FROM user_requests GROUP BY media_type, letterboxd_lists ORDER BY n DESC LIMIT 30"),
            "id_source": safe(rows_of, conn, "SELECT COALESCE(id_source,'(none)') AS id_source, COUNT(*) AS n FROM user_requests GROUP BY id_source"),
            "manual_or_dismissed (not rebuildable from sources)": safe(rows_of, conn, "SELECT SUM(id_source='manual') AS manual_links, SUM(id_source='rejected') AS rejected_guesses, SUM(ignore_match=1) AS dismissed FROM user_requests"),
        }
    if has("service_cache"):
        diag["service_cache"] = {
            "id_coverage": safe(id_coverage, conn, "service_cache", "service, media_type"),
            "status_counts": safe(rows_of, conn, "SELECT service, media_type, status, in_library, COUNT(*) AS n FROM service_cache GROUP BY 1,2,3,4 ORDER BY 1,2,3"),
            "jellyfin_episodes": safe(jellyfin_episode_diag, conn),
        }
    if has("media_catalog"):
        diag["media_catalog"] = {
            "id_coverage": safe(id_coverage, conn, "media_catalog", "media_type"),
            "slug_prefixes": safe(slug_prefixes, conn, "media_catalog", "movie_slug"),
            "status_counts": safe(rows_of, conn, "SELECT media_type, status, COUNT(*) AS n FROM media_catalog GROUP BY 1,2 ORDER BY 1,2"),
            "duplicates": safe(catalog_duplicates, conn),
        }
    if has("tv_episodes"):
        diag["tv_episodes_cache"] = safe(tv_episode_cache_diag, conn)
    if has("user_profile_mappings"):
        diag["profiles"] = safe(rows_of, conn, "SELECT COUNT(*) AS profiles, SUM(is_active=1) AS active, SUM(COALESCE(letterboxd_username,'')!='') AS with_letterboxd, SUM(COALESCE(serializd_username,'')!='') AS with_serializd, SUM(COALESCE(jellyfin_user_id,'')!='') AS with_jellyfin FROM user_profile_mappings")
    return diag


# ---------------------------------------------------------------- cross-source TV view

def where_ids(show, prefix=""):
    clauses, params = [], []
    for col in ("tvdb_id", "tmdb_id"):
        if not blank(show.get(col)):
            clauses.append(f"{prefix}{col} = ?")
            params.append(str(show[col]))
    if not blank(show.get("movie_slug")):
        clauses.append(f"{prefix}movie_slug = ?")
        params.append(show["movie_slug"])
    return clauses, params


def build_tv_view(conn, tables, anon, how_many, wanted_titles, seed):
    if "media_catalog" not in tables:
        return {"error": "media_catalog table not found"}
    shows = rows_of(conn, "SELECT * FROM media_catalog WHERE media_type IN ('tv','episode')")
    catalog_by_name = defaultdict(list)
    for s in shows:
        catalog_by_name[norm(s["title"])].append(s)

    jf_episodes = rows_of(conn, "SELECT season_number, episode_number, status, tvdb_id, tmdb_id, movie_slug, title FROM service_cache WHERE service='jellyfin' AND media_type='episode'") if "service_cache" in tables else []
    by_tvdb, by_tmdb, with_slug = defaultdict(list), defaultdict(list), []
    orphan_by_name = defaultdict(list)
    for ep in jf_episodes:
        if not blank(ep["tvdb_id"]):
            by_tvdb[str(ep["tvdb_id"])].append(ep)
        if not blank(ep["tmdb_id"]):
            by_tmdb[str(ep["tmdb_id"])].append(ep)
        if not blank(ep["movie_slug"]):
            with_slug.append(ep)
        if blank(ep["tvdb_id"]) and blank(ep["tmdb_id"]):
            m = EPISODE_TITLE_RE.match(ep["title"] or "")
            if m:
                orphan_by_name[norm(m.group(1))].append(ep)

    service_tv = rows_of(conn, "SELECT service, title, year, media_type, tvdb_id, tmdb_id, movie_slug, external_item_id, status, in_library FROM service_cache WHERE media_type IN ('tv','series')") if "service_cache" in tables else []
    service_by_name = defaultdict(list)
    for r in service_tv:
        service_by_name[norm(r["title"])].append(r)

    views = []
    for show in shows:
        clauses, params = where_ids(show)
        view = {"catalog": {k: show[k] for k in ("title", "release_year", "tvdb_id", "tmdb_id", "imdb_id", "movie_slug", "status", "in_jellyfin", "in_sonarr", "available_seasons", "user_count")}}
        view["catalog"]["requested_by_users"] = anon.name_list(show.get("requested_by_users"))
        flags = []

        if "user_requests" in tables and clauses:
            req_clauses, req_params = where_ids(show)
            reqs = rows_of(conn, f"SELECT username, movie_slug, tvdb_id, tmdb_id, id_source, letterboxd_lists, season_number FROM user_requests WHERE media_type IN ('tv','episode') AND ({' OR '.join(req_clauses)})", req_params)
            view["requests"] = [anon.row(r) for r in reqs]

        id_matched_services = []
        if "service_cache" in tables and clauses:
            id_matched_services = rows_of(conn, f"SELECT service, title, year, tvdb_id, tmdb_id, movie_slug, external_item_id, status, in_library FROM service_cache WHERE media_type IN ('tv','series') AND ({' OR '.join(clauses)})", params)
        view["service_rows_matched_by_id"] = id_matched_services
        matched_keys = {(r["service"], r["external_item_id"]) for r in id_matched_services}
        name_only = [r for r in service_by_name.get(norm(show["title"]), []) if (r["service"], r["external_item_id"]) not in matched_keys]
        view["service_rows_matched_by_name_only"] = name_only
        if name_only:
            flags.append("service row has the same name but different/missing IDs (possible split)")

        tvdb, tmdb, slug = str(show.get("tvdb_id") or ""), str(show.get("tmdb_id") or ""), show.get("movie_slug") or ""
        matched = {"by_tvdb": by_tvdb.get(tvdb, []) if tvdb else [], "by_tmdb": by_tmdb.get(tmdb, []) if tmdb else [],
                   "by_slug_prefix": [e for e in with_slug if slug and e["movie_slug"].startswith(slug)]}
        id_keys = {id(e) for k in ("by_tvdb", "by_tmdb") for e in matched[k]}
        slug_only = [e for e in matched["by_slug_prefix"] if id(e) not in id_keys]
        title_only = orphan_by_name.get(norm(show["title"]), [])
        conflicting = [e for e in matched["by_tmdb"] if tvdb and not blank(e["tvdb_id"]) and str(e["tvdb_id"]) != tvdb]
        conflicting += [e for e in matched["by_tvdb"] if tmdb and not blank(e["tmdb_id"]) and str(e["tmdb_id"]) != tmdb]
        every = {id(e): e for k in matched for e in matched[k]}
        per_episode = Counter((e["season_number"], e["episode_number"]) for e in every.values())
        seasons = Counter(e["season_number"] for e in every.values())
        view["jellyfin_episodes"] = {
            "matched_by_tvdb": len(matched["by_tvdb"]), "matched_by_tmdb": len(matched["by_tmdb"]),
            "matched_only_by_slug_prefix": len(slug_only), "unlinked_but_title_starts_with_show_name": len(title_only),
            "rows_with_conflicting_ids": len(conflicting), "duplicate_season_episode_pairs": sum(1 for n in per_episode.values() if n > 1),
            "rows_per_season": {str(k): v for k, v in sorted(seasons.items(), key=lambda kv: (kv[0] is None, kv[0]))},
            "jf_missing_rows": sum(1 for e in every.values() if e["status"] == "jf_missing"),
        }
        if slug_only: flags.append("episodes matched only via slug prefix")
        if title_only: flags.append("episodes without IDs that carry this show's name")
        if conflicting: flags.append("episodes with conflicting TVDB/TMDB IDs")
        if view["jellyfin_episodes"]["duplicate_season_episode_pairs"]: flags.append("duplicate season/episode rows")

        if "tv_episodes" in tables and clauses:
            id_clauses, id_params = where_ids({"tvdb_id": show.get("tvdb_id"), "tmdb_id": show.get("tmdb_id")})
            if id_clauses:
                view["tv_episodes_cache"] = rows_of(conn, f"SELECT tvdb_id, tmdb_id, season_number, COUNT(*) AS episodes FROM tv_episodes WHERE {' OR '.join(id_clauses)} GROUP BY 1,2,3 ORDER BY 3", id_params)

        if blank(show.get("tvdb_id")) and blank(show.get("tmdb_id")): flags.append("no TVDB or TMDB ID")
        elif blank(show.get("tvdb_id")): flags.append("TMDB only (no TVDB ID)")
        if len(catalog_by_name[norm(show["title"])]) > 1: flags.append("several catalog rows share this title")
        view["flags"] = flags
        views.append(view)

    wanted = [v for v in views if any(w.lower() in (v["catalog"]["title"] or "").lower() for w in wanted_titles)]
    flagged = sorted((v for v in views if v["flags"] and v not in wanted), key=lambda v: -len(v["flags"]))
    rest = [v for v in views if not v["flags"] and v not in wanted]
    random.Random(seed).shuffle(rest)
    chosen = (wanted + flagged + rest)[:max(how_many, len(wanted))]
    return {"total_tv_shows_in_catalog": len(views), "shows_with_flags": sum(1 for v in views if v["flags"]),
            "flag_counts": dict(Counter(f for v in views for f in v["flags"]).most_common()), "shows": chosen}


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only SyncHub database sample export.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to the SQLite file (default: data/letterboxd.db)")
    parser.add_argument("--out", help="output folder (default: db_sample_<timestamp> in the current directory)")
    parser.add_argument("--rows", type=int, default=25, help="sample rows per table (default 25)")
    parser.add_argument("--shows", type=int, default=30, help="TV shows in tv_shows.json (default 30)")
    parser.add_argument("--show", action="append", default=[], help="always include shows whose title contains this text (repeatable)")
    parser.add_argument("--anonymize-users", action="store_true", help="replace profile/user names with user1, user2, ...")
    parser.add_argument("--zip", action="store_true", help="also create <out>.zip")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        sys.exit(f"Database not found: {db_path}")
    out = Path(args.out or f"db_sample_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    (out / "samples").mkdir(parents=True, exist_ok=True)

    anon = Anonymizer(args.anonymize_users)
    try:
        conn = connect(db_path)
        tables = table_names(conn)
    except sqlite3.Error as exc:
        sys.exit(f"Could not open {db_path} read-only: {exc}\nIf the app is running, copy data/letterboxd.db (and any -wal/-shm files beside it) and point --db at the copy.")

    write_schema(conn, out)
    summary = {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "database": db_path.name,
               "anonymized_users": args.anonymize_users, "tables": {}}
    for table in tables:
        info = {"rows": conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]}
        if table in COUNTS_ONLY_TABLES:
            info["note"] = "counts only (contains headers/IP addresses)"
        else:
            info["columns"] = safe(column_stats, conn, table, anon)
            info["sample_rows_written"] = write_sample(conn, table, out, args.rows, anon)
        summary["tables"][table] = info
    summary["diagnostics"] = build_diagnostics(conn, tables)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    tv = safe(build_tv_view, conn, tables, anon, args.shows, args.show, 1)
    (out / "tv_shows.json").write_text(json.dumps(tv, indent=2, default=str), encoding="utf-8")
    conn.close()

    if args.zip:
        archive = Path(f"{out}.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(out.parent))
        print(f"Zip written: {archive}")

    print(f"Export written to: {out}")
    print(f"  tables: {len(tables)} | TV shows in catalog: {tv.get('total_tv_shows_in_catalog', '?')} | flagged: {tv.get('shows_with_flags', '?')}")
    print("Secrets in app_settings are redacted; payload-log and DDNS tables are counts only.")
    print("Please skim the files before sharing them." + ("" if args.anonymize_users else " Tip: add --anonymize-users to hide profile names."))


if __name__ == "__main__":
    main()
