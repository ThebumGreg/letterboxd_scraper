# SyncHub worker agent

**Setting up, updating and fixing a worker is in [docs/WORKERS.md](../docs/WORKERS.md).** This page is about what the agent program does.

The agent runs on a worker PC. It asks SyncHub for jobs over HTTP, does them, and reports back. SyncHub never connects to the PC, so nothing needs to be opened or forwarded on it. Two Python files, standard library only: `synchub_agent.py` (the agent) and `media_analysis.py` (what it knows about media files; without it the agent still copies, it just cannot scan).

What it does today: **verified copies** between your shares (SyncHub's *Test copy* button), **library scans** (it lists the video files on a share, looks at the ones SyncHub does not know yet or that changed with ffprobe and ffmpeg, and reports what it found) and **duplicate searches** (it reads files of the same size to see whether they are the same). It also runs **conversions** for approved Library files (copy in, ffmpeg remux or re-encode, check, copy out to the destination share); this needs ffmpeg and ffprobe and `media_analysis.py` next to the agent.

## What it tells SyncHub

- **Its own log, on request.** The Workers page's **Logs** button asks the agent for the tail of `worker.jsonl` (and its rotated files, newest last, at most about 1 MB); the agent sends it with its next poll, a few seconds later. The page can filter by level and search it, and the log can be copied or downloaded. Only the latest one is kept.
- **The machine.** At start the agent reads the CPU (model, cores, threads, the instruction sets that matter for encoding), memory, graphics cards (`nvidia-smi`, `lspci`, `/dev/dri`), whether it runs in a container, what ffmpeg and ffprobe it found, ffmpeg's hardware decoders and which encoders are built in. It then tests each of the main ones (x264, x265, SVT-AV1 and the NVENC, Quick Sync and VAAPI ones) by encoding a test frame, so SyncHub can tell an encoder that **works** from one that is built into ffmpeg but **failed** (usually no GPU, driver or `/dev/dri` in the container). With every poll it sends the figures that change: CPU use, load, free memory, how long the PC has been up. All of it is in the **Machines** section of the Workers tab. A GPU inside the Docker image needs the device passed in (`devices: ["/dev/dri:/dev/dri"]`) and the matching ffmpeg driver packages; the tab shows what the agent can actually see.

## What it promises

- **Nothing is overwritten.** A copy goes to `<name>.synchub-partial` and is renamed only when it has been read back and its SHA-256 matches the source. An existing, different file at the destination fails the job.
- **Nothing is lost on a crash.** Each job has its own folder with a `manifest.json` (written atomically), a log, and a verified local copy of the source. After a power cut or `kill -9` the agent finds those folders, tells SyncHub what it holds, and continues from the last checkpoint (every 256 MB by default), without copying a finished step again.
- **A result is never lost.** A finished job's report is saved first and re-sent until SyncHub accepts it.
- **An unmounted share is never written to.** Each share has a `.synchub-root` marker file holding its name. The agent only uses a share whose marker matches, so an empty folder from a failed mount, or `Media` mounted where `TFiles` belongs, is refused. A hung mount (stale NFS handle, dead SMB server) is reported as "not responding" instead of freezing the agent.
- **Nothing is deleted before it is verified.** Local files are removed only after SyncHub has the result; a failed job's folder is kept (14 days by default) with its log, and SyncHub's **Fetch full log** button asks the agent for it.

## Encoding: GPU, chunks, quality scores and kept originals

- **GPU encoding.** The agent test-encodes `hevc_nvenc`, `hevc_qsv` and `hevc_vaapi` at start (`--check` lists the ones that work). A plan uses one only when SyncHub's *Library settings > GPU encoding* is on and the picture is not HDR (HDR stays on the CPU encoder, which writes the mastering data correctly). `video_encoder` (`SYNCHUB_VIDEO_ENCODER`) picks which: `auto` tries nvenc, qsv, vaapi in that order, `cpu` never uses a GPU. If the GPU encode fails or comes out over the size limit, the job is encoded again on the CPU and the job says so. Expect GPU files to be 20 to 40 percent larger than x265 at the same look. For an Intel GPU in Docker, add `examples/docker-compose.gpu.yml` (it passes `/dev/dri` through); the image already has the Intel media driver.
- **Chunked, resumable encodes.** A video longer than twice `chunk_minutes` (default 10; 0 turns it off) is encoded as video-only pieces in the job folder, then joined without re-encoding together with the source's audio, subtitles and chapters. A finished piece is never encoded again, so a reboot loses at most one. Chunks need about one extra output's worth of free space on the work disk until the join.
- **Dolby Vision 7 to 8.1 and smaller extra versions.** With `dovi_tool` and `mkvmerge` the worker converts a profile 7 film without re-encoding the picture (the video is streamed through `dovi_tool -m 2 convert --discard`, joined with the other tracks by `mkvmerge` and checked), and a plan can scale the picture down (`max_height`) to make a smaller copy beside the original (see `docs/DESIGN_ENCODING.md` 11.4).
- **Quality scores.** When the plan asks for it and ffmpeg has libvmaf (the worker image includes a static ffmpeg with libvmaf by default on amd64; if it could not be downloaded, or `FFMPEG_URL=none`, the score is skipped and the job says so; see `agent/Dockerfile`), `vmaf_samples` windows of 20 seconds are scored against the source (scaled to 1080p). HDR pictures are skipped. In *hold* mode a mean under 93 (95 for 2160p) pauses the job for review: approve to use the file anyway, skip or reject to keep the original.
- **Kept originals.** `work/originals/` holds each replaced original with a `meta.json`. SyncHub's Originals tab lists them (the agent sends the list with its poll) and can queue *restore*, *keep longer* and *delete now* as jobs for this worker. A restore copies the original back under its old name, checks its checksum, and moves the converted file aside into `<folder>-converted` (kept 7 days) rather than deleting it.

## Settings

Most of these are now chosen on SyncHub's Workers page (More, Edit name, tags, settings) and reach the worker without editing a file; the table is for the file or environment fallback.

A file in INI style (`--config`, or `./synchub-agent.ini`, or `/etc/synchub-agent.ini`); environment variables override it.

| Setting (`[section]`) | Environment | Default | Meaning |
|---|---|---|---|
| `[synchub] url` | `SYNCHUB_URL` | required | SyncHub's address, as this PC reaches it (not `localhost`) |
| `[synchub] token` | `SYNCHUB_TOKEN` | required | The worker's token (shown once on the Workers page) |
| `[synchub] name` | `SYNCHUB_NAME` | | Informational; SyncHub names a worker by its token |
| `[synchub] use_proxy` | `SYNCHUB_USE_PROXY` | `no` | Use the system HTTP proxy to reach SyncHub (normally wrong on a LAN) |
| `[worker] work_dir` | `SYNCHUB_WORK` | `./work` (`/work` in Docker) | Job folders, logs and `originals/` (files replaced in place, kept for the retention SyncHub sets); use a big local disk |
| `[worker] slots` | `SYNCHUB_SLOTS` | `1` | Jobs at once (SyncHub's own limit for the worker also applies) |
| `[worker] scan_threads` | `SYNCHUB_SCAN_THREADS` | `2` | Files a scan analyses at the same time (each is a short ffprobe and ffmpeg run; most of the time is the share) |
| `[worker] scan_batch` | `SYNCHUB_SCAN_BATCH` | `25` | How many analysed files are reported to SyncHub at a time, so an interrupted scan loses at most this many |
| `[worker] max_copy_mb_s` | `SYNCHUB_MAX_COPY_MB_S` | `0` (no limit) | Cap copy speed so a big copy cannot take the whole network |
| `[worker] verify` | `SYNCHUB_VERIFY` | `hash` | `hash` reads every copy back and compares SHA-256; `size` only compares sizes |
| `[worker] copy_chunk_mb` / `checkpoint_mb` | `SYNCHUB_COPY_CHUNK_MB` / `SYNCHUB_CHECKPOINT_MB` | `8` / `256` | Block size; how often a resumable checkpoint is saved |
| `[worker] done_retention_hours` | `SYNCHUB_DONE_RETENTION_HOURS` | `1` | How long a finished job's folder (log and manifest, no big files) is kept |
| `[worker] failed_retention_days` | `SYNCHUB_FAILED_RETENTION_DAYS` | `14` | How long a failed or cancelled job's folder and files are kept |
| `[worker] log_level`, `log_max_mb`, `log_backups` | `SYNCHUB_LOG_LEVEL`... | `INFO`, `10`, `5` | The worker log |
| `[worker] ffmpeg`, `ffprobe` | `SYNCHUB_FFMPEG`... | `ffmpeg`, `ffprobe` | Where they are, if not on the PATH |
| `[worker] dovi_tool`, `mkvmerge` | `SYNCHUB_DOVI_TOOL`, `SYNCHUB_MKVMERGE` | `dovi_tool`, `mkvmerge` | Where they are, if not on the PATH. When both run, the worker offers Dolby Vision profile 7 to 8.1 jobs (feature `dovi`) |
| `[roots] <name>` | `SYNCHUB_ROOT_<NAME>` | SyncHub's mount point | Only if a share is mounted somewhere else on this PC |

## Folder layout under the work folder

```
work/jobs/<job-id>-<hash>/    manifest.json  job.log  source/ (the verified local copy, removed once SyncHub has the result)
work/logs/worker.jsonl        (+ .1 .. .5)
work/alive                    touched every loop, for the Docker health check
```

`examples/` holds sample setup files generated by SyncHub for a made-up worker (`env.example` is the sample `.env`). The real ones, with your token and shares, come from the Workers page.
