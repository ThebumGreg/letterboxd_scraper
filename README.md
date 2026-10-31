# SyncHub

**Version 1.4.0** ([what changed](CHANGELOG.md)). Keeps Letterboxd/Serializd watchlists, Jellyfin, Radarr and Sonarr in one place, shows what you have, what is on its way and what is missing, and can add wanted titles to Radarr/Sonarr. It is also a whole-house TV remote (Roku, Google TV / Android TV) that plays Jellyfin titles, and has profiles with PINs, page permissions and parental controls.

**Documentation**

| | |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Installing and updating on Unraid (the image is published to GitHub), moving a database, workers, backups, restoring, troubleshooting |
| [docs/RELEASING.md](docs/RELEASING.md) | How a change reaches the server: CI, the published image, version tags |
| [docs/DESIGN_NOTIFICATIONS.md](docs/DESIGN_NOTIFICATIONS.md) | Notifications: Pushover's rules, the design, what each phase adds |
| [docs/TV_REMOTE.md](docs/TV_REMOTE.md) | How the TV remote works, supported devices, adding another kind |
| [docs/CAMERAS.md](docs/CAMERAS.md) | UniFi Protect cameras on the TV remote, and the go2rtc relay on a worker |
| [docs/READINESS_CHECKLIST.md](docs/READINESS_CHECKLIST.md) | A checklist to run through before trusting a new version |
| [docs/DESIGN_V2.md](docs/DESIGN_V2.md), [docs/DESIGN_ENCODING.md](docs/DESIGN_ENCODING.md), [docs/DDNS_DELAY.md](docs/DDNS_DELAY.md) | Design notes: data model, the workers and Library plan, the Dynamic DNS findings |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## Installing

**On Unraid (recommended):** the image is `ghcr.io/thebumgreg/synchub`. Add the template from `unraid/synchub.xml` once, install it from the Docker tab, and update it later with *Check for Updates* and *apply update*, no command line. [docs/DEPLOY.md](docs/DEPLOY.md) has the steps. Anywhere with Docker: `docker compose up -d` (see `docker-compose.yml` and `.env.example`).

**From a checkout, for trying it out or developing:** the next section. The database is `data/synchub.db`, created on first start; port 5001 (`SYNCHUB_PORT` changes it).

## Running from source

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # includes Flask-WTF: forms and buttons are protected against forged requests (CSRF)
python -m synchub.web
```

Set `SYNCHUB_SCHEDULER=0` to start without the background jobs (no syncs, scrapes, DDNS updates or nightly backups run by themselves; the Run buttons on Tools & Operations still work). Use it to try a copy of your live database without a second set of jobs acting on your real services.

Then open http://localhost:5001 and:

1. **Settings, Connections**: enter your TMDB API key (needed for titles, posters and episode lists) and the Jellyfin/Radarr/Sonarr URLs and API keys (use *Test connection*). Then **System, People**: create your profiles. Each Letterboxd, Serializd or Jellyfin identity can belong to only one profile.
2. **Tools & Operations**: press **Run all**. It reads Jellyfin, scrapes the watchlists, resolves IDs, matches everything to a movie or show, fetches every episode from TMDB, then reads Radarr and Sonarr and works out each item's status.
3. **Resolution Center**: approve or dismiss the matches the app was not sure about, or link an item by TMDB ID. **Approve all** (top of the page) approves the strongest suggestion for every title listed and leaves titles whose best suggestions are tied for you to decide.

After that the scheduler repeats the work on the intervals set in Settings, Automation (0 turns a job off). Radarr/Sonarr webhooks (`/api/webhook/radarr`, `/api/webhook/sonarr`) refresh the status within seconds of a download starting or finishing.

## Running it in Docker (Unraid for the app, Ubuntu for the workers)

The image is published to GitHub by an Actions workflow on every change to `main` (`ghcr.io/thebumgreg/synchub:latest`, or a version such as `:1.2.0`). One `/data` volume holds the database, logs, poster cache, TV settings and backups. Workers (heavy file work on other PCs) are built from `agent/` with its own `Dockerfile`. **[docs/DEPLOY.md](docs/DEPLOY.md) is the step-by-step guide**: getting the image, installing and updating on Unraid from its web UI, moving your existing database, the shares and workers, restoring, and what to do when something is wrong. The TV remote needs the container on your LAN for *Scan network* and Wake-on-LAN (Unraid: Custom br0); see "The TV remote in Docker" there.

Two commands worth knowing, which also work from a checkout (in Docker: `docker exec synchub python -m synchub.backup ...`): `python -m synchub.backup create FILE` makes a **consistent copy of the database while the app runs** (copying `synchub.db` by hand can miss recent changes that still sit in the `-wal` file next to it) and `python -m synchub.backup check FILE` says what is in a copy, whether it is sound, and which service addresses will stop working after a move (a `localhost` address means the container itself inside Docker). SyncHub also saves a copy every night at 03:30 (the newest 7 are kept; Settings, Automation, *Database backups*) and before any upgrade changes the database. The TV pages keep their settings (TVs added, app choices, Google TV pairing) in a `tv` folder beside the database rather than in it, so each copy comes with a second small file, `<name>.tv.zip`, and `backup restore` puts both back (the TV folder it replaces is kept beside it). `backup check` says whether the TV settings are there. `GET /healthz` answers while the app and its database are up.

## What the statuses mean

Missing, Queued (monitored in Radarr/Sonarr, nothing yet), Downloading (in the Radarr/Sonarr queue), Downloaded (the file exists but Jellyfin has not picked it up), In Library. **Transferring** and **Encoding** are defined but unused until your own scripts report them. A show is **Partial** while only some of its aired episodes are in the library. Episodes that have not aired show as *Not aired* and are not counted as missing. Specials (Season 0) are listed last and are not counted in a show's totals.

New states need no code or schema change: add a row to `state_def` and have a script write `processing_event` rows.

## Workers (control plane)

**Workers** (Media menu) is where the PCs that do the heavy work are added and watched. **Add worker** gives a name, tags, a slot count and a one-time token, together with ready-made setup files for that PC (a Docker compose file, an `.env`, an agent settings file and `/etc/fstab` lines). A worker can be paused, drained (finish what it has, take nothing new), disabled, given a new token or have its token revoked. The **Logs** button fetches a worker's own log into SyncHub (filter, search, download) for troubleshooting, and the **Machines** section of the Workers tab shows each machine: CPU model, cores and instruction sets, memory, graphics cards, system, ffmpeg, and which encoders really work (x264, x265, SVT-AV1, NVENC, Quick Sync, VAAPI), with live CPU use and load. The page also lists each worker's shares ("ready", "not mounted", "wrong share"...), recent jobs with their stage, progress and attempts, lets you cancel, retry or decide jobs that are waiting for you, fetch a job's full log from its worker, and queue a **Test copy** (a verified copy of one file between two shares) to prove a worker end to end.

**Storage** (Media menu) defines the Unraid shares the workers use: a short name, SMB or NFS, the server address, the share, and the folder every worker mounts it at. SyncHub cannot mount anything on a worker PC itself, so it generates the files that do (Docker mounts the shares, and a worker refuses any share whose `.synchub-root` marker file is missing or names a different share). Share logins never enter SyncHub; they stay on the worker PC (`setup-worker.sh` keeps them in a private `/etc/synchub-smb.cred`; Docker's own mounting reads them from `.env`).

The worker side is a small API (`/api/worker/v1/...`, token per worker, documented in `docs/DESIGN_ENCODING.md`, Appendix A) and an agent for Ubuntu in `agent/` (one Python file; `agent/README.md` has the install steps). **`sudo ./agent/setup-worker.sh --url ... --token ...` sets a worker PC up in one go** (dependencies, the latest code from GitHub, the shares in `/etc/fstab`, the `.env` file, start and check) and updates it when run again. Workers ask for jobs; a job whose worker goes silent returns to the queue and resumes where its files are. Verified copies and the Library scans work today; processing approved files and the encoding itself are the next phases of that plan.

## Library

**Library** (Media menu) is the original media analysis and migration tool as a page. It looks at every video file on your shares, says in plain words what it would do with each one (copy as it is, a clean name, remove tracks you do not need, convert an old format) and how sure it is, and you approve what is right. Everything is meant to end up in one share (**Media** by default, as `Movies/<name>/<name>` and `TV Shows/<show>/Season NN/<name>`); files that are already in the right place are left alone.

- **Look at a share** starts a scan on a worker, so it needs a worker with the share mounted (see Workers and Storage). Files it has already looked at and that have not changed are skipped, so running it again is quick. A scan can be stopped, and one that was interrupted carries on with what is left. Looking at files never changes anything on a share.
- **The queues** (Ready to approve, Suggested changes, Worth a look, Format conversion, Needs you, Done) show how many files wait with high, medium and low confidence. Open a list to search and filter, or **Review one by one**: it shows the suggestion, the file name (editable), the tracks and why, and has Approve, Reject and Skip (keys `a`, `r`, `s`; `e` edits the name).
- **Approve together** approves everything in a list that qualifies, after showing what it will approve. Low-confidence files are never included, and duplicates are decided one at a time.
- **Find duplicates** compares files of the same size across all shares (reading only what it must) and suggests keeping the copy already in place.
- **Export** gives the same CSV report as the original tool.
- **Approving records a decision and nothing else.** Copying and converting the approved files is the next phase.

## TV remote

**Remote** (top bar) controls the TVs and streamers in the house from SyncHub, as a remote that fits a phone or an iPad. It is the everyday page: what the TV is playing, power and sleep, quick-launch apps, the D-pad, volume (tap or hold) and **Watch a movie**, which wakes the TV from whatever state it is in, opens the Jellyfin app and plays the title. Under **System** there is a TV section: **Remote Apps** chooses which apps each TV's remote shows. **TVs** adds, edits and removes TVs and **finds them on the network** (Roku and Google TV / Android TV), so no file has to be edited. **Remote Tools** is the testing page: every key, wake and sleep tests, Jellyfin sessions and commands, and the log of every call sent to a TV. Roku, Google TV (Android TV Remote protocol, optionally with ADB) and Android TV (ADB) are supported; opening an app from a screensaver, and playing a new title on a Roku that is already playing one, are handled by going Home first. People has two permissions for it (TV remote opens Remote and Remote Apps, TV setup opens TVs and Remote Tools). How it works, the supported devices and how to add another kind: [docs/TV_REMOTE.md](docs/TV_REMOTE.md).

## Notifications

**My profile** (top bar) opens your own page under People, where you choose how SyncHub reaches you: where messages go, which alerts you get (a title you wanted is available, new episodes of shows you follow, and for administrators job, worker, share and TV alerts), quiet hours and grouping. The administrator switches sending on and enters the Pushover application token once in Settings, Connections. See [docs/DESIGN_NOTIFICATIONS.md](docs/DESIGN_NOTIFICATIONS.md).

- **Pushover** is the first delivery method. It needs two things: an **application token** that the administrator creates once (register SyncHub as an application at pushover.net/apps/build, which is free) and pastes under **Settings, Connections, Notifications** (*Test token* checks it and shows how much of the month's allowance is left), and each person's own **user key**, shown at the top of the Pushover app. Pushover allows 10,000 messages a month for the whole account, and every message to every person counts toward it.
- **A person** opens their page (My profile, Notifications tab), pastes their user key, presses **Check** (SyncHub asks Pushover whether the key works and lists their devices so one can be chosen, or all), gives it a name and presses **Add**. **Send test** sends a message at once, whatever the switch below says; **Switch off** and **Remove** do what they say. The page keeps a history of what was sent to the person and how it went.
- **The administrator's switch** (*Send notifications*, same panel) is off until turned on; while it is off, nothing is sent on its own, only the tests people ask for. The same panel has an optional **Apprise** address (email, SMS, Telegram, ntfy and many more) that is kept for administrator alerts, and how long the history is kept (default 90 days; 0 keeps it for ever).
- **Keys are private.** A key is shown again only as its last four characters, is never sent back to a browser, and is blanked out of the log viewer. Anyone on the network can choose a profile that has no PIN, so once any profile has a PIN, a profile without one cannot change its notifications (give it a PIN on People). The **Notifications** permission on People decides who may open the page. Deleting a profile deletes its keys and its history.
- **Pushover is treated carefully.** A key that is the wrong shape is refused before Pushover is asked. A message Pushover refuses is never repeated (it says that cannot work, and a run of refusals gets the server's address blocked), a key it says is invalid is switched off after three such answers, a temporary problem is retried with a growing wait (30 seconds up to an hour, five tries), and a used-up monthly allowance or a rejected application token puts Pushover on hold (shown on both pages) until it can work again.
- Sending needs the container to reach `api.pushover.net` over HTTPS.

## Search and add

**Search** (top bar) looks a title up on TMDB and can send it straight to Radarr or Sonarr. Type a title, optionally with a year (`Heat 1995`); if a title simply ends in a number-like year (`Blade Runner 2049`) the whole text is searched when the year finds nothing. Results show movies, then shows, with where each already stands (In Library, Queued, Downloading ...). Nothing is saved until you add something.

- **Movies**: **Add to Radarr**. A movie already in Radarr is shown as such instead of offering the button.
- **Shows**: **Add to Sonarr...** opens a season picker (every regular season ticked, Specials last and unticked). SyncHub reads every episode from TMDB first, then sends the ticked seasons to Sonarr. A show can be asked for more seasons later with **Request seasons...**. Sonarr needs a TVDB ID; if TMDB has none the picker says so.
- **Who asked**: the profile chosen on the Who-is-watching page is credited ("Wanted by") and counts as demand, even if they have watched the title. A guest can add too, but nobody is credited. Requests stay when a profile is deleted (they just stop counting).
- A failed add (for example a Radarr root folder that does not exist) says why and leaves nothing behind in the catalog.

## The menu

**Dashboard, Discover, Remote, Media, System** and **Settings** are in the top bar, and your name at the right opens **My profile** and **Switch profile**. **Discover** is a dropdown with **New & trending** (TMDB's trending, popular and upcoming lists, what was added to Jellyfin lately, and what Radarr and Sonarr expect in the next three weeks; each person's age limit applies) **Find & request** (search TMDB and add a title to Radarr or Sonarr) and **My watchlist** (your own list, seeded from your Letterboxd and Serializd watchlists; add or remove films and shows, and optionally have watched titles come off; it never requests or downloads anything, and auto-add works as before). The Dashboard list can be sorted by **Date Added** (when Jellyfin added the title to the library; a show counts as new as its newest episode) and shows that date under each title. The dates come from Jellyfin's next sync after an upgrade. While a TV is playing something through Jellyfin, a **Playing now** strip at the top of the Dashboard names it and links to its remote. **Media** is a dropdown with Library and Resolution Center (Workers and Storage are tabs of the Library page). **System** is a dropdown in four headed groups:

| Group | Pages |
|---|---|
| Run | Tools & Operations |
| Monitor | System status, What it's doing now, Logs, Jellyfin events, Payloads (things you look at) |
| Admin | Dynamic DNS, People |
| TV | Remote Apps, TVs, Remote Tools |

A person only sees the entries their profile may open (see People below); a group with nothing in it is hidden, and so is a dropdown with nothing in it. **Settings** has three tabs, each saved on its own: *Connections* (Jellyfin, TMDB, Radarr, Sonarr, push notifications), *Automation* (schedules, live updates and webhooks, backups, and the rules for adding titles to Radarr and Sonarr) and *Logging* (what is kept for the Payloads page). The old addresses `/access` and `/diagnostics` still work and lead to People and What it's doing now.

## Who is watching, PINs and permissions

The first visit from a browser shows **Who is watching?**: pick your profile, or "Just looking" to continue as a guest. The choice is remembered on that device for a year, the top bar shows who you are with a **switch** link, and the profile's Jellyfin user is the one the Jellyfin app links open as. With no profiles nothing is asked.

**People** (System, Admin) lists the profiles (their Letterboxd, Serializd and Jellyfin identities, and whether they take part in scraping and requests) and, below them, gives each profile an optional **PIN** (4 to 8 digits) and a list of the pages it may open: Dashboard, Search, Requests (the buttons that request or add titles), Notifications, Resolution Center, Dynamic DNS, Library, Workers, Storage, TV remote, TV setup, Tools & Operations, Logs, Payloads, Settings and People itself. Guests have their own list. Quick picks (Everything, Viewer, Dashboard + remote, Nothing) fill in the boxes.

- **Nothing is restricted until some profile has a PIN.** Until then nobody can be told apart, so every device can open everything (otherwise the first profile you add would lock you out of People). Give your own profile a PIN to switch the restrictions on.
- **Guests, and profiles without a PIN, get the guest settings.** Once any profile has a PIN, a guest ("Just looking", or a device that has not chosen) *and any profile that has no PIN* can open only the **Dashboard** and the **Remote** (which includes Remote Apps), and sees nothing rated above **PG**. A profile without a PIN can be chosen by anyone on the network, so its own page list and rating limit only take effect once it has a PIN. It is still the profile requests are credited to. Change the guest pages and limit on the People page, under Guests.
- **A profile with a PIN** asks for it when chosen, then remembers that device for 30 days (changeable on the People page). Five wrong PINs in a row lock that profile for 30 seconds, doubling each time up to 15 minutes; **Unlock** on the People page clears it. Changing a PIN, or **Sign out everywhere**, makes every device ask again.
- **A profile without a PIN** can still be chosen by anyone on your network, which is why it is limited to the guest settings (the People page marks it "guest limits apply").
- A page you may not open shows a short explanation (a script gets a 403 answer). Webhooks, workers, images and the sign-in page are never behind a permission; each worker and webhook has its own way in.
- **Parental controls.** Every profile and the guest account has a **most mature rating** it may see: G (G, TV-Y, TV-G), PG (PG, TV-PG), PG-13 (PG-13, TV-14), R (R, TV-MA) or no limit. Ratings are TMDB's US ratings, read when a title's details are refreshed (and for titles that have none yet during *Match & update*, a few hundred per run; open a title or run Match & update after upgrading if the dashboard looks empty for a limited profile). The limit applies to the Dashboard (rows, counts, the export), movie and show pages, search results (each result's rating is looked up once and remembered), the request and add buttons, and the Remote's Jellyfin search, episode lists, play calls and "Playing now" strip (Jellyfin's own rating counts too; the stricter of the two wins). **A title with no US rating, or one not looked up yet, is hidden from anyone who has a limit**; NC-17 only shows to someone with no limit. A profile's own limit only applies once it has a PIN; before that it gets the guest limit. Posters are served without a check, so a poster address someone already knows still works.
- Somebody with a PIN must always keep the People permission: removing it from the last such profile (or deleting it) is refused, unless that is the last PIN, which returns everything to the unrestricted state above.
- **Lost every PIN?** On the machine that runs SyncHub: `python -m synchub.access reset-pin "Alice"` takes one profile's PIN off and gives it every permission back; `python -m synchub.access reset-all` does that for everyone, guests included (a rating limit is not touched); `python -m synchub.access list` shows where things stand. In Docker: `docker exec <container> python -m synchub.access reset-all`.

This is a household lock, not a login system: the PIN is stored hashed, the sign-in is a signed cookie over plain HTTP on your own network, and the app is not meant to face the internet.

## Live updates: webhooks and download progress

Without these, Radarr and Sonarr are read every 6 hours. With them, a grab or import shows up within seconds and a download's percentage moves while you watch.

- **Webhook** (Settings, Automation, Live updates): enter SyncHub's address as Radarr/Sonarr reach it (not `localhost`), then press **Set up in Radarr** / **Set up in Sonarr**. SyncHub creates a Webhook connection in that service from the service's own template, switched on for grab, import, upgrade, rename, add and delete. Pressing it again changes nothing. The panel shows when the last event arrived, so you can tell it works. A grab only triggers a look at the download queue; imports, deletes and adds read the library. Health and test events are ignored. If the service says it could not send a test message, it cannot reach SyncHub at that address from where it runs (a Docker network name or the machine's LAN address usually fixes that).
- **Download progress**: every 30 seconds (Settings, Automation, "Download progress check"; 0 turns it off) SyncHub reads just the download queue, but only while something is downloading. Otherwise it looks at most every 5 minutes, which also catches downloads started directly in Radarr/Sonarr when no webhook is set up. A download that leaves the queue triggers a library read, so "Downloaded" appears without waiting for the schedule. Open dashboards update live without a reload.
- **Jellyfin**: press **Set up in Jellyfin** (same panel; needs Jellyfin's Webhook plugin installed). SyncHub adds a Generic destination to the plugin that sends **every one of its 24 notification types** (item added and deleted, playback start, progress and stop, sessions, sign-ins, tasks, plugin and user changes and more), for every kind of item and every user, with all properties as JSON. It leaves the plugin's other destinations alone; pressing it again changes nothing. Only *Item added*, *Item deleted* and *Task completed* make SyncHub read the library again. Everything else is kept (below).

## Watch progress and resuming

SyncHub shows, and uses, **where each person is in what they watch**. Jellyfin already keeps this for every user (every app that plays something writes it there), so SyncHub copies it rather than keeping a second record: **Tools & Operations, Watch history** reads, for every active profile that has a Jellyfin user (set on People), what that user has watched and how far they got in anything unfinished. It runs with the Jellyfin sync (every 6 hours by default) and in *Run all*; and when Jellyfin's webhook (Settings, Live updates) says someone stopped playing a movie or an episode (or, from a webhook that only sends *Playback Progress*, every 90 seconds while they watch), that one item is read again within seconds. Tick **Playback Stop** and **User Data Saved** in the plugin for the fastest and most accurate picture. **Only Jellyfin's played flag counts as watched**: its play count goes up every time a title is opened, even for a second. Jellyfin forgets where you stopped when that was before about 5% of the runtime, so SyncHub also reads the webhook's progress events (Jellyfin events page) to show **Started watching** and to resume from there; an open under 15 seconds is treated as a click. A last event at 90% or more reads as finished. Only movies and episodes that were played or started get a row; an item marked unwatched in Jellyfin drops out.

- **Movie and show pages** have a **Your progress** block for the profile chosen on the Who-is-watching page: *Not watched*, *In progress* (42:10 of 1:58:00, 35%, with a bar) or *Watched* (last watched, and how many times), *Watching again* when a finished title is part-way through a second time. A show says how many of the episodes in the library they have watched, **what to continue with** (the episode they are part-way through, at the minute they stopped, else the next one after the one they watched last) and what they watched last. Each episode in the season list shows its own status. A profile with no Jellyfin user is told how to set one (guests see nothing). Nobody sees anyone else's progress.
- **Watch now** reads **Resume at 42:10** (a show: **Resume S2E4 at 12:30**) when they are part-way through, and a TV picks up from there. The menu then has **Start from the beginning**. A show's button reads **Continue S2E4** (or **Start S1E1**) and plays that episode from where they stopped in it. A **Start over** button sits beside **Resume**, and the dashboard has a **Continue Watching** row. Details, and what each kind of TV can do (a Roku's official Jellyfin app cannot be moved to a position, for example): [docs/TV_REMOTE.md](docs/TV_REMOTE.md), "Where a title starts".
- **This device** (the Jellyfin app or web page) is Jellyfin's own business: it resumes there as it always did.
- SyncHub only knows what Jellyfin knows. If the TV's Jellyfin app is signed in as a different Jellyfin user than the profile, the progress goes to that user.

## Jellyfin events (what Jellyfin told us, kept)

Every event Jellyfin's Webhook plugin sends is stored (**System, Monitor, Jellyfin events**) with its type, who, what (title, series, season and episode), device, client, playback position and the whole body as it arrived, so it can be used later. The page shows counts by type, filters by type, user and text, opens any event in full, and exports everything as JSON lines (oldest first). Playback progress arrives every few seconds per player and is stored at most every 30 seconds per player and title. Events are kept for 365 days by default (Settings, Automation, Live updates, "Keep Jellyfin events for"; 0 keeps them for ever) and older ones are deleted once a day. The plugin sends plain text unless told otherwise; SyncHub reads the body as JSON either way.

## Opening items in the Jellyfin app (iPhone and iPad)

**Watch now** is the button wherever a title in your library can be watched (the dashboard list and cards, movie and show pages, and each episode). It opens a small menu: **This device**, and every TV added under System, TVs. **This device** does what the old "Watch on Jellyfin" link did, described below. Choosing a **TV** takes you straight to that TV's remote, which wakes it if needed, opens Jellyfin and starts the title, showing each step (it is the same as Watch a movie on the Remote page). A dot beside each TV shows whether it is on, asleep or not answering. Without the TV remote permission the button is just the This device link. **For a show**, the TV plays the episode to watch next: Jellyfin's "Next up" for the profile chosen on the Who-is-watching page (else Jellyfin's default user), or the first episode when everything has been seen.

"This device" opens the Jellyfin web page everywhere. On an iPhone or iPad it opens the item in the Jellyfin (Swiftfin) app instead, using `swiftfin://<server id>/<user id>/item/<item id>`, and falls back to the web page after about two seconds if the app does not take over.

- The **server ID** is read from Jellyfin during a Jellyfin sync, so run **Tools, Sync movies/TV** once after upgrading.
- The **user** must be a Jellyfin user that is signed in to the app on that device. Give each profile its Jellyfin user in Settings; the profile chosen on the Who-is-watching page then decides. A guest, or a profile without a Jellyfin user, is asked once which Jellyfin user to use (remembered on that device; the link at the bottom of every page changes it).
- If tapping does nothing useful, check that the user chosen is the one signed in to the app, and that the app is installed.
- On a show's page every episode that is in the library has its own **Watch now** button, whose This device choice opens that episode in Jellyfin the same way (that page is one tap from Play; no documented Jellyfin link starts playback by itself, so this is as direct as it gets). Seasons are all closed when the page opens; open the ones you want.

## Troubleshooting

**Dynamic DNS takes 30 to 45 seconds to show the new address:** most of that is usually not SyncHub. **Dynamic DNS, Run the check** times the path to the services, then asks Dynu's own name servers and the big public resolvers what they say about the name, with the time each still has the old answer in its cache, and says in words where the wait sits. Every update reports how long each step took (public address, Dynu), and after a real address change SyncHub watches how long each server took to show it (shown on the same page). Background and findings: [docs/DDNS_DELAY.md](docs/DDNS_DELAY.md).

**Posters missing:** **Tools, Refresh posters** forgets earlier failures, asks TMDB for the poster of every title that has none, and saves every poster that is not cached yet. The log names the first titles that have no poster anywhere.

**Radarr/Sonarr "root folder does not exist" or a wrong quality profile:** **Settings, Test connection** (Radarr/Sonarr) now checks the chosen profile and root folder against the service itself and lists the real ones; the dropdowns load when the page opens and flag a saved value the service does not have. Nothing is added until both are valid, and automatic adding stops with one message (`BLOCKED, N waiting: ...` in the task status) instead of one failure per title.


A task looks stuck, or fails with "database is locked":

1. **Tools & Operations** shows what each running task is doing right now (for example `Checked 40 of 150`, `Waiting 14s before the next user`, `TMDB: Show Name (3 of 40)`). The Letterboxd resolver and scrapers pause a few seconds between pages on purpose, so a long job is normal; a message that never changes for minutes is not.
2. **What it's doing now** (System, Monitor; it needs the Logs permission, and Tools & Operations links to it) shows every running task with its age, every open database connection (and whether it has unsaved writes), and where each background thread is right now. A thread whose last line is `socket.py ... readinto` is waiting on a website; one waiting in `sqlite3` is waiting on the database.
3. **Logs** has one line per task start and finish (with the duration) and the full traceback for any failure. For every outgoing HTTP call set `SYNCHUB_LOG_LEVEL=DEBUG` before starting the app; calls slower than 5 seconds are always logged as warnings.
4. A "database is locked" message now ends with `(held by: ...)`, naming the code and thread that is holding the database. If it says no connection in this app holds it, check that a second copy of the app is not running against the same `data/synchub.db`.
5. Keep the project on the Linux filesystem (for example `~/projects`), not on `/mnt/c`: SQLite cannot use WAL mode there and the app logs a warning at startup if it cannot.
6. A task stuck as `IN_PROGRESS` cannot be re-run until the app restarts (restarting also marks it failed).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests use a temporary database and never touch the network (a guard fails any test that tries).

## Layout

The code is in `synchub/`: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `library/` (the Library's decision logic, scans and duplicate search), `storage.py` and `jobs.py` (shares and worker jobs), `access.py` (PINs and permissions) and `ratings.py` (age ratings and the rating limit), `backup.py` (consistent copies, checks and restore of the database), `jellyfin_events.py` (kept Jellyfin events), `watch.py` and `ingest/jellyfin_userdata.py` (each user's watch progress), `notifications/` (delivery methods such as Pushover, the outbox and its sender, see docs/DESIGN_NOTIFICATIONS.md), `ddns.py` and `ddns_check.py` (dynamic DNS and its delay check), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `tv/` (the TV remote: device drivers for Roku and Google / Android TV, discovery, wake and play logic), `web/` (pages, templates and API; `web/tv.py` is the TV menu). `config.py`, `logger.py` and `settings_validation.py` at the top level are shared helpers; `static/` holds the CSS, JavaScript and cached posters.

Also in the repository: `Dockerfile`, `docker-compose.yml` and `docker/entrypoint.sh` (the app image), `agent/` (the worker), `unraid/synchub.xml` (the Unraid template), `.github/workflows/` (tests and publishing the image), and `docs/`.
