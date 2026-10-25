# Readiness checklist

A basic list of things to try before this branch goes to `main` as V1.1. It assumes you run the branch in WSL against a **copy** of your live database, with the live SyncHub still running in Docker on another machine.

Items marked ★ could not be checked without your hardware or devices, so they matter most. If something fails, copy the matching line from the Log on **System, TV tools** and send it along.

## 0. Setup

- [ ] Make a consistent copy on Unraid: `docker exec synchub python -m synchub.backup create /data/backups/test-copy.db`, then copy the file to your PC.
- [ ] In WSL, point at a data folder on the Linux side (not `/mnt/c`): `export SYNCHUB_DATA_DIR=~/synchub-test`.
- [ ] `python -m synchub.backup check ~/test-copy.db` says the copy is sound (and lists any addresses that will not work from here).
- [ ] `python -m synchub.backup restore ~/test-copy.db`.
- [ ] Start with the scheduler off: `SYNCHUB_SCHEDULER=0 python -m synchub.web`.
- [ ] The terminal shows the "background jobs are switched off" warning, and **Tools & Operations** shows a yellow "Scheduled jobs are switched off" notice.
- [ ] The dashboard shows the same titles and counts as production.
- [ ] Optional: make a separate Jellyfin API key for this test and paste it under **Settings, Connections** (on the test copy only), so it can be revoked on its own.

## 1. Menu and permissions

- [ ] The top bar reads Dashboard, Remote, Search, Media, System, Settings.
- [ ] **Media** has Library, Resolution Center, Workers, Storage.
- [ ] **System** has four groups: Run (Tools & Operations), Monitor (What it's doing now, Logs, Jellyfin events, Payloads), Admin (Dynamic DNS, People), TV (TV apps, TVs, TV tools).
- [ ] ★ On an iPhone and an iPad the hamburger menu and the dropdown groups open and tap cleanly.
- [ ] The old addresses `/access` and `/diagnostics` land on People and What it's doing now.
- [ ] On **People**, give a profile that has a saved page list the new **TV remote** and **TV setup** permissions. A profile that has never used the page list sees everything.
- [ ] A restricted profile sees only the menu entries it may open, and a page it may not open explains why instead of showing an error.

## 2. Pages that moved or were reorganised

- [ ] **Settings** has three tabs: Connections, Automation, Logging. Change one value in each, save, and confirm you stay on that tab and the value stuck.
- [ ] Connections: **Test connection** works for Jellyfin, TMDB, Radarr and Sonarr, and the push notification test works.
- [ ] Automation: **Load options** fills the Radarr and Sonarr dropdowns and your saved quality profile and root folder show as selected.
- [ ] Logging: switch payload logging on and the **Payloads** page then says it is on, for the services you chose.
- [ ] **People** lists your profiles and, below them, PINs and pages. Add, edit and delete a test profile; set and remove a PIN.
- [ ] **Workers**: the Workers and Jobs tabs switch and keep their place through the 5-second refresh. A job that needs a decision shows "waiting for you" on the Jobs tab.
- [ ] **What it's doing now** loads, refreshes, and needs the Logs permission.
- [ ] Each page's heading matches its menu name.

## 3. TV remote (real TVs)

- [ ] **System, TVs, Scan network** with your real address range finds the Roku and the Google TV. Add both from the results.
- [ ] Press **Start pairing** on the Google TV and enter the code. Its status shows "on".
- [ ] **Remote**: D-pad, volume (tap, and hold), power and sleep, and the quick-launch apps all work on both TVs.
- [ ] **TV apps**: tick a few apps and see them on the Remote page below the quick-launch row.
- [ ] ★ **Google TV from the screensaver**: open an app from the remote. It should press Home, wait about 2 seconds, then launch.
- [ ] ★ **Roku already playing Jellyfin**: play a different title. It should go Home first and then open the new one.
- [ ] ★ **Roku from standby**: Watch a movie wakes it and plays.
- [ ] ★ **Google TV from standby**: run **Probe network** on TV tools with the TV on and again in standby, and note which wake method works.

## 4. Watch now

- [ ] The dashboard rows and cards, movie pages, show pages and each episode show **Watch now**.
- [ ] The menu offers **This device** and each TV, and is not clipped inside the dashboard table.
- [ ] **This device** on a desktop opens Jellyfin web in a new tab.
- [ ] ★ **This device** on an iPhone or iPad opens the Swiftfin app on that title.
- [ ] Choosing a TV shows a dot next to it, then a "Starting..." message, then "Now playing", at the bottom of the screen.
- [ ] A profile without the TV remote permission sees just a plain Watch now link.
- [ ] ★ **A show** on the Google TV: note what it starts (a Roku starts the series from the beginning).

## 5. Safety while testing a copy

- [ ] Nothing changed in production: Radarr and Sonarr have no new entries and the Dynu hostname is untouched.
- [ ] You did not press **Set up in Radarr/Sonarr/Jellyfin** or **Run update** on Dynamic DNS in the test copy.
- [ ] When finished, delete `~/synchub-test` (it holds API keys and PINs).

## 6. Before the Docker phase

- [ ] ★ For testing from a phone, and for Wake-on-LAN and automatic discovery: put `networkingMode=mirrored` in `.wslconfig` (Windows 11), run `wsl --shutdown`, and confirm each works.
- [ ] ★ The Docker image builds for this branch, and the TV pages work from inside it (it needs the same network as the TVs).
- [ ] Decide how the `tv` data folder (TVs added, app choices, Google TV pairing) is backed up: the database backup does not include it.
- [ ] Run the automated tests: `python -m pytest` (about 3.5 minutes; all should pass).
