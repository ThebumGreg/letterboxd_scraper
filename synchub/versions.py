"""Extra versions and Dolby Vision conversion for films that are already in the library (docs/DESIGN_ENCODING.md section 11.4).

Two kinds of action, both on a scanned Library record and both started by a person (a recommendation is only ever a suggestion):

  variant    a second, smaller file next to the original, for phones and remote viewing. It is named `<folder name> - <label>.mkv`, which is how
             Jellyfin recognises another version of the same film, so the original is never touched. Only films, for now; the plan and the job do
             not depend on that.
  dv_remux   Dolby Vision profile 7 converted to profile 8.1 in place: the picture is not re-encoded, the enhancement layer is dropped and the
             original is kept on the worker for `originals_retention_days`. Almost nothing plays profile 7 natively.

Each action is one `library` job with `ref = (library_action, id)`; the row in `library_action` follows the job (see sync) and remembers a
"skip" so a declined recommendation does not come back.
"""
import dataclasses
import json
import os
import re

from synchub import db, jobs, settings
from synchub.library import migrate, size_policy, store

REF = "library_action"
VARIANT, DV_REMUX = "variant", "dv_remux"
KINDS = (VARIANT, DV_REMUX)
OPEN = ("queued", "converting")
JOB_STATUS = {"queued": "queued", "running": "converting", "needs_review": "blocked", "done": "done", "failed": "failed", "cancelled": "failed", "skipped": "failed"}
GIB = 1024 ** 3
TEXT_SUBTITLES = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
FRIENDLY_AUDIO = ("aac", "ac3", "eac3")                         # played by phones, TVs and browsers without a conversion
ENGLISH = ("eng", "en", "english")

# What Jellyfin treats as another version of a film: a file in the film's folder named `<folder name> - <anything>`. Such a file must never be renamed or moved
# on its own, so the Library leaves it alone (see library.recommend).
VERSION_STEM = re.compile(r"^.+ \(\d{4}\) - \S.*$")


class VersionError(Exception):
    """The action cannot be done; the message says why in plain words."""


@dataclasses.dataclass
class Config:
    on: bool = True
    min_gb: float = 30.0
    height: int = 1080
    label: str = "1080p"
    crf: int = 23
    maxrate_kbps: int = 8000
    audio_kbps: int = 192
    stereo: bool = True
    dv7: bool = True


def _int(key, default, conn, low, high):
    try:
        return min(high, max(low, int(settings.get(key, conn=conn))))
    except (TypeError, ValueError):
        return default


_PROFILE_FIELDS = {"extra_copy": "on", "extra_min_gb": "min_gb", "extra_height": "height", "extra_label": "label", "extra_crf": "crf", "extra_maxrate_kbps": "maxrate_kbps",
                   "extra_audio_kbps": "audio_kbps", "extra_stereo": "stereo"}


def load(conn=None, profile=None):
    """The extra-version settings: the Options page, with whatever the conversion profile overrides on top."""
    cfg = _load_global(conn)
    overrides = (profile or {}).get("rules") or {}
    changes = {field: overrides[key] for key, field in _PROFILE_FIELDS.items() if key in overrides}
    if "dv_profile_7_8" in overrides:
        changes["dv7"] = overrides["dv_profile_7_8"] == "remux"
    return dataclasses.replace(cfg, **changes) if changes else cfg


def load_for(conn, rec, cache=None):
    """The settings for one Library record: those of the profile that applies to it (approved, else scan, else default)."""
    from synchub.library import profiles
    profile = profiles.effective(conn, rec)
    if cache is not None and profile["id"] in cache:
        return cache[profile["id"]]
    cfg = load(conn, profile)
    if cache is not None:
        cache[profile["id"]] = cfg
    return cfg


def _load_global(conn=None):
    try:
        min_gb = max(0.0, float(settings.get("variant_min_gb", conn=conn)))
    except (TypeError, ValueError):
        min_gb = 30.0
    label = settings.get("variant_label", conn=conn) or "1080p"
    return Config(on=settings.get("versions_on", conn=conn) == "1", min_gb=min_gb, height=_int("variant_height", 1080, conn, 240, 2160),
                  label=label if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,19}", label) else "1080p", crf=_int("variant_crf", 23, conn, 14, 35),
                  maxrate_kbps=_int("variant_maxrate_kbps", 8000, conn, 500, 100000), audio_kbps=_int("variant_audio_kbps", 192, conn, 64, 640),
                  stereo=settings.get("variant_stereo", conn=conn) == "1", dv7=settings.get("dv7_remux", conn=conn) == "1")


def is_version_file(file_name):
    """Whether a file name looks like another version of a film (`Heat (1995) - 1080p.mkv`)."""
    return bool(VERSION_STEM.match(os.path.splitext(file_name or "")[0]))


def _json(value, default):
    try:
        out = json.loads(value) if isinstance(value, str) and value else value
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def dv_profile(rec):
    """The Dolby Vision profile of the record's picture as text ('7', '8'...), or None when it is not Dolby Vision."""
    info = _json(rec.get("video_info_json"), {})
    if info.get("hdr") != "dv":
        return None
    return str(info.get("dv_profile")) if info.get("dv_profile") is not None else "?"


def picture_label(rec):
    info = _json(rec.get("video_info_json"), {})
    hdr = info.get("hdr") or "sdr"
    return {"dv": f"Dolby Vision profile {dv_profile(rec)}", "hdr10": "HDR10", "hlg": "HLG"}.get(hdr, "SDR")


def _height(rec):
    return size_policy._resolution(rec, _json(rec.get("video_info_json"), {}))[1] or 0


def version_path(rec, label):
    """Where the extra version of a film goes: next to it, named after the film's folder. None when the film is not in a folder of its own
    (Jellyfin only groups versions that share a folder named like the film)."""
    parts = (rec.get("relative_path") or "").replace("\\", "/").rsplit("/", 1)
    if len(parts) != 2 or not parts[0]:
        return None
    folder = parts[0]
    base = folder.rsplit("/", 1)[-1]
    stem = os.path.splitext(parts[1])[0]
    if not stem.startswith(base):
        return None
    return f"{folder}/{base} - {label}.mkv"


# ------------------------------------------------------------------------------------------- what is on record
def _actions(conn):
    """{(kind, path_key, label): row} for every action."""
    return {(r["kind"], r["path_key"], r["label"]): dict(r) for r in conn.execute("SELECT * FROM library_action")}


def action_for(conn, kind, path_key, label=""):
    r = conn.execute("SELECT * FROM library_action WHERE kind = ? AND path_key = ? AND label = ?", (kind, path_key, label)).fetchone()
    return dict(r) if r else None


def _open_for(conn, path_key):
    r = conn.execute("SELECT kind FROM library_action WHERE path_key = ? AND status IN ('queued', 'converting', 'blocked')", (path_key,)).fetchone()
    return r["kind"] if r else None


# ------------------------------------------------------------------------------------------- recommendations
def _readable(rec):
    return rec["analysis_status"] == "completed" and not rec.get("missing_since") and not rec.get("unreadable_reason") and (rec.get("size_bytes") or 0) > 0


def _title(rec):
    return rec.get("recommended_name") or rec["file_name"]


def _entry(rec, kind, headline, notes, can_approve=True, why_not=None, **extra):
    duration = float(rec.get("duration_seconds") or 0)
    return {"id": rec["id"], "kind": kind, "title": os.path.splitext(_title(rec))[0], "path": rec["source_path"], "size_gb": round((rec.get("size_bytes") or 0) / GIB, 1),
            "resolution": rec.get("resolution"), "picture": picture_label(rec), "duration_minutes": round(duration / 60) if duration else None, "headline": headline, "notes": notes,
            "can_approve": can_approve, "why_not": why_not, **extra}


def variant_entry(rec, cfg, height=None):
    """The suggestion of an extra version for one record, or None when it does not apply. `height` is the person's override of the default."""
    if not cfg.on or not _readable(rec) or not (rec.get("media_type") or "").lower().startswith("movie") or is_version_file(rec["file_name"]):
        return None
    height = height or cfg.height
    taller = _height(rec)
    if taller <= height or (rec["size_bytes"] or 0) < cfg.min_gb * GIB:
        return None
    dest = version_path(rec, cfg.label)
    notes = [f"A {height}p copy named “{cfg.label}”, HEVC at up to {cfg.maxrate_kbps / 1000:g} Mbps; the original stays as it is."]
    duration = float(rec.get("duration_seconds") or 0)
    ceiling = round(cfg.maxrate_kbps * 1000 / 8 * duration / GIB, 1) if duration else None
    if ceiling:
        notes.append(f"At most about {ceiling:g} GB.")
    picture = picture_label(rec)
    if picture.startswith("Dolby Vision profile 5"):
        return _entry(rec, VARIANT, f"Make a {height}p copy", notes, False, "Dolby Vision profile 5 cannot be re-encoded without turning the colours green and purple.", dest=dest, height=height)
    if picture.startswith("Dolby Vision"):
        notes.append("The copy is HDR10; the Dolby Vision layer is not kept.")
    elif picture in ("HDR10", "HLG"):
        notes.append(f"{picture} is kept in the copy.")
    if dest is None:
        return _entry(rec, VARIANT, f"Make a {height}p copy", notes, False, "The film is not in a folder named like it, so Jellyfin would not group the copy with it as another version.", dest=dest, height=height)
    return _entry(rec, VARIANT, f"Make a {height}p copy", notes, True, dest=dest, height=height)


def companion_copy(rec, cfg):
    """The variant entry that can ride along with a Dolby Vision conversion (one copy-in, two outputs), or None."""
    entry = variant_entry(rec, cfg)
    return entry if entry and entry["can_approve"] else None


def dv_entry(rec, cfg):
    if not cfg.dv7 or not _readable(rec) or dv_profile(rec) != "7":
        return None
    notes = []
    copy = companion_copy(rec, cfg)
    if copy:
        notes.append(f"A {copy['height']}p copy named “{cfg.label}” is made in the same job, from the same copy of the film.")
    return _entry(rec, DV_REMUX, "Convert Dolby Vision 7 to 8.1", notes, True, dest=rec["relative_path"], also_copy=bool(copy), height=copy["height"] if copy else None)


def recommendations(conn, cfg=None):
    """{'variant': [...], 'dv_remux': [...], 'declined': [...]}: what could be done, what a person declined, and what failed and can be tried again."""
    taken = _actions(conn)
    cache = {}
    out = {VARIANT: [], DV_REMUX: [], "declined": []}
    for row in conn.execute("SELECT * FROM library_file WHERE analysis_status = 'completed' AND missing_since IS NULL AND unreadable_reason IS NULL ORDER BY recommended_name, file_name"):
        rec = dict(row)
        rcfg = cfg or load_for(conn, rec, cache)
        for kind, label, make in ((VARIANT, rcfg.label, lambda r: variant_entry(r, rcfg)), (DV_REMUX, "", lambda r: dv_entry(r, rcfg))):
            entry = make(rec)
            if entry is None:
                continue
            state = taken.get((kind, rec["path_key"], label))
            if state is None or state["status"] == "failed":
                if state is not None:
                    entry["retry"] = state["message"] or "It failed earlier."
                out[kind].append(entry)
            elif state["status"] == "skipped":
                out["declined"].append(entry)
    return out


# ------------------------------------------------------------------------------------------- planning
def _audio_choice(tracks, cfg):
    """(indexes to keep, {index: conversion}, default index) for the phone copy: the English audio (else the first track), without commentaries."""
    audio = [t for t in tracks if t.get("type") == "audio"]
    spoken = [t for t in audio if not t.get("comment")] or audio
    english = [t for t in spoken if str(t.get("lang") or "").lower() in ENGLISH]
    chosen = (english or spoken[:1])
    keep, convert = [], {}
    for t in chosen:
        keep.append(t["index"])
        channels = int(t.get("channels") or 2)
        if cfg.stereo:
            convert[str(t["index"])] = {"codec": "aac", "channels": 2}
        elif str(t.get("codec") or "").lower() not in FRIENDLY_AUDIO:
            convert[str(t["index"])] = {"codec": "aac", "channels": min(channels, 6)}
    return keep, convert, (keep[0] if keep else None)


def variant_plan(conn, rec, cfg, height):
    """(destination, job plan) for an extra version of `rec`."""
    dest = version_path(rec, cfg.label)
    if dest is None:
        raise VersionError("This film is not in a folder named like it, so Jellyfin would not show the copy as another version of it.")
    info = _json(rec.get("video_info_json"), {})
    tracks = [t for t in _json(rec.get("streams_json"), []) if isinstance(t, dict)]
    keep_audio, audio_convert, default = _audio_choice(tracks, cfg)
    subtitles = [t["index"] for t in tracks if t.get("type") == "subtitle" and str(t.get("codec") or "").lower() in TEXT_SUBTITLES and not t.get("comment")
                 and str(t.get("lang") or "").lower() in ENGLISH + ("und", "")]
    change = {"op": "convert", "container": "matroska", "keep_audio": keep_audio, "keep_subtitle": subtitles, "audio_convert": audio_convert, "default_audio": default,
              "video": {"action": "transcode", "from": (rec.get("video_codec") or "").lower(), "codec": "hevc"}}
    hdr = info.get("hdr") or "sdr"
    profile = {"video_codec": "hevc", "crf": cfg.crf, "preset": "medium", "ten_bit": hdr != "sdr" or int(info.get("bit_depth") or 8) >= 10, "audio_kbps": cfg.audio_kbps,
               "maxrate_kbps": cfg.maxrate_kbps, "bufsize_kbps": cfg.maxrate_kbps * 2, "max_height": height, "deinterlace": bool(info.get("interlaced")),
               "gpu": size_policy.load(conn).gpu, "vmaf_mode": "off", "name": f"{cfg.label} copy"}
    plan = {"op": "convert", "change": change, "profile": profile, "needs": ["profiles", "scale"], "revision": rec["source_revision"], "duration": rec.get("duration_seconds"),
            "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "variant": {"label": cfg.label, "height": height}}
    return dest, plan


def dv_plan(conn, rec, extra=None):
    """(destination, job plan, policy) for the Dolby Vision 7 to 8.1 conversion. `extra` = (variant destination, variant plan) adds the smaller copy as a second output of the same job."""
    from synchub import downloads
    dest = rec["relative_path"]
    change = {"op": "convert", "container": "matroska", "video": {"action": "copy", "dv": "p8_1"}}
    if not dest.lower().endswith(".mkv"):
        change["target_extension"] = ".mkv"
        dest = os.path.splitext(dest)[0] + ".mkv"
    days = downloads.retention_days(conn)
    plan = {"op": "convert", "replace": True, "change": change, "needs": ["dovi"], "revision": rec["source_revision"], "duration": rec.get("duration_seconds"),
            "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "originals_retention_days": days}
    if extra:
        extra_dest, extra_plan = extra
        plan["extra"] = {"rel": extra_dest, "label": extra_plan["variant"]["label"], "height": extra_plan["variant"]["height"], "change": extra_plan["change"], "profile": extra_plan["profile"]}
        plan["needs"] = sorted(set(plan["needs"]) | set(extra_plan["needs"]) | {"two_outputs"})
    return dest, plan, {"originals_retention_days": days}


# ------------------------------------------------------------------------------------------- approving and skipping
def approve(conn, kind, record_id, by, height=None, now=None, with_copy=True):
    """Queues the job for one film. Returns the job id. Raises VersionError with a message for the page."""
    if kind not in KINDS:
        raise VersionError("Unknown kind of action.")
    from synchub import downloads
    rec = store.get_record(conn, record_id)
    if rec is None:
        raise VersionError("That file is not in the Library any more. Scan again.")
    cfg = load_for(conn, rec)
    if height is not None and kind == VARIANT:
        try:
            height = int(height)
        except (TypeError, ValueError):
            raise VersionError("The height must be a number of pixels.")
        if not 240 <= height <= 2160:
            raise VersionError("Choose a height between 240 and 2160 pixels.")
    entry = variant_entry(rec, cfg, height) if kind == VARIANT else dv_entry(rec, cfg)
    if entry is None:
        raise VersionError("This is not something that applies to this file now (check the sizes and the settings on this tab).")
    if not entry["can_approve"]:
        raise VersionError(entry["why_not"])
    label = cfg.label if kind == VARIANT else ""
    height = height or cfg.height
    existing = action_for(conn, kind, rec["path_key"], label)
    if existing and existing["status"] in OPEN + ("blocked",):
        raise VersionError("This one is already queued.")
    if existing and existing["status"] == "done":
        raise VersionError("This one is already done.")
    other = _open_for(conn, rec["path_key"])
    if other and other != kind:
        raise VersionError("Another job is still working on this film. Wait for it to finish, then do this one.")
    if downloads.busy(conn, rec) or migrate.active_job(conn, rec["id"]) is not None:
        raise VersionError("A worker is already busy with this file.")
    now = now or db.now()
    options = {"height": height} if kind == VARIANT else {}
    companion = None
    if kind == VARIANT:
        dest, plan = variant_plan(conn, rec, cfg, height)
        policy, need = None, (rec["size_bytes"] or 0) + migrate.WORK_MARGIN_BYTES
        title = f"{cfg.label} copy: {rec['file_name']}"
    else:
        extra = None
        copy = companion_copy(rec, cfg) if with_copy else None
        if copy and not _variant_taken(conn, rec, cfg.label):
            extra = variant_plan(conn, rec, cfg, cfg.height)
            companion = (extra[0], cfg.label, cfg.height)
        dest, plan, policy = dv_plan(conn, rec, extra)
        need = (rec["size_bytes"] or 0) * 3 + migrate.WORK_MARGIN_BYTES + ((rec["size_bytes"] or 0) // 2 if extra else 0)
        title = f"Dolby Vision 7 to 8.1{' and ' + cfg.label + ' copy' if extra else ''}: {rec['file_name']}"
    conn.execute(
        """INSERT INTO library_action (kind, path_key, root_name, relative_path, label, status, dest_rel, options_json, decided_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
           ON CONFLICT (kind, path_key, label) DO UPDATE SET status = 'queued', dest_rel = excluded.dest_rel, options_json = excluded.options_json, decided_by = excluded.decided_by,
                                                             message = NULL, result_json = NULL, finished_at = NULL, updated_at = excluded.updated_at""",
        (kind, rec["path_key"], rec["root_name"], rec["relative_path"], label, dest, json.dumps(options), by, now, now))
    action = action_for(conn, kind, rec["path_key"], label)
    job_id = jobs.create(conn, "library", title=title, ref=(REF, action["id"]), source=(rec["root_name"], rec["relative_path"]), dest=(rec["root_name"], dest), plan=plan,
                         policy=policy, need_bytes=need, now=now)
    conn.execute("UPDATE library_action SET job_id = ?, message = ? WHERE id = ?", (job_id, "Queued for a worker.", action["id"]))
    if companion:                                     # the smaller copy is a row of its own on the tab, followed by the same job
        extra_dest, extra_label, extra_height = companion
        conn.execute(
            """INSERT INTO library_action (kind, path_key, root_name, relative_path, label, status, dest_rel, options_json, decided_by, job_id, message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (kind, path_key, label) DO UPDATE SET status = 'queued', dest_rel = excluded.dest_rel, options_json = excluded.options_json, decided_by = excluded.decided_by,
                                                                 job_id = excluded.job_id, message = excluded.message, result_json = NULL, finished_at = NULL, updated_at = excluded.updated_at""",
            (VARIANT, rec["path_key"], rec["root_name"], rec["relative_path"], extra_label, extra_dest, json.dumps({"height": extra_height, "with_job": job_id}), by, job_id,
             "Queued with the Dolby Vision conversion.", now, now))
        conn.execute("UPDATE library_action SET options_json = ? WHERE id = ?", (json.dumps({"companion_label": extra_label}), action["id"]))
    return job_id


def _variant_taken(conn, rec, label):
    """Whether the smaller copy is already queued or made (then the conversion does not make it again)."""
    existing = action_for(conn, VARIANT, rec["path_key"], label)
    return bool(existing and existing["status"] in OPEN + ("blocked", "done"))


def approve_many(conn, kind, items, by):
    """Approves several films, each {'id': record id, 'height': optional override}. Returns (queued, [(record id, why not)]): one film that cannot be done never stops the rest."""
    done, skipped = 0, []
    for item in items:
        try:
            approve(conn, kind, int(item.get("id")), by, height=item.get("height"), with_copy=item.get("with_copy", True) is not False)
            done += 1
        except (VersionError, TypeError, ValueError) as exc:
            skipped.append((item.get("id"), str(exc)))
    return done, skipped


def approve_recommended(conn, kind, by):
    """Approves every film the tab recommends for `kind` with the default settings (the 'approve all' button)."""
    entries = [e for e in recommendations(conn)[kind] if e["can_approve"]]
    return approve_many(conn, kind, [{"id": e["id"]} for e in entries], by)


def skip(conn, kind, record_id, by, now=None):
    """Declines the recommendation for one film: it stays off the list until it is restored."""
    rec = store.get_record(conn, record_id)
    if rec is None or kind not in KINDS:
        raise VersionError("That file is not in the Library any more.")
    label = load_for(conn, rec).label if kind == VARIANT else ""
    existing = action_for(conn, kind, rec["path_key"], label)
    if existing and existing["status"] in OPEN + ("blocked", "done"):
        raise VersionError("This one is already queued or done.")
    now = now or db.now()
    conn.execute(
        """INSERT INTO library_action (kind, path_key, root_name, relative_path, label, status, message, decided_by, created_at, updated_at, finished_at)
           VALUES (?, ?, ?, ?, ?, 'skipped', ?, ?, ?, ?, ?)
           ON CONFLICT (kind, path_key, label) DO UPDATE SET status = 'skipped', job_id = NULL, message = excluded.message, decided_by = excluded.decided_by, updated_at = excluded.updated_at""",
        (kind, rec["path_key"], rec["root_name"], rec["relative_path"], label, f"Declined by {by}.", by, now, now, now))


def restore(conn, kind, record_id):
    """Puts a declined film back among the recommendations."""
    rec = store.get_record(conn, record_id)
    if rec is None or kind not in KINDS:
        raise VersionError("That file is not in the Library any more.")
    label = load_for(conn, rec).label if kind == VARIANT else ""
    conn.execute("DELETE FROM library_action WHERE kind = ? AND path_key = ? AND label = ? AND status = 'skipped'", (kind, rec["path_key"], label))


# ------------------------------------------------------------------------------------------- following the job
def get(conn, action_id):
    r = conn.execute("SELECT * FROM library_action WHERE id = ?", (action_id,)).fetchone()
    return dict(r) if r else None


def sync(conn, job_id, now=None):
    """Makes the action follow its job. Returns True when this call is the one that finished it (Jellyfin should be told)."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    row = get(conn, job["ref_id"])
    if row is None or row["job_id"] != job_id:
        return False
    target, now = JOB_STATUS.get(job["status"], "queued"), now or db.now()
    if row["status"] == target:
        return False
    values = {"status": target, "updated_at": now}
    if target == "done":
        result = job["result"] if isinstance(job["result"], dict) else {}
        values.update(result_json=json.dumps(result)[:4000], finished_at=now,
                      message=(f"Made a smaller copy ({round((result.get('bytes') or 0) / GIB, 1)} GB)." if row["kind"] == VARIANT
                               else "Converted to Dolby Vision 8.1; the original is kept on the worker." if result.get("kept_original") else "Converted to Dolby Vision 8.1; the original was not kept."))
    elif target in ("failed", "blocked"):
        values.update(message=(job["error"] or job["review_reason"] or job["message"] or ("Cancelled." if job["status"] == "cancelled" else ""))[:500], finished_at=now)
    elif target == "converting":
        values.update(message="A worker is converting it.", finished_at=None)
    else:
        values.update(message="Queued for a worker.", finished_at=None)
    conn.execute(f"UPDATE library_action SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), row["id"]))
    _sync_companion(conn, row, job, target, now)
    return target == "done"


def _sync_companion(conn, row, job, target, now):
    """The smaller copy made by a Dolby Vision job follows the job; when the job is done, the worker's report of the second output says whether the copy was made."""
    label = _json(row.get("options_json"), {}).get("companion_label")
    if row["kind"] != DV_REMUX or not label:
        return
    companion = action_for(conn, VARIANT, row["path_key"], label)
    if companion is None or companion["job_id"] != job["id"]:
        return
    values = {"status": target, "updated_at": now}
    if target == "done":
        made = ((job["result"] if isinstance(job["result"], dict) else {}).get("extra")) or {}
        if made.get("error") or not made.get("bytes"):
            values.update(status="failed", finished_at=now, message=("The smaller copy could not be made: " + (made.get("error") or "the worker did not report it") + " The Dolby Vision conversion itself is done. You can try the copy again.")[:500])
        else:
            values.update(finished_at=now, result_json=json.dumps(made)[:4000], message=f"Made a smaller copy ({round(made['bytes'] / GIB, 1)} GB).")
    elif target in ("failed", "blocked"):
        values.update(message=(job["error"] or job["review_reason"] or job["message"] or "")[:500], finished_at=now)
    elif target == "converting":
        values.update(message="A worker is converting it.", finished_at=None)
    else:
        values.update(message="Queued with the Dolby Vision conversion.", finished_at=None)
    conn.execute(f"UPDATE library_action SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), companion["id"]))


def sync_active(conn):
    """Brings every open action in step with its newest job (a job re-queued after its worker went silent, a retry from the Workers page...). Returns how many finished."""
    rows = conn.execute("SELECT job_id FROM library_action WHERE job_id IS NOT NULL AND status IN ('queued', 'converting', 'blocked', 'failed')").fetchall()
    return sum(1 for r in rows if sync(conn, r["job_id"]))


# ------------------------------------------------------------------------------------------- the tab
LABELS = {"queued": "Waiting for a worker", "converting": "Working", "done": "Done", "failed": "Failed", "blocked": "Needs you", "skipped": "Declined"}


def listing(conn):
    """Everything the Versions tab shows."""
    cfg = load(conn)
    rec = recommendations(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM library_action WHERE status != 'skipped' ORDER BY (status IN ('queued', 'converting', 'blocked')) DESC, updated_at DESC, id DESC LIMIT 80")]
    for r in rows:
        r["status_label"] = LABELS.get(r["status"], r["status"])
        r["name"] = os.path.basename(r["relative_path"])
        r["kind_label"] = f"{cfg.label} copy" if r["kind"] == VARIANT and r["label"] == cfg.label else (f"{r['label']} copy" if r["kind"] == VARIANT else "Dolby Vision 7 to 8.1")
        r["options"] = _json(r.get("options_json"), {})
        r["active"] = r["status"] in OPEN + ("blocked",)
    return {"config": dataclasses.asdict(cfg), "recommended": {k: rec[k] for k in KINDS}, "declined": rec["declined"], "actions": rows,
            "counts": {"variant": len(rec[VARIANT]), "dv_remux": len(rec[DV_REMUX]), "open": sum(1 for r in rows if r["active"])}}
