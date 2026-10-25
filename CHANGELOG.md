# Changelog

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
