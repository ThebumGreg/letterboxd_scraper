# TV: the SyncHub remote

**Remote** in SyncHub's top bar, with its pages under **System** (a TV section), drives your TVs like a remote, launches their apps, shows what is playing and starts a movie
on any TV from whatever state it is in. Every outbound call (ADB, Android TV Remote, Roku ECP, Wake-on-LAN, Jellyfin) is
shown in the Log on **System ▸ TV tools** with request, response, status and timing, and written to `tv/tv.log` in SyncHub's
data folder as JSON lines.

There is nothing to start separately: the TV pages are part of SyncHub (`python -m synchub.web`) and use the Jellyfin
address and API key from **Settings ▸ Connections**. Open **System ▸ TVs** and press *Scan network* to add your TVs.

Everything the TV pages save is in one folder (backed up with the database: each backup has a `.tv.zip` beside it), `<data folder>/tv/` (`data/tv/` from a checkout, `/data/tv/` in Docker):
`prefs.json` (TVs added in the app, app choices, session choices), `certs/` (the pairing identity of Google TVs), `tv.log`,
and an optional hand-written `devices.json` for the options the pages do not edit (see *Config reference*). Set
`SYNCHUB_TV_DEVICES` to read that file from somewhere else and `ADB_PATH` for an `adb` binary that is not on the path.

**Access.** Two permissions (People page): **TV remote** opens Remote and TV apps, **TV setup** opens TVs and TV tools
(adding and removing TVs, probes, pairing, raw Jellyfin commands). Profiles may use both until you change it; guests get only TV remote. A profile's or guest's rating limit (People page) also applies here: the Jellyfin search, episode lists, play calls and the "Playing now" strip leave out, and refuse, titles above it.

**Networking.** The TV pages talk straight to your TVs, so SyncHub must be on the same network as them. Wake-on-LAN
broadcasts and mDNS discovery do not cross Docker's bridge network or WSL2's NAT (the range scan and unicast calls do).

## The four pages (Remote in the top bar; the other three in the TV section of the System menu)

**Remote** (`/tv/remote`) is the everyday page, laid out for a phone: the TV's name and state, a **Now playing** block built
into the remote (what is on, with Pause/Resume, ±30 s and Stop when Jellyfin is playing it), Power on / Sleep, a
**quick-launch row** of up to four apps, Back / Home / Options, the D-pad, rewind / play-pause / fast-forward, volume,
and the **Watch a movie…** button. The now-playing position and bar run smoothly in the browser between the server's updates (see `static/tv/clock.js`): pause, resume and the ±30 s buttons show at once and the next update corrects any difference. A link such as `/tv/remote?tv=<tv id>&play=<Jellyfin item id>&title=<name>`, which the **Watch now** button on the dashboard and title pages uses, opens that TV's remote and starts the title with the usual progress box. On a computer the arrow keys, Enter, Esc, Space, `m` and `+`/`-` drive it too.

**TV apps** (`/tv/settings`) holds the everyday choices. *Apps on your remote*: pick, per TV, which apps appear
below the quick-launch row (tap an app to show or hide it; Show all / Hide all). Until you choose, only the quick-launch
row is shown. *Quick launch*: tap ☆ on an app (or ‹ › ✕ on the strip to reorder or remove), up to four. Each TV has its
own lists, shared by every browser that opens the app and saved in `tv/prefs.json`. Until you pin your own, the first four of Jellyfin, Netflix, YouTube, Disney+, Prime Video, Hulu, Plex and
Max that the TV has are used. A Google TV controlled without ADB cannot list its installed apps, so only common apps are
offered; enable ADB for it on the TVs page to see everything.

**TVs** (`/tv/devices`) manages the devices. *Find TVs on your network* looks for Rokus (SSDP) and Google / Android TVs (mDNS,
which needs the `zeroconf` package), and also tries the control ports of every address in a range (default: the /24
around the TVs and Jellyfin server you already have). The range scan is what works under **WSL2, Docker and VMs**, where
multicast does not reach your network; only private ranges up to a /22 are accepted. Add a found TV with one tap (or
add one by hand), then press *Start pairing* for a Google TV. Edit or remove TVs saved in the app; TVs from
`devices.json` are listed but edited in that file. TVs saved in the app live in `prefs.json`.

**TV tools** (`/tv/advanced`) is the testing page: every key, text entry, screenshot, wake and sleep tests
(Wake-on-LAN, screensaver, Probe network, Diagnose, Detect MAC), pairing, the full app list, Jellyfin sessions, library
search and commands, and the log. Use it to try a new TV model or configuration without cluttering the pages above.

**What's new.** New features announce themselves with a banner at the top of the Remote and TV apps pages (one at a
time, shown once per browser until dismissed); TV apps keeps the full list. Add an entry to `synchub/tv/notices.py`
when a feature ships.

**Volume:** tap Vol +/− for one step, tap repeatedly for one step per tap, or hold for a steady repeat that speeds up
after about 1.5 s and 3 s (the `+`/`-` keys do the same on a computer). Requests never overlap: steps that pile up
while one is in flight are sent as a single "×N" press, so a slow device (ADB takes about 1.5 s a call) still lands
on the right volume. The Log shows these as `key KEYCODE_VOLUME_UP x12`.

**iPhone and iPad:** the layout adapts to the screen. On a phone it is one column with the quick-launch row up top; on
an iPad (portrait or landscape) the now-playing block, power, quick launch and Watch button sit on the left and the
full pad on the right, with larger buttons. Controls are at least 44 pt, inputs are 16 px so iOS does not zoom when
you tap a field, double-tap zoom is off on buttons, and the notch / home-indicator areas are respected. In Safari use
Share ▸ **Add to Home Screen** to get a full-screen app with its own icon and no browser bar.

## Using it from a phone

The TV pages are laid out for a phone and an iPad: open SyncHub's address on the device (it is behind the same profile and
PIN rules as the rest of SyncHub). The "Watch a movie…" button asks where to watch: this phone, or any configured TV.
"This phone" opens the movie's page in Jellyfin web (the Jellyfin address from Settings); tap play there.

## Starting a movie from any state

"Watch a movie…" and the Library "Prepare device first" option run the same sequence and log each step:

| TV is | What happens |
|---|---|
| unreachable or asleep (not answering) | every configured wake method at once (see "Waking and sleeping"), then wait |
| reachable but screen off / screensaver | the device's own power key |
| on, another app | launch the Jellyfin app, wait for its session to appear |
| already in Jellyfin | play right away |

It then sends PlayNow (optionally resuming where you left off) and waits for Jellyfin to report that item
playing. If the command is accepted but nothing starts, the result says so instead of claiming success.
It cannot tell which HDMI input the TV is showing: if the streamer is on but the TV is on a game console
input, switch inputs (Roku: "More keys"; Google TV: HDMI keys, or CEC usually does this when an app starts).

## Devices

**Google TV, Remote protocol** (`"type": "googletv"`, recommended): no developer mode. `pip install androidtvremote2`,
open the device in the UI, press **Start pairing**, and type the code shown on the TV. This is the protocol the Google
TV phone app uses and normally works while the TV is in standby. It reports power, current app and volume, but cannot
list installed apps (the app grid uses `"apps": [...]` from the config, or a built-in list of common ones) or read
what is playing. Set `"adb": true` to also use ADB for the installed-app list, screenshots and now-playing title.

**Google TV, ADB** (`"type": "androidtv"`): install `adb` (`sudo apt install adb`), enable Developer options >
Network debugging on the TV, accept the "Allow USB debugging" prompt. Reports what is playing from Android's
media session (best effort; depends on the Android version and whether the app publishes metadata).

**Roku / Roku TV** (`"type": "roku"`): Settings > System > Advanced system settings > Control by mobile apps >
Network access = Default or Permissive. Real app icons come from the Roku. Playback state comes from the Roku
media player (many apps report state but not the title; Jellyfin's own session supplies it when you use Jellyfin).

### Opening apps on a Google TV

**Opening an app from the screensaver.** A launch sent to a TV that is showing its screensaver is often dropped, so the
app first presses Home, waits two seconds for the launcher to draw, and then opens the app. This applies to both Google TV
driver types and to every route that opens an app (quick launch, Watch a movie, Prepare device).

Some TVs reject the Remote protocol's app links (a TCL logged `Received an error from the device:
remote_app_link_launch_request` for every launch). So for `googletv` devices with `"adb": true`, launching goes
over ADB first, which worked on that TV. Without ADB the app tries an Intent link, then the `market://` link, and
checks that the app really came to the front; a TV-side rejection is logged and reported instead of being
treated as success. Set `"adb": true` if app launching fails.

### Roku: how a movie gets started

**The official Jellyfin Roku app cannot be controlled through Jellyfin sessions, by design.** Its source registers
`SupportedCommands: []` and `SupportsMediaControl: false` and has no handler for server commands, so TV tools shows its
session as `remote control: no` and play commands never reach it. What it does support is a **deep link**, which it acts on
at once. So a Roku plays like this (`"play": "auto"`, the default):

1. **Deep link.** The app is launched with `contentId` and `mediaType`. If the Jellyfin app is already on screen (for example
   still playing something else), the Roku is sent **Home** first, then the app is launched with the new link, because
   handing a new item to an app that is busy playing fails. It starts playing inside the real Jellyfin app, so Jellyfin sees the session and records progress and watched
   state. Works for movies, episodes, series, seasons, collections and music; a series link starts the series. The app
   always starts from the beginning (it ignores a resume point), and there is no queue (Play now only). Success is confirmed
   from Jellyfin's session list.
2. **Stream URL**, only if the deep link *request* fails (for example the Roku answers with an error). The Roku streams
   from Jellyfin through the "Play on Roku" system channel, the ECP call the Roku phone app casts through. Direct play for
   files the Roku understands (MP4/MKV with H.264 or HEVC video and AAC, AC3, E-AC3 or MP3 audio), otherwise an HLS
   transcode. Jellyfin is not told about this playback (no resume or watched mark), and the URL contains your API key (masked
   in every log, but visible to the Roku). `stream_base` is the address the *Roku* should use to reach Jellyfin if it differs
   from the Jellyfin address in Settings; `stream_mode` is `auto`, `direct` or `hls`.

A link that was sent but not confirmed (app still loading, or not signed in) is reported as such and is **not** followed by
a stream, so two playbacks never compete. TV tools ▸ Jellyfin has a **Play via** menu (device setting, Jellyfin session,
deep link, stream URL) to try each route, and `"play": "deeplink"`, `"url"` or `"session"` in `devices.json` pins one.

**About DLNA.** The "Roku Media Player" channel is a DLNA *client*: it browses a DLNA server (Jellyfin's DLNA plugin can be
one) and you pick titles with the Roku remote, so nothing can tell it what to play. Pushing to a Roku as a DLNA renderer
depends on the model and firmware, and finding renderers uses multicast, which does not cross WSL2's NAT. The deep link is
more direct and reports to Jellyfin.

### Waking and sleeping a TV

A TV in standby is the hard part. Many TVs (TCL included, going by the logs from a real one) switch their network
off in standby, so ADB hangs, Wake-on-LAN is ignored, and the only thing that wakes them is their own remote.
"Wake / power on" therefore does not rely on one trick. If the TV is not clearly on it fires every method listed
in the device's `wake` setting at once, then waits (`wake_timeout`, default 45 s) for it to report on:

| method | how | needs |
|---|---|---|
| `remote` | connects over the Android TV Remote protocol and presses power if the TV says it is off | `googletv` type, paired |
| `adb` | queues `KEYCODE_WAKEUP`; delivered whenever the TV's network wakes, so it is not waited on | ADB enabled |
| `wol` | Wake-on-LAN magic packets (broadcast, your subnet's `broadcast`, and unicast) | `mac`, TV setting enabled |
| `cast` | launches a Cast receiver, which wakes Chromecast-built-in TVs that support wake-on-cast | `pip install pychromecast` |
| `key` | the device's own power-on key (Roku; also used whenever a device is reachable but screen-off) | reachable |

Defaults: `googletv` = remote, wol, cast (+adb if enabled); `androidtv` = adb, wol, cast; `roku` = key, wol.
Every attempt and its result is in the Log, so you can see which one worked.

**Find out what your TV does in standby.** Press **Probe network** while the TV is on, put it to sleep, wait
a minute, and press it again. Any port that still answers in standby is a way in: 6466 means the Remote protocol
can wake it (use `googletv`), 8009 means Cast can, and if nothing answers, only the TV's own remote, an IR
blaster, or avoiding standby will work. **Diagnose** (ADB) prints the TV's power, screensaver and network
settings, including which interface (Wi-Fi or Ethernet) and MAC the TV uses. Wake-on-LAN needs the MAC of the
interface the TV is actually connected with.

**Avoid standby: `"sleep": "screensaver"`.** The Sleep button then starts the screensaver instead of putting the
TV to standby. The TV stays reachable, any key wakes it instantly, and everything keeps working. The panel stays
lit, and you need a screensaver enabled on the TV (Settings > System > Ambient mode / Screen saver); the app
reports it clearly if there is none. The **Screensaver** button does this on demand whatever the setting is.
Needs ADB (`androidtv`, or `googletv` with `"adb": true`). Check the TV's own power-saving timers too, since a
TV can still drop into standby by itself after a while.

**Wake-on-LAN notes:** press **Detect MAC** while the device is on and save it (the TVs page does, for TVs added in the app, or put it in `devices.json` as
`"mac"`) (Roku reports its own). Add `"broadcast": "192.168.1.255"` (your subnet's broadcast) if the default
broadcast does not reach it. The packet only arrives if this computer is on the same network segment; under
WSL2's default NAT networking it will not (use `networkingMode=mirrored` in `.wslconfig`, or run the app on
the host).

## Config reference

`tv/devices.json` is optional: TVs added on the TVs page need no file. Use it for options the page does not edit, or to
keep a TV in a file you manage yourself (those are listed on the TVs page but changed only in the file):

```json
{"devices": [
  {"id": "front-room", "name": "Front Room Google TV", "type": "googletv", "host": "192.168.1.50",
   "mac": "AA:BB:CC:DD:EE:FF", "broadcast": "192.168.1.255", "adb": true, "sleep": "screensaver",
   "wake": ["remote", "adb", "wol", "cast"], "wake_timeout": 45,
   "jellyfin": {"app": "org.jellyfin.androidtv", "session": {"client": "Android TV"}}},
  {"id": "roku", "name": "Roku TV", "type": "roku", "host": "192.168.1.60",
   "jellyfin": {"app": "Jellyfin", "session": {"client": "Jellyfin Roku"}}}
]}
```

**How a TV is matched to its Jellyfin session.** Jellyfin reports each app as a session with a `Client` name
(for example `Jellyfin Roku`, `Jellyfin Android TV`, `Swiftfin tvOS`) and a `DeviceName`. A device's
`jellyfin.session` rule says which one is its own. Version numbers are ignored (`Jellyfin Roku 3.2.4` matches
`Jellyfin Roku`; the version is Roku's app version, not part of the name). If the rule matches nothing the default for
that kind of device is tried, with a warning in the Log. To see the real names, open **TV tools ▸ Jellyfin ▸
Sessions Jellyfin can see**: every session is listed with the TV it maps to, and **Use for this TV** saves the right one
(in `prefs.json`, so `devices.json` stays untouched; **reset** undoes it). If a session shows `remote control: no`,
that app tells Jellyfin it cannot be controlled remotely and play commands cannot reach it.

`jellyfin.app`: app to open for playback (package name on Android TV; name or channel id on Roku).
`jellyfin.session`: how that app's session looks in Jellyfin's `/Sessions` (`client` and `device` substrings,
and/or exact `device_id`); the Session menu in the UI shows the real values.
`sleep`: `standby` (default) or `screensaver`. `wake`, `wake_timeout`: see above. `broadcast`: subnet broadcast
address for Wake-on-LAN. `apps`: (googletv without ADB) list of package names to show in the app grid.

App icons: the Roku supplies its own. For everything else the app draws the service's logo (bundled, see
`static/icons/logos/README.md`) on its brand colour, or an initials badge. Vendor/system packages such as `com.tcl.*`
are hidden behind "show system apps". To override any icon, drop `<package>.png` into `static/tv/icons/`.

## Adding another kind of device

Subclass `Driver` in `synchub/tv/devices/`, set `type`, `keymap` (logical key -> native key),
`capabilities`, implement `status/send_key/power/apps/launch/jellyfin_app` (plus `icon`, `detect_mac`, pairing,
`status()["media"]` if the device can), decorate with `@register`, and import it at the bottom of
`devices/__init__.py`. The remote layout, apps, prepare-and-play flow, Watch chooser and log need no UI changes.
Apple TV (pyatv) and Fire TV (ADB, like Google TV) would fit this shape.


**Playing through a Jellyfin session (Google TV, Android TV).** Before sending a title, SyncHub opens the Jellyfin app if another app is in front (a session left over from an earlier run says it cannot be remote controlled until the app registers a new one, so that only counts as a failure once the app has been in front for 15 seconds). If the session reports something already open in the player, SyncHub sends a Stop first. If the app accepts the command but never reports the title playing, SyncHub presses Home, reopens the app and sends the command once more; the steps of each attempt are in the progress box and the log.
