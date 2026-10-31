# Readiness checklist

> **Historical.** This is the checklist for the V1.1 branch (a copy of the database tested in WSL). It is kept for the ideas; for a current release use [RELEASING.md](../RELEASING.md) and the notes in [CHANGELOG.md](../../CHANGELOG.md).

A basic list of things to try before this branch goes to `main` as V1.1. It assumes you run the branch in WSL against a **copy** of your live database, with the live SyncHub still running in Docker on another machine.

Items marked ★ could not be checked without your hardware or devices, so they matter most. If something fails, copy the matching line from the Log on **System, Remote Tools** and send it along.

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

- [ ] The top bar reads Dashboard, Discover, Remote, Media, System, Settings, with your name (My profile, Switch profile) at the right.
- [ ] **Media** has Library and Resolution Center (Workers and Storage are tabs on the Library page). **Discover** has New & trending, Find & request and My watchlist.
- [ ] **System** has four groups: Run (Tools & Operations), Monitor (System status, What it's doing now, Logs, Jellyfin events, Payloads), Admin (Dynamic DNS, People), TV (Remote Apps, TVs, Remote Tools).
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

## 2b. Ratings, parental controls and guests

- [ ] A movie page and a show page show **Rated** (PG-13, TV-MA ...) and the **Audience score**. A title TMDB has no US rating for says "Not rated". Run **Match & update** first if they are blank.
- [ ] **People**: each profile and the Guests card has a "most mature rating" box. Set a test profile to PG and choose it: the dashboard, its counts, search and the Remote's search show nothing above PG, and typing the address of an R-rated movie goes back to the dashboard.
- [ ] Unrated titles are hidden from that profile; a profile with no limit sees everything.
- [ ] As a guest (choose "Just looking" once a profile exists): the menu is only Dashboard and Remote, other addresses (try `/search` and `/settings`) say you have no access, and nothing above PG shows. Change the guest limit and the dashboard follows.
- [ ] ★ Play an R-rated movie from the Remote as the PG profile: it is refused ("not available for your profile"). The "Playing now" strip does not name it.
- [ ] With no PIN on any profile nothing is restricted (the People page says so). Give your own profile a PIN: from then on guests and every profile without a PIN get only the Guests pages and rating limit, and the People page marks them "guest limits apply". Give a profile a PIN and its own pages and limit apply once it is entered.

- [ ] ★ **Front Room (Google TV) playback**: play a movie, then play a different movie or an episode while the Jellyfin app is still on the player screen. The progress box should show "is still open in the app: stopping it first" and then Now playing. With the TV on another app (the Jellyfin session is a leftover) it should open the app and play, not stop with "cannot be controlled remotely".
- [ ] ★ After playing, the box shows a **Close** button (and tapping outside closes it). When playback fails it stays open with **Try again** and the steps to read.

## 3. TV remote (real TVs)

- [ ] **System, TVs, Scan network** with your real address range finds the Roku and the Google TV. Add both from the results.
- [ ] Press **Start pairing** on the Google TV and enter the code. Its status shows "on".
- [ ] **Remote**: D-pad, volume (tap, and hold), power and sleep, and the quick-launch apps all work on both TVs.
- [ ] **Remote Apps**: tick a few apps and see them on the Remote page below the quick-launch row.
- [ ] ★ **Google TV from the screensaver**: open an app from the remote. It should press Home, wait about 2 seconds, then launch.
- [ ] ★ **Roku already playing Jellyfin**: play a different title. It should go Home first and then open the new one.
- [ ] ★ **Roku from standby**: Watch a movie wakes it and plays.
- [ ] ★ **Google TV from standby**: run **Probe network** on Remote Tools with the TV on and again in standby, and note which wake method works.

## 4. Watch now

- [ ] The dashboard rows and cards, movie pages, show pages and each episode show **Watch now**.
- [ ] The menu offers **This device** and each TV, and is not clipped inside the dashboard table.
- [ ] **This device** on a desktop opens Jellyfin web in a new tab.
- [ ] ★ **This device** on an iPhone or iPad opens the Swiftfin app on that title.
- [ ] Choosing a TV shows a dot next to it, then takes you to that TV's Remote page, where the "Starting playback" box shows each step and ends in "Now playing". The address no longer has `?play=` in it afterwards (a refresh does not start it again).
- [ ] A profile without the TV remote permission sees just a plain Watch now link.
- [ ] **A show**: the TV plays the episode to watch next (the progress box says which, and for whom). Check it for a show you have part-watched and one you have not started. ★ Check it on the Google TV too.

## 4b. Date Added, Playing now, timer, backups

- [ ] After one Jellyfin sync (press **Run all** or the Jellyfin buttons on Tools & Operations), the dashboard shows "Added <date>" under each title in your library, and **Date Added** in the sort menu puts the newest first. A show's date is its newest episode's; titles not in the library sort last.
- [ ] While a TV plays something through Jellyfin, a **Playing now** strip at the top of the dashboard names the TV and title, and **Open remote** goes to that TV.
- [ ] ★ On the Remote page, the timer and bar run smoothly (no jumps every few seconds) on both the Roku and the Google TV. Pause and resume show at once; the ±30 s buttons move the timer at once.
- [ ] After a backup (`python -m synchub.backup create`, or the nightly one), a `.tv.zip` sits beside the `.db`. `python -m synchub.backup check` on the `.db` mentions "also holds: the TV settings".

## 5. Safety while testing a copy

- [ ] Nothing changed in production: Radarr and Sonarr have no new entries and the Dynu hostname is untouched.
- [ ] You did not press **Set up in Radarr/Sonarr/Jellyfin** or **Run update** on Dynamic DNS in the test copy.
- [ ] When finished, delete `~/synchub-test` (it holds API keys and PINs).

## 6. Docker and Unraid (V1.1)

- [ ] After the code reaches `main`, GitHub's *Actions* tab shows **Publish image** green, and the package `ghcr.io/thebumgreg/synchub` exists (make it public or log Unraid in: DEPLOY.md, section 1).
- [ ] ★ Install the Unraid template and start SyncHub from the Docker tab. It shows healthy, the footer says `SyncHub v1.1.0`, and the Settings banner about `localhost` addresses is dealt with.
- [ ] ★ Move a database in (DEPLOY.md, section 2). The log says a copy was saved before upgrading, and the dashboard matches production.
- [ ] ★ The TV pages from inside the container: add a TV by address, pair the Google TV, open an app, play a title. With the container on Custom br0, *Scan network* finds the TVs and Wake-on-LAN works; on Bridge only those two do not.
- [ ] ★ Restart the container: the TVs and the Google TV pairing are still there, and the Google TV does not ask to "Allow USB debugging" again (the adb key is kept in `/data/tv`).
- [ ] ★ Push a small change to `main` (or tag `v1.1.1`). **Check for Updates** shows SyncHub as *update ready*, **apply update** replaces it, and everything is still there.
- [ ] A backup from the container (`docker exec synchub python -m synchub.backup create /data/backups/manual.db`) restores on a fresh data folder (DEPLOY.md, section 6).
- [ ] Run the automated tests: `python -m pytest` (about 4 minutes; all should pass).
