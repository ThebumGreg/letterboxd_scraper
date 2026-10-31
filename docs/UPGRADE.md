# Upgrading SyncHub and its workers

Two things get upgraded, in this order: the **SyncHub app** (Unraid) first, then every **worker**. SyncHub runs database migrations when it starts, and a new SyncHub can talk to an older worker, but a new feature (a new job type, a new rule) only works once the worker has the matching agent code. The Workers page shows each worker's agent version and what it can do; a job a worker cannot do fails with "Update the agent".

The code only ever comes from GitHub. SyncHub never sends code to a worker: a worker updates by pulling from GitHub, either when you press **Update** on the Workers page (the PC's update timer does the pull and rebuild) or by itself on a schedule.

## 1. Update SyncHub (Unraid)

1. Note the current version (footer of any page, or *Settings*), and make sure a recent backup exists (SyncHub makes one before each migration; `/data/backups`).
2. Unraid, Docker tab: *Check for updates*, then *apply update* on `synchub` (it follows `ghcr.io/thebumgreg/synchub:latest`). If you build the container from a checkout instead: `git pull --ff-only` and `docker compose up -d --build`.
3. Open SyncHub. A healthy container, a loading Library page and no red banner on the Workers page mean it worked. The container log (`docker logs synchub`) names any migration that ran.

## 2. Update a worker

On the **Workers** page press **Update** on the worker (or let its update timer do it). That is all; the steps, the by-hand route and how a failed update is put back are in [WORKERS.md](WORKERS.md#4-updating). A worker set up by hand is adopted once with `setup-worker.sh --adopt` ([WORKERS.md](WORKERS.md#2-install-one-command-one-time)).

If a worker does not come online after an update, or you need to go back to older code by hand, see [WORKERS.md, "When something is wrong"](WORKERS.md#6-when-something-is-wrong).

## 3. Notes for particular releases

### Dolby Vision conversion (worker image since 1.3)

After this update the worker image also contains `mkvmerge` and `dovi_tool` (a prebuilt release, checked against a fixed checksum while the image is built). If `dovi_tool` cannot be downloaded the image is still built, but that worker does not offer the Dolby Vision job. Check after `docker compose up -d --build`:

```
cd /opt/synchub-worker
sudo docker compose run --rm --entrypoint dovi_tool worker --version
sudo docker compose run --rm --entrypoint mkvmerge worker --version
```

Both should print a version. The conversion needs about three times the film's size free in the work folder (the copy, the converted stream and the joined file). Start with one film from *Media > Versions* and play it on the Google TV or Roku before approving more.


### GPU encoding

`setup-worker.sh` now finds an Intel, AMD or NVIDIA GPU itself and adds it to the compose file, and the worker image includes the VMAF ffmpeg, so there is nothing to switch on by hand. Check with `sudo docker compose run --rm worker --check` (look for `hevc_vaapi`, `hevc_qsv` or `hevc_nvenc` under "encoders that work", and "VMAF: yes"), then compare CPU and GPU with **Run benchmark** on the Workers page. A job uses the GPU when its profile allows GPU encoding and the picture is not HDR. Older, hand-made workers can follow the commented `agent/examples/docker-compose.gpu.yml`, or adopt the worker with `--adopt`.

### Worker features added by the profile-rules release

A job only goes to a worker that lists the features it needs, so an old worker simply gets fewer jobs until it is rebuilt: `audio_add` (a compatible audio track), `two_outputs` (a Dolby Vision conversion plus the smaller copy in one job) and `benchmark` (the *Run benchmark* button). After rebuilding, `python3 synchub_agent.py --benchmark` on the worker prints the same table the Workers page shows. Port 80 is optional: see `docker-compose.port80.yml`.
