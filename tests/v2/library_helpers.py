"""Builds Library records the way a scan plus SyncHub's finishing step would, so tests can say what matters and leave the rest to defaults."""
import json

from synchub.library import findings, store
from synchub.library import vocab as v

CFG = v.LibraryConfig()                     # media, "Movies", "TV Shows"


def track(index, language="eng", codec="aac", kind="audio", **kw):
    base = {"index": index, "type": kind, "codec": codec, "lang": language, "title": "", "channels": 2, "default": index == 1, "forced": False,
            "hearing_impaired": False, "comment": False}
    base.update(kw)
    return base


def plan(remove_audio=(), remove_subtitle=(), keep_audio=(1,), keep_subtitle=(), default_audio=None, convert=None):
    """A proposed-track-changes plan like the worker's propose_track_changes makes."""
    ref = lambda t: {"index": t[0], "language": t[1], "title": ""}                     # noqa: E731
    out = {"op": "convert" if convert else "keep_tracks", "container": "matroska", "keep_audio": list(keep_audio), "remove_audio": [ref(t) for t in remove_audio],
           "keep_subtitle": list(keep_subtitle), "remove_subtitle": [ref(t) for t in remove_subtitle], "default_audio": default_audio, "default_audio_language": "eng" if default_audio else None}
    if convert:
        out.update({"source_container": "avi", "target_extension": ".mkv" if convert.get("remux", True) else None,
                    "video": {"action": convert.get("video", "copy"), "from": convert.get("from", "mpeg4"), "codec": "h264"}, "audio_convert": convert.get("audio", {})})
    return out


def analysis(root="tfiles", rel="Movies/Heat.1995.1080p.mkv", name="Heat (1995).mkv", *, size=1_000_000, mtime=1_700_000_000.0, media_type="Movie/Unknown", show=None,
             season=None, action="copy_rename", action_reason="", reasons=(), naming_flags="", naming_error=False, naming_label="High", naming_score=0.9,
             integrity=v.INT_TAIL, audit_error=None, container="matroska,webm", video="h264", audio=None, subtitles=(), proposals=None, english="present",
             status=v.ANALYSIS_COMPLETED, unreadable=None, sha256=None, run="run-1", logic=1, source_dir=None):
    """The record a worker reports for one file (before findings that depend on other files)."""
    audio = [track(1)] if audio is None else audio
    streams = audio + list(subtitles)
    findings_json = {"action": action, "action_reason": action_reason, "naming_flags": naming_flags, "naming_error": naming_error, "reasons": [list(r) for r in reasons]}
    rec = {"path_key": v.record_key(root, rel), "root_name": root, "relative_path": rel, "source_path": f"{root}:{rel}", "file_name": rel.rsplit("/", 1)[-1],
           "size_bytes": size, "mtime": mtime, "sha256": sha256, "analysis_status": status, "analysis_run_id": run, "analyzed_at": "2025-01-01 00:00:00",
           "analysis_logic_version": logic, "integrity_status": integrity, "integrity_mode": "tail", "audit_error": audit_error, "unreadable_reason": unreadable,
           "container": container, "video_codec": video, "resolution": "1920x1080", "duration_seconds": 6000.0, "audio_codecs": ", ".join(sorted({t["codec"] for t in audio})),
           "video_streams": 1, "audio_streams": len(audio), "subtitle_streams": len(subtitles), "streams_json": json.dumps(streams),
           "media_type": media_type, "recommended_name": name, "show_title": show, "season_number": season, "naming_confidence": naming_label, "naming_score": naming_score,
           "naming_flags": naming_flags or None, "duplicate_status": v.DUP_NOT_CHECKED, "recommended_action": action, "action_reason": action_reason or None,
           "english_audio_status": english, "proposed_track_changes_json": json.dumps(proposals) if proposals else None,
           "warnings_json": "[]", "errors_json": "[]", "findings_json": json.dumps(findings_json), "source_dir": source_dir or root}
    return rec


def finish(rec, notes=()):
    """Adds what SyncHub works out: the record as it is stored."""
    return {**rec, **findings.derive(rec, notes), "review_status": findings.derive(rec, notes)["new_review"]}


def add(conn, run="run-1", **kw):
    """Stores one record and returns its id."""
    rec = finish(analysis(run=run, **kw))
    store.upsert_records(conn, [rec], rec["source_dir"], run)
    conn.commit()
    return store.by_key(conn, rec["path_key"])["id"]


def refresh(conn, cfg=CFG):
    findings.refresh(conn, cfg)
    conn.commit()
