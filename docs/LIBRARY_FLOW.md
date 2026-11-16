# From a file on a share to a finished film: the whole Library flow

This page follows one file through SyncHub, step by step, and says where each number you see on the Record page comes from. The other pages go deeper: [FEATURES.md](FEATURES.md) (the manual), [QUALITY.md](QUALITY.md) (quality checks and the compression test), [WORKERS.md](WORKERS.md).

## The steps at a glance

1. **Scan.** A worker reads the share and reports each file's facts. SyncHub stores them and works out a suggestion.
2. **Sort.** Every file lands in one status (queue) with a confidence.
3. **Approve.** You choose a profile and approve, one file at a time or together.
4. **Check first.** Before a conversion, a quality check (and a compression test when the file would be compressed) decides if it should be converted at all.
5. **Convert and place.** A worker makes the new file, verifies it and puts it in the Media share.
6. **Afterwards.** Jellyfin, Radarr and Sonarr are told, the original is kept or removed as you chose, and the check runs on the new file.

## 1. Scan

*Library > Look at a share* starts a scan job on a worker that has the share mounted. For each video file the worker runs ffprobe and reports: container, video codec, resolution, bitrate, HDR and Dolby Vision profile, every audio and subtitle track, and a quick integrity check. SyncHub then works out (`synchub/library/findings.py`):

- a **clean name** and place (`Movies/Heat (1995)/Heat (1995).mkv`), with a naming confidence;
- the **track plan**: keep English, remove other languages, keep commentary, say which audio is the default;
- the **recommended action**: copy, rename, remux an old container, or re-encode an old codec;
- **flags**: no English audio, a duplicate group, unreadable or incomplete.

A file that has not changed since the last scan is skipped. Nothing on a share is changed by a scan.

## 2. Which queue a file waits in

`synchub/library/recommend.py` turns the stored facts and the profile's rules into one suggestion and one queue:

| Queue | A file is here when |
|---|---|
| **Ready to approve** | Nothing unusual: copy it, and the profile's rules add nothing. |
| **Suggested changes** | There is a plan that needs no video work: a name, foreign tracks to remove, a compatible audio track to add, a duplicate to skip, a rename inside the Media share. |
| **Worth a look** | A flag such as no English audio, or a compression the size rules could not decide alone ("ask before compressing"). |
| **Format conversion** | Video work: an old format to remux or re-encode, a compression the size rules ask for, Dolby Vision 7 to 8.1, or a smaller 1080p copy of a 4K film. |
| **Needs you** | It cannot be approved yet (unreadable, scan did not finish, needs a playback test), or an approved job failed. |
| **Approved and queued** | A job exists and a worker is, or soon will be, on it. |
| **Done** | Migrated, already in the right place, or another version of a film. |
| **Rejected** | You said no. |
| **Below minimum quality** | Held: smaller than the minimum resolution, scored low or damaged. It leaves every other queue. |

The profile's rules matter: a default profile that adds a compatible audio track to every DTS or TrueHD film, compresses above the bitrate trigger and makes a 1080p copy of every 4K film means most big files wait in Format conversion, and Ready holds only plain, already-clean files. The Files page filters (Share, Confidence, Type, Folder, Quality check, Kind of file, Size, Resolution) narrow any queue; **All files** shows everything.

**Confidence** is the lowest of the levels of the steps in the plan (naming, tracks removed, a re-encode, a flag). **Approve together** never includes low confidence files or duplicates.

## 3. Approve

Approving chooses the **profile** for the file: the one picked on the approve screen, else the one chosen at the scan, else the default profile (Options > Conversion profiles). The Record page shows which. Approving does not change the file; it records the decision (`approval_status`, `approved_name`, `approved_track_changes_json`, `approved_profile_id`) and queues the next step.

## 4. Check first (before a conversion only)

`quality.wait_for_check` holds the conversion job back until these are done and good:

1. **Quality check** (movies): a worker samples 10 slices and measures blocks, blur, interlacing, freezes and bitrate per pixel. The result is a score out of 100. Below the flag (60), or a picture under the minimum resolution (1080p), the file is **held** and never converted.
2. **Damage check** (optional): decodes the whole file for errors.
3. **Compression test** (only when the plan compresses): a worker encodes 4 clips of 15 seconds, spread over the film but never in the opening or the end credits, with exactly the settings the conversion would use, and scores each against the film with VMAF (CPU libvmaf). HDR and Dolby Vision pictures are tone-mapped to SDR on both sides first, so their score is a rough guide only and does not hold the film.
   - **Pass**: the average reaches the VMAF to keep and no clip is more than the margin under it. The job is made.
   - **Borderline**: the average passes but one clip is far under it. The file waits for you (**Convert anyway** accepts it).
   - **Fail**: the average is under the VMAF to keep. The file is held (**Convert anyway** overrides it for the profile).
   - The **VMAF to keep** is set in Options > Quality; blank means 93 (95 for 4K). Changing it judges every finished test again.
   - The **size estimate** is the clips' own bitrate for a bitrate-targeted encode. A clip much quieter than the others (credits, a black scene) is left out of the estimate and shown as "not counted".

Tests and checks are jobs on Media > Jobs, and each writes a line to the file's History.

**Conversions at once.** Options > Quality > *Conversions at once* limits how many conversions are queued, waiting for these checks or running (0 = no limit). A bulk approval beyond the limit leaves the rest approved with no job; each poll from a worker queues the oldest of them when a place frees up. Start with about 6.

## 5. Convert and place

`migrate.queue` builds the plan (`plan_for`): copy, rename inside the share, remux, or convert. A worker:

1. copies the source into its work drive (or reads it in place for a rename);
2. applies the track plan, adds the compatible audio track, converts Dolby Vision 7 to 8.1 or re-encodes the video (bitrate-targeted x265, or the GPU encoder when allowed);
3. verifies the result (duration, streams, optional VMAF of the finished file);
4. copies it into the Media share under its clean name and puts the original aside as `.synchub-original` on the share.

The Record page's *Jobs for this file* lists every job, and the *History* lists every decision and result.

## 6. Afterwards

- Jellyfin is told to refresh, and Radarr/Sonarr are updated according to *Options > After migration*.
- The original stays for the time you chose (Cleanup page), then is deleted.
- The **After conversion** VMAF row on the Record page comes from the last finished conversion; it is blank when the profile's VMAF mode is off, the video was not re-encoded, or the picture is HDR.

## The Record page: what each field is and when it changes

| Field | Where it comes from | When it updates |
|---|---|---|
| What SyncHub suggests | Worked out on every page view from the stored scan facts and the profile's rules | Scan, a profile or setting change, a quality result |
| **Why this is suggested** | The scan's own reasons (`review_reasons_json`), warnings and errors | Only when the file is scanned again |
| About this file badges, video bitrate, ceiling, "would compress" | Stored scan facts against the size rules | On a scan, or when the size rules change |
| Picture quality (score, damage) | The newest quality check of this file | When a check finishes; stale when the file changes |
| Compression test | The newest test of this file | When a test finishes; "test again" when the file or the profile changes |
| After conversion (VMAF) | Last finished conversion job | When a conversion finishes |
| Jobs for this file | The job table, by reference to this file | Live |
| History | `library_event` rows: scans, decisions, approvals, tests, checks, conversions, cleanup | Each time one of those happens (tests and checks since 1.9.0) |
