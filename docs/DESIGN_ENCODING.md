# SyncHub Encoding and Library Migration: Plan (v2.2)

Status: **v2.2: Phases 1 (control plane) and 2 (worker agent and storage) are built and tested.** The agent copies files with verification, survives being killed mid-copy, and the site manages shares and generates the worker's setup files. The next piece is Phase 3, the Library page. See §14 for the order, §16 for what is still open and Appendix A for the worker protocol exactly as built. v1 was written from the Media Analysis and Migration tool you attached (`main.py`, `migrate.py`, `media_db.py`, `media_jobs.py`, `media_config.py`, `webui.py`, `media_recommend.py`, `media_layout.py`, `media_roots.py`, `media_report.py`, `review.py` and the three `.md` files). Nothing from the tool was run.

## 1. Requirements and decisions

| # | Decision or requirement | Effect on the design |
|---|---|---|
| 1 | All your players direct-play **HEVC** | Compressed movies use HEVC 10-bit (§11) |
| 2 | Movies over the limit are compressed **automatically**, with a notification at start and at finish. Only **low-confidence** cases pause for you | §8.5, §11, §13 |
| 3 | Worker PCs run **Ubuntu** and run the provided script. **CPU** encoding now; keep the design ready for **GPU** later | Agent is one script plus a systemd unit (§6.1); encoders are pluggable (§11.3) |
| 4 | Originals are kept **on the worker PC for 30 days** (default), adjustable or **off**, and are never deleted before the replacement is verified | §9 |
| 5 | **Library page first**, download pipeline later | Order in §14 |
| 6 | **No size limit on TV episodes** by default, but the logic stays and is configurable | `tv_episode_max_gb` setting, off by default (§11.1) |
| 7 | **No web UI token yet**, planned for later | Every UI change goes through one access hook so a token is a one-place change; workers authenticate already (§15) |
| 8 | **Resume after a restart or interruption** without losing progress; do not copy or encode the same file twice; keep original and new file apart until verified; keep files and logs when a job fails | Work folders and manifests (§6.3, §8), resumable encoding (§8.3), failure preservation (§8.4) |
| 9 | **Low-confidence** cases pause for review, with a clear approve / retry / skip workflow in the UI, and a **30 to 60 second preview clip** at the recommended compression to watch before approving | §8.5, §12 |
| 10 | Worker **logging**; recommendation on a **local database**; central status; log rotation; easy to find a job's logs | §6.3, §6.4, §6.5 |
| 11 | **Manifests, not SQLite**, agreed. The worker runs in **Docker**, which provides its working directory as a volume | Docker image and compose file (§6.1) |
| 12 | The workers need both Unraid shares (`Media`, `TFiles`); unsure whether NFS or SMB, and whether the site can manage it | §6.6: the site holds the definitions, generates the Docker or fstab setup and shows each worker's mount health; the mounting itself happens on the worker PC |
| 13 | **Review a sample before automatic approval**: force a manual look at a sample first, to learn what the low-confidence cases are | §12.1: the sample is made and sent for review **before** the full encode, and the system stops asking once you have approved enough similar ones |

Earlier decisions still hold: SyncHub is the master controller and tells Jellyfin about new files; the original tool lives on its own standalone **Library** page; movies above about 30 GB are compressed toward about 25 GB.

## 2. What exists today

**SyncHub** already has: a Flask app and SQLite database; a state engine in which **Transferring** and **Encoding** are defined states fed by `processing_event` rows; live dashboard updates; Radarr/Sonarr webhooks and queue polling; a Jellyfin client; the profile picker; Apprise notifications.

**Your tool** is a careful batch pipeline for one machine: scan (ffprobe) → plain-language recommendation → human approval → migrate (copy, remux or re-encode) into the Jellyfin library. Worth keeping, nearly unchanged:

| Piece | Why keep it |
|---|---|
| `media_recommend.py` | Turns a record into a queue (ready, suggested, worth a look, format conversion, needs you), a confidence (low is never bulk-approved) and an approve action. Pure logic. |
| `media_layout.py` | The one definition of the library layout: `Movies/<name>/<name>.ext` and `TV Shows/<Show>/Season NN/<name>.ext`. |
| `media_roots.py` | A file is identified by **root name + relative path**, never by a machine's path. This is exactly the path mapping workers need. |
| `migrate.py` encode core | `.partial` file, verify, rename without overwriting. Verification checks tracks, languages, default flag, codecs, picture size, duration within 0.5 percent, and a full decode. The exact ffmpeg command is stored per attempt. ffmpeg `-progress` output is parsed. |
| `main.py` analysis | Stream audit, English-track policy, naming, duplicate detection by SHA-256, integrity checks. |
| `media_report.py`, `review.py` | The CSV columns and every review operation (approve-ready, approve-category, keep duplicate, reject, reset). |

What does **not** carry over:

| Limit in the tool | Consequence |
|---|---|
| One global migration lock: one migration at a time | Replaced by per-job **leases**, so several workers run at once |
| SQLite file that "must be on a local disk" | Workers never open SyncHub's database; they use HTTP |
| Progress only written to a log every 15 s | Structured progress sent to SyncHub every few seconds |
| `webui.py`: standard-library server with its own token scheme | Rebuilt as SyncHub pages; its queue and bulk-approval ideas are kept |
| `media_roots.SOURCE_ROOTS` hard-coded in the file | Roots are managed in SyncHub; each worker lists its own mount points in its settings file |
| A re-encode "cannot resume" | Long encodes are split into chunks so an interruption loses at most one chunk (§8.3) |

## 3. Architecture

```
                         ┌─────────────────────────────────────────────┐
  Radarr / Sonarr ─────► │ SyncHub (control plane, Flask + SQLite)     │ ◄──── Web UI
   (import webhook)      │  job queue · worker registry · size policy  │
                         │  review queues · state engine · live events │
                         └───────┬───────────────────────┬─────────────┘
                                 │ HTTP: workers PULL    │ Jellyfin API
                                 │ jobs, push progress   ▼
                  ┌──────────────┼──────────────┐    ┌──────────┐
                  ▼              ▼              ▼    │ Jellyfin │
             Worker PC 1    Worker PC 2    Worker PC 3 └────▲─────┘
             (Ubuntu,       (Ubuntu,       (Ubuntu,         │ notified by SyncHub
              agent +        agent +        agent +         │ after each publish
              ffmpeg)        ffmpeg)        ffmpeg)         │
                  └──────── network shares (mounted) ───────┘
                    source share ──► local work folder ──► Jellyfin share
```

**Principles**

1. **SyncHub never touches media bytes.** It stores records and decisions and runs no ffmpeg.
2. **Workers pull.** No inbound ports on worker PCs; a new worker is "run the script with SyncHub's address and a token".
3. **Decisions are central, work is local.** A worker probes a file and reports facts; SyncHub decides the plan (§8.2, §11), so rules change in the UI without updating workers.
4. **Never overwrite, never lose the original silently.** Output goes to `.partial`, is verified, then renamed; the original is replaced only after the new file is verified, and a copy is kept on the worker (§9).
5. **Two features, one engine.** The Library migration and the download pipeline both produce jobs for the same workers and the same publish-and-notify step, but they have separate pages.
6. **Authorization in one place.** Every UI action that changes something goes through the existing request hook that already identifies the profile; adding the planned access token later means changing that hook. Worker endpoints have their own token from day one.

Rejected: a message broker (another service for a few PCs), pushing jobs over SSH/RPC, workers writing to SyncHub's database (SQLite over a network share is unsafe).

## 4. Pages

| Page | Purpose |
|---|---|
| **Workers** | Each worker: online or not, version, encoders found, free work-folder space, originals held, slots, current job. Add (token shown once), enable, pause, **drain**, rename, rotate or revoke token. A recent-jobs list with cancel and retry |
| **Library** | The original tool as a standalone page: Overview (queues and counts), Review (one by one, lists, bulk approve), Scan, Migration status, Export CSV |
| **Encoding** | Jobs created from downloads: queue, stages, progress, speed, ETA, predicted and actual size, quality check, logs; **Needs review** with preview clips; retry, cancel, pin |
| **Settings → Encoding** | Size policy, retention, preview policy, encoder preferences, storage roots, notification events |
| Dashboard | Unchanged. Transferring and Encoding badges with live progress come from the state engine |

## 5. Data model (SyncHub side)

| Table | Holds |
|---|---|
| `worker` | Name, token (stored hashed), enabled, paused, draining, tags, slots, last seen, protocol, version, OS, capabilities (JSON), latest status summary (JSON) |
| `job` | `kind` (`scan`, `library`, `download`, `preview`), links to a library file or media, source and destination as root + relative path, plan and policy (JSON), status, stage, progress, fps, speed, ETA, attempts and maximum, lease expiry, assigned worker, preferred worker (where its files are), pinned worker, required tags, priority, error class and text, cancel flag, review reason and decision, result (JSON: sizes, verification, quality) |
| `job_attempt` | One row per try: worker, start, end, exact ffmpeg command, last log lines, outcome |
| `storage_root`, `storage_alias` | Logical share names, and how Jellyfin, Radarr and Sonarr each see them (to translate webhook paths and tell Jellyfin the right path) |
| `library_file`, `library_attempt`, `library_event` | The tool's `media_files`, `migration_attempts` and `media_events`, **keeping the column names** so its pure modules run on these rows unchanged |
| `preview` | Preview clips: job, window, sizes, files, expiry |

Job states and stages are data and text, so new stages need no schema change.

## 6. The worker

### 6.1 Agent and installation (Ubuntu)

- **One Python file, standard library only** (`agent/synchub_agent.py`). It runs in **Docker** (recommended): the image is Python plus ffmpeg, the compose file mounts a volume for the work folder (job folders, kept originals, logs) and the shares, and Docker restarts it after a crash or reboot. Without Docker, the same file runs under a **systemd unit** with ffmpeg from the package manager.
- Settings come from a file in the style of `media_review.ini` or from environment variables (handier in Docker): SyncHub address, token, work folder, mount points per root, ffmpeg paths, slots, retention overrides, optional active hours. The Workers page hands out a ready-made compose file and settings snippet with the token filled in.
- At start the agent reports capabilities (ffmpeg version, which encoders actually work by encoding one second of test video, CPU cores, free space), checks its shares (§6.6) and reads its local state (§6.3). `--check` prints all of that and exits.

### 6.2 Encoders (CPU now, GPU later)

An encoder is a small description: its name (`libx265`, `libx264`, later `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi`), how a quality target maps to its options, which pixel formats and HDR options it supports, and a measured speed. The plan says "HEVC 10-bit at about this quality or bitrate"; the worker maps that onto the best encoder it has. A GPU worker only adds entries and a preference in its settings file; no SyncHub change. Calibration (§11.2) always measures the encoder actually used.

### 6.3 Local state: files, not a database (recommendation)

**Recommendation: no local database. Each job gets its own folder with a `manifest.json` (written atomically) and its own log; recovery means scanning those folders.**

| Question | Per-job folders and manifests | Local SQLite |
|---|---|---|
| Recover after restart or crash | Scan `jobs/*/manifest.json`; each says exactly which stage and which chunks are finished | Same information, but a second source of truth next to the files |
| Prevent duplicate work | SyncHub already guarantees one claim per job; the folder name carries the job id and a source fingerprint, so a returning job finds its files | Same |
| Originals retention | Each kept original has a `meta.json` with its expiry; a daily sweep of a handful of folders | A table, for a handful of rows |
| History and statistics | SyncHub keeps them centrally (attempts, results, log tails) | Duplicates SyncHub |
| Dependencies and upkeep | None: plain files, readable with `cat` | Schema and migrations to maintain on every worker; one corrupt file affects every job |
| Failure behaviour | A bad manifest affects one job; delete its folder to start that job fresh | A corrupt database affects all |

SQLite would earn its place only for something with many thousands of small facts, such as a scan cache. That cache lives in SyncHub instead: a scan asks SyncHub which files it already knows (path, size, modified time) and probes only new or changed ones. The manifest code sits behind a small interface, so if a real need ever appears, swapping in SQLite touches one module.

**Work folder layout (worker)**

```
<work>/jobs/<job-id>-<fingerprint>/
    manifest.json        stage, per-stage verification, chunk list, command, retry count
    job.log              this job's structured log (JSON lines)
    source/<name>        verified local copy of the source (kept until the output is verified)
    chunks/ , output/    encoded pieces and the assembled output (never touched by the source copy)
    preview/             preview clips
<originals>/<date>_<job-id>/   the kept original + meta.json (kept until, verified, replaced at)
<logs>/worker.jsonl (+ .1 .. .5)
```

### 6.6 Shares: how a worker reaches Unraid

A worker needs the shares mounted as ordinary folders (for example `/mnt/tower/Media` and `/mnt/tower/TFiles`). Two ways, both work with Unraid:

| | **SMB (cifs)** | **NFS (v4)** |
|---|---|---|
| Setup on Unraid | Already on; give the workers a user with read/write on the two shares | Turn NFS on (Settings), then export each share to the worker IPs with `rw` (rule syntax and the Unraid web UI have had bugs: check the system log after saving) |
| Credentials | A user name and password (kept in a `.env` file next to the compose file, never in SyncHub) | None; access is by IP address |
| Known issues | A password to manage | Anecdotal reports of "stale file handle" errors on `/mnt/user` shares when the mover relocates files from the cache; NFSv4 and keeping the share off the cache are the usual workarounds |
| My recommendation | **Start with SMB**: you already use it from Windows and it avoids the mover issue reports. Move to NFSv4 later if speed matters | |

The files being copied are large, and the encoder runs for hours, so both options use "hard" behaviour: if the share drops, the copy waits and then fails visibly rather than writing a short file.

**What the site manages.** SyncHub cannot mount anything on a worker PC (that needs the PC's own permissions), but it holds everything else:

1. **Storage page**: each root's name (`media`, `tfiles`), protocol, Unraid address, share or export path and options, plus how Jellyfin, Radarr and Sonarr see the same root (for path translation and Jellyfin notifications).
2. **Setup files**: from those definitions SyncHub generates the worker's `docker-compose.yml` (Docker's own NFS or CIFS volume definitions, so Docker does the mounting and a failed mount stops the container instead of writing into an empty local folder) and, for non-Docker workers, the matching `/etc/fstab` lines.
3. **Mount health**: every worker reports, for each root, whether it is there, whether it is the right share, whether it is writable and how much space is free. The Workers page shows it, and a worker refuses to use a root it cannot verify.
4. **Marker file**: each share root holds a small `.synchub-root` file containing its name. The agent will not read or write a root without it. This prevents the classic accident of a share that failed to mount (an empty local folder) quietly receiving files, and a mix-up between `Media` and `TFiles`. `--init-roots` creates the marker files once, when you ask it to.

### 6.4 Logging

- **Structured JSON lines**, one object per event: time, level, worker, job id, stage, message and fields. Job start and completion times, source and output paths (root + relative), the exact encoder settings and command, sizes before and after, verification results, errors, retry attempts.
- **Two files**: a rotating `worker.jsonl` (size-based, for example 10 MB × 5) and a `job.log` inside each job's folder, so everything about one job is in one place and goes away with it.
- **Retention**: job folders of successful jobs are removed after a short delay; folders of **failed or paused jobs are kept** (default 14 days, configurable), with their logs, so you can troubleshoot.
- **From the web UI**: SyncHub keeps the last lines of each attempt; "Fetch full log" marks a request that the worker answers on its next poll by uploading the job's log. You never need to log in to a worker.

### 6.5 Central status

Every poll carries a small status summary (current stage per job, speed, free space in the work and originals folders, originals held and their size, version). The Workers page shows it live, notices a worker that went silent, and raises the "worker offline" notification when work is waiting.

## 7. Protocol (JSON over HTTP, `Authorization: Bearer <worker token>`)

| Call | Meaning |
|---|---|
| `POST /api/worker/v1/register` | Protocol version, agent version, OS, capabilities, slots. Refused if the protocol version is unsupported. |
| `POST /api/worker/v1/poll` | "I have N free slots and X GB free; these jobs are still in my folders (with their stage)." Reply: jobs to run, jobs to cancel, pause and drain flags, log requests. Long-poll about 20 s so new work starts promptly. The `running` list is how a restarted worker tells SyncHub what it can resume; jobs SyncHub thinks it owns that it does not list go back to the queue. |
| `POST /api/worker/v1/jobs/<id>/analysis` | The worker's ffprobe facts. Reply: the plan (copy unchanged, remux, convert, compress with its settings), or "nothing to do", or "needs review" with the reason. |
| `POST /api/worker/v1/jobs/<id>/progress` | Stage, percent, fps, speed, ETA, message. Doubles as the heartbeat. Reply: lease extended, cancel requested or not. |
| `POST /api/worker/v1/jobs/<id>/needs_review` | The worker found something it cannot decide (§8.5), optionally with a preview. The slot is freed; the worker keeps the job folder. |
| `POST /api/worker/v1/jobs/<id>/complete` | Destination, final size, verification results, quality score, command. SyncHub then publishes (§10). |
| `POST /api/worker/v1/jobs/<id>/fail` | Error class, message, last log lines, whether a retry is worthwhile, whether the folder was kept. |
| `POST /api/worker/v1/scan/...` | Library scans (§11): known-file signatures out, analysed records in, in batches. |

**Leases.** A claimed job has a lease (about 90 s) renewed by every progress message. A reaper (every 30 s) returns jobs of silent workers to the queue and counts the attempt (`worker_lost`). The job remembers which worker holds its files and prefers that worker for a while, so a restarted PC resumes instead of another PC starting over.

**Assignment.** A claim, chosen by: priority (new downloads before the library backlog), pinned worker, required tags, preferred worker, then oldest. A worker only receives jobs that fit its free space.

## 8. Jobs

### 8.1 Stages

`queued → copy in → analyze → plan → (preview) → encode → verify → copy out → publish → done`; other endings: `failed`, `needs review`, `cancelled`.

| Stage | Dashboard state |
|---|---|
| copy in, copy out | **Transferring** |
| analyze, preview, encode, verify | **Encoding** (progress is the encode percent; the stage is the detail) |
| publish | the event closes and Jellyfin's sync turns the title **In Library** |

### 8.2 Plans

Decisions are central. The worker sends facts, SyncHub answers with a plan and a confidence. A **high** confidence plan runs on its own; **low** pauses (§8.5). Library records use the tool's own recommendation and approval; downloads use the size policy (§11) and the same track rules.

### 8.3 Resume and avoiding repeated work

- **Stages are idempotent and recorded.** `manifest.json` marks a stage done only after it is verified: *copy in* is done when the local copy's size and hash match the source; *encode* is done when every chunk verifies and the assembled output probes correctly; *copy out* is done when the `.partial` on the share has the right size and is renamed.
- **Resume**: on restart the agent scans its job folders, tells SyncHub what it holds, and continues each job from the first unfinished stage. A job whose source changed (size or modified time differs from the manifest's fingerprint) discards its copy and chunks and starts fresh.
- **Chunked encoding.** A long re-encode is split into time chunks (default 10 minutes), each encoded on its own with the same settings and joined losslessly at the end, with audio, subtitles, chapters and metadata taken from the source in one piece. A finished chunk is never encoded again, so an interruption loses at most one chunk. Short jobs (about 20 minutes or less) and plain copies and remuxes are done whole. Constant-quality chunks keep the look consistent; HDR settings are identical in every chunk. (Chunks could later also be spread over several workers.)
- **No duplicate work**: the copy-in step reuses a verified copy; a retry reuses finished chunks; SyncHub never queues a file whose library record and revision are already migrated.

### 8.4 When a job fails

The job folder, its log and the last failing command are **kept** (default 14 days, configurable), the attempt is recorded in SyncHub with the log tail, and the job shows what failed. Retry reuses whatever is still valid; "Clean up" removes the folder.

| Class | Examples | Behaviour |
|---|---|---|
| transient | share offline, network drop, disk full | retry later with back-off up to the attempt limit (default 3) |
| worker lost | PC off, agent stopped | lease expires, job re-queued, resumes where its files are |
| source changed | size or time differs from the analysis | re-analysed once, then needs review |
| verification failed | wrong tracks, duration, size, decode error | one retry, then needs review with the failed check |
| encode failed | ffmpeg non-zero exit | needs review with the last 30 ffmpeg lines and the command |
| size target missed | result above the maximum | one automatic retry at lower quality, then needs review |
| cancelled | you pressed Cancel | ffmpeg stopped within seconds; partial output removed, copy kept until cleanup |

### 8.5 Needs review (low confidence)

(The first time you meet each kind of file, a sample is reviewed too; see §12.1.)

A job pauses, instead of guessing, when the system cannot be confident. Reasons include: the bits-per-pixel floor would be broken (§11.2), unusual HDR or Dolby Vision profile 5, no English audio or unknown language, the source changed twice, a low-confidence name or destination (library), the size target missed after the retry, verification failed twice, quality score below threshold, unreliable calibration.

The review screen shows the reason, the facts, the plan, and a **preview clip** (§12) when the case involves compression. You choose, from the web UI only:

| Action | Result |
|---|---|
| **Approve as recommended** | Continues from where it paused |
| **Approve with changes** | For example a different target size, which tracks to keep, a name |
| **Retry** | Run again (reusing valid files) or from scratch |
| **Skip** | The file is left as it is and remembered, so it is not queued again until the file changes |
| **Reject / remove** | Closed; the worker's files are cleaned up on request |

A paused job releases the worker's slot but keeps its local files (default 14 days). When you decide, the job goes back to the queue preferring the worker that holds them. Each decision records which profile made it and a note.

## 9. Files and originals

**Paths.** Every job names files as root + relative path. Each worker resolves them with its own mount points. SyncHub keeps Jellyfin's, Radarr's and Sonarr's view of each root, used for webhook paths and Jellyfin notifications.

**Worker steps.** (1) check free space; (2) copy in to the work folder and verify size and hash; (3) analyze; (4) encode from the local copy into a separate output; (5) verify (the tool's checks plus size and quality); (6) copy out to `<destination>.partial`, check the size, rename without overwriting; (7) report complete.

**Library migration** writes into the Jellyfin library root using the layout rules and leaves the source alone (the tool's rule). Nothing to retain.

**Downloads replace in place** (later phase). Radarr, the encoder and Jellyfin see the same paths, so the result is written in the same folder under the same base name, which keeps the quality tag Radarr/Sonarr parsed and avoids an "upgrade" search. Order:

1. The worker already holds a **hash-verified copy of the original** (from copy in). The new file is encoded and verified in a separate folder.
2. Only then is the library file replaced: the verified result is copied out as `.partial`, size-checked and renamed in.
3. The original is **kept on the worker** in the originals folder with its `meta.json` for **`originals_retention_days` (default 30)**. After that it may be deleted, automatically (setting) and not before. Setting the retention to 0 turns keeping off (the original is then gone once the replacement is verified, which is your explicit choice). A UI list shows what is held and allows an early delete with a confirmation, and later a "Restore original".
4. Radarr/Sonarr are asked to rescan that title (*to verify against your versions before relying on it*) and Jellyfin is told (§10).

The worker keeps a free-space guard: it declines new jobs when the work or originals folder is too full, and says so on the Workers page. Note the trade-off of your choice: if a worker PC dies, the originals it held are lost, so the 30 days protects against a bad encode, not against losing that PC.

## 10. Telling Jellyfin

SyncHub, not the worker, tells Jellyfin, after a job completes and the result is on the share. From Jellyfin's own source (`LibraryController`):

- `POST /Library/Media/Updated` with `{"Updates": [{"Path": "<path as Jellyfin sees it>", "UpdateType": "Created"}]}` (types: Created, Modified, Deleted). When a file was replaced, one call carries `Deleted` for the old path and `Created` for the new one.
- `POST /Library/Refresh` starts a full scan (administrator key). Fallback and a "Scan Jellyfin now" button.
- `Library/Movies/Added?tmdbId=` and `Library/Series/Added?tvdbId=` only match items Jellyfin already has, so they are not used for new titles.

The path is translated with the Jellyfin alias of the root. Afterwards SyncHub starts its own Jellyfin sync after a short delay; when Jellyfin lists the item, the title turns **In Library** and the job is marked confirmed. If it does not appear in time, the job shows "published, Jellyfin has not indexed it yet" with a Rescan button, never a silent "done".

## 11. The Library page and the size policy

### 11.1 Library page (the original tool, standalone)

It works on its own: no download pipeline, no extra PCs needed (a worker can run on the SyncHub machine).

| Part | How it works |
|---|---|
| **Scan** | Started from the page; a worker runs the analysis (the tool's file inspection, integrity modes, duplicates, naming), skipping files whose signature SyncHub already knows. A scan can be stopped. A changed file gets a new revision and **its approval resets** (the tool's rule). |
| **Overview and Review** | The tool's `classify()` over stored records: the same queues, confidences and headlines. One-by-one review, grouped lists, bulk approve with a confirmation page. **Low confidence is never bulk-approved; duplicates are decided one at a time.** |
| **Approve → jobs** | An approved record becomes a `library` job in the tool's stage order (copy, tracks, remux, convert), cheapest first. `--stop-after-hours` becomes a per-worker time window. Dry run and "verify finished files" are kept. |
| **Export** | The CSV from `media_report.CSV_COLUMNS`. |
| **Audit** | Every decision recorded as in `media_events`. |

**How the code comes over.** `media_recommend`, `media_layout`, `media_roots` and `media_report` move into a `synchub/library/` package with their imports adjusted. Their helpers (`final_name`, `filename_problems`, the status constants, `set_approval` with its revision and fingerprint rules) come from a port of the relevant part of `media_db`, over `library_*` tables with the original column names. The scan and migrate logic becomes a shared library used by the worker agent. The tool's CLI scripts, global lock, local database and web server are replaced as in §2.

**As built (phase 3).** The decision logic is the tool's, unchanged: `synchub/library/recommend.py` (queues `ready`, `suggested`, `check`, `convert`, `needs_you`, `done`; high, medium and low confidence; `FLAG_TIERS`), `layout.py`, `report.py` and `vocab.py`; the inspection code (ffprobe, naming, tracks, integrity) is `agent/media_analysis.py`, so analysis runs where the files are.

- **Identity.** A file is its share name plus its relative path (`record_key`), never a machine's path. It is therefore the same file whichever worker looks at it. Both `media` and `tfiles` are scanned; everything is brought into the **destination share** (setting, default `media`, laid out as `Movies/<name>/<name>` and `TV Shows/<show>/Season NN/<name>`). A file already at its place in the destination share is **Done** and is never moved; one in the destination share but in the wrong place is a rename/move candidate, not a duplicate.
- **Scan** (one `scan` job per share or folder; scanning the same place twice at once is refused). The worker walks the share and sends an **inventory** (`rel`, `size`, `mtime`, batches of up to 5000); SyncHub answers with the files it needs analysed: new files, changed files (size, or modified date beyond 2 s), files never completed or unreadable before, files analysed by older logic (`ANALYSIS_LOGIC_VERSION`) or with a different integrity mode, or all of them with "look at every file again". The rest are only noted as seen. The worker analyses the needed ones (a small thread pool, `scan_threads`) and sends **records** in batches of up to 200, then **finishes** the scan. A scan that is interrupted runs again and looks only at what was not yet stored. Files not seen by a scan that walked its folder without errors are marked **not found any more**; if the walk had errors nothing is marked missing.
- **Findings split.** The worker reports what it can say about one file (`findings_json`); SyncHub works out what needs other files (`findings.derive/refresh`): two files that land on the same destination name, a file already in place, duplicates. They are refreshed after every scan and when the settings change.
- **Duplicates.** A separate run. SyncHub groups files of the same size across **all** shares, the workers read the first and last megabyte (`partial_signature`) and then hash only the files that still agree. The primary copy is the one already in place in the destination share, otherwise the first; the others are suggested "skip this copy". **Duplicates are decided one at a time**, and a low-confidence file is never bulk-approved.
- **Decisions** are stored per file (`approval_status`, who, when, note; a changed file resets its approval) and every change is in `library_event`. The profile chosen on the Who-is-watching page is the "who"; with no profile it says `guest`. Approving records a decision; since the processing step (`synchub/library/migrate.py`) it also queues the job that carries the decision out.
- **Page.** Overview (queue cards with counts by confidence, share table, scans and searches, refreshed every 4 s while one runs and every 30 s otherwise), a list per queue (search, share and confidence filters, TV grouped by show and season, 100 per page), review one by one (keys `a` approve, `r` reject, `s` skip, `e` edit the name), the record page (what we suggest in plain words, editable file name, tracks to remove with locked ones disabled, identical copies, history), **Approve together** (a confirmation page that lists what will be approved; low confidence never included) and the CSV export with the original columns. Settings: destination share, movie and TV folder names, the usual file check (details only, last seconds, whole file), folders always left out.
- **Processing (phase 4, first cut):** an approved record becomes a `library` job (`op: copy` or `op: convert`); the agent's `convert` op runs ffmpeg on a local copy, verifies it and copies it into the destination share; Jellyfin is asked to refresh. Not yet: previews, preserving originals, per-file Jellyfin path updates.

### 11.2 Movie size policy

- **Movies only by default.** `tv_episode_max_gb` and `tv_episode_target_gb` exist and are **off (0)**; the same logic applies if you turn them on.
- `movie_max_gb` (default **30**): a movie larger than this is compressed, automatically. `movie_target_gb` (default **25**): the ceiling it is compressed toward. A movie between 25 and 30 GB is left alone; a movie under 25 is never re-encoded for size.
- **The target is a ceiling, not a goal.** Bits are spent on quality up to the ceiling, never padded: if fitting 25 GB would need an absurdly low quantiser (below `crf_floor`), the result is smaller than 25 GB.
- Sizes are computed after the existing track rules (English tracks only, subtitles kept), so removing foreign tracks is tried first and is lossless.

**Decision procedure** (run by SyncHub from the worker's probe facts: duration, resolution, frame rate, source codec and bitrate, bit depth, HDR flags, Dolby Vision profile, interlacing, each audio and subtitle stream):

1. **Budget.** `video budget = target × 0.99 − kept audio − subtitles`; `target video bitrate = budget × 8 ÷ duration`; never above 90 percent of the source video bitrate.
2. **Quality floor.** `bpp = bitrate ÷ (width × height × fps)`. For HEVC, floors of about 0.05 (2160p), 0.06 (1440p), 0.07 (1080p) and 0.09 (720p) are treated as visually safe. Under the floor the job goes to **needs review** with the numbers and options (accept, raise the target for this title, transcode lossless audio to free space).
3. **Calibrate.** Encode four 30-second samples spread across the film (skipping the first and last 5 percent) with the encoder actually used, fit quantiser against bitrate, and choose the quantiser whose predicted full-film bitrate meets the target, within `crf_floor` to `crf_ceiling`. This also measures the worker's real speed for the ETA.
4. **Encode** at that quantiser with a VBV cap (`maxrate` about 1.5 × target, `bufsize` about 2 × maxrate): constant quality with a safety ceiling.
5. **Check the size.** Within about 5 percent of the target is expected. Above `movie_max_gb`: retry once two quantiser steps higher; then two-pass average-bitrate at 97 percent of target; then needs review.
6. **Check the quality** (optional, where the worker's ffmpeg has VMAF): the sample windows against the source; a mean VMAF under the threshold (about 93; 95 for 2160p) pauses the job.

### 11.3 Format choices

| Item | Default | Reason |
|---|---|---|
| Video codec for compressed movies | **HEVC (x265), 10-bit** | Commonly 30 to 40 percent smaller than H.264 at the same quality; all your players direct-play it |
| Encoder | CPU **libx265** now; any hardware encoder a worker lists later | Software x265 gives the best quality per byte; hardware is faster but needs more bitrate for the same quality |
| Preset | `slow` when the measured speed allows, else `medium` | Chosen from the calibration speed and queue depth |
| Audio | **Copy** the best English track unchanged; never re-encode unless a plan says so | Only the floor-breaking review option can offer transcoding lossless audio, never automatically |
| Subtitles | Copy | Small |
| HDR | HDR10 and HLG preserved (BT.2020, PQ/HLG transfer, mastering-display and content-light data passed to x265, 10-bit) | Dropping the tags looks washed out |
| Dolby Vision | Profile 5: **not compressed**, goes to needs review. Profiles 7 and 8: encoded as HDR10 with the Dolby Vision layer lost, and the plan says so | Safe by default; a setting changes it |
| Interlaced | `yadif` only when flagged | As in the tool |
| Already HEVC | Re-encoded only if over the maximum; conservative quantiser; the plan warns about generation loss | |

## 12. Preview clips

You asked to watch a 30 to 60 second clip at the recommended compression before approving. **Yes.**

- The worker picks a window (**random**, away from the first and last 10 percent, or on request the **most demanding** part found by looking at packet sizes: the real worst case), cuts it from the source and encodes it with **exactly the planned settings** (same quantiser, filters, HDR, encoder). It also makes the matching unmodified segment of the source for comparison. The same encode doubles as a calibration sample and a size prediction.
- Default length **45 s** (30 to 60 configurable). Preview files are MP4 with the video and one stereo AAC audio track, so a browser can play them.
- The worker uploads the two clips to SyncHub (a size cap, kept 7 days, then purged). The review screen plays them side by side or toggles between them, shows the numbers, and offers **Another clip**, **Approve**, **Approve with a different target**, **Skip** and **Reject**. A download link is there for players that open HEVC better than the browser (Firefox does not decode it).
- When it is made: always for **needs review** cases that involve compression (so it is ready when you look), and on demand from any compression recommendation through a **Preview** button. A setting can also require a preview before every automatic compression (off by default, because compression is automatic).
- Previews live in SyncHub's data folder and in the job's folder on the worker; neither is ever put in the Jellyfin library. A low-confidence sample file from you becomes a test case for this and the review workflow.

### 12.1 Sample review first

You asked to be forced to look at a sample before automatic approval, to learn which files are low-confidence. **Yes, and the sample is made before the full encode.**

1. A job with a lossy plan (compress or re-encode) copies the source to the worker, **analyzes it, then makes only the preview clip** (a minute or two of work). The job pauses as **needs review** with the clip, the numbers and the plan, and **keeps its local copy**, so nothing is copied twice.
2. You watch, then **Approve** (the full encode starts at once with exactly the settings you saw), **Approve with changes** (a new target, then a new clip), **Skip** or **Reject**.
3. **Policy** (`review_first`, in Settings → Encoding): `always` (every lossy job, the default while you are learning), `until_trusted` (ask until you have approved K similar files without changes, default K = 3, then those go straight through), `low_confidence_only` (the plan's own confidence decides) or `never`.
4. "Similar" means the same **signature**: resolution class, video codec family, HDR type, bit depth and a size band. Approving without changes counts toward trust for that signature; rejecting, changing or finding a problem resets it.
5. Every reviewed sample is recorded with its numbers and your answer. That record is how the low-confidence rules get tuned, and it shows which kinds of file you actually turn down.

## 13. Notifications

Sent through the Apprise settings SyncHub already has, one message per event: **compression started** (title, source size, target, predicted size, worker), **compression completed** (before and after size, saving, quality score, time taken), **needs your review** (reason, link), **job failed** (what failed, link), **worker offline** with work waiting. Each can be switched on or off in Settings.

## 14. Delivery phases (Library first)

Each phase ends with passing tests and something you can use. Tests use a fake worker against the control plane and a fake ffmpeg runner for the agent (as your tool's tests do); a small real-ffmpeg test runs only when ffmpeg is present.

| Phase | Scope | Done when |
|---|---|---|
| **1. Control plane** | `worker` and `job` tables, tokens, register/poll/progress/complete/fail/needs-review/cancel, leases and the reaper, affinity, the Workers page and a recent-jobs list | A fake worker claims, reports, completes, loses a lease, is cancelled and is revoked, and the Workers page shows it |
| **2. Worker agent and storage** (built) | The agent (one Python file): register and poll, job folders and manifests, structured logging with rotation, status and mount-health reporting, log upload on request, crash recovery, Dockerfile, compose file and systemd unit. The site gets the **Storage page**, generated worker setup files and a "test copy" job form. The first real job type is the hash-verified **copy** (the library's "copy unchanged" stage) | A copy job between two real folders survives killing and restarting the agent mid-copy and finishes without copying twice; the Workers page shows each root's health |
| **3. Library: analysis and review** (built) | `library_*` tables, storage roots, the ported recommendation, layout, roots and report modules, scan jobs on workers (skipping known files), the Library page with queues, review, bulk approve and CSV export | Everything the tool's CLI and web UI did for analysis and approval works from the Library page |
| **4. Library: processing** | Approved records become jobs; resumable copy, track changes, remux and re-encode (whole-file); verification; publish and **Jellyfin notification** with confirmation; "verify finished files"; migration status | An approved legacy file is converted and appears In Library in Jellyfin, with the source untouched |
| **5. Review workflow and previews** | The needs-review states and actions, the review screen, preview clips (random and worst-case), sample-based tests from your low-confidence file | A flagged job pauses, shows its clip, and obeys approve, retry, skip and reject |
| **6. Size policy and robust encoding** | Budget, floor, calibration, capped quality encode, HDR and Dolby Vision, size and quality checks, chunked resumable encoding, notifications, the TV-cap setting | A movie over 30 GB ends near 25 GB (or smaller by design), start and finish notifications arrive, and an interrupted encode resumes from its last finished chunk |
| **7. Download pipeline** | Radarr/Sonarr import creates a job; in-place replace; originals kept on the worker; Radarr/Sonarr rescan; Jellyfin confirm | A download is imported, compressed if needed, replaced and visible in Jellyfin without a manual step |
| **8. Polish** | Tags, priorities, time windows, drain, per-worker encoder profiles and GPU encoders, originals housekeeping UI and restore, worker install notes | A new worker is added by its settings file alone |

## 15. Security and safety

- Each worker has its **own token** (random, shown once, stored hashed, revocable and rotatable). Worker endpoints accept only that token.
- The rest of SyncHub stays as it is: it listens on every interface with no login, and the profile picker is identity, not security, as you chose. When you add the planned access token it goes into the one request hook (§3), and the UI actions that change files will already be recorded with the profile that did them.
- Meanwhile: nothing is deleted before verification, originals are kept for the retention period, destructive actions need a confirmation, and ffmpeg commands are built only from validated options (no shell, no free text).

## Appendix A. Worker protocol as built (phases 1 to 3)

Base path `/api/worker/v1`, every call a `POST` with JSON, header `Authorization: Bearer <token>`. Answers are `{"ok": true, ...}` or `{"ok": false, "message": ...}`. 401 = token not accepted (missing, wrong or revoked), 403 = worker disabled, 409 = "that job is no longer yours" (the body has `"stop": true`: stop working on it and delete nothing yet), 426 = unsupported protocol version.

| Call | Body | Answer |
|---|---|---|
| `/register` | `protocol` (must be 1), `version`, `os`, `hostname`, `slots`, `capabilities` (`ops`, `ffmpeg`, `encoders`, `cores`...) | `worker_id`, `name`, `protocol`, `poll_seconds` (5), `lease_seconds` (90), `roots` (each share: `name`, `mount_point`, `marker`, `marker_text`), `roots_hash` |
| `/roots` | | The same `roots` and `roots_hash` without registering or marking the worker seen: for `--check` and `--init-roots` |
| `/poll` | `free_slots`, `free_bytes` (free space in the work folder), `active` (ids it is working on right now), `holding` (ids of job folders it has), `status` (summary shown on the Workers page: `work_free_bytes`, `roots` {name: `ok`, `state`, `message`, `free_bytes`}, `jobs`, `version`) | `jobs` (new jobs), `resume` (jobs SyncHub thinks it runs that are not in `active`: it restarted), `cancel` (ids to stop), `cleanup` (ids whose local files and half-written share files may be deleted; answer with `/cleaned`), `release` (folders of jobs that are done or unknown to SyncHub), `log_requests` (ids whose full log to upload), `roots_hash` (changed: call `/roots`), `paused`, `draining`, `poll_seconds`, `lease_seconds` |
| `/jobs/<id>/progress` | `stage`, `progress` (0 to 1), `fps`, `speed`, `eta_seconds`, `message`. Also the heartbeat | `cancel` (true: stop), `lease_seconds` |
| `/jobs/<id>/complete` | `result` (object), `command` | |
| `/jobs/<id>/fail` | `error_class`, `message`, `retryable`, `log_tail`, `command`, `keep_state` | `outcome`: `queued` (retry), `needs_review`, `failed` or `cancelled` |
| `/jobs/<id>/needs_review` | `reason`, `detail` (object), `keep_state` | |
| `/jobs/<id>/log` | `text` (the log; SyncHub keeps the end of anything over 1 MB), `truncated` | Accepted from the worker that ran, holds or last tried the job |
| `/jobs/<id>/cleaned` | | the worker has deleted that job's local files |
| `/jobs/<id>/scan/inventory` | `files` (`[{rel, size, mtime}]`, at most 5000), `unreadable` (`[{rel, reason}]`) | `need`: the `rel` paths to analyse |
| `/jobs/<id>/scan/records` | `records` (at most 200; each the worker's facts about one file) | `stored`, `failed`, `changed` |
| `/jobs/<id>/scan/hashes` | `results` (`[{id, size, mtime, sha256, partial_signature}]`) for duplicate-search jobs | `stored` |
| `/jobs/<id>/scan/finish` | `complete` (the walk covered everything asked for), `walk_errors` (folders that could not be listed) | `groups`, `refreshed`; marks files not seen as missing only when `complete` and there were no walk errors |

A job in `jobs` and `resume` carries: `id`, `kind`, `title`, `attempt`, `plan`, `policy`, `decision` (a person's answer, if any), `source` and `dest` (`{root, rel}` or null). **Stages** a worker may report: `claimed, copy_in, analyze, plan, preview, encode, verify, copy_out, publish, scan, done`. **Error classes** with their own rules: `transient` (retry, back-off 1, 5, 15, 60 minutes), `worker_lost`, `source_changed`, `verification_failed`, `size_target_missed` (each: one more try, then a person), `encode_failed` (a person at once); any other class (the agent also sends `dest_exists`, `permission_denied`, `unsupported_job`, `bad_job`, `agent_error`) is not retried unless `retryable` is true.

Rules worth knowing: a claim is atomic (two workers never get the same job); a job whose worker is silent for 90 s goes back to the queue and its worker keeps first call on it for 30 minutes; after a decision or retry the worker that holds the job's files also gets 30 minutes' first call; a worker only receives jobs that fit its `free_bytes`, its tags and its free slots, whose `plan.op` is among its `capabilities.ops` (an agent that lists none is trusted), and whose shares it reports as ready (a worker that reports shares but not the one a job names, or says it is not ready, is skipped; the Jobs list then says "No online worker has X mounted and ready"); a paused, draining or disabled worker receives none. A scan job's plan is `{op: scan, run_id, root, prefix, integrity, tail_seconds, exclude, force, logic_version}`; a duplicate-search job's is `{op: hash, run_id, roots, groups: [{size, partial_only, known, files: [{id, root, rel, size, mtime}]}]}`. SyncHub validates every scan call (the job must be this worker's, of kind `scan` and of the matching op). Still to come (later phases): processing jobs (copy, tracks, remux, convert into the library) and the review and preview calls.

### The agent as built (`agent/synchub_agent.py`, `agent/README.md`)

- **Copy job** (`plan {"op": "copy"}`, source and destination as share + relative path): copy in (checkpoint every 256 MB, SHA-256 while reading, resumable), copy out to `<name>.synchub-partial` (checkpointed, resumable), read the copy back and compare SHA-256, then rename. A different file already at the destination fails the job (`dest_exists`); an identical one counts as done. The source's modified date is kept. A mismatch deletes both copies so the retry starts clean.
- **Resume**: manifests are written atomically in `<work>/jobs/<id>-<hash>/`. Checkpoints are fsynced before they are recorded; on restart the agent truncates to the last checkpoint and carries on. A killed agent was tested with `SIGKILL` in the middle of copy in and of copy out: the resumed run read the source from the checkpoint and did not repeat a finished copy in.
- **Reporting**: progress and heartbeat go from a thread of their own, so a slow SyncHub never slows a copy; if a job makes no progress for two minutes (a hung mount) the heartbeat stops and SyncHub takes the job back. The final report is saved first and re-sent until accepted; the big local files are removed only after SyncHub has it.
- **Shares**: checked in the background (mounted, marker matches, writable, free space); a check that hangs is reported as "not responding" and never blocks the agent. `--init-roots` writes the markers once and never creates a folder.
- **Scan and hash jobs** (`op: scan`, `op: hash`): the worker lists the share, asks SyncHub what is needed, analyses with `agent/media_analysis.py`, and reports in batches, retrying a report that fails (`scan_retry_seconds`). The `hash` op reads only what a duplicate group needs, with the partial read first. Settings: `scan_threads` (2), `scan_batch` (25). A scan only reads: it never writes to a share.
- **Commands**: `--check`, `--init-roots [--force]`, `--healthcheck`, `--version`. Settings: INI file or environment (`agent/README.md`).
- **Shutdown** (SIGTERM, Docker stop): stops at the next safe point and reports nothing; the lease runs out and the job resumes when the agent is back.

**Settings file for the agent**, as the Workers page hands it out (the same dialog also gives `.env`, `docker-compose.yml` and `/etc/fstab` lines for the shares defined on the Storage page):

```
[synchub]
url = http://<synchub address>:5001
token = sw_...
name = encoder-1
```

**Not verified yet** (no Docker daemon or Unraid server was available when this was built): that Docker's `cifs` and `nfs` volume options mount your Unraid shares as generated (the compose file validates with `docker compose config`); that Docker accepts an SMB host *name* (it looks names up for NFS only, so the generated file warns and an IP address is recommended); and that a comma in an SMB password written as two commas is passed through. `agent/Dockerfile` has not been built.

## 16. Risks and what is still open

| Risk | Handling |
|---|---|
| Replacing a file Radarr/Sonarr imported could make them search again | Same base name and quality tag; rescan after replacing; verified on your versions in phase 7 before it is switched on |
| Originals live only on the worker PC for 30 days | Stated above; the setting can keep them on a share instead if you prefer |
| Chunk joins visible in some content | Each chunk starts on a clean frame with identical settings; verified by decode and a duration check; whole-file mode stays available per profile |
| Browser cannot play the HEVC preview | Download link and a note; the clips are plain MP4 |
| Calibration samples unrepresentative | Four spread samples, a size check, one automatic retry |
| Jellyfin slow to notice a file | Confirm step, Rescan button, optional Webhook plugin |

**Answered:** manifests instead of SQLite (yes), Docker workers, both Unraid shares needed (`Media` is the main share and the destination; `TFiles` also holds files to clean up; both are mounted on every worker), **SMB** for the workers, **workers upload preview clips to SyncHub**, sample review first instead of a prepared low-confidence file.

**Still open:**

1. **Unraid details** for the Storage page: the server's address (an IP address is the safe choice for SMB in Docker) and the exact share names.
2. **After a legacy file has been brought into the destination share, what happens to the source copy?** Options: leave it, or remove it after verification, chosen per decision. I would build "leave it" first and add removal behind a confirmation.
