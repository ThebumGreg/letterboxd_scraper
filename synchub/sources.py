"""Where an episode came from, as plain sentences for the episode list. Only facts SyncHub has: it cannot prove that the file Sonarr downloaded is the
file Jellyfin plays, so each source is listed on its own."""
from synchub import dvr


def _marks(ids):
    return ",".join("?" * len(ids))


def for_episodes(conn, episode_ids):
    """{episode_id: [sentence, ...]}; an episode nobody has anything for is left out. Queries are batched."""
    out = {}
    ids = list(episode_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        jelly, sonarr, queue = {}, {}, {}
        for r in conn.execute(f"""SELECT l.entity_id AS eid, j.* FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                                  WHERE l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND j.removed_at IS NULL AND l.entity_id IN ({_marks(chunk)})""", chunk):
            jelly.setdefault(r["eid"], []).append(r)
        for r in conn.execute(f"""SELECT l.entity_id AS eid, s.has_file, s.monitored FROM link l JOIN src_sonarr_episode s ON s.sonarr_episode_id = l.source_key
                                  WHERE l.source = 'sonarr_episode' AND l.entity_type = 'episode' AND s.removed_at IS NULL AND l.entity_id IN ({_marks(chunk)})""", chunk):
            sonarr[r["eid"]] = (max(sonarr.get(r["eid"], (0, 0))[0], r["has_file"] or 0), max(sonarr.get(r["eid"], (0, 0))[1], r["monitored"] or 0))
        for r in conn.execute(f"""SELECT target_id, progress FROM processing_event WHERE ended_at IS NULL AND target_type = 'episode' AND provider = 'sonarr_queue'
                                  AND target_id IN ({_marks(chunk)})""", chunk):
            queue[r["target_id"]] = r["progress"]
        for eid in chunk:
            lines = []
            files = jelly.get(eid, [])
            has_file, monitored = sonarr.get(eid, (0, 0))
            for j in files:
                if j["is_dvr"]:
                    label, percent = dvr.completeness(conn, j)
                    detail = {"full": "full recording", "short": f"short recording, about {percent}% of the scheduled length",
                              "unknown": "length unknown"}[label]
                    when = f", recorded {j['date_created'][:10]}" if j["date_created"] else ""
                    lines.append(f"DVR recording in Jellyfin ({detail}{when})")
            if any(not j["is_dvr"] for j in files):
                lines.append("In the Jellyfin library" + ("" if has_file else " (source unknown)"))
            if has_file:
                lines.append("Sonarr has a downloaded file")
            elif eid in queue:
                pct = f" ({round(queue[eid] * 100)}%)" if queue[eid] is not None else ""
                lines.append(f"Sonarr is downloading it{pct}")
            elif monitored:
                lines.append("Sonarr is monitoring it, not downloaded yet")
            if lines:
                out[eid] = lines
    return out
