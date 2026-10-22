# SyncHub Encoding and Library Migration: Plan

Status: **Plan, awaiting approval. No code has been written.** Written from the Media Analysis and Migration tool you attached (`main.py`, `migrate.py`, `media_db.py`, `media_jobs.py`, `media_config.py`, `webui.py`, `media_recommend.py`, `media_layout.py`, `media_roots.py`, `media_report.py`, `review.py` and the three `.md` files). Nothing from the tool was run.

## 1. What you asked for

| # | Requirement | Where it lives in this plan |
|---|---|---|
| 1 | SyncHub is the **master controller**: queue, settings, progress, errors, in a web UI | §3, §4 |
| 2 | Two or three **worker PCs** do the encoding; the web app and media server stay light; more workers can be added later without big changes | §3, §5, §6 |
| 3 | A worker copies the source from the network share to local storage, encodes with FFmpeg, validates, then transfers the result to the Jellyfin share | §7 |
| 4 | **SyncHub tells Jellyfin** about the new file so the library updates | §8 |
| 5 | The original **analyze, review, migrate** workflow is kept as a **standalone feature on its own page**, separate from encoding management | §9 |
| 6 | **Movies above about 30 GB are compressed to about 25 GB**, keeping as much picture and sound quality as possible; the encoder looks at the source's quality, resolution, bitrate and so on | §10 |
| 7 | Radarr, the encoder and Jellyfin currently see the **same paths** | §7.4 |

## 2. What exists today

**SyncHub** already has: a Flask app and SQLite database; a state engine in which **Transferring** and **Encoding** are defined states fed by `processing_event` rows; live dashboard updates; Radarr/Sonarr webhooks and queue polling; a Jellyfin client; the profile picker.

**Your tool** is a careful batch pipeline for one machine: scan (ffprobe) → plain-language recommendation → human approval → migrate (copy, remux or re-encode) into the Jellyfin library. What is worth keeping, nearly unchanged:

| Piece | Why keep it |
|---|---|
| `media_recommend.py` | Turns a record into a queue (ready, suggested, worth a look, format conversion, needs you), a confidence (low is never bulk-approved) and an approve action. Pure logic. |
| `media_layout.py` | The one definition of the library layout: `Movies/<name>/<name>.ext` and `TV Shows/<Show>/Season NN/<name>.ext`. |
| `media_roots.py` | A file is identified by **root name + relative path**, never by a machine's path, so a UNC path on Windows and a mount on Linux are the same file. This is exactly the path mapping workers need. |
| `migrate.py` encode core | `.partial` file, then verify, then rename without overwriting. Verification checks tracks, languages, default flag, codecs, picture size, duration within 0.5 percent, and a full decode. The exact ffmpeg command is stored per attempt. ffmpeg `-progress` output is parsed. |
| `main.py` analysis | Stream audit, English-track policy, naming, duplicate detection by SHA-256, integrity checks. |
| `media_report.py` / `review.py` | The CSV columns and every review operation (approve-ready, approve-category, keep duplicate, reject, reset), which the web page must offer too. |

What does **not** carry over, and why:

| Limit in the tool | Consequence |
|---|---|
| One global migration lock: one migration at a time | Replaced by per-job **leases**, so several workers run at once. |
| SQLite file that "must be on a local disk" | Workers must never open the database. Only SyncHub does; workers use HTTP. |
| Progress only written to a log every 15 s | Replaced by structured progress sent to SyncHub every few seconds. |
| `webui.py`: standard-library server with its own token scheme | Rebuilt as SyncHub pages. Its queue and bulk-approval ideas are kept. |
| `media_roots.SOURCE_ROOTS` hard-coded in the file | Roots are managed in SyncHub (Settings), with per-worker overrides in the worker's own settings file. |

## 3. Architecture

```
                         ┌─────────────────────────────────────────────┐
  Radarr / Sonarr ─────► │ SyncHub (control plane, Flask + SQLite)     │ ◄──── Web UI
   (import webhook)      │  job queue · worker registry · size policy  │
                         │  review queues · state engine · live events │
                         └───────┬───────────────────────┬─────────────┘
                                 │ HTTPS/HTTP, workers   │ Jellyfin API
                                 │ PULL jobs, push       ▼
                                 │ progress          ┌──────────┐
                  ┌──────────────┼──────────────┐    │ Jellyfin │
                  ▼              ▼              ▼    └────▲─────┘
             Worker PC 1    Worker PC 2    Worker PC 3     │ notified by SyncHub
             (agent +       (agent +       (agent +        │ after each publish
              ffmpeg)        ffmpeg)        ffmpeg)        │
                  │              │              │          │
                  └──────── network shares ─────┴──────────┘
                    source share ──► local scratch ──► Jellyfin share
```

**Principles**

1. **SyncHub never touches media bytes.** It stores records and decisions and runs no ffmpeg. This keeps the web app and the media server light.
2. **Workers pull.** A worker asks SyncHub for work. No inbound ports on worker PCs, nothing to reconfigure when a PC changes address, and a new worker is "install the agent, give it SyncHub's address and a token".
3. **Decisions are central, work is local.** A worker probes a file and reports facts; SyncHub decides the plan (§6.3, §10), so rules can change in the UI without updating workers.
4. **Never overwrite, never lose the original silently.** Output goes to `.partial`, is verified, then renamed. Originals are kept for a retention period (§7.4).
5. **Two features, one engine.** Both the new-download pipeline and the library migration produce jobs for the same workers and the same publish-and-notify step, but they have separate pages (§4).

Rejected alternatives: a message broker (Redis/Celery: another service for two or three PCs), pushing jobs over SSH/RPC (firewalls, addresses, credentials on the server), workers writing to the database directly (SQLite over SMB is unsafe).

## 4. Pages

| Page | Purpose | Notes |
|---|---|---|
| **Workers** | Each worker: online/offline, version, OS, encoders found (x264, x265, NVENC, QSV), free scratch space, slots, current job. Enable, pause, **drain** (finish the current job, then stop), rename, create or revoke its token | Shared by both features |
| **Encoding** | Jobs created from downloads: queue, per-job stages, progress, speed, ETA, predicted and actual size, quality check, logs. Retry, cancel, pin to a worker, change priority | New downloads only |
| **Library** | The original tool as a standalone page: Overview (queues and counts), Review (one by one and as lists, bulk approve), Scan (start, progress), Migration (status of approved files), Export CSV | Its own nav item; works on its own (§9) |
| **Settings → Encoding** | Size policy (§10), encoder preferences, retention days, storage roots, worker defaults | |
| Dashboard | Unchanged. Transferring and Encoding badges with live progress come from the state engine | |

## 5. Data model (new migrations)

| Table | Holds |
|---|---|
| `storage_root` | Logical share name (`media`, `jellyfin`, `originals`...). One row per root. |
| `storage_alias` | How each *service* sees a root: Jellyfin's path, Radarr's, Sonarr's, SyncHub's own. Used to translate webhook paths and to tell Jellyfin the right path. (Workers keep their own alias list in their settings file.) |
| `worker` | Name, token (stored hashed), enabled, draining, tags, slots, last seen, version, OS, capabilities (JSON), free scratch bytes |
| `encode_job` | `kind` (`download` or `library`), links to media or library file, source and destination as root + relative path, plan and policy (JSON), status, stage, progress, fps, speed, ETA, attempts, lease expiry, assigned worker, pinned worker, required tags, priority, error class and text, cancel flag, result (JSON: sizes, verification, quality) |
| `encode_attempt` | One row per try: worker, start, end, exact ffmpeg command, last log lines, outcome |
| `library_file`, `library_attempt`, `library_event` | The tool's `media_files`, `migration_attempts` and `media_events`, **keeping the same column names** so the tool's pure modules work on these rows unchanged |

States stay data: an encode job's stage maps onto the existing state definitions (§6.2), so adding stages never needs a schema change.

## 6. Workers and jobs

### 6.1 Worker agent

- One Python file, **standard library only** (like your tool), plus FFmpeg on the PC. Runs on Windows or Linux; on Windows as a service (Task Scheduler or NSSM, instructions provided).
- Settings file in the style of `media_review.ini`: SyncHub address, token, scratch folder, **roots** (every way this PC reaches each share, as in `media_roots`), ffmpeg/ffprobe paths, slots, optional active hours.
- At start it reports its capabilities (ffmpeg version, which encoders actually work, CPU cores, scratch free) and removes stale scratch files and `.partial` files it left behind.

### 6.2 Protocol (JSON over HTTP, `Authorization: Bearer <worker token>`)

| Call | Meaning |
|---|---|
| `POST /api/worker/v1/register` | Name, protocol version, OS, capabilities, slots. Refused if the protocol version is not supported. |
| `POST /api/worker/v1/poll` | "I have N free slots and X GB scratch." Reply: a job (with plan), or nothing; plus control messages (pause, cancel job N). Long-poll about 20 s so new work starts promptly. |
| `POST /api/worker/v1/jobs/<id>/analysis` | Worker's ffprobe facts. Reply: the plan SyncHub decided (copy unchanged / remux / convert / compress with its settings), or "nothing to do". |
| `POST /api/worker/v1/jobs/<id>/progress` | Stage, percent, fps, speed, ETA, message. Doubles as the heartbeat. Reply: lease extended, and whether cancel was requested. |
| `POST /api/worker/v1/jobs/<id>/complete` | Destination, final size, verification results, quality score, exact command. SyncHub then **publishes** (§8). |
| `POST /api/worker/v1/jobs/<id>/fail` | Error class, message, last log lines, whether it is worth retrying. |
| `POST /api/worker/v1/scan/...` | Library scans (§9): the worker streams analysed records in batches. |

**Leases.** A claimed job has a lease (about 90 s) renewed by every progress message. A reaper (every 30 s) returns jobs of silent workers to the queue with an attempt counted and the error class `worker_lost`. A re-encode cannot resume, so it restarts from the beginning, and the new worker or the restarted worker removes the old partial file.

**Assignment.** Pull means "assign" is a claim, chosen by: priority (new downloads before the library backlog), pinned worker, required tags, then oldest first. A worker only receives jobs that fit its free scratch space.

### 6.3 Job stages

`queued → copy in → analyze → (plan) → encode → verify → copy out → publish → done`, with `failed`, `needs you` and `cancelled` as other endings.

| Stage | SyncHub state shown on dashboards |
|---|---|
| copy in, copy out | **Transferring** |
| analyze, encode, verify | **Encoding** (progress is the encode percent; the stage name is the detail) |
| publish | the event closes and Jellyfin's sync turns the title **In Library** |

If analysis says nothing needs doing, the job skips straight to publish, which for a file already in place means only telling Jellyfin.

### 6.4 Errors

| Class | Examples | Behaviour |
|---|---|---|
| transient | share offline, network drop, scratch full | retry later with back-off (up to `max_attempts`, default 3), may go to another worker |
| worker lost | PC off, agent killed | lease expires, job re-queued |
| source changed | file size or time differs from the analysis | job re-analysed once, then **needs you** |
| verification failed | wrong tracks, duration, size or decode error | one retry, then **needs you** with the check that failed |
| encode failed | ffmpeg non-zero exit | **needs you** with the last 30 ffmpeg lines and the command |
| size target missed | result above the maximum | one automatic retry at a lower quality setting, then **needs you** |
| cancelled | you pressed Cancel | ffmpeg stopped within seconds; partial files removed |

Every attempt keeps its command and log tail. Nothing is deleted without being verified first.

## 7. Files

### 7.1 Paths

Every job names files as **root + relative path**. Each worker resolves them with its own aliases (UNC on Windows, mount on Linux), so adding a PC with different mounts needs only its settings file. SyncHub keeps Jellyfin's, Radarr's and Sonarr's view of each root, which is also how webhook paths and Jellyfin notifications are translated.

### 7.2 Worker steps

1. Check free space (source size plus a margin) in scratch.
2. **Copy in** to scratch; verify size and (optionally) hash.
3. **Analyze** with ffprobe; send facts; receive the plan.
4. **Encode** from the local copy with ffmpeg `-progress`, writing to scratch.
5. **Verify** with the tool's checks (tracks, languages, default flag, codecs, picture size, duration, full decode), plus the size and quality checks of §10.
6. **Copy out** to `<destination>.partial` on the target share; check the size; rename without overwriting.
7. Report **complete**; remove scratch files.

### 7.3 Destination

Library migration writes into the Jellyfin library root using `media_layout` (`Movies/<name>/<name>.ext`, `TV Shows/<Show>/Season NN/<name>.ext`); the source is left alone (your tool's rule). Download jobs use the in-place rule below.

### 7.4 Downloads: replace in place

You said Radarr, the encoder and Jellyfin all see the same paths. So for a downloaded file the result is written **in the same folder** as the file Radarr or Sonarr imported, under the same base name (only the extension may change, for example `.avi` to `.mkv`). Keeping the base name keeps the quality tag Radarr/Sonarr parsed from it, so they do not think the file needs upgrading.

Sequence, after verification:

1. Write `<name>.<ext>.partial`, rename to the final name.
2. Move the original into the **originals** root (outside any Jellyfin library), kept for a retention period (default 14 days, then deleted by SyncHub's housekeeping). Disk space is not a concern for you, so this is the safety net for the one place a file is replaced.
3. Tell **Radarr/Sonarr** to rescan that movie or series so the new file is the known file (*to verify against your Radarr/Sonarr versions before relying on it; a failed rescan is reported, not hidden*).
4. Tell **Jellyfin** (§8).

If you ever move Radarr's root away from the Jellyfin library, the second mode (copy into the library, leave the source) is already the library-migration behaviour.

## 8. Telling Jellyfin

SyncHub, not the worker, tells Jellyfin, after a job reports complete and the result is on the share. From Jellyfin's own source (`LibraryController`):

- `POST /Library/Media/Updated` with `{"Updates": [{"Path": "<path as Jellyfin sees it>", "UpdateType": "Created"}]}` (types: Created, Modified, Deleted). When an original was replaced, one call carries a `Deleted` for the old path and a `Created` for the new one.
- `POST /Library/Refresh` starts a full scan (needs an administrator key). Used as the fallback, and from a "Scan Jellyfin now" button.
- `Library/Movies/Added?tmdbId=` and `Library/Series/Added?tvdbId=` only match items Jellyfin already has, so they are not used for new titles.

The path is translated with the Jellyfin alias of the root. Afterwards SyncHub starts its own Jellyfin sync after a short delay; when Jellyfin lists the item the title turns **In Library** and the job is marked confirmed. If it does not appear within a set time, the job is marked "published, Jellyfin has not indexed it yet" with a Rescan button, never silently "done". The existing `/api/webhook/jellyfin` (Jellyfin's Webhook plugin, "Item Added") makes the confirmation near-instant when installed.

## 9. The Library page (the original tool, standalone)

It must work on its own: no download pipeline needed, no extra PCs needed (a worker can run on the SyncHub machine as a separate process, as your tool's job runner already did).

| Part | How it works |
|---|---|
| **Scan** | Start from the page; a worker runs the analysis (`main.py`'s file inspection, integrity modes, duplicates, naming) and streams records to SyncHub. A scan can be stopped; results are upserted by root + relative path; a changed file gets a new revision and **its approval resets** (the tool's rule). |
| **Overview and Review** | Your `media_recommend.classify()` runs on the stored records: the same queues, confidences and plain-language headlines. One-by-one review (approve, reject, skip, edit name), grouped lists (TV by show and season), and bulk approve with a confirmation page. **Low confidence is never bulk-approved; duplicates are decided one at a time.** |
| **Approve → jobs** | An approved record becomes a `library` job in the tool's stage order (copy, tracks, remux, convert), cheapest first. `--stop-after-hours` becomes a per-worker time window. Dry run and "verify finished files" are kept. |
| **Export** | The CSV from `media_report.CSV_COLUMNS`, same columns. |
| **Audit** | Every decision is recorded as in `media_events`. |

**How the code is brought over.** The pure modules (`media_recommend`, `media_layout`, `media_roots`, `media_report`) move into a `synchub/library/` package with only their imports adjusted. Their shared helpers (`final_name`, `filename_problems`, the status constants and `set_approval` with its revision and fingerprint rules) come from a port of the relevant parts of `media_db`, over the `library_*` tables that keep the original column names. The scan and migrate logic moves into a shared library used by the worker agent. The tool's own standard-library web UI, CLI scripts, global lock and local database are replaced as described in §2.

**What changes for the size policy:** a movie over the maximum gets a `compress` plan next to your `convert` plan, shown in the **Format conversion** queue with medium confidence (it is lossy), the predicted size and an estimated time.

## 10. Movie size policy

### 10.1 Rules

- **Applies to movies.** TV episodes are not capped (a per-episode cap can be added later).
- `movie_max_gb` (default **30**): a movie larger than this is compressed. `movie_target_gb` (default **25**): what it is compressed toward. A movie between 25 and 30 GB is left alone; a movie under 25 is never re-encoded just for size.
- **The target is a ceiling, not a goal.** Bits are spent on quality up to the ceiling, but nothing is padded: if the source is so generous that fitting 25 GB would need an absurdly low quantiser (below `crf_floor`), the result is simply smaller than 25 GB.
- Sizes are computed on the file after the existing track rules (English tracks only, image subtitles kept), so removing foreign tracks is tried first, and it is lossless.

### 10.2 Decision procedure (run by SyncHub from the worker's probe facts)

Inputs: duration, resolution, frame rate, source video codec and bitrate, bit depth, HDR flags (HDR10, HLG, Dolby Vision profile), interlaced flag, each audio and subtitle stream's codec and size, container.

1. **Budget.** `video budget = target × 0.99 − kept audio − subtitles`; `target video bitrate = budget × 8 ÷ duration`. Never above 90 percent of the source video bitrate (re-encoding to a higher bitrate than the source helps nothing).
2. **Quality floor.** Compute bits per pixel per frame, `bpp = bitrate ÷ (width × height × fps)`. With HEVC, floors of about 0.05 (2160p), 0.06 (1440p), 0.07 (1080p) and 0.09 (720p) are treated as visually safe. Under the floor the job is **not run automatically**: it goes to **needs you** with the numbers and the options (accept, raise the target for this title, or transcode lossless audio to free space).
3. **Calibrate.** Encode four 30-second samples spread across the film (skipping the first and last 5 percent) at a guessed quantiser, measure bytes per second, fit the quantiser-to-bitrate curve, and choose the quantiser whose predicted full-film bitrate meets the target (within `crf_floor`..`crf_ceiling`). Film grain, animation and action make bitrate unpredictable, which is why this measures the real source and does not guess. The samples also give the worker's real speed, so the ETA is honest.
4. **Encode** at that quantiser with a VBV cap (`maxrate` about 1.5 × the target bitrate, `bufsize` about 2 × maxrate) so a pathological scene cannot blow the budget. This is quality-first (constant quality) with a safety ceiling. It is not constant bitrate, so easy scenes get fewer bits and hard scenes more.
5. **Check the size.** Within about 5 percent of the target is expected. If the result is above `movie_max_gb`, retry once at a quantiser two steps higher; if that still fails, fall back to two-pass average-bitrate at 97 percent of the target; if that fails, **needs you**.
6. **Check the quality** (optional, on by default only where the worker has VMAF support): compare the same sample windows against the source; a mean VMAF under the threshold (about 93; 95 for 2160p) flags the job instead of publishing it.

### 10.3 Format choices

| Item | Default | Reason |
|---|---|---|
| Video codec for compressed movies | **HEVC (x265), 10-bit** | Commonly 30 to 40 percent smaller than H.264 at the same quality, which is what makes 25 GB transparent for most films. Needs your clients to direct-play HEVC (see questions). |
| Encoder | CPU x265 for compressed movies; hardware encoders (NVENC, QSV) allowed per worker | Software x265 gives the best quality per byte; hardware is much faster but needs a higher bitrate for the same quality. A worker's profile can say which it prefers. |
| Preset | `slow` when the worker's measured speed allows, else `medium` | Chosen from the calibration speed and the queue depth |
| Audio | **Copy** the best English track unchanged (lossless stays lossless); never re-encode unless the plan says so | You asked to preserve audio quality. Only the step-2 "needs you" option can transcode lossless audio, never automatically. |
| Subtitles | Copy | Small |
| HDR | HDR10 and HLG are preserved (BT.2020, PQ/HLG transfer, mastering-display and content-light metadata passed to x265, 10-bit) | A re-encode that drops the HDR tags looks washed out |
| Dolby Vision | Profile 5: **not compressed**, flagged to you (it has no HDR10 fallback and looks wrong when re-encoded). Profiles 7/8: encoded as HDR10, with the Dolby Vision layer lost, and the plan says so. | Safe by default; a setting can change this |
| Interlaced | Deinterlace with `yadif` only when flagged (as your tool does) | |
| Already HEVC | Re-encoded only if over the maximum; the plan warns about generation loss and picks a conservative quantiser | |

### 10.4 What you see

Before anything runs (Encoding and Library pages): predicted size, predicted quantiser, estimated time, the bits-per-pixel figure, HDR handling, and a one-line reason ("1080p AVC at 38 Mbps, 32 GB, over the 30 GB limit: compressing toward 25 GB, expect about 21 GB"). After: actual size, the quality score, and the exact command.

### 10.5 Settings

`movie_max_gb`, `movie_target_gb`, `crf_floor`, `crf_ceiling`, `prefer_hardware`, `preset_policy`, `dolby_vision_policy`, `quality_check` (off / samples), `originals_retention_days`, and per-worker overrides. All in Settings → Encoding, all validated.

## 11. Security and safety

- Each worker has its **own token** (random, shown once, stored hashed, revocable). Worker endpoints accept only that token; nothing else on the site does.
- The rest of SyncHub stays as it is today: it listens on every network interface with no login, and the profile picker is identity, not security, as agreed for your LAN. Approving and replacing files raises the stakes, so: originals are kept for the retention period, **nothing is deleted before verification**, and destructive actions record which profile did them. Because anyone on the network could press Approve, decision 7 below proposes a simple shared access token for the web UI (your tool already has one) before phase 5 turns on automatic replacement.
- Commands to ffmpeg are built only from validated options (no shell, no free text), as in your tool.

## 12. Delivery phases

Each phase ends with passing tests and something you can use. Tests use a fake worker for the control plane and a fake ffmpeg runner for the agent (as your tool's tests do); a small real-ffmpeg test runs only when ffmpeg is present.

| Phase | Scope | Done when |
|---|---|---|
| 1. Control plane | Storage roots and aliases; worker table, tokens, register/poll/progress/complete/fail; job table, leases and the reaper; Workers page; live progress through the state engine | A fake worker claims, reports progress, completes and loses a lease, and the dashboard shows it |
| 2. Agent and Jellyfin | The agent (stdlib only): register, poll, copy in and out with verification, scratch handling, cancel; **publish and Jellyfin notification**; a "copy only" job end to end | A file moves from one share to the Jellyfin share through a real worker and appears In Library |
| 3. Encoding | Port the probe, plan, command and verify logic; stages and ffmpeg progress; error classes; the Encoding page (queue, detail, logs, retry, cancel, pin) | A remux and a re-encode finish with the tool's verification, and every failure class is shown correctly |
| 4. Size policy | Budget, bits-per-pixel floor, calibration, capped quality encode, HDR and Dolby Vision handling, size checks, optional quality check, settings and predictions in the UI | A movie over the maximum ends near 25 GB (or smaller by design) with the reason and numbers shown |
| 5. Download pipeline | Radarr/Sonarr import webhook creates a job; in-place replace; originals root and retention; Radarr/Sonarr rescan; Jellyfin confirm | A download is imported, encoded, replaced and visible in Jellyfin without any manual step |
| 6. Library page | The `library_*` tables; port of `media_recommend`, `media_layout`, `media_roots`, `media_report`; scan as a worker job; review queues and bulk approve; approved records become jobs; CSV export; audit | Everything the tool's CLI and web UI did can be done from the Library page |
| 7. Multi-worker polish | Tags, priorities, time windows, drain, per-worker encoder profiles, worker install notes (Windows service) | A third worker is added by settings only |

**Order.** I suggest 1, 2, 3, 4, then 6 before 5 if the library migration is your priority, or 5 before 6 if new downloads are. Phases 1 to 4 are needed for both.

## 13. Risks and things to verify

| Risk | Handling |
|---|---|
| Replacing a file Radarr/Sonarr imported could make them search again | Same base name and quality tag; rescan after replacing; verify on your versions in phase 5 before enabling it for everything |
| HEVC not direct-playable on some of your clients, causing Jellyfin transcoding | Question below; H.264 is possible but needs a larger target for the same quality |
| Jellyfin slow to notice a file | Confirm step, Rescan button, optional Webhook plugin |
| A re-encode of many hours lost to a crash | Restart from the beginning (ffmpeg cannot resume); chapters of the film can be split in a later phase if this proves painful |
| Calibration samples unrepresentative | Four spread samples plus the size check and one automatic retry |
| Windows details (UNC, service accounts, long paths) | Agent tested on both OSes; paths always root + relative |
| The attached tool was read but not run | Phase 3 and 6 port with its tests as the reference; behaviour is compared case by case |

## 14. Decisions needed

| # | Question | My recommendation |
|---|---|---|
| 1 | Output codec for compressed movies | HEVC 10-bit. Do all your players direct-play HEVC? |
| 2 | Should compression of an over-limit movie start by itself, or wait for approval? | Automatic for movies over the limit (that is what you asked), with a notice, originals kept 14 days; low-confidence cases wait for you |
| 3 | Worker hardware: any GPU (NVENC, QSV)? Are all workers Windows? | Allow both; prefer CPU x265 for compressed movies |
| 4 | Where should originals be kept before deletion, and for how long? | A separate share (`originals`), 14 days |
| 5 | Phase order: library page first or download pipeline first? | Library page first if converting the existing library matters most |
| 6 | Should TV episodes have a size cap too? | No for now |
| 7 | Add a simple shared access token to the web UI before automatic replacement is switched on? | Yes: one token, remembered by the browser; workers keep their own tokens |
