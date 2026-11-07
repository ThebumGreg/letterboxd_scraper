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

## The flag

A file whose current check scores under *Flag a file scoring under* (default 60) is **Low quality**. The Dashboard shows a link with the count, and the **Low quality** page (`/library/quality`) lists the files with their findings and the settings. *Accept as is* stops the flag for that file until the file changes; *Flag it again* undoes it.

## Movie page: Your copies

For a film that Jellyfin knows, the movie page lists every file in the film's folder with its picture size, HDR or Dolby Vision type, bit depth, codec, video bitrate, bits per pixel, frame rate, audio, subtitles, file check and quality. Above the list:

- **Full quality copy**: the best file (largest picture, not an extra version).
- **Remote copy**: a smaller extra version (for example `Heat (1995) - 1080p.mkv`).
- **No smaller copy for remote viewing**: the full copy is above 1080p, extra versions are on and none exists. The Versions tab makes one.

The film is matched to its files through the Jellyfin item's path and the share's *Jellyfin path* on the Storage page. A film at the top of a share (not in a folder of its own) is not matched, because its neighbours are other films.

## Not built yet

- Asking Radarr for a better copy (block the bad release, search again). This is the next step.
- TV shows (movies first).
- Banding and upscale detection.
