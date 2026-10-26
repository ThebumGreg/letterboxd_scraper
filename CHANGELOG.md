# Changelog

## Unreleased

**Watching: honest progress, Continue buttons, and a choice for how a Roku resumes.** No database migration.
- **Opening a title is not watching it.** Jellyfin adds to a title's play count every time it is opened, even for a second (a TV's deep link opens at 0:01). SyncHub counted that as *Watched*, which is how Reno 911! episode 1 read as watched. Now only Jellyfin's own played flag counts, and an open under 15 seconds reads as *Not watched*.
- **Started watching.** The movie and episode states are *Not watched*, *Started watching*, *Watching again* and *Watched*. Jellyfin throws away a place before about 5% of the runtime; the Jellyfin events log (the webhook) still has it, so SyncHub reads it back: "Started watching, 2:00 of 1:58:00", with Resume. A last event at 90% or more reads as finished without waiting for Jellyfin.
- **Webhook that only sends progress.** Playback progress events now also refresh a person's watch record (at most every 90 seconds per title). Ticking *Playback Stop* and *User Data Saved* in the Jellyfin webhook plugin is still better.
- **Start over, and Continue.** A title you are part-way through shows **Resume at 12:30** and a **Start over** button. A show's button says **Continue S2E4** (or **Start S1E1**) and plays that exact episode on a TV, from where you stopped.
- **Continue Watching** row on the dashboard: your unfinished movies and shows, newest first, with a progress bar and one button.
- **How a Roku resumes** (Remote Apps, *When a title is resumed*, per TV). The official Jellyfin Roku app reads only an item id and type from a deep link, so it starts from the beginning. The choices: **save the place in Jellyfin, then open the title** (default; the play steps say whether the app picked it up), **stream it from the place** (exact, but Jellyfin records no progress), **open it and say where to skip to**, and **press Right to skip ahead** (experimental, `resume_key_seconds` per TV, default 10).

**Finding things, and what is new.**
- **Dashboard search** has visible labels (*Find in my library*, *Type*, *Requested or watched by*, *Library status*, *My progress*, *Age rating*, *Released from/to*, *Sort by*), searches the title, year, requesters and age rating, and shows the filters in force as removable chips. The top bar's **Search** is now **Find & request** (it searches TMDB to add a title).
- **Discover** (*New & trending*): Trending Today, Popular Movies, Popular TV Shows and Upcoming Releases from TMDB (kept for three hours), Recently Added from Jellyfin, and Coming Up in Your Library from the Radarr and Sonarr calendars. Each TMDB card shows where the title stands and the usual request buttons; each person's age limit applies.
- **Daily picks** notification (off until a person switches it on under their Notifications): each morning at 08:00, the day's trending and upcoming titles.
- **System status** page (System menu) with connections, workers, the queue, jobs waiting for you and disk space. The dashboard keeps one dot ("All systems OK") for people who may read the logs, and its count cards are now In Library, In Progress, Queued and Missing (Partial when there is one).

**A simpler menu.** Dashboard, **Discover**, Remote, Media (Library, Resolution Center; Workers and Storage are tabs of Library), System, Settings. Your name at the right opens *My profile* and *Switch profile*. *TV apps* is now **Remote Apps** and *TV tools* is **Remote Tools**, everywhere.

**A worker recovers from a network outage by itself.** Agent files only; no database migration.
- **`agent/mount-watchdog.sh`**: install it once on a worker PC (`sudo ./agent/mount-watchdog.sh --install`) and a timer checks the shares' marker files every minute. A stale or unmounted share is mounted again from `/etc/fstab`; a worker container that cannot see the shares afterwards is restarted (at most once in ten minutes), and one that failed to start for want of a share is started. It never creates a marker and never touches a share that is mounted but unmarked. Tests: `tests/v2/test_mount_watchdog.py`.
- **Mount options.** `agent/README.md` has a "Surviving a network outage" section: add `x-systemd.automount` and a wait for the network to the shares' `/etc/fstab` lines, so a mount that fails at boot with "Network is unreachable" is tried again on first use. SyncHub's generated lines are unchanged.
- It also documents the bind-mount `:rslave` option and warns against making Docker depend on the mounts.

## 1.2.0

SyncHub and the worker agent now share one version number (agent 0.5.0 becomes 1.2.0, so update every worker: `docs/UPGRADE.md`). From here on both are raised together at each release.

**A file's tracks show what really happens, and a file can be redone.** No database migration.
- **Tracks after a decision.** Once a file is approved or migrated, its Tracks table lists each track as *keep*, *removed* or *dropped* (MKV cannot hold it), using the approved choices and the conversion profile's subtitle rule. Before, every track said *keep* because the page only knew the suggestion of a pending file.
- **Redo with other settings.** On an approved or converted file's page, *Redo with new settings* takes it back to pending (optionally scanning its folder again) so you can pick another profile or other tracks and approve it again. It needs the source: after the source is deleted, only the converted copy is left. A worker never overwrites a file, so for an already converted file you remove the converted copy first and tick the box saying so.

**One page per person, and new-episode alerts.** Everything about a person is now together: People, then *Open*. Database migration 019.
- **A page for each person** (People, *Open*, or *My profile* in the top bar for your own): *Profile* (names, Jellyfin link, and a switch for administrator alerts), *Access* (pages they may open, rating limit, PIN) and *Notifications* (where to send them, what to tell them, new episodes, quiet hours and grouping, and what was sent). The People list now shows each person's PIN state and admin alerts at a glance. Another person's delivery methods hold private keys, so only they can change them; you see a summary.
- **Administrator alerts are chosen on the person**, not in Settings. People who were chosen before keep it.
- **Settings has one Notifications block**: the master switch, the Pushover application token and the optional Apprise address, and how long to keep history. The old Notifications page address redirects to your own page.
- **New-episode alerts.** For shows on your watchlist or that you requested. Later episodes arrive as one notice per show ("S1E2 and S1E3 of Severance are now in the library"); the first episode is still the "title I wanted" notice. Each person chooses *when* (when it reaches the library, when it airs, or both) and *for which shows* (watchlist, requests, or both), and can **mute a single show** with the switch on its page. The airing notice is checked hourly and is sent once per show per day.

**Preview clips before a compression (Library, Settings, *Preview clips before compressing*).** A worker cuts a short window of the film, compresses it with exactly the settings the real job would use, makes the matching unmodified clip and sends both to SyncHub. You watch them side by side, with the numbers, and answer. Database migration 018; the worker agent needs the new `preview` operation, so update every worker (`docs/UPGRADE.md`); a worker without it is simply not given preview jobs.
- **When you are asked.** *Every time* (the default while you learn to trust it), *until I have approved a few like it*, *only when it is close to the edge* (HDR, interlaced, or within a quarter of the quality floor), or *never*. "Like it" means the same resolution class, codec family, HDR type, bit depth and 10 GB size band. Approving without changes counts toward trust; changing the target or rejecting starts that kind over. This applies to new Radarr/Sonarr downloads: a compression waiting for you gets its preview made for you (switchable) and shows a **Watch the preview** button. A downloaded film is not compressed until you answer, even in *Replace when safe* mode, unless the setting is *never*.
- **Any Library file.** A file's page has **Make a preview** when the size rules would compress it. Approving there only records that the size looks right; the Library approval still decides.
- **The clip.** 45 seconds by default (30 to 60), a random stretch away from the first and last tenth, or **Another clip** for the most demanding stretch (found by sampling 16 places for the most video data). Both clips are MP4 with the video and one stereo AAC track; the unmodified one is copied when MP4 can hold it and otherwise re-encoded at high quality (HEVC for HDR so the colours stay). Kept 7 days, then removed; so are the clips of answered previews after a day.
- **The review screen.** Two players that start, pause and seek together, download links for browsers that cannot decode HEVC, the video bitrate before and after, and an estimate of the whole film's size from that window (a busier or calmer part will differ). Buttons: *Looks good*, *Try this target* (a new clip at the size typed), *Another clip*, *Leave the file alone* and *Not good enough*. Typing a different target and pressing *Looks good* compresses at that size for that film only.
- Not done: gating a Library file's own approval on a preview (previews there are on demand), the four-sample calibration, VMAF.

**Size policy for big movies (off by default).** Library, Settings, *Compress movies that are too big*. A movie over the limit (default 30 GB) is re-encoded to HEVC so it ends under the target (default 25 GB). It applies to approved Library files and to Radarr/Sonarr downloads (in *Replace when safe* mode a plain compression runs on its own).
- **Tracks first.** Foreign tracks are removed before anything is re-encoded; if that alone brings the film under the limit, it is not compressed.
- **A ceiling, not a goal.** The encode is constant quality (CRF, default 22) with a bitrate cap set from the target after audio and subtitles, so the result cannot pass the target but may end well under it. The cap is never above 90 % of the source's own video bitrate.
- **Quality floor.** If fitting the target would give fewer bits per pixel than HEVC needs at that resolution (0.05 at 2160p, 0.06 at 1440p, 0.07 at 1080p, 0.09 below), the import waits on the Library page with the numbers; approving accepts the lower quality. Audio that alone leaves no room is blocked.
- **HDR.** HDR10 and HLG are kept (10-bit, colour tags, mastering display and light level read from the file). Dolby Vision profile 7 and 8 become HDR10 and the Dolby layer is lost (a setting can ask first); profile 5 is left alone (a setting can compress it as HDR10, which looks wrong). An HDR picture is never turned into H.264. Interlaced pictures are deinterlaced.
- **Size check.** If the result is still over the limit it is tried once more two quality steps lower, then held for a person.
- Uses the conversion profiles' fields (`video_codec`, `crf`, `preset`, `ten_bit`); a profile you chose that encodes to HEVC keeps its own quality and only gets the cap. The analysis now records bit depth, HDR type, Dolby Vision profile and track bitrates (analysis version 2: the next scan looks at every file again). Database migration 017. Not done: the four-sample calibration, chunked resumable encodes and VMAF checks.

**Conversion and migration module.** Profiles, rename only, a cancel that stays cancelled, deleting sources later, telling Radarr/Sonarr, and the Dashboard stages of a job. Database migration 016; the worker agent is 0.5.0, so update every worker (`docs/UPGRADE.md`).
- **Conversion profiles** (Library, *Conversion profiles*). A profile sets the video (keep it, HEVC or H.264), the quality (CRF) and encoder speed, 10-bit, the audio bitrate and what happens to subtitles: remove other languages or keep every subtitle. Built in: *Standard* (today's behaviour), *Keep all subtitles*, *Move only* (copies the file exactly, nothing inside changes), *HEVC smaller / balanced / high quality* (HEVC 10-bit, CRF 27 / 23 / 20). Make your own on the same page. The default is set under Settings; each file can use another one on its page. A job carries a copy of its profile, so changing a profile later does not change jobs already queued. A job that needs a profile only goes to an agent that supports it, and an HDR video is never re-encoded by a profile (it is refused with the reason).
- **Rename only.** A file on any share can be renamed into the Movies / TV Shows layout *inside its own share*: no copy, nothing in the file changes, and its subtitle and info files (Heat.en.srt, Heat.nfo) follow it. A file already on the destination share at the wrong path is now suggested this instead of a copy next to itself. Choose it on a file's page under *How should it be handled?*. Radarr/Sonarr are always told the new location afterwards (unless set to leave them alone).
- **A cancelled job stays cancelled.** The file shows as cancelled on the Library page with a **Queue again** button; *Queue them now* no longer revives cancelled files and no longer retries failed ones (they have the same button).
- **Delete the source later.** Off by default. Under Settings choose 7, 14 or 30 days: after a verified copy or conversion a worker deletes the source only if the library copy still matches what was verified and the source is the file that was copied. Turning it back to Off stops every pending deletion; any file's source can be kept from its page.
- **Radarr and Sonarr.** Choose what they are told after a migration: nothing, the new location (Radarr's folder is changed without moving files, Sonarr's only when no other episode of the show is left in the old folder), stop monitoring, or stop tracking (Radarr only: Sonarr cannot forget one episode without deleting its file, so TV is unmonitored). And when: right after the copy, or only when the source is deleted. Set each share's Radarr and Sonarr path on the Storage page.
- **Job stages.** Jobs show one set of labels everywhere: Queued, Assigned to a worker, Transferring to worker, Working, Converting, Transferring to library, Completed, Needs you, Failed, Cancelled. The Dashboard shows the same stages for the title being handled (Transferring to Worker, Working, Converting, Transferring to Library, then In Library); a title already in the library shows its stage while it is converted and goes back to In Library without a new arrival notice.
- Agent 0.5.0: `rename` and `delete_source` operations, HEVC/H.264 profile encodes. Not done yet: per-file Jellyfin updates with an "In Library" confirmation (after the first real run).

**One Media page.** Library, Workers and Storage are now tabs of one page (the menu entries and the /library, /workers and /storage addresses still work and open their own tab). On the Workers tab the *More* menu now opens above the table instead of being cut off by it, and the inner Workers/Jobs/Worker Status switch is drawn as pills. Troubleshooting: a worker whose token SyncHub no longer accepts (docs/DEPLOY.md, docs/UPGRADE.md).

**Worker upgrades.** `agent/update-worker.sh --report` prints a worker's current state without changing anything, and `docs/UPGRADE.md` is the step-by-step upgrade guide (SyncHub first, then each worker; report, dry run, update, check, roll back).

**Download pipeline (off by default).** A file Radarr or Sonarr imports can now be checked and, where the rules call for it, replaced in place. Switch it on under Library, Settings, *New Radarr/Sonarr downloads*: *Ask me first* lists each import on the Library page with a **Replace it** button, *Replace when safe* converts files whose plan is plain (tracks removed, lossless remux, English audio confirmed) and still asks about the rest.
- **Flow.** The import webhook (Radarr `Download`, Sonarr `Download` and `ImportComplete`) leaves a row; a worker scans the file's folder (the Library's analysis and track rules), SyncHub decides, and a `download` job converts the file on the worker and swaps the result in under the same base name (an `.avi` becomes `.mkv` beside it, the old file is removed). The file's modified date is kept so Jellyfin's "date added" does not move. Then Radarr/Sonarr is asked to rescan the title and Jellyfin to scan.
- **Originals are kept on the worker** in `work/originals/` for *Keep originals (days)* (default 30; 0 drops the original once the replacement is verified), with a `meta.json` saying where each came from. The worker removes them only after that time. The Workers page shows how many are held.
- **Safe by construction.** The worker checks the file on the share is still the one that was analysed before and again just before the swap; the new file is written as `.synchub-partial`, verified, and renamed in. Nothing is replaced if the file changed.
- **Needs setup.** Each share on the Storage page needs the path Radarr/Sonarr see for it. An import no share covers is listed as blocked with that reason.
- Agent: `replace` conversions, originals folder and sweep. Rebuild the worker (`docker compose up -d --build`). Database migration 015. Not done yet: the size policy for 30 GB+ movies and preview clips (next PRs).

Notifications: each person chooses what they hear about and when.

**Notifications**
- **What to tell me about**: an on/off switch per kind of notification on the Notifications page. Administrator alerts show only for the profiles chosen under Settings, Notifications, *Who gets administrator alerts*.
- **Quiet hours** (per person, with a time zone) and **grouping** (every 15 minutes or hour). Ordinary notices wait for the end of quiet hours or the next group time and are then sent as one message. Urgent ones (worker offline, share not mounted) always come through at once.
- **New notices**: a wanted title reaches the library (a movie, or the first episode of a show; a title above the person's rating limit is never named to them), a job fails or needs a decision (or finishes, off by default), a worker goes offline or comes back, a share stops working or recovers, and a Google TV has not accepted SyncHub's ADB key. Conditions notify once when they change, not on every check, and a first look says nothing. More than 50 titles arriving in one pass is treated as an import and is not announced.

Approved Library files are now processed: approving queues a job, a worker converts the file and brings it into the destination share, and Jellyfin is told.

**Library processing**
- **Approving queues a job.** Approving a file (one by one, in bulk or after testing) creates a `library` job: source share and path, destination share and path (the Movies / TV Shows layout) and the approved plan. The next free worker that has both shares ready takes it; if every worker is busy it waits in the queue. Rejecting or undoing takes a waiting job back; a file that changed on disk withdraws its waiting job along with the approval.
- **Migration queue panel** on the Library page: how many files are in the queue, running, waiting for a decision, failed and completed. Files approved before this release show a **Queue them now** button.
- **Completed means done.** A file's migration status moves to *in progress* when a worker takes it and to *completed* only after the converted file has been copied into the destination share and verified. A failed or held job shows as failed or manual review and follows retries, skips and cancels from the Workers page.
- **Agent `convert` operation**: copies the source in, runs ffmpeg for the approved plan (lossless remux to MKV, H.264 re-encode, audio to AAC, tracks removed, English default), checks the result (video present, planned track counts, duration within 0.5 %, clean decode at the end), then copies it out with the usual verification. The source is never changed.
- **Jellyfin** is asked to scan its libraries each time a file arrives (`POST /Library/Refresh`). Best effort: if Jellyfin is unreachable the file is still in place and Jellyfin finds it on its next scheduled scan.

Worker setup, worker logs and a Worker Status tab.

**Workers**
- **`agent/setup-worker.sh`**: one script that sets up (and later updates) a worker PC. It installs the dependencies, asks SyncHub for the worker's settings and shares, pulls the latest code from GitHub, updates `/etc/fstab` (one marked block, with a backup) and a private SMB credentials file, mounts the shares, writes `.env`, builds and starts the worker, marks the shares and checks it. `--mode native` sets up a PC without Docker; `--dry-run` shows what it would do. The Setup dialog on the Workers page has the command as its first tab. New worker API calls for it: `POST /api/worker/v1/setup/<file>`.
- **Logs button** on the Workers page: fetches the tail of a worker's own log (`worker.jsonl` and its rotated files) into SyncHub, with a level filter, search, copy and download. Worker API: the poll answer carries `worker_log`, and the agent answers with `POST /api/worker/v1/log`.
- **Worker Status tab**: per worker, the CPU (model, cores, threads, instruction sets, use, load), memory, graphics cards, system, ffmpeg and ffprobe, hardware decoders, VMAF, and a table of encoders that says which **work** (a test frame was encoded), which are built into ffmpeg but **failed** (usually no GPU or driver) and which are not tested. The agent also tests SVT-AV1 and the AV1 hardware encoders now.
- Agent 0.4.0. An older agent still works; the new tab says its hardware has not been reported, and the Logs button needs 0.4.0.
- Database migration 013 (worker logs).

## 1.1.1

Where each person is in what they watch, with TVs that pick up from there, and the groundwork for notifications (Pushover). The database upgrades itself on start (migrations 011 and 012) and saves a copy first; going back to an older image afterwards is not supported, so restore a backup if you ever need to (docs/DEPLOY.md, section 6).

**Watch progress and resuming**
- Each profile's Jellyfin watch data (what they have watched, and where they stopped) is copied from Jellyfin: a **Watch history** task (with the Jellyfin sync and in *Run all*) and a re-read of one item right after a playback stop event. Database migration 012.
- Movie and show pages show **Your progress**: status, position and percentage, last watched, how many times; for a show, how many episodes watched, what to continue with and what was watched last. Each episode in a season shows its own status.
- **Watch now** says **Resume** (at 42:10, or S2E4 at 12:30) and a TV picks up where the person using the page left off; the menu has **Start from the beginning**. Before, a title always started from the beginning, a show dropped the position of the episode they were in, and the search dialog's *Resume at* showed the Jellyfin administrator's position instead of the viewer's.
- A deep link (Roku) cannot be moved to a position because the official app accepts no remote commands; the progress box says so instead of claiming it. A direct stream cannot start part-way either (a transcoded one can); both say so.

**Notifications: the foundation** (see docs/DESIGN_NOTIFICATIONS.md; nothing is sent about requests or episodes yet)
- A **Notifications** page and permission: each person adds a Pushover user key (checked with Pushover, which also lists their devices), sends a test, switches a method off or removes it, and sees a history of what was sent. Keys are masked, never sent back to a browser and blanked from the log viewer; a profile without a PIN cannot change them once any profile has one. Profiles that have a saved page list need **Notifications** added on People (guests do not get it).
- Settings, Connections, Notifications: the Pushover application token (with a *Test token* that shows the month's remaining allowance), the master switch, the Apprise address (now for administrator alerts) and how long the history is kept.
- A delivery layer that more methods can be added to as one file each: an outbox that sends from a scheduler job (so `SYNCHUB_SCHEDULER=0` silences it), retries temporary failures, never repeats a refusal, switches off a recipient Pushover says is invalid, and holds Pushover when its monthly allowance is used up or its token is rejected.
- Database migration 011 (delivery methods, events, the outbox and history). The old single Apprise notifier is gone.

## 1.1.0

The whole-house TV remote, parental controls, and a published Docker image.

**TV remote** (Roku, Google TV, Android TV)
- Remote page with now playing (a clock that runs smoothly between updates), D-pad, volume, power and sleep, and quick-launch apps; TV apps picks what each remote shows; TVs adds, edits and removes TVs and finds them on the network; TV tools is the testing page.
- Plays Jellyfin titles: wakes the TV, opens the app and plays, going Home first where the TV needs it (screensaver, a Roku already playing something). A show plays its next episode. A Google TV that is on the player screen gets the old title stopped first, and one that ignores the command gets Home, a reopen and one more try.
- **Watch now** (dashboard, movie, show and episode pages) offers This device or any TV, and a TV opens its Remote page with each step shown. The progress box has a Close button when playback worked and stays open, with Try again, when it did not.
- A **Playing now** strip on the dashboard; TV settings are backed up with the database (`.tv.zip`).

**Pages and menu**
- Menu: Dashboard, Remote, Search, Media, System (Run, Monitor, Admin, TV), Settings. Settings in three tabs; People joins profiles, PINs and permissions; What it's doing now is its own page; Workers and Jobs are tabs.
- Dashboard sorts by **Date Added** and shows it; the System menu no longer opens under the search bar; "Match & update" shows correctly; Clear History on Payloads works with CSRF protection on.

**Ratings and parental controls**
- Movie and show pages show the US age rating and the TMDB audience score.
- A most-mature-rating limit for every profile and for guests, applied to the dashboard, pages, search, requests and the Remote. Unrated titles are hidden from anyone with a limit.
- Guests, and any profile without a PIN, get only the Dashboard and Remote and nothing above PG (both changeable). Nothing is restricted until some profile has a PIN, so first-time setup works.

**Deployment**
- GitHub Actions runs the tests and publishes the image to `ghcr.io/thebumgreg/synchub` (`:latest` and `:1.1.0` from main). An Unraid template makes installing and updating a few clicks in the Docker tab. The image includes `adb`, and its key is kept in `/data/tv`. `SYNCHUB_SCHEDULER=0` starts the app without background jobs, for trying a copy of a live database. The version is shown on every page.
- Database migrations 009 (date added) and 010 (ratings).

## 1.0

Docker deployment (app image, database snapshot and restore tools, CSRF protection), workers and the Library, Radarr/Sonarr/Jellyfin live updates, search and add, profiles with PINs and permissions.
