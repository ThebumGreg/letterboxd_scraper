# Whole-house remote

One web UI to pick a TV, drive it like a remote, launch its apps, see what is playing, and start a movie
on any TV from whatever state it is in. Every outbound call (ADB, Android TV Remote, Roku ECP, Wake-on-LAN,
Jellyfin) is shown in the Log panel with request, response, status and timing, and written to
`remote_control.log` as JSON lines.

```bash
pip install -r remote_control/requirements.txt
export JELLYFIN_URL=http://192.168.128.55:5000
export JELLYFIN_API_KEY=...
cp devices.example.json devices.json     # edit hosts / names
python remote_control_app.py             # http://127.0.0.1:5055
```

## The two pages

**Remote** (`/`) is the everyday page, laid out for a phone: the TV's name and state, a **Now playing** block built
into the remote (what is on, with Pause/Resume, ±30 s and Stop when Jellyfin is playing it), Power on / Sleep, a
**quick-launch row** of up to four apps, Back / Home / Options, the D-pad, rewind / play-pause / fast-forward, volume,
and the **Watch a movie…** button. On a computer the arrow keys, Enter, Esc, Space, `m` and `+`/`-` drive it too.

**Settings & testing** (`/settings`, the ⚙ button) holds everything else: every key, text entry, screenshot, wake and
sleep tests (Wake-on-LAN, screensaver, Probe network, Diagnose, Detect MAC), pairing, the full app list, Jellyfin
sessions, library search and commands, and the log.

**Choosing quick-launch apps:** on Settings, tap ☆ on an app (or ‹ › ✕ on the quick-launch strip to reorder or
remove). Each TV has its own list, shared by every browser that opens the app, and saved in
`.remote_control/prefs.json` (`--prefs` changes the location). Until you choose, the first four of Jellyfin,
Netflix, YouTube, Disney+, Prime Video, Hulu, Plex and Max that the TV has are used.

## Using it from a phone

The "Watch a movie…" button asks where to watch: this phone, or any configured TV. To open the page on a phone
run it where the phone can reach it and set a token, because the page can power devices on and launch apps:

```bash
python remote_control_app.py --host 0.0.0.0 --token SOMETHING-LONG --public-url http://192.168.128.55:5000
```

then open `http://<this-computer>:5055/?token=SOMETHING-LONG` once on the phone (it remembers the token).
"This phone" opens the movie's page in Jellyfin web (`--public-url`, or `--url` if omitted); tap play there.

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

Some TVs reject the Remote protocol's app links (a TCL logged `Received an error from the device:
remote_app_link_launch_request` for every launch). So for `googletv` devices with `"adb": true`, launching goes
over ADB first, which worked on that TV. Without ADB the app tries an Intent link, then the `market://` link, and
checks that the app really came to the front; a TV-side rejection is logged and reported instead of being
treated as success. Set `"adb": true` if app launching fails.

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

**Wake-on-LAN notes:** press **Detect MAC** while the device is on and put the address in `devices.json` as
`"mac"` (Roku reports its own). Add `"broadcast": "192.168.1.255"` (your subnet's broadcast) if the default
broadcast does not reach it. The packet only arrives if this computer is on the same network segment; under
WSL2's default NAT networking it will not (use `networkingMode=mirrored` in `.wslconfig`, or run the app on
the host).

## Config reference

`jellyfin.app`: app to open for playback (package name on Android TV; name or channel id on Roku).
`jellyfin.session`: how that app's session looks in Jellyfin's `/Sessions` (`client` and `device` substrings,
and/or exact `device_id`); the Session menu in the UI shows the real values.
`sleep`: `standby` (default) or `screensaver`. `wake`, `wake_timeout`: see above. `broadcast`: subnet broadcast
address for Wake-on-LAN. `apps`: (googletv without ADB) list of package names to show in the app grid.

App icons: the Roku supplies its own. For everything else the app draws the service's logo (bundled, see
`static/icons/logos/README.md`) on its brand colour, or an initials badge. Vendor/system packages such as `com.tcl.*`
are hidden behind "show system apps". To override any icon, drop `<package>.png` into `remote_control/static/icons/`.

## Adding another kind of device

Subclass `Driver` in `remote_control/devices/`, set `type`, `keymap` (logical key -> native key),
`capabilities`, implement `status/send_key/power/apps/launch/jellyfin_app` (plus `icon`, `detect_mac`, pairing,
`status()["media"]` if the device can), decorate with `@register`, and import it at the bottom of
`devices/__init__.py`. The remote layout, apps, prepare-and-play flow, Watch chooser and log need no UI changes.
Apple TV (pyatv) and Fire TV (ADB, like Google TV) would fit this shape.
