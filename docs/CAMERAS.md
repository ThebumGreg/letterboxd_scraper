# UniFi Protect cameras on the TV remote

The remote's **Cameras** button pauses what the TV is playing in Jellyfin, remembers the title and the place, and shows a camera full screen. A box on the remote switches to another camera, or closes the camera and carries on where you were. For now this works on a **Google TV / Android TV**.

> This feature was written without a UniFi console or a Google TV to try it on. The Protect address paths, the way a TV player app takes a stream and go2rtc's `rtspx://` source are taken from their documentation. The first run on your hardware is the real test, so the Test connection button and the TV log (Remote Tools) say exactly which step failed.

## What you need

- **UniFi Protect** with the Integrations page (*Settings, Control Plane, Integrations*), and an API key made there.
- A **worker** that stays on, to run the camera relay (go2rtc). Protect gives out RTSPS streams that most TV players cannot open; the relay serves the same pictures as plain RTSP, and SyncHub only ever gives the TV the relay's address.
- **VLC for Android** (`org.videolan.vlc`) installed on the TV, or another player app that opens `rtsp://` addresses (set it in Settings).
- **ADB turned on for that TV** in SyncHub (`"adb": true`, see `docs/TV_REMOTE.md`). Without it SyncHub can only send the TV the stream address over the Remote protocol, which many TVs do not act on.

## Setting it up (everything from SyncHub)

1. **Settings, Connections, UniFi Protect cameras.** Switch it on, type the console's address, paste the API key and press **Test connection**. It lists the cameras it can see. A console signs its own certificate, so leave *Check the console's certificate* off unless you have installed a trusted one.
2. **Choose the worker** that runs the relay and **save**.
3. **Camera relay card** (same section, below Test connection). It is a checklist and refreshes by itself:
   - the relay worker is online;
   - *its update helper is checking in*. This is the one thing done on the PC, once: `sudo ./agent/auto-update.sh --install` (`setup-worker.sh` does it for a new worker);
   - **go2rtc is installed.** Press **Install / repair on the worker**. SyncHub gives the helper a small `docker-compose.override.yml` (go2rtc plus `GO2RTC_URL` for the worker); Docker Compose loads it next to the worker's own `docker-compose.yml`, which is never edited. The helper waits until the worker has no running job, because starting go2rtc recreates the worker container once (unfinished jobs resume);
   - *go2rtc answers on the worker* and *cameras served: N*. The worker reports what go2rtc really serves.
4. **Test relay** asks the relay from SyncHub itself, the way a TV will reach it. **Send cameras now** asks Protect again and gives go2rtc the cameras again (they are also sent again every 5 minutes, and whenever they change).
5. The Workers page marks the relay worker with a badge (*Camera relay: 3 camera(s)* or *down*).
6. Open the **TV remote**. **📹 Cameras** appears on a Google TV once Protect is on.

Other things worth knowing:
- A worker set up from SyncHub's own setup files (a new worker) already has go2rtc in its `docker-compose.yml`. The helper sees that and does not add the override.
- If a `docker-compose.override.yml` that SyncHub did not make is already there, it is left alone and the card says so. Add the go2rtc block from `docker-compose.yml` of a new worker by hand, or move that file away.
- Choosing another worker (or switching Protect off) removes the override and go2rtc from the old one.
- The relay's address is the address the worker polls SyncHub from. If SyncHub sees a Docker address instead of the PC's, type the PC's address under *Relay address*.

Keep ports 1984 and 8554 on your home network only. go2rtc has no password.

## Using it

Choose the cameras under *Cameras to serve* (Settings > Protect, Load cameras from the console, tick, Save). With none ticked every connected camera is served. go2rtc serves each camera as its own stream; it does not draw a picture-in-picture or a grid.

- Press **Cameras**, pick a camera. With one camera it opens straight away.
- Pick another camera in the box to switch. The first title is still remembered.
- **Close camera and carry on** closes the player and unpauses the title. If it was already paused it stays paused. If Jellyfin's app was closed meanwhile, SyncHub opens it again and plays the title from the saved place.

## If something does not work

- *go2rtc was not started: the worker's compose setup does not load docker-compose.override.yml*: the worker's folder picks its own compose files (`COMPOSE_FILE` in `.env`). Reinstall the worker with `setup-worker.sh`, whose compose file already includes go2rtc.

- **Test connection says 404 or "no Integration API".** The console is too old, or the address is not the console's own.
- **The list of cameras is empty.** Only connected cameras are shown.
- **"SyncHub does not know where the camera relay is".** The relay worker has not polled yet, or type its address in Settings.
- **The card stops at "update helper is checking in".** Install it on that PC once: `sudo ./agent/auto-update.sh --install --dir /opt/synchub-worker`.
- **"go2rtc is installed" shows a red failure.** The card shows the Docker error (a port already in use is the usual one). `journalctl -t synchub-auto-update` on the PC has the full story.
- **The TV says nothing happened.** Remote Tools shows the ADB lines: *VLC would not open the stream* means the app is not installed or the stream address is not reaching it. Open `http://<relay>:1984` in a browser: the relay's page lists the streams and plays them.
- **The picture opens but stays black.** Try *Medium* or *Low* picture quality, or open the stream in the relay's web page to see whether the camera itself is sending video.
