# SyncHub v2 — Data Model and Logic Redesign

Status: **approved; in progress** (Phases 0-1 implemented in `synchub/`). The current app keeps running unchanged until cutover.

## 1. Decisions already made

| Topic | Decision |
|---|---|
| Database | Fresh database file (`data/synchub.db`). The current `data/letterboxd.db` is never touched and stays as a backup. |
| Metadata authority | **TMDB is primary**; TVDB is the backup and the bridge to Sonarr. Matching uses several signals, never one ID alone. |
| TV coverage | Every episode of a series is stored, including ones not in the library and not yet aired. |
| Specials | Season 0 is stored, shown **last**, labelled **"Specials (Season 0)"**, collapsed by default, and **excluded from progress totals and "request all"**. Individual specials can still be requested. |
| Unaired episodes | Shown as **"Not aired"**; not counted as Missing. |
| States | Missing, Queue, Downloading, **Downloaded** (file exists, Jellyfin has not picked it up), In Library. **Transferring** and **Encoding** come later from your own scripts. All states live in a table, so adding one needs no schema change. |
| Kept data | Items removed from Letterboxd stay in the database. Deleting a profile does not delete its requests. Serializd pruning is unchanged for now. Items marked `jf_missing` are not re-requested automatically. |
| Profiles | One Letterboxd username, one Serializd username and one Jellyfin user may belong to **only one profile** (enforced by the database). |
| Existing data | **Nothing is migrated.** The new app starts empty: settings and profiles are entered again and everything else is re-synced from the sources. |

## 2. What the data export showed

| Finding | Evidence | Consequence |
|---|---|---|
| Jellyfin episodes cannot be linked to their show | 564 of 607 episode rows carry the episode's own TVDB ID (e.g. *The Last Ship* S03E10 = 5648401; series = 269533). The series ID appears only as a fallback when an episode has none. `SeriesId` is never stored. | Only 3 of 23 series get episodes. This is the show-page bug. Episodes must link to shows through Jellyfin's `SeriesId`, never through provider IDs. |
| Titles alone are unsafe | *Alice in Wonderland* 1999/1951, *The Jungle Book* 2016/1967. Jellyfin calls a show *Special Ops: Lioness*, Sonarr calls it *Lioness*. | Titles are a supporting signal only. Year and IDs come first. |
| Dashes and years vary | *Breaking Dawn – Part 1* vs *- Part 1*; Sonarr titles like *Doctor Who (2023)*. | Titles are normalised before comparison. |
| Duplicate catalog rows | e.g. *Toy Story 4*, *The Shining*: one `jf-…` row with a TMDB ID plus one Letterboxd row with none (153 movies had no ID). | Unresolved items are matched by TMDB lookup, then by title and year, and low-confidence matches go to review. |
| Sonarr only knows TVDB | All 16 Sonarr rows are TVDB-only; 4 shows (*Daria*, *Doctor Who (2023)*, *Black Mirror*, *The Rain*) have no TMDB ID. | TMDB `/find` maps TVDB and IMDb IDs to TMDB IDs. |
| Specials are large | 485 of 759 cached episodes are Season 0 (*Game of Thrones* 300, *Outlander* 86). | Specials are collapsed and excluded from totals (decided). |
| Irreplaceable data | 42 manual links, 359 Letterboxd requests (212 Watched), settings. | Imported once; everything else is re-synced. |
| No download progress | `queue_status` / `queue_progress` are empty in all 1,841 rows. | The Radarr and Sonarr queues are ingested. |
| Duplicate profiles | 3 Letterboxd usernames appear in two profiles each (left by the old startup seeding, since fixed). | Uniqueness is enforced in the new schema. |

## 3. Principles

1. **Three layers**: source tables (what each system says) → identity (which real movie or show it is) → state (where it is in the pipeline). Pages read only the top layer.
2. **Sources are never deleted.** Rows are upserted and carry `first_seen`, `last_seen` and `removed_at`.
3. **Identity is stable.** `media.id` never changes. The catalog is never "deleted and rebuilt".
4. **Links are explicit and auditable.** Every link records the method, confidence and status. Manual decisions are stored and survive re-syncs.
5. **Episodes are keyed by (show, season, episode)**, with provider IDs as secondary data.
6. **No network calls while a page renders.**
7. **States are data, not code.**

## 4. Schema

Plain SQLite and `sqlite3`. A `schema_version` table with numbered migration files replaces `_ensure_column`.

### 4.1 Core

- `schema_version(version, applied_at)`
- `app_setting(key PK, value)`, copied from the current table.
- `task_status` as today.
- `profile(id, name UNIQUE, letterboxd_username UNIQUE, serializd_username UNIQUE, jellyfin_user_id UNIQUE, is_active)`. The three unique columns allow NULL but never an empty string.

### 4.2 Source layer (append and update only)

Every table has `first_seen`, `last_seen`, `removed_at`. Raw payloads are kept where useful.

| Table | Key columns |
|---|---|
| `src_letterboxd_entry` | `username` (not a profile ID, so deleting and recreating a profile re-attaches its entries), `slug`, `list` (watchlist or watched), `title`, `year`, `tmdb_id`, `imdb_id`. UNIQUE(username, slug, list). Entries no longer on a completely scraped list get `removed_at`; they stay visible but no longer count as active demand for auto-add. |
| `src_serializd_entry` | `profile_id`, `serializd_show_id`, `title`, `tmdb_id`, `tvdb_id`, `imdb_id`, `season`. |
| `src_jellyfin_item` | `item_id` PK, `type` (movie, series, season, episode), **`series_id`**, **`season_id`**, `name`, `series_name`, `year`, `season_number`, `episode_number`, `premiere_date`, `tmdb_id`, `tvdb_id`, `imdb_id`. |
| `src_radarr_movie` | `radarr_id`, `tmdb_id`, `imdb_id`, `title`, `year`, `monitored`, `has_file`. |
| `src_sonarr_series` | `sonarr_id`, `tvdb_id`, `tmdb_id` and `imdb_id` if Sonarr supplies them, `title`, `year`, `monitored`. |
| `src_sonarr_episode` | `sonarr_episode_id`, `sonarr_id`, `season`, `episode`, `tvdb_episode_id`, `air_date`, `monitored`, `has_file`. |
| `src_arr_queue` | `service`, `queue_id`, target (movie id or episode id), `status`, `progress`, `size`, `time_left`. |

### 4.3 Identity layer

- **`media`**: `id`, `kind` (movie or show), `title`, `original_title`, `year`, `tmdb_id`, `tvdb_id`, `imdb_id`, `status`, `first_air_date`, `poster_path`, `metadata_refreshed_at`. UNIQUE(kind, tmdb_id) and UNIQUE(kind, tvdb_id) where not NULL.
- **`episode`**: `id`, `media_id`, `season_number`, `episode_number` (TMDB numbering), `title`, `overview`, `air_date`, `runtime`, `tmdb_episode_id`, `tvdb_episode_id`, `tvdb_season_number`, `tvdb_episode_number` (Sonarr numbering), `numbering_status` (same, mapped, unmapped). UNIQUE(media_id, season_number, episode_number).
- **`link`**: `entity_type` (media or episode), `entity_id`, `source`, `source_key`, `method`, `confidence`, `status` (auto or manual), `created_at`. UNIQUE(source, source_key). A dismissed suggestion is remembered in `match_review`, not in `link`.
- **`match_review`**: persisted review items (source record, candidate media, confidence, reason, pending, approved or dismissed). This replaces the computed suggestions in the Resolution Center.

### 4.4 State layer

- **`state_def`**: `code` PK, `label`, `rank`, `color`, `icon`, `is_terminal`. Seeds:

  | code | rank |
  |---|---|
  | missing | 0 |
  | queued | 10 |
  | downloading | 20 |
  | downloaded | 30 |
  | transferring | 40 |
  | encoding | 50 |
  | in_library | 100 |

  Only the first three and the last two are active at launch.
- **`processing_event`**: `target_type` (movie or episode), `target_id`, `provider` (e.g. `sonarr_queue`, `encode_script`), `state_code` FK, `progress`, `detail`, `external_ref`, `started_at`, `updated_at`, `ended_at`. Providers open and close events. Your future transfer and encode scripts will write here through a small authenticated API.
- **`item_state`**: `target_type`, `target_id`, `state_code`, `progress`, `updated_at`. It is updated in place by the state engine, never wiped.
- A show's progress is a rollup of its episodes. Counts per state are shown, plus a derived **Partial** label. Specials and unaired episodes are counted separately and excluded from the totals.

**State rules** (highest rank wins, per movie and per episode): present in Jellyfin → `in_library`; active `processing_event` → that state; file in Radarr or Sonarr but not in Jellyfin → `downloaded`; in a queue → `downloading`; monitored with no file → `queued`; otherwise `missing`. Air date in the future and no file → displayed as **Not aired**.

## 5. Matching rules

Every link stores a score. Automatic links need a high score; middle scores go to `match_review`; low scores never link. **Two records that carry different IDs of the same kind are never merged.**

**Movies**
1. TMDB ID equal → auto.
2. IMDb or TVDB ID → TMDB via `/find` → auto.
3. Letterboxd page TMDB ID (current resolver) → auto.
4. Normalised title + year equal, single candidate → auto (0.9).
5. Year off by one → review (0.7). Title only → review at most.

**Shows**
1. TMDB ID, or TVDB / IMDb ID → TMDB via `/find` (the result is cached on `media`).
2. Normalised name + first-air year.
3. Tie-breaker: season and episode-count structure.

**Episodes**
1. Jellyfin episodes attach to their show through **`series_id`** only. Then (season, episode) in TMDB numbering, which matches your Jellyfin. Then episode-level IDs. Then air date plus title similarity.
2. Sonarr episodes use TVDB numbering. Match via `tvdb_episode_id` (TMDB's episode `external_ids`), else (season, episode) if the show's numbering agrees on sampled air dates, else air date + title. Anything unresolved is reviewed.

## 6. TV metadata

For each show, TMDB supplies the show and all seasons including Season 0, in batched calls. Refresh policy: airing shows frequently, ended shows rarely, plus a manual "Refresh" button. Sonarr data supplies monitored and file flags only.

## 7. UI impact

The layout stays. Templates change only where they decide state themselves.
- **Show details** reads one query: seasons ascending, then **Specials (Season 0)** last and collapsed, with per-state counts from `item_state`.
- **Dashboard and movie page** read `media` + `item_state`.
- **Badges** use `state_def`, so a new state appears with no template edit.
- **Resolution Center** reads `match_review`.

## 8. Starting fresh (no migration)

1. `data/synchub.db` is created from the migrations on first start. The old `data/letterboxd.db` is not read or modified.
2. Enter settings (API keys, URLs) and create profiles in the new app. Uniqueness is enforced.
3. Run the syncs: Jellyfin, Radarr, Sonarr, Letterboxd, Serializd, then TMDB metadata. Everything is rebuilt from the sources.
4. Manual ID links and dismissed suggestions from the old app are not carried over; they are made again through the Resolution Center if still needed.

The old and new apps can run side by side (new one on another port) until you are happy, then you cut over.

## 9. Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 | Migration framework, schema, `state_def` seeds | Fresh database creates cleanly; schema tests pass |
| 1 | Source ingest (Jellyfin with series links, Radarr, Sonarr with episodes and queue, Letterboxd and Serializd entries) | Re-running a sync changes nothing; removed items get `removed_at`; no row is ever deleted |
| 2 | TMDB metadata, matching engine, review queue | Test suite built from the messy cases above passes (dashes, same-title films, TVDB-only shows) |
| 3 | State engine, queue ingest, `processing_event` | State table matches the rules for movies and episodes |
| 4 | UI switched to the new tables | Show page shows the correct episodes for every show in your export |
| 5 | Side-by-side run, cutover, retire old modules | You approve the cutover |
| 6 *(later)* | Authenticated API for transfer and encode scripts | A script can open and close a Transferring or Encoding event |

## 10. Risks and open items

- **Sonarr numbering** differs from TMDB for some shows, so `numbering_status` and the review queue are essential rather than optional.
- **Jellyfin `SeriesId`** is standard in Jellyfin's item data. I will confirm it against your server in Phase 1 before building on it.
- **Sonarr / Radarr versions:** whether Sonarr returns TMDB IDs depends on the version, so the `/find` bridge is the safe path.
- **TMDB key** is required for the metadata layer, and all TV pages depend on it.
- The Dynu payload and whether Sonarr searches after monitoring are still unverified against live services.
- Security stays out of scope for now, apart from the planned authentication on the future processing API.
