"""
Plain-language recommendations for the Library page.

For every record this module works out WHAT WE SUGGEST, in words a person can check at a
glance, how confident we are, and whether it can be approved with one click. The aim is
that a reviewer mostly just approves; only real exceptions need thought.

Groups (the queues in the UI):
  ready       nothing unusual: approve as is
  suggested   we have a plan (rename, remove foreign tracks, skip a duplicate): approve it
  check       a flag worth a look (e.g. no English audio): suggestion is "copy unchanged"
  convert     older formats: a lossless remux into MKV, or a re-encode (slower, lossy)
  needs_you   cannot be approved yet (unreadable, incomplete analysis, integrity problem...)
  done        already approved / rejected / migrated

This module only DECIDES and APPLIES decisions through store.set_approval(); it never
touches media files. (The original tool's media_recommend.py, over the Library tables. One addition: a file already at the
right place in the destination share is "done", because there is nothing to move.)
"""

import dataclasses
import json
import os
from typing import List, Optional

from synchub.library import findings, layout as media_layout, migrate, store
from synchub.library import vocab as db
from synchub.library.vocab import LibraryConfig

HIGH, MEDIUM, LOW = "high", "medium", "low"
_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}

G_READY, G_SUGGESTED, G_CHECK = "ready", "suggested", "check"
G_CONVERT, G_NEEDS, G_DONE = "convert", "needs_you", "done"
GROUP_ORDER = (G_READY, G_SUGGESTED, G_CHECK, G_CONVERT, G_NEEDS, G_DONE)

# Rough speed of a re-encode relative to the video's length (1.5 = 1.5x faster than real time); only used for estimates shown to people.
TRANSCODE_SPEED = 1.5
GROUP_TITLES = {
    G_READY: "Ready to approve", G_SUGGESTED: "Suggested changes", G_CHECK: "Worth a look",
    G_CONVERT: "Format conversion", G_NEEDS: "Needs you", G_DONE: "Done",
}
GROUP_HINTS = {
    G_READY: "Nothing unusual found. Approve and they are copied as they are.",
    G_SUGGESTED: "We have a plan for each file (a name, tracks to remove, a duplicate to skip). Check and approve.",
    G_CHECK: "A flag, such as no English audio. We suggest copying unchanged; look and approve.",
    G_CONVERT: "Older formats Jellyfin may have to transcode. We convert the copy to MKV/H.264: a lossless remux where possible, a re-encode (slower) where needed.",
    G_NEEDS: "These cannot be approved yet: unreadable files, incomplete scans, integrity problems.",
    G_DONE: "Approved, rejected or already migrated.",
}

LANGUAGES = {
    "eng": "English", "ita": "Italian", "fra": "French", "fre": "French", "deu": "German",
    "ger": "German", "spa": "Spanish", "por": "Portuguese", "jpn": "Japanese", "kor": "Korean",
    "zho": "Chinese", "chi": "Chinese", "yue": "Cantonese", "rus": "Russian", "hin": "Hindi",
    "nld": "Dutch", "dut": "Dutch", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "pol": "Polish", "ces": "Czech", "cze": "Czech", "hun": "Hungarian",
    "ell": "Greek", "gre": "Greek", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew",
    "tha": "Thai", "und": "Unknown", "": "Unknown",
}

HANDLED = {"naming", "duplicate", "tracks", "convert", "audio", "subtitle"}
OK_CONTAINERS = ("matroska", "mp4")
OK_VIDEO = ("h264", "hevc", "vp9", "av1")
OK_AUDIO = ("aac", "ac3", "eac3", "mp3", "opus", "flac", "vorbis")
IMAGE_SUBS = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "vobsub")


@dataclasses.dataclass
class Recommendation:
    group: str
    kind: str                       # plain|tracks|naming|keep_duplicate|skip_duplicate|check|blocked|waiting|done
    decision: str                   # approve | reject | none
    headline: str
    steps: List[str]
    notes: List[str]
    confidence: str
    can_approve: bool
    accept_tracks: bool = False
    action: Optional[str] = None
    path: Optional[str] = None      # where it will go in the library (relative)
    mode: Optional[str] = None      # 'rename_only': approving renames the file inside its share and changes nothing else
    needs_name: bool = False        # a valid name must be entered before approving
    override_allowed: bool = False  # "I tested it" override (integrity only)
    badges: list = dataclasses.field(default_factory=list)
    removals: list = dataclasses.field(default_factory=list)   # [{index,kind,language}] for the track editor


def categories(rec):
    return {c.strip() for c in (rec.get("review_categories") or "").split(";") if c.strip()}


def language_name(code):
    return LANGUAGES.get((code or "").lower(), (code or "Unknown"))


def _json(value):
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


def format_badges(rec):
    """What Jellyfin clients like: label, ok?, hint. Informational for now."""
    badges = []
    container = (rec.get("container") or "").lower()
    kind = "MKV" if "matroska" in container else "MP4" if "mp4" in container else (container.split(",")[0].upper() or "?")
    badges.append({"label": f"Container {kind}", "ok": any(c in container for c in OK_CONTAINERS),
                   "hint": "MKV and MP4 are the safest containers for Jellyfin."})
    video = (rec.get("video_codec") or "").lower()
    badges.append({"label": f"Video {video or '?'}", "ok": any(v in video for v in OK_VIDEO),
                   "hint": "H.264 plays almost everywhere; older codecs usually force transcoding."})
    audio = [a.strip().lower() for a in (rec.get("audio_codecs") or "").split(",") if a.strip()]
    badges.append({"label": f"Audio {', '.join(audio) or '?'}",
                   "ok": bool(audio) and all(a in OK_AUDIO for a in audio),
                   "hint": "AAC/AC3/EAC3 play on most devices; DTS/TrueHD are often transcoded."})
    subs = {s.get("codec", "") for s in _json(rec.get("streams_json")) if s.get("type") == "subtitle"}
    image = sorted(s for s in subs if s in IMAGE_SUBS)
    if image:
        badges.append({"label": "Image subtitles", "ok": False,
                       "hint": "Image subtitles (PGS/VobSub) often force video transcoding."})
    return badges


def _relative_path(rec, cfg):
    try:
        return media_layout.relative_display(rec, cfg), None
    except media_layout.LayoutError as exc:
        return None, str(exc)


def _confidence_from_naming(rec):
    label = (rec.get("naming_confidence") or "").lower()
    if "collision" in (rec.get("naming_flags") or "").lower():
        return LOW
    return {"high": HIGH, "medium": MEDIUM}.get(label, LOW)


def _lowest(*levels):
    return min(levels, key=lambda lv: _RANK[lv])


def estimate_text(rec, mode):
    """A rough, honest time estimate shown to people (never used for decisions)."""
    minutes = (rec.get("duration_seconds") or 0) / 60
    if mode == "transcode" and minutes:
        hours = minutes / TRANSCODE_SPEED / 60
        return (f"Rough time: about {hours * 60:.0f} minutes" if hours < 1.5 else f"Rough time: about {hours:.1f} hours") \
            + f" (assumes {TRANSCODE_SPEED:g}x real time)."
    size = (rec.get("size_bytes") or 0) / 1024 ** 3
    return f"Rough time: reads the whole file once ({size:.1f} GB), a few minutes on a fast connection."


def recommend(rec, cfg=None) -> Recommendation:
    """The suggestion for one record (a dict from the library_file table)."""
    cfg = cfg or LibraryConfig()
    cats = categories(rec)
    path, path_error = _relative_path(rec, cfg)
    badges = format_badges(rec)
    reasons = [text for _, text in _json(rec.get("review_reasons_json")) if text]
    base = dict(path=path, badges=badges)

    # ---- already decided --------------------------------------------------------------------
    if rec["migration_status"] != db.MIGRATION_PENDING:
        label = {"completed": "Migrated", "in_progress": "Being migrated now", "failed": "Migration failed",
                 "manual_review": "Migration needs a person"}[rec["migration_status"]]
        notes = [rec["migration_last_error"]] if rec.get("migration_last_error") else []
        return Recommendation(G_DONE, "done", "none", label, [], notes, HIGH, False, **base)
    if rec["approval_status"] == db.APPROVAL_APPROVED:
        steps = (["Will be renamed inside its share. Nothing is copied and nothing in the file changes."] if rec.get("approved_mode") == "rename_only"
                 else ["Will be copied" + (" with the approved track changes" if rec.get("approved_track_changes_json") else " unchanged") + ", using the conversion profile"])
        return Recommendation(G_DONE, "done", "none", "Approved for migration", steps, [], HIGH, False, **base)
    if rec["approval_status"] == db.APPROVAL_REJECTED:
        return Recommendation(G_DONE, "done", "none", "Rejected: will not be migrated", [], [], HIGH, False, **base)
    from synchub import versions                       # imported here: versions uses the Library, not the other way round
    if rec["root_name"] == cfg.destination_root and versions.is_version_file(rec["file_name"]):
        return Recommendation(G_DONE, "version", "none", "Another version of a film: left as it is",
                              ["A file named like “Film (Year) - 1080p” is shown by Jellyfin as another version of the film next to it, so it must keep its name and place."], [], HIGH, False, **base)

    # ---- cannot be approved ------------------------------------------------------------------------
    if rec.get("unreadable_reason"):
        return Recommendation(G_NEEDS, "blocked", "none", "Cannot read this file",
                              ["Fix access to the file, then run the scan again"],
                              [rec["unreadable_reason"]], LOW, False, **base)
    if rec.get("missing_since"):
        return Recommendation(G_NEEDS, "blocked", "none", "File not found at the source anymore",
                              ["Restore the file or reject this record"], [], LOW, False, **base)
    if rec["analysis_status"] != db.ANALYSIS_COMPLETED:
        why = rec.get("audit_error") or rec.get("integrity_status") or rec["analysis_status"]
        return Recommendation(G_NEEDS, "blocked", "none", "The scan of this file did not finish",
                              ["Run the scan again (the same record is updated)"], [str(why)], LOW, False, **base)
    if (cats - HANDLED) or (rec["review_status"] == db.REVIEW_MANUAL and not cats):
        notes = reasons or ["The scan flagged this file for a playback check."]
        return Recommendation(G_NEEDS, "blocked", "none", "Needs a playback test",
                              ["Play the file; if it is fine use 'I tested it', otherwise reject it"],
                              notes, LOW, False, override_allowed=True, **base)

    if path_error and rec["media_type"] == "TV Episode" and (
            not rec.get("show_title") or rec.get("season_number") is None):
        return Recommendation(G_NEEDS, "blocked", "none", "Cannot place this episode",
                              ["Reject it, or fix the file name at the source and scan again"],
                              [path_error], LOW, False, **base)

    if findings.in_place(rec, cfg):
        return Recommendation(G_DONE, "in_place", "none", "Already in the library, in the right place", [], [], HIGH, False, **base)

    action = (rec["recommended_action"] if rec["recommended_action"] in db.MIGRATABLE_ACTIONS
              else rec.get("base_action"))
    plans = [p for p in _json(rec.get("proposed_track_changes_json")) if p.get("op") in ("keep_tracks", "convert")]
    if action in (db.ACTION_REMUX_COPY, db.ACTION_TRANSCODE_COPY) and not any(p["op"] == "convert" for p in plans):
        return Recommendation(G_NEEDS, "blocked", "none", "No conversion plan for this older format",
                              ["Run the scan again so a conversion plan is created"], [rec.get("action_reason") or ""],
                              LOW, False, action=action, **base)

    # ---- duplicates --------------------------------------------------------------------------------
    status = (rec.get("duplicate_status") or "").lower()
    if rec.get("duplicate_group") and "review before removal" in status:
        return Recommendation(G_SUGGESTED, "skip_duplicate", "reject", "Skip this duplicate",
                              ["Do not migrate this copy: it is identical to another file we suggest keeping"],
                              reasons, MEDIUM, True, **base)

    # ---- build the plan -----------------------------------------------------------------------------
    steps, levels = [], []
    notes = [t for t in reasons if not t.startswith("Proposed:")]      # the steps already say this
    kind, accept_tracks, removals = "plain", False, []
    needs_name = path_error is not None
    for plan in plans:
        accept_tracks = True
        if plan["op"] == "convert":
            kind = "convert"
            video = plan["video"]
            if video["action"] == "transcode":
                steps.append(f"Re-encode the video ({video['from']} → H.264). This is lossy and takes longer.")
                levels.append(MEDIUM)
                notes.append(estimate_text(rec, "transcode"))
            elif plan.get("target_extension"):
                steps.append(f"Remux {plan['source_container']} → MKV: lossless, nothing is re-encoded.")
                levels.append(HIGH)
                notes.append(estimate_text(rec, "remux"))
            for index, audio in plan["audio_convert"].items():
                steps.append(f"Convert audio #{index} ({audio['from']} → AAC)")
                levels.append(MEDIUM)
        elif kind == "plain":
            kind = "tracks"
        gone_audio, gone_subs = plan["remove_audio"], plan["remove_subtitle"]
        wanted_gone = [t for t in gone_subs if not t.get("reason")]
        forced_gone = [t for t in gone_subs if t.get("reason")]
        if gone_audio:
            steps.append("Remove audio: " + ", ".join(f"#{t['index']} {language_name(t['language'])}" for t in gone_audio))
        if wanted_gone:
            steps.append("Remove subtitles: " + ", ".join(f"#{t['index']} {language_name(t['language'])}" for t in wanted_gone))
        if forced_gone:
            steps.append("Drop subtitles MKV cannot hold: " + ", ".join(f"#{t['index']}" for t in forced_gone))
        if plan.get("default_audio") is not None:
            steps.append(f"Keep English audio #{plan['default_audio']} as the default")
        removals += ([{"index": t["index"], "kind": "audio", "language": language_name(t["language"]), "locked": False} for t in gone_audio]
                     + [{"index": t["index"], "kind": "subtitle", "language": language_name(t["language"]),
                         "locked": bool(t.get("reason"))} for t in gone_subs])
        if gone_audio:
            levels.append(MEDIUM)
        elif gone_subs and plan["op"] != "convert":
            levels.append(HIGH)
    if "naming" in cats or needs_name:
        kind = "naming" if kind == "plain" else kind
        steps.insert(0, f"Use the name: {rec['recommended_name']}")
        levels.append(LOW if needs_name else _confidence_from_naming(rec))
    if cats & {"audio", "subtitle"}:
        kind = "check" if kind == "plain" else kind
        steps.append("Copy the remaining tracks unchanged")
        status_en = rec.get("english_audio_status")
        if status_en == db.ENGLISH_NONE:
            notes.insert(0, "No English audio track was found (every audio track is another language).")
            levels.append(MEDIUM)
        else:
            levels.append(LOW if status_en in (db.ENGLISH_UNKNOWN, db.ENGLISH_NO_AUDIO) or "audio" in cats else MEDIUM)
    if rec.get("duplicate_group") and "primary candidate" in status:
        kind = "keep_duplicate" if kind in ("plain", "check", "naming") else kind
        steps.append("Keep this copy and skip the other identical copies")
        levels.append(MEDIUM)
    if not steps:
        steps = ["Copy as is"]

    group = (G_READY if kind == "plain" else G_CHECK if kind == "check" else
             G_CONVERT if kind == "convert" else G_SUGGESTED)
    convert_plan = next((p for p in plans if p["op"] == "convert"), None)
    convert_headline = ("Re-encode to H.264 in MKV" if convert_plan and convert_plan["video"]["action"] == "transcode"
                        else "Convert audio to AAC" if convert_plan and convert_plan["audio_convert"]
                        else "Remux to MKV (lossless)")
    headline = {"plain": "Looks good: ready to copy", "convert": convert_headline, "tracks": "Keep English tracks only",
                "naming": f"Rename to “{rec['recommended_name']}”", "check": "Copy unchanged (worth a look)",
                "keep_duplicate": "Keep this copy"}[kind]
    if needs_name:
        notes.insert(0, f"No valid destination yet: {path_error}. Enter a valid name to continue.")
    confidence = _lowest(HIGH, *levels) if levels else HIGH
    if (rec["root_name"] == cfg.destination_root and path and kind in ("plain", "naming") and not plans and action == db.ACTION_COPY_RENAME and not needs_name):
        # already on the destination share, just not at the right path: moving it inside the share beats copying it next to itself
        return Recommendation(G_SUGGESTED, "rename", "approve", f"Rename in place to “{db.final_name(rec)}”",
                              [f"Move it inside {rec['root_name']} to {path}. Nothing is copied and nothing in the file changes."], notes, confidence, True,
                              action=action, mode="rename_only", **base)
    can_approve = action in db.MIGRATABLE_ACTIONS and not (needs_name)
    return Recommendation(group, kind, "approve", headline, steps, notes, confidence, can_approve,
                          accept_tracks=accept_tracks, action=action, needs_name=needs_name,
                          removals=removals, **base)


# ---------------------------------------------------------------------------------------- queues

def _sort_key(rec):
    return ((rec.get("show_title") or rec.get("recommended_name") or rec["file_name"]).casefold(),
            rec.get("season_number") if rec.get("season_number") is not None else -1,
            rec["source_path"].casefold())


def all_records(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM library_file")]


def classify(conn, cfg=None, match=None, root=None):
    """[(record, Recommendation)] for every record (optionally filtered), in display order."""
    cfg = cfg or LibraryConfig()
    rows = []
    for rec in sorted(all_records(conn), key=_sort_key):
        if match and match.lower() not in (rec["source_path"] + " " + rec["file_name"]).lower():
            continue
        if root and rec.get("root_name") != root:
            continue
        rows.append((rec, recommend(rec, cfg)))
    return rows


def counts(conn, cfg=None, rows=None):
    """{group: {'total': n, 'high': n, 'medium': n, 'low': n}} for the overview."""
    table = {g: {"total": 0, HIGH: 0, MEDIUM: 0, LOW: 0} for g in GROUP_ORDER}
    for _, r in (rows if rows is not None else classify(conn, cfg)):
        table[r.group]["total"] += 1
        table[r.group][r.confidence] += 1
    return table


# ----------------------------------------------------------------------------------- applying

def _validate_name(rec, name, cfg):
    try:
        media_layout.relative_parts(dict(rec, approved_name=name), cfg)
    except media_layout.LayoutError as exc:
        raise ValueError(f"that name cannot be used: {exc}")


def rename_path(rec, cfg):
    """Where a rename-only job would put this file (inside its own share), or None when it has no valid layout path or is already there."""
    path, error = _relative_path(rec, cfg)
    if error or not path:
        return None
    path = os.path.splitext(path)[0] + os.path.splitext(rec["relative_path"])[1]
    return None if db.clean_relative(rec["relative_path"]).casefold() == path.casefold() else path


def apply_recommendation(conn, record_id, by, name=None, keep_streams=None, note=None, cfg=None, mode=None, profile_id=None):
    """Do what the recommendation says (approve, or reject a duplicate). `name` edits the
    filename (required when the recommendation says a valid name is needed);
    `keep_streams` keeps tracks the plan would have removed. `mode` 'rename_only' renames the file inside its share instead of copying it; `profile_id` is
    the conversion profile for this file (the default one when None). Raises ValueError."""
    cfg = cfg or LibraryConfig()
    rec = store.get_record(conn, record_id)
    if rec is None:
        raise ValueError(f"no record {record_id}")
    r = recommend(rec, cfg)
    if r.needs_name:
        if not name:
            raise ValueError("this file needs a valid name before it can be approved")
    elif not r.can_approve:
        raise ValueError(r.headline)
    if name and name.strip() and name.strip() != rec["recommended_name"]:
        name = name.strip()
        _validate_name(rec, name, cfg)
    else:
        name = None
    if r.needs_name and name is None:
        raise ValueError("enter a name different from the recommended one")
    note = note or "approved on the Library page"
    if r.kind == "skip_duplicate":
        store.set_approval(conn, record_id, db.APPROVAL_REJECTED, by, note="duplicate: skipped")
        return "rejected"
    mode = r.mode if mode is None else (None if mode == "copy" else mode)       # "copy" says: not the rename the recommendation suggested
    if mode not in (None, "rename_only"):
        raise ValueError("unknown way of handling the file")
    if mode == "rename_only":
        probe = dict(rec, approved_name=name) if name else rec
        if rename_path(probe, cfg) is None:
            raise ValueError("this file is already at its place, or has no valid name to rename it to")
    if profile_id is not None and conn.execute("SELECT 1 FROM conversion_profile WHERE id = ?", (profile_id,)).fetchone() is None:
        raise ValueError("that conversion profile no longer exists")
    if r.kind == "keep_duplicate":
        return _keep_duplicate(conn, rec, r, by, name, keep_streams, note, cfg, mode, profile_id)
    store.set_approval(conn, record_id, db.APPROVAL_APPROVED, by, action=r.action, name=name, note=note,
                    mark_reviewed=rec["review_status"] == db.REVIEW_MANUAL,
                    accept_track_changes=r.accept_tracks and mode != "rename_only", keep_streams=keep_streams, profile_id=profile_id, mode=mode)
    migrate.queue(conn, record_id, cfg, by)
    return "approved"


def _keep_duplicate(conn, rec, r, by, name, keep_streams, note, cfg, mode=None, profile_id=None):
    siblings = [dict(s) for s in conn.execute(
        "SELECT * FROM library_file WHERE duplicate_group=? AND id != ?", (rec["duplicate_group"], rec["id"]))]
    busy = [s["id"] for s in siblings if s["migration_status"] in (db.MIGRATION_IN_PROGRESS, db.MIGRATION_COMPLETED)]
    if busy:
        raise ValueError(f"duplicate(s) {busy} are being or already migrated; nothing changed")
    for sibling in siblings:                       # approved siblings first so names cannot clash
        if sibling["approval_status"] == db.APPROVAL_APPROVED:
            store.set_approval(conn, sibling["id"], db.APPROVAL_REJECTED, by, note=f"duplicate of {rec['id']}")
    store.set_approval(conn, rec["id"], db.APPROVAL_APPROVED, by, action=r.action, name=name, note=note,
                    mark_reviewed=rec["review_status"] == db.REVIEW_MANUAL,
                    accept_track_changes=r.accept_tracks and mode != "rename_only", keep_streams=keep_streams, profile_id=profile_id, mode=mode)
    for sibling in siblings:
        if sibling["approval_status"] != db.APPROVAL_REJECTED:
            store.set_approval(conn, sibling["id"], db.APPROVAL_REJECTED, by, note=f"duplicate of {rec['id']}")
    migrate.queue(conn, rec["id"], cfg, by)
    return f"kept; skipped {len(siblings)} duplicate(s)"


def approve_after_testing(conn, record_id, by, note, action=db.ACTION_COPY_RENAME, cfg=None):
    """The 'I tested it' override for a playback/integrity hold. A note is required."""
    rec = store.get_record(conn, record_id)
    if rec is None or not recommend(rec, cfg).override_allowed:
        raise ValueError("this record does not need a playback test")
    if not (note or "").strip():
        raise ValueError("please write a short note about what you tested")
    store.set_approval(conn, record_id, db.APPROVAL_APPROVED, by, action=rec.get("base_action") or action,
                    note=f"tested by hand: {note.strip()}", mark_reviewed=True)
    migrate.queue(conn, record_id, cfg or LibraryConfig(), by)
    return "approved"


def reject(conn, record_id, by, note=None):
    store.set_approval(conn, record_id, db.APPROVAL_REJECTED, by, note=note or "rejected on the Library page")


def undo(conn, record_id, by):
    store.set_approval(conn, record_id, db.APPROVAL_PENDING, by, note="decision undone on the Library page")


def bulk_candidates(conn, group, min_confidence=MEDIUM, match=None, root=None, cfg=None):
    """Records in `group` that can safely be approved together: one-click approvable and at
    least `min_confidence`. Low-confidence records are never included."""
    floor = max(_RANK[min_confidence], _RANK[MEDIUM])        # low confidence is never bulk-approved, whatever is asked for
    return [(rec, r) for rec, r in classify(conn, cfg, match, root)
            if r.group == group and r.can_approve and not r.needs_name and _RANK[r.confidence] >= floor]


def bulk_approve(conn, group, by, min_confidence=MEDIUM, match=None, root=None, cfg=None):
    """Apply the recommendation to every candidate. Returns (done, [(id, why)]).
    Duplicate groups are skipped: they need a person to choose which copy to keep."""
    done, skipped = 0, []
    for rec, r in bulk_candidates(conn, group, min_confidence, match, root, cfg):
        if r.kind in ("keep_duplicate", "skip_duplicate"):
            skipped.append((rec["id"], "duplicates are decided one at a time"))
            continue
        try:
            apply_recommendation(conn, rec["id"], by, note=f"bulk approved on the Library page ({group})", cfg=cfg)
            done += 1
        except (ValueError, db.StateError) as exc:
            skipped.append((rec["id"], str(exc)))
    return done, skipped


def next_record_id(conn, group, after_id=None, match=None, root=None, cfg=None):
    """Id of the record to show next in `group`. With `after_id` still in the group (the
    reviewer skipped it) this is the following one (wrapping); otherwise the first
    undecided record. None if the group is empty."""
    ids = [rec["id"] for rec, r in classify(conn, cfg, match, root) if r.group == group]
    if not ids:
        return None
    if after_id in ids:
        position = ids.index(after_id) + 1
        return ids[position] if position < len(ids) else ids[0]
    return ids[0]