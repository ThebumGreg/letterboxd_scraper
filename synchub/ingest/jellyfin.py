"""Jellyfin -> src_jellyfin_item. Episodes keep the ID of the series they belong to, so they never depend on provider IDs."""
from synchub import db, dvr
from synchub.ingest.common import mark_unseen, touch
from synchub.text import clean_id

TYPE_MAP = {"Movie": "movie", "Series": "series", "Season": "season", "Episode": "episode"}
SCOPES = {"movies": ("Movie",), "tv": ("Series", "Season", "Episode"), "all": ("Movie", "Series", "Season", "Episode")}


def _provider(item, name):
    for key, value in (item.get("ProviderIds") or {}).items():
        if key.lower() == name:
            return clean_id(value)
    return None


def sync(conn, client, scope="all", now=None, progress=None):
    now = now or db.now()
    progress = progress or (lambda message, force=False: None)
    jf_types = SCOPES[scope]
    # fetch fully before touching the database; a "Virtual" item is a placeholder Jellyfin makes for an episode it has no file for
    items = [i for i in client.iter_items(jf_types) if i.get("Type") in TYPE_MAP and i.get("LocationType") != "Virtual"]
    progress(f"Read {len(items)} item(s) from Jellyfin; saving them...", force=True)

    season_series = {i["Id"]: (i.get("SeriesId") or i.get("ParentId")) for i in items if i.get("Type") == "Season"}
    series_ids = {i["Id"] for i in items if i.get("Type") == "Series"}

    folders = dvr.prefixes(conn)
    seen = []
    for item in items:
        kind = TYPE_MAP[item["Type"]]
        series_id = None
        if kind == "season":
            series_id = season_series.get(item["Id"])
        elif kind == "episode":
            series_id = item.get("SeriesId")
            if not series_id:
                parent = item.get("SeasonId") or item.get("ParentId")
                series_id = season_series.get(parent) or (parent if parent in series_ids else None)
        season_number = item.get("ParentIndexNumber") if kind == "episode" else (item.get("IndexNumber") if kind == "season" else None)
        touch(conn, "src_jellyfin_item", ("item_id",), {
            "item_id": item["Id"], "type": kind, "series_id": series_id,
            "season_id": item.get("SeasonId") or (item.get("ParentId") if kind == "episode" else None),
            "name": item.get("Name"), "series_name": item.get("SeriesName"), "year": item.get("ProductionYear"),
            "season_number": season_number, "episode_number": item.get("IndexNumber") if kind == "episode" else None,
            "premiere_date": (item.get("PremiereDate") or "")[:10] or None,
            "tmdb_id": _provider(item, "tmdb"), "tvdb_id": _provider(item, "tvdb"), "imdb_id": _provider(item, "imdb"),
            "date_created": (item.get("DateCreated") or "")[:19].replace("T", " ") or None,
            "path": item.get("Path"), "runtime_seconds": round(item["RunTimeTicks"] / 10_000_000) if item.get("RunTimeTicks") else None, "is_dvr": 1 if dvr.is_dvr_path(item.get("Path"), folders) else 0,
        }, now, keep=("date_created", "runtime_seconds"))                # a sync that does not give the date never erases one we have
        seen.append(item["Id"])

    types = ", ".join(f"'{TYPE_MAP[t]}'" for t in jf_types)
    removed = mark_unseen(conn, "src_jellyfin_item", ("item_id",), seen, now, scope_sql=f"type IN ({types})")
    return {"seen": len(seen), "removed": removed}
