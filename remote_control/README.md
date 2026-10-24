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
| unreachable | Wake-on-LAN (needs `mac`), wait until it answers |
| standby / screensaver | power-on key, then Wake-on-LAN as a fallback if the key does nothing |
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

### Wake-on-LAN

Press **Detect MAC** while a device is on, then put the address in `devices.json` as `"mac"` (Roku reports its own).
Add `"broadcast": "192.168.1.255"` (your subnet's broadcast address) if the default broadcast does not reach it.
Not every TV can be woken this way: look for a "Wake on LAN / Wake on Wi-Fi / network standby" setting, and note
that streamers such as the Chromecast with Google TV have none but stay reachable in standby, so the power key
works instead. The magic packet only reaches the TV if this computer is on the same network segment. Under WSL2's
default NAT networking it will not; use `networkingMode=mirrored` in `.wslconfig`, or run the app on the host or a Pi.

## Config reference

`jellyfin.app`: app to open for playback (package name on Android TV; name or channel id on Roku).
`jellyfin.session`: how that app's session looks in Jellyfin's `/Sessions` (`client` and `device` substrings,
and/or exact `device_id`); the Session menu in the UI shows the real values.
App icons: the Roku supplies its own; for Android TV drop `<package>.png` into `remote_control/static/icons/`,
otherwise a coloured badge is used.

## Adding another kind of device

Subclass `Driver` in `remote_control/devices/`, set `type`, `keymap` (logical key -> native key),
`capabilities`, implement `status/send_key/power/apps/launch/jellyfin_app` (plus `icon`, `detect_mac`, pairing,
`status()["media"]` if the device can), decorate with `@register`, and import it at the bottom of
`devices/__init__.py`. The remote layout, apps, prepare-and-play flow, Watch chooser and log need no UI changes.
Apple TV (pyatv) and Fire TV (ADB, like Google TV) would fit this shape.
