"""
The ONE CSV report format: the original tool's columns, for the current state of the Library. It is a REPORT: nothing reads it back as
migration instructions. There are no destination-path columns.
"""

import csv
import io
import json
from datetime import datetime


def _join_json_list(value):
    if not value:
        return ""
    try:
        return " | ".join(str(v) for v in json.loads(value))
    except (TypeError, ValueError):
        return str(value)


def _track_changes_text(value):
    if not value:
        return ""
    try:
        parts = []
        for change in json.loads(value):
            if change.get("op") == "keep_tracks":
                gone = [f"audio #{t['index']} {t['language'] or 'und'}" for t in change["remove_audio"]]
                gone += [f"subtitle #{t['index']} {t['language'] or 'und'}" for t in change["remove_subtitle"]]
                parts.append("keep English only; remove " + ", ".join(gone)
                             + f"; default audio #{change['default_audio']}")
            else:
                parts.append(f"{change.get('op')}: audio #{change.get('stream_index')} "
                             f"({change.get('language') or 'und'})")
        return " | ".join(parts)
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _unreadable(record):
    return record.get("unreadable_reason") is not None and not record.get("size_bytes")


def _size_bytes(record):
    return "" if _unreadable(record) else record.get("size_bytes")


def _gb_text(record):
    size = record.get("size_bytes")
    return "" if size is None or _unreadable(record) else round(size / (1024 ** 3), 3)


def _mtime_text(record):
    mtime = record.get("mtime")
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else ""


# (header, database column | callable(record))
CSV_COLUMNS = [
    ("Record ID", "id"), ("Root", "root_name"), ("Relative Path", "relative_path"),
    ("Source Path", "source_path"), ("Current Name", "file_name"),
    ("Suggested Clean Name", "recommended_name"), ("Approved Name", "approved_name"),
    ("Media Type", "media_type"), ("Show Title", "show_title"), ("Season", "season_number"),
    ("File Size (Bytes)", _size_bytes), ("File Size (GB)", _gb_text),
    ("Modified Time", _mtime_text), ("Source Revision", "source_revision"),
    ("Missing Since", "missing_since"), ("Unreadable Reason", "unreadable_reason"),
    ("Duration (Seconds)", "duration_seconds"), ("Bitrate (kbps)", "bitrate_kbps"),
    ("Container", "container"), ("Video Codec", "video_codec"),
    ("Resolution", "resolution"), ("Frame Rate", "frame_rate"),
    ("English Audio", "english_audio_status"),
    ("Audio Codecs", "audio_codecs"), ("Audio Languages", "audio_languages"),
    ("Subtitle Languages", "subtitle_languages"),
    ("Video Streams", "video_streams"), ("Audio Streams", "audio_streams"),
    ("Subtitle Streams", "subtitle_streams"),
    ("SHA-256", "sha256"), ("Duplicate Group", "duplicate_group"),
    ("Duplicate Status", "duplicate_status"),
    ("Integrity Mode", "integrity_mode"), ("Integrity Status", "integrity_status"),
    ("Integrity Scope", "integrity_scope"),
    ("Naming Confidence", "naming_confidence"),
    ("Naming Confidence Score", "naming_score"), ("Naming Flags", "naming_flags"),
    ("Default Audio Language", "default_audio_language"), ("Audio Detail", "audio_detail"),
    ("Audio Flags", "audio_flags"), ("Audio Recommendation", "audio_recommendation"),
    ("Subtitle Detail", "subtitle_detail"), ("Subtitle Flags", "subtitle_flags"),
    ("Subtitle Recommendation", "subtitle_recommendation"),
    ("Proposed Track Changes", lambda r: _track_changes_text(r.get("proposed_track_changes_json"))),
    ("Approved Track Changes", lambda r: _track_changes_text(r.get("approved_track_changes_json"))),
    ("Analysis Status", "analysis_status"),
    ("Recommended Action", "recommended_action"), ("Action Reason", "action_reason"),
    ("Review Status", "review_status"), ("Review Categories", "review_categories"),
    ("Review Reason", "review_reason"),
    ("Review Decided By", "review_decided_by"), ("Review Decided At", "review_decided_at"),
    ("Approval Status", "approval_status"), ("Approved Action", "approved_action"),
    ("Approval Decided By", "approval_decided_by"), ("Approval Decided At", "approval_decided_at"),
    ("Approval Note", "approval_note"),
    ("Migration Status", "migration_status"),
    ("Migration Attempts", "migration_attempt_count"),
    ("Migration Last Error", "migration_last_error"),
    ("Migration Verification", "migration_verification_status"),
    ("Migration Finished At", "migration_finished_at"),
    ("Warnings", lambda r: _join_json_list(r.get("warnings_json"))),
    ("Errors", lambda r: _join_json_list(r.get("errors_json"))),
    ("Cache Status", "cache_status"), ("Analyzed At", "analyzed_at"),
    ("Scan Run ID", "analysis_run_id"),
]


def write_csv(stream, records):
    """Writes the header and one row per record (dicts keyed by library_file column names) to a text stream."""
    writer = csv.writer(stream)
    writer.writerow([header for header, _ in CSV_COLUMNS])
    for record in records:
        row = []
        for _, source in CSV_COLUMNS:
            value = source(record) if callable(source) else record.get(source)
            row.append("" if value is None else value)
        writer.writerow(row)


def csv_text(records):
    """The whole report as text (with a byte-order mark, so a spreadsheet reads the encoding right)."""
    buffer = io.StringIO()
    buffer.write("\ufeff")
    write_csv(buffer, records)
    return buffer.getvalue()
