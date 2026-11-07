The film is matched to its files through its Jellyfin item. Jellyfin reports the main file's path; SyncHub turns it into a share and a path with the share's **Jellyfin** settings on the Storage page. Under *How Jellyfin, Radarr and Sonarr see this folder* there are two ways to say it:

- **Jellyfin** (one path): Jellyfin sees the whole share under one folder, for example `/data`.
- **Jellyfin: other mounts of this share** (one per line): Jellyfin mounts parts of the share separately. Write each as `path Jellyfin sees = folder inside the share`, for example `/OldMovie = Old Movies`, `/media = Movies`, `/TV1 = TV Shows`. The longest matching line wins. These lines also tell Jellyfin where a finished file is.

The other files of the film are the ones in the same folder named after it (`Heat (1995) - 1080p.mkv` beside `Heat (1995).mkv`), or every file in the folder when the folder is the film's own. A loose file in a shared folder such as `Old Movies/` matches only itself, never the other films beside it.

**Radarr:** set the share's **Radarr** path to where Radarr sees the share root. If Radarr mounts `/mnt/user` at `/media`, the Media share is `/media/Media` and TFiles is `/media/TFiles`.

# Quality checks

SyncHub can rate how a film file looks **without** an original to compare with. A worker samples a few short parts of the file and measures what a viewer would notice. SyncHub turns the numbers into a score from 0 to 100, a grade (good, ok, poor, bad) and a short list of findings in plain words.

This is different from VMAF. VMAF compares a conversion with its original, so it can only run while converting. A quality check judges one file on its own, so it works for anything in the Library, including a new download.

## What is measured

| Finding | How | How far to trust it |
|---|---|---|
| Decode errors | ffmpeg decodes each sampled part and every error line is counted | Reliable. A damaged or badly encoded file. |
| Too few bits for the picture | video bitrate ÷ (width × height × frame rate), scaled so HEVC and AV1 count for more than H.264 | Reliable. Starved bitrate shows as blocks in dark scenes and grain. |
| Interlaced picture | ffmpeg `idet`, the share of frames that are interlaced | Fairly reliable on real films. |
| Frozen picture | ffmpeg `freezedetect`, frozen for 2 seconds or more | Reliable; long black or still shots count too. |
| Blockiness | ffmpeg `blockdetect` | **Advisory.** Varies with the film. It lowers the score a little and never makes a file "bad" alone. |
| Blur | ffmpeg `blurdetect`, per 1080 lines of height | **Advisory.** Some films are shot soft on purpose. |

Not measured: real motion blur, banding, an upscaled source, audio problems. A trained no-reference model could be added later if these checks disagree with what you see.

The thresholds are starting points. Tune them after you have looked at a few films you know: they are constants at the top of `synchub/quality.py`. Change `CHECK_VERSION` there when you change the rules; older results then show "(old)" and the Library pass checks those films again. The worker stores no score, only numbers, so changing the rules never needs a worker update.

## How a check runs

- A check is a job of the `scan` kind with the operation `quality`. It shows as a row on Media > Jobs. Only a worker that lists the `quality` feature takes it, so update workers first (Workers page, **Update**); until then the check waits.
- The worker reads about 10 parts of 30 seconds, spread over the film and skipping the first and last 3 minutes. Settings: *parts sampled* (1 to 24) and *seconds in each part* (10 to 120). A part is read from the share where it is; nothing is copied to the worker's disk.
- It runs at low CPU priority.
- The result belongs to the file as it was (size and modified time). If the file changes, the old result stops counting.

## Where checks start

1. **On demand:** *Check quality* on the movie page (**Your copies**) and on the Library record page.
2. **New downloads:** when a Radarr or Sonarr import has been analysed, before the size policy decides. Setting *New downloads*: **Check, and flag low scores** (default) or **Do not check**. A low score only flags the file; the import carries on as before.
3. **Library pass (off by default):** every 5 minutes SyncHub queues up to 3 unchecked films, newest first or oldest first. It does nothing while anyone is playing something in Jellyfin. If Jellyfin cannot be asked, the pass goes ahead.

## Minimum resolution

*Minimum resolution* (default 1080p, 0 = none) flags any file whose picture is smaller, whatever its quality check says, and even if it was never checked. Width counts as well as height, so a widescreen 1920x800 film meets 1080p. A flagged file says "Below your minimum resolution". SyncHub does not upscale: stretching a 480p picture adds no detail. The fix is a real larger copy (see below), or *Accept as is* for a film that only exists in SD.

## The flag

A file below the minimum resolution, or whose current check scores under *Flag a file scoring under* (default 60), is **Low quality**. The flagged files are listed in the **Below minimum quality** section of the **Library** tab (Media > Library), with the findings and the buttons below. The old address `/library/quality` opens that section. The settings (minimum resolution, score limit, parts sampled, new downloads, Library pass) are on **Media > Options > Quality check settings**; they are yours to change, none is fixed. The Dashboard no longer carries a flagged-files line. *Accept as is* stops the flag for that file until the file changes; *Flag it again* undoes it.

## Movie page: Your copies

For a film that Jellyfin knows, the movie page lists every file in the film's folder with its picture size, HDR or Dolby Vision type, bit depth, codec, video bitrate, bits per pixel, frame rate, audio, subtitles, file check and quality. Above the list:

- **Full quality copy**: the best file (largest picture, not an extra version).
- **Remote copy**: a smaller extra version (for example `Heat (1995) - 1080p.mkv`).
- **No smaller copy for remote viewing**: the full copy is above 1080p, extra versions are on and none exists. The Versions tab makes one.

The film is matched to its files through the Jellyfin item's path and the share's *Jellyfin path* on the Storage page. A film at the top of a share (not in a folder of its own) is not matched, because its neighbours are other films.

## Request a better copy (movies, through Radarr)

On a flagged file (the Library tab's Below minimum quality section, the movie page, the Library record page):

- **Show releases** is read only. SyncHub asks Radarr for what the indexers have for this film (the same list as Radarr's interactive search) and lists the real upgrades: allowed to download, not on the blocklist, not the release being replaced, and at least the minimum height (or the file's own height when that is larger). Best first. Nothing in Radarr changes. It can take a minute, because Radarr asks every indexer.
- **Get this one** sends one release from that list to the download client. SyncHub first blocks the release that was grabbed for the file, then grabs the chosen one. This ignores Radarr's quality-profile cutoff.
- **Request better copy** blocks the release that was grabbed for the file and asks Radarr to search again. Radarr applies its own quality profile. If the profile says the existing file already meets its cutoff, Radarr may grab nothing; then use *Show releases*.

Blocking uses Radarr's "mark as failed" on the grab in the film's history, which puts that release on Radarr's blocklist. If Radarr's history no longer has the grab (an old file, or a manual import), nothing can be blocked; SyncHub says so and goes on. Each request is recorded (`quality_request`) and shown under the file, with who asked, which release was blocked and what Radarr was told.

The old file is not deleted. When the new download arrives it goes through the normal download workflow, including a quality check, and Radarr replaces the old file on import if it treats the new one as an upgrade. The share needs its **Radarr path** on the Storage page, so SyncHub can tell which Radarr movie a file belongs to.

A film that is **not in Radarr** (or sits outside Radarr's folders, like loose files in `Old Movies/`) cannot be requested this way: there is no Radarr movie and no grab to block, and the buttons say so. Checks, scores, the flag and *Accept as is* still work. To replace such a film, add it to Radarr with *Request in Radarr* on the movie page; Radarr downloads a new copy into its own folder, and the old file is deleted by hand once the new one is in Jellyfin.

Not run against a live Radarr from here: the calls are Radarr v3's documented ones (`history/movie`, `history/failed/{id}`, `command` MoviesSearch, `release`). If one differs on your version, the page shows Radarr's own error.

## Not built yet

- TV shows (Sonarr). Episodes show "not built yet" instead of a button's result.
- Banding and upscale detection.
- Grabbing a replacement automatically without a person choosing.

## Quality test: standard or AI (Media > Options)

A test card to compare today's check with an AI model on one file. Type part of a file name, choose **Standard**, **AI** or **Both**, press *Run test*, and the table below shows both results side by side, with the worker's own row on the Jobs tab (`AI quality test: <file>`).

- **Standard** is the usual ffmpeg check above.
- **AI** is a `quality_ai` job that only a worker reporting the `quality_ai` feature takes: it samples the same parts of the film, tone-maps HDR to SDR, and scores them with FAST-VQA, DOVER (technical score) and MUSIQ. Each model reports a 0 to 100 mean (higher is better). **AI** and **Both** stay greyed out until an online worker has the feature.
- It is a test only. It never flags a file, asks Radarr for a better copy or changes anything. The models were trained on phone and web video, so read their scores as a comparison with the standard score, not as a verdict. Plan: `plans/ai-quality-worker-plan.md`.
