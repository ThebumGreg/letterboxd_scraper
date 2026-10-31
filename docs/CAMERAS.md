# UniFi Protect cameras on the TV remote

The remote's **Cameras** button pauses what the TV is playing in Jellyfin, remembers the title and the place, and shows a camera full screen. A box on the remote switches to another camera, or closes the camera and carries on where you were. For now this works on a **Google TV / Android TV**.

> This feature was written without a UniFi console or a Google TV to try it on. The Protect address paths, the way a TV player app takes a stream and go2rtc's `rtspx://` source are taken from their documentation. The first run on your hardware is the real test, so the Test connection button and the TV log (Remote Tools) say exactly which step failed.

## What you need

- **UniFi Protect** with the Integrations page (*Settings, Control Plane, Integrations*), and an API key made there.
- A **worker** that stays on, to run the camera relay (go2rtc). Protect gives out RTSPS streams that most TV players cannot open; the relay serves the same pictures as plain RTSP, and SyncHub only ever gives the TV the relay's address.
- **VLC for Android** (`org.videolan.vlc`) installed on the TV, or another player app that opens `rtsp://` addresses (set it in Settings).
- **ADB turned on for that TV** in SyncHub (`"adb": true`, see `docs/TV_REMOTE.md`). Without it SyncHub can only send the TV the stream address over the Remote protocol, which many TVs do not act on.

## Setting it up

1. **Settings, Connections, UniFi Protect cameras.** Switch it on, type the console's address, paste the API key, press **Test connection**. It lists the cameras it can see. A console signs its own certificate, so leave *Check the console's certificate* off unless you have installed a trusted one.
2. **Choose the worker** that runs the relay and save.
3. Give that worker the relay:
   - **A new worker:** the *Setup* files on the Workers page (and `setup-worker.sh`) now include a `go2rtc` service in `docker-compose.yml`. Run the setup as usual.
   - **A worker that is already running** (such as worker-1, whose compose file was written by hand): add this to its `docker-compose.yml`, under `services:`, and add `GO2RTC_URL: http://go2rtc:1984` to the worker's `environment:`:

     ```yaml
       go2rtc:
         image: alexxit/go2rtc
         container_name: synchub-go2rtc
         restart: unless-stopped
         ports:
           - "1984:1984"
           - "8554:8554"
     ```

     Then update the worker as in `docs/UPGRADE.md` (`git pull --ff-only origin main`, then `sudo ./agent/update-worker.sh --dir /opt/synchub-worker`), because the worker's agent is what gives go2rtc its cameras, and `docker compose up -d --build` in `/opt/synchub-worker`.
4. Wait a minute for the worker to poll once. The relay's address is the address the worker polls SyncHub from; if SyncHub sees a Docker address instead of the PC's, type the PC's address under *Relay address*.
5. Open the **TV remote**. **📹 Cameras** appears on a Google TV once Protect is on.

Keep ports 1984 and 8554 on your home network only. go2rtc has no password.

## Using it

- Press **Cameras**, pick a camera. With one camera it opens straight away.
- Pick another camera in the box to switch. The first title is still remembered.
- **Close camera and carry on** closes the player and unpauses the title. If it was already paused it stays paused. If Jellyfin's app was closed meanwhile, SyncHub opens it again and plays the title from the saved place.

## If something does not work

- **Test connection says 404 or "no Integration API".** The console is too old, or the address is not the console's own.
- **The list of cameras is empty.** Only connected cameras are shown.
- **"SyncHub does not know where the camera relay is".** The relay worker has not polled yet, or type its address in Settings.
- **The TV says nothing happened.** Remote Tools shows the ADB lines: *VLC would not open the stream* means the app is not installed or the stream address is not reaching it. Open `http://<relay>:1984` in a browser: the relay's page lists the streams and plays them.
- **The picture opens but stays black.** Try *Medium* or *Low* picture quality, or open the stream in the relay's web page to see whether the camera itself is sending video.
