# TV players: the small PCs behind your TVs

A **player** is a small PC (Ubuntu or Debian) plugged into a TV by HDMI. SyncHub is the master and the players are its slaves, the same way workers are: a player only calls out to SyncHub, with a token of its own, so nothing on it is opened or forwarded, and everything is managed from SyncHub's **Media > Players** tab. You never log in to a player to update or restart it.

**What works today (first step):** connecting, status, **Ping**, **Update**, **Restart program**, **Logs**, new token and revoke. The player tells SyncHub what it can see (mpv, graphics, screens plugged in) so you can prove the PC, the GPU and the HDMI cable before anything plays. **Playing media is the next step.** The plan for all steps (playback, home screen and TV guide, camera picture-in-picture, scenes) is `plans/tv-control-nuc-plan.md` in the project files.

Streaming apps (Netflix and the like) stay on the TV's Google TV: their copy protection does not allow a PC to show them in full quality, and SyncHub does not try to get around it. A player plays your own library.

## 1. Set up a player

1. Install Ubuntu Server 24.04 (or Debian) on the PC, connect it to the network by cable and to the TV by HDMI.
2. In SyncHub: **Media > Players > Add player.** Give it a name and, as a label, the TV it is plugged into. A window shows the setup command **once** (it holds the player's token). Copy it.
3. On the PC, run it. It is two commands:

```bash
git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
sudo ./player/setup-player.sh --url http://UNRAID_IP:5001 --token sp_... --name "living-room"
```

The address is SyncHub's as the PC reaches it (not `localhost`). Add `--dry-run` first to see what it would do. The script, in order: installs git, python3, mpv and the graphics tools; asks SyncHub whether the token is accepted (a wrong token stops it before anything is changed); creates the system user `synchubplayer` (groups video, render, input); clones the code to `/opt/synchub-player`; writes `/etc/synchub-player.ini` (mode 600, it holds the token) and the `synchub-player` systemd service; starts it and runs `--check`. A private repository needs `export GITHUB_TOKEN=...` (read access to Contents) and `sudo -E`.

Within a few seconds the player shows **Online** on the tab, with its version, mpv version, graphics and the screens it sees. If **No screen detected** is shown, check the cable and that the TV is switched on.

## 2. Day to day, on the Players tab

| Do this | Where | What happens |
|---|---|---|
| Check the link | **Ping** | The player answers within a second; the answer shows under *Details*. |
| Update to the newest code | **Update** | The player fetches the newest code from GitHub, fast-forwards its checkout and restarts its own program. It never discards local changes: a checkout that cannot be fast-forwarded reports the failure and keeps running. |
| Restart | More > **Restart program** | The program exits and systemd starts it again. |
| Look at the log | **Logs** | The player uploads the tail of its own log. |
| Change name or TV label | More > **Edit name and TV** | Saved at once. |
| New token / revoke | More | The old token stops working at once. Run the setup command again on the PC after a new token. |

A command waits at most ten minutes for a player that is offline, then it is dropped, so a Restart never fires an hour later.

## 3. How it works (for the curious and for fixing)

- The player holds a request open (`POST /api/player/v1/poll`, up to 20 seconds) and SyncHub answers the moment a command is queued, so commands arrive within about a second. Each held request uses one of SyncHub's 16 web threads, which is fine for a handful of TVs.
- Calls: `register`, `poll` (sends status, takes a command), `commands/<id>/answer`, `log`. All carry `Authorization: Bearer sp_...`. A player token does not open the worker API and the other way round.
- The player program is `player/synchub_player.py` (standard library only). It shares SyncHub's version number; the tab shows a yellow version when it differs. Press **Update** to bring it level.
- On the PC: `journalctl -u synchub-player`, `sudo -u synchubplayer python3 /opt/synchub-player/player/synchub_player.py --config /etc/synchub-player.ini --check` (shows what it sees and whether SyncHub accepts the token).

## 4. When something is wrong

| Symptom | Likely cause and fix |
|---|---|
| Setup says the token was refused | Copy it again, or **More > New token** and use the new command. |
| Setup cannot reach SyncHub | Use SyncHub's address on your network, not `localhost`; check the PC can reach it (`curl http://UNRAID_IP:5001/healthz`). |
| Player stays **Offline** | `journalctl -u synchub-player` on the PC. A 401 means the token was replaced or revoked. |
| **Update** reports a git failure | The checkout has its own commits or no network. The message says which. For a private repository put `github_token = ...` in `/etc/synchub-player.ini`. |
| **No screen detected** | HDMI cable, TV input, or the TV is off. `--check` on the PC lists the connectors it sees. |
