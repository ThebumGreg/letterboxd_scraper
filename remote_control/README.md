# Whole-house remote

One web UI to pick a TV, drive it like a remote, launch its apps, and control Jellyfin playback on it.
Every outbound call (ADB, Roku ECP, Jellyfin) is shown in the Log panel with request, response, status
and timing, and written to `remote_control.log` as JSON lines.

```bash
pip install flask requests
export JELLYFIN_URL=http://192.168.128.55:5000
export JELLYFIN_API_KEY=...
cp devices.example.json devices.json     # edit hosts / names
python remote_control_app.py             # http://127.0.0.1:5055
```

Add `"mac"` to a device to let "Prepare device" try Wake-on-LAN when it is unreachable.

## Devices

**Google TV / Android TV** (`"type": "androidtv"`) over ADB: install `adb` (`sudo apt install adb`), enable
Developer options > Network debugging on the TV, and accept the "Allow USB debugging" prompt on first use.
Volume keys only reach the TV's speakers if the device is set up for CEC/IR volume control.

**Roku / Roku TV** (`"type": "roku"`) over ECP: on the Roku, Settings > System > Advanced system settings >
Control by mobile apps > Network access = Default or Permissive. Roku TVs report `Ready` in standby;
`PowerOn` wakes them (Fast TV start must be on to wake from fully off without Wake-on-LAN).

`jellyfin.app` is the app to open for playback (package name on Android TV; name or channel id on Roku) and
`jellyfin.session` says how that app's session looks in Jellyfin's `/Sessions` (`client`, `device`
substrings and/or exact `device_id`). Use the Session menu in the UI to see the real values.

## Adding another kind of device

Subclass `Driver` in `remote_control/devices/`, set `type`, `keymap` (logical key -> native key),
`capabilities`, implement `status/send_key/power/apps/launch/jellyfin_app`, decorate with `@register`, and
import it at the bottom of `devices/__init__.py`. The remote layout, app launcher, prepare-and-play flow and
log all work without UI changes. Apple TV (pyatv) and Fire TV (ADB, like Google TV) would fit this shape.
