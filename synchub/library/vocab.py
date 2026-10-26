"""Words, rules and small helpers shared by the Library (the original tool's `media_db` vocabulary, `media_roots` identity rules and settings)."""
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass

# analysis_status: written only by scans
ANALYSIS_PENDING, ANALYSIS_COMPLETED, ANALYSIS_INCOMPLETE, ANALYSIS_FAILED = "pending", "completed", "incomplete", "failed"
# review_status: a scan sets not_required or manual_review; a person sets reviewed
REVIEW_NOT_REQUIRED, REVIEW_MANUAL, REVIEW_REVIEWED = "not_required", "manual_review", "reviewed"
# approval_status: people only
APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED = "pending", "approved", "rejected"
# migration_status: the processing jobs only (a later phase)
MIGRATION_PENDING, MIGRATION_IN_PROGRESS, MIGRATION_COMPLETED, MIGRATION_FAILED, MIGRATION_MANUAL = "pending", "in_progress", "completed", "failed", "manual_review"

ACTION_COPY_RENAME = "copy_rename"          # byte-for-byte copy
ACTION_REMUX_COPY = "remux_copy"            # lossless container change, nothing re-encoded
ACTION_TRANSCODE_COPY = "transcode_copy"    # video and/or audio re-encoded (lossy, slow)
ACTION_MANUAL_REVIEW = "manual_review"
MIGRATABLE_ACTIONS = (ACTION_COPY_RENAME, ACTION_REMUX_COPY, ACTION_TRANSCODE_COPY)

CONVERT_VIDEO, CONVERT_AUDIO = "h264", "aac"

ENGLISH_PRESENT, ENGLISH_NONE, ENGLISH_UNKNOWN, ENGLISH_NO_AUDIO = "present", "none", "unknown", "no_audio"

# Integrity statuses say exactly what was proven.
INT_METADATA = "Not Checked (Metadata Only)"
INT_TAIL = "Tail Sample Passed (Partial)"
INT_FULL = "Full Decode Passed"
INT_DECODE_ERROR = "Decode Error (Review)"
INT_TIMEOUT = "Not Verified (Timeout)"
INT_IO_ERROR = "Not Verified (I/O Error)"
INT_AUDIT_ERROR = "Audit Error"
INT_BAD = (INT_DECODE_ERROR, INT_TIMEOUT, INT_IO_ERROR, INT_AUDIT_ERROR)

DUP_NOT_CHECKED = "Not Checked"
DUP_NONE = "No exact duplicate found"
DUP_RULED_OUT = "No exact duplicate found (partial read differs)"
DUP_INCOMPLETE = "Hash incomplete (error); duplicate status unknown"
DUP_PRIMARY = "Exact duplicate group (primary candidate)"
DUP_OTHER = "Exact duplicate candidate (review before removal)"

# Migration stages, cheapest and safest first.
STAGE_COPY, STAGE_TRACKS, STAGE_REMUX, STAGE_TRANSCODE = "copy", "tracks", "remux", "transcode"
STAGES = (STAGE_COPY, STAGE_TRACKS, STAGE_REMUX, STAGE_TRANSCODE)
STAGE_TITLES = {STAGE_COPY: "Copy unchanged", STAGE_TRACKS: "Copy with audio/subtitle changes", STAGE_REMUX: "Lossless remux to MKV", STAGE_TRANSCODE: "Convert (re-encode)"}

# The version of the worker's analysis logic. A record analysed by older logic is analysed again at the next scan.
ANALYSIS_LOGIC_VERSION = 2
MTIME_TOLERANCE_SECONDS = 2.0       # SMB and FAT timestamp granularity

DEFAULT_EXCLUDES = ("@eaDir", "#recycle", ".Recycle.Bin", "$RECYCLE.BIN", "System Volume Information", "lost+found", ".Trash-*")
INTEGRITY_MODES = ("metadata", "tail", "full")

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class LibraryError(Exception):
    """Something that cannot be done; the message is meant for the person using the page."""


class StateError(LibraryError):
    """A change was refused because the record is not in the expected state."""


@dataclass(frozen=True)
class LibraryConfig:
    """Where approved files go and how they are laid out. The destination is one share: the point is to bring everything into it."""
    destination_root: str = "media"
    tv_folder: str = "TV Shows"
    movie_folder: str = "Movies"


# FLAG_TIERS from the original tool: info = never needs review; change = a deterministic, approvable change; hold = a person decides.
FLAG_TIERS = {
    "AUDIO_MULTIPLE_STREAMS": "info", "AUDIO_MULTIPLE_NO_ENGLISH": "info", "SUB_NO_ENGLISH": "info", "SUB_FORCED_PRESENT": "info",
    "SUB_NON_ENGLISH_OPTIONAL_REMOVAL": "info", "SUB_ENGLISH_DEFAULT_FLAG": "info", "LANG_INFERRED_FROM_TITLE": "info", "AUDIO_ASSUMED_ENGLISH": "info",
    "AUDIO_DEFAULT_NON_ENGLISH": "change",
    "AUDIO_NONE": "hold", "AUDIO_NO_ENGLISH": "hold", "AUDIO_DEFAULT_LANG_UNKNOWN": "hold", "AUDIO_ENGLISH_UNCONFIRMED": "hold",
    "SUB_UNKNOWN_LANGUAGE": "hold", "SUB_ENGLISH_MARKED_FORCED": "hold",
}


def filename_problems(name):
    """Problems that make a filename unusable on Windows and SMB shares (an empty list is fine)."""
    problems = []
    stem = os.path.splitext(name)[0]
    if not name.strip():
        return ["name is empty"]
    if _INVALID_FILENAME_CHARS.search(name):
        problems.append('invalid character(s) (< > : " / \\ | ? *)')
    if name != name.strip() or stem != stem.strip() or stem.endswith("."):
        problems.append("leading/trailing space or trailing dot")
    if stem.split(".")[0].upper() in _RESERVED_NAMES:
        problems.append(f"reserved Windows name '{stem}'")
    if len(name.encode("utf-8")) > 255:
        problems.append("longer than 255 bytes")
    return problems


def final_name(record):
    """The filename processing must use: the approved (possibly edited) name when set, otherwise the recommended one."""
    return record.get("approved_name") or record.get("recommended_name")


def decision_fingerprint(rec):
    """Hash of everything an approval depends on. If a new analysis changes it, an existing approval is no longer trustworthy and is reset."""
    payload = [rec.get("recommended_action"), rec.get("recommended_name"), rec.get("media_type"), rec.get("show_title"), rec.get("season_number"),
               rec.get("proposed_track_changes_json")]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- identity: share name + relative path
def clean_relative(relative):
    return unicodedata.normalize("NFC", str(relative or "").replace("\\", "/").strip("/"))


def record_key(root_name, relative):
    """THE RECORD IDENTITY RULE: share name + relative path, Unicode-NFC normalised and case-folded (SMB is case-insensitive)."""
    return f"{root_name.lower()}|{clean_relative(relative).casefold()}"


def under(relative, prefix):
    """Is `relative` equal to, or inside, `prefix` (both '/'-separated, compared case-insensitively)?"""
    rel, pre = clean_relative(relative).casefold(), clean_relative(prefix).casefold()
    return not pre or rel == pre or rel.startswith(pre + "/")


def json_list(value):
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []
