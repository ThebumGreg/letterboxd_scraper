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

4. **Damage check (on demand, and an optional pass that is off by default):** reads the **whole file** with ffmpeg, throws the picture away and counts decode errors. It gives no score. A file with errors is flagged "Damaged: N decode errors" in Below minimum quality; a clean one is left alone, and it never replaces a scored check. Use the *Damage check* button on a film, or switch on *Damage check pass* under Media > Options > Quality, which reads one file at a time and never while anyone is watching in Jellyfin. It reads every byte, so it is slow and heavy on the share. Workers need an update to take it.

**Pause and stop.** *Automatic checks: Paused* (Options > Quality) stops new downloads and both passes from queuing checks; a check you ask for by hand still runs. *Stop all checks* cancels everything waiting or running. *Clear quality results* removes stored scores, flags and Accept-as-is marks (the Library list is not touched). *Clear results* on the scan form also cancels waiting checks of the files it removes.

## Minimum resolution

*Minimum resolution* (default 1080p, 0 = none) flags any file whose picture is smaller, whatever its quality check says, and even if it was never checked. Width counts as well as height, so a widescreen 1920x800 film meets 1080p. A flagged file says "Below your minimum resolution". SyncHub does not upscale: stretching a 480p picture adds no detail. The fix is a real larger copy (see below), or *Accept as is* for a film that only exists in SD.

## The flag

A file below the minimum resolution, or whose current check scores under *Flag a file scoring under* (default 60), is **Low quality**. The flagged files are listed on the **Below minimum quality** page (`/library/quality`), with the findings and the buttons below. The Library tab shows a **Below minimum quality** box next to Done, Rejected and the other boxes, with the count; **Open list** opens the page. The page has a search, a share filter, paging and **Export this list**, like the other Library lists. The settings (minimum resolution, score limit, parts sampled, new downloads, Library pass) are on **Media > Options > Quality check settings**; they are yours to change, none is fixed. The Dashboard no longer carries a flagged-files line. *Accept as is* stops the flag for that file until the file changes; *Flag it again* undoes it.

## Held files: never converted

A flagged file is **held** (below the minimum resolution, a current score under the limit, or decode errors from a damage check, and not accepted). A held file is never converted: no re-encode, remux, audio change, track removal, Dolby Vision remux, extra 1080p copy or compression. It can still be put in the right place.

- **One page.** A held file leaves Ready, Suggested, Worth a look, Format conversion and Done and lives only on the Below minimum quality page. Files already Queued (a job exists), Rejected or Needs you keep their place there, with a badge on this page.
- **Rename and move only** (button on each row) approves a rename inside the share (a file already on the destination share) or a byte for byte copy to the right folder. The picture, audio and tracks are never touched.
- **Everything else on a row:** Request better copy, Show releases, Damage check, Check again and **Accept as is**. Accept as is is the only way back to normal conversion; there is no Convert anyway button.
- **Enforced on the server.** Approving a held file (one, bulk, review one by one) only makes a copy or rename; a request to keep fewer tracks or use a converting profile is refused with a sentence. The Versions tab and the download pipeline skip held files (a held new import is brought in and left as it is).
- **A job that already exists keeps running.** If its file becomes held afterwards, the Library record shows a warning; cancel the job on the Jobs page if you do not want it.
- **Unchecked files** are held only for their resolution, since there is no score yet. Run the Library pass (and the damage check pass) before a bulk conversion so score and damage holds exist.

### Check first, then convert

Approving a **conversion** queues a quality check for that file (and a damage check when its pass is on) first. A file that passes gets its conversion job; a low or damaged one is held, goes back to pending and gets no job. A check that fails or is stopped also leaves the file pending (approve it again). Only files you approve are checked. A plain copy or rename never waits. Switch it off with **Before converting** on Options > Quality check settings. A file that already has a current check converts at once.

## Movie page: Your copies

For a film that Jellyfin knows, the movie page lists every file in the film's folder with its picture size, HDR or Dolby Vision type, bit depth, codec, video bitrate, bits per pixel, frame rate, audio, subtitles, file check and quality. Above the list:

- **Full quality copy**: the best file (largest picture, not an extra version).
- **Remote copy**: a smaller extra version (for example `Heat (1995) - 1080p.mkv`).
- **No smaller copy for remote viewing**: the full copy is above 1080p, extra versions are on and none exists. The Versions tab makes one.

The film is matched to its files through every Jellyfin item linked to it: the item's path and the share's *Jellyfin path* (and *other mounts*) on the Storage page. Any other Library file whose cleaned name starts with the film's title and year is found too, wherever it sits, and says "found by name". A film at the top of a share matches only itself, because its neighbours are other films. When Jellyfin's path is not covered by any share, the card names it. Two mappings with the same Jellyfin path: the one that names a folder wins, and the Storage page warns when you save.

## Request a better copy (movies, through Radarr)

On a flagged file (the Library tab's Below minimum quality section, the movie page, the Library record page):

- **Show releases** is read only. SyncHub asks Radarr for what the indexers have for this film (the same list as Radarr's interactive search) and lists the real upgrades: allowed to download, not on the blocklist, not the release being replaced, and at least the minimum height (or the file's own height when that is larger). Best first. Nothing in Radarr changes. It can take a minute, because Radarr asks every indexer.
- **Get this one** sends one release from that list to the download client. SyncHub first blocks the release that was grabbed for the file, then grabs the chosen one. This ignores Radarr's quality-profile cutoff.
- **Request better copy** blocks the release that was grabbed for the file, then picks the best release on offer (the same list as *Show releases*: allowed, not blocklisted, not the one being replaced, at least the minimum height; best resolution first, then seeders, then size) and grabs it. A manual grab ignores Radarr's cutoff, so a film that "already meets its profile" is still replaced. If nothing qualifies, nothing is blocked or grabbed and the page says so; use *Show releases* to look.

**Following a request.** While a request is open (up to a week) SyncHub reads Radarr's download queue every minute and shows the state next to the request on the flagged list and the movie page: Radarr is searching, queued, downloading (with percent and time left), importing, imported, a problem (with Radarr's message), waiting (no download started yet) or nothing found (nothing started in a day). It asks Radarr only while a request is open.

Blocking uses Radarr's "mark as failed" on the grab in the film's history, which puts that release on Radarr's blocklist. If Radarr's history no longer has the grab (an old file, or a manual import), nothing can be blocked; SyncHub says so and goes on. Each request is recorded (`quality_request`) and shown under the file, with who asked, which release was blocked and what Radarr was told.

The old file is not deleted. When the new download arrives it goes through the normal download workflow, including a quality check, and Radarr replaces the old file on import if it treats the new one as an upgrade. The share needs its **Radarr path** on the Storage page, so SyncHub can tell which Radarr movie a file belongs to.

A film that is **not in Radarr** (or sits outside Radarr's folders, like loose files in `Old Movies/`) has no grab to block. *Request better copy* then adds the film to Radarr (its default quality profile and root folder from Settings) and starts a search, using the TMDB match Jellyfin has for the file. If Jellyfin has no match, the button says so. Radarr downloads the new copy into its own folder; the old file is never deleted by SyncHub, so delete it by hand once the new copy is in Jellyfin. *Show releases* and *Get this one* still need the film to be in Radarr first.

Not run against a live Radarr from here: the calls are Radarr v3's documented ones (`history/movie`, `history/failed/{id}`, `command` MoviesSearch, `release`). If one differs on your version, the page shows Radarr's own error.

## Not built yet

- TV shows (Sonarr). Episodes show "not built yet" instead of a button's result.
- Banding and upscale detection.
- Grabbing a replacement automatically without a person choosing.

## Quality test: standard or AI (Media > Options)

**The AI test is an experiment.** It stays off by default, never flags a file, never asks Radarr for a better copy and never changes a score; only the standard check above is used. The card is kept for trying models later. A test card to compare today's check with an AI model on one file. Type part of a file name, choose **Standard**, **AI** or **Both**, press *Run test*, and the table below shows both results side by side, with the worker's own row on the Jobs tab (`AI quality test: <file>`).

- **Standard** is the usual ffmpeg check above.
- **AI** is a `quality_ai` job that only a worker reporting the `quality_ai` feature takes. It takes 3 frames from each sampled part of the film (HDR is tone-mapped to SDR first, and anything wider than 1920 pixels is scaled down), and scores every frame with three models from the `pyiqa` library: **MUSIQ**, **TOPIQ** (no-reference) and **CLIP-IQA**. Each reports a 0 to 100 mean (higher is better). **AI** and **Both** stay greyed out until an online worker has the feature.
- The models judge one picture at a time, so they cannot see motion problems, and they were trained on photos, not films: film grain and soft photography can read as poor quality. Read the scores next to the standard score, not as a verdict. FAST-VQA and DOVER (video models) are not part of this first version: they are separate projects, not in `pyiqa`.
- It is a test only. It never flags a file, asks Radarr for a better copy or changes anything. The models were trained on phone and web video, so read their scores as a comparison with the standard score, not as a verdict. Plan: `plans/ai-quality-worker-plan.md`.

### Turning the AI test on for a worker

The AI layer is PyTorch (CPU build) and the three models, about 1 GB, built into the worker's own image (no second image). It is off by default.

- **Existing worker (no SSH):** Update the worker first (Workers page, *Update*), so the PC has the new helper. Then *More > Edit* the worker and tick **AI quality test**. Within a minute, when the worker is idle, the PC rebuilds the image with the layer and the Jobs page shows *Rebuilding the worker with the AI quality test layer*. The first build downloads about 1 GB. When it finishes, the worker reports the `quality_ai` feature and the Options card turns AI and Both on. Untick the box to go back to the small image.
- **New worker:** `sudo ./agent/setup-worker.sh ... --ai-quality`, then tick the box in Edit worker so automatic updates keep it.
- **Weights** are downloaded at build time, so the running worker needs no internet. If a download fails the image still builds without the layer and the build output says so; the Workers page then shows no AI feature. The choice is a file, `AI_QUALITY`, in the worker's build folder; `update-worker.sh --ai-status` prints `on` or `off`, and `--ai-quality on|off` changes it.
- **GPU:** the default build is CPU only. An NVIDIA card needs the CUDA build (`AI_TORCH_INDEX=https://download.pytorch.org/whl/cu124` as a build arg) and the GPU given to the container; set `SYNCHUB_AI_DEVICE` to force `cpu`, `cuda` or `xpu`.
- **Licence:** `pyiqa` is PolyForm Noncommercial. Fine for a home server; check it before using SyncHub commercially.


## Files under the minimum resolution

A file whose picture is smaller than your minimum resolution (Settings, *Minimum resolution*) is not checked by the background passes (new downloads and the Library pass). It shows **Too small, needs a bigger copy** instead of a score, because the standard score judges how clean the encode is, not how big the picture is, and a clean 640x352 file would otherwise look "good". Press *Check quality* on the file to check it anyway.
