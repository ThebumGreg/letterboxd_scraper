"""Conversion profiles: how a file is converted when it is brought into the library.

A profile names the video codec and quality, the audio bitrate for audio that has to be re-encoded, and what happens to subtitles.
`move_only` copies the file exactly as it is. The built-in profiles cannot be changed; a person can make their own. The profile that
applies to a job is copied into the job's plan when it is queued, so a queued job is not affected by a later change.
"""
import re

from synchub import db, settings

VIDEO_CODECS = ("keep", "hevc", "h264")
SUBTITLE_POLICIES = ("remove_non_english", "keep_all")
SUBTITLE_LABELS = {"remove_non_english": "Remove subtitles in other languages", "keep_all": "Keep every subtitle"}
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
CRF_RANGE = {"hevc": (14, 35), "h264": (12, 32)}
DEFAULT_CRF = {"hevc": 23, "h264": 20}
AUDIO_KBPS_RANGE = (64, 640)
DEFAULT_PROFILE_ID = 1
FEATURE = "profiles"                       # what a worker's agent must list to run a plan that carries a profile

_NAME = re.compile(r"^[\w .,'()+&-]{1,60}$")
_COLUMNS = "id, name, description, video_codec, crf, preset, ten_bit, audio_kbps, subtitles, move_only, builtin, created_by"


class ProfileError(ValueError):
    """The profile cannot be saved or removed; the message is for the person using the page."""


def _row(r):
    p = dict(r)
    p.update(ten_bit=bool(p["ten_bit"]), move_only=bool(p["move_only"]), builtin=bool(p["builtin"]))
    p["summary"] = describe(p)
    return p


def describe(p):
    """One line saying what the profile does."""
    if p["move_only"]:
        return "Copies the file exactly as it is."
    video = {"keep": "Video kept as it is", "hevc": f"HEVC{' 10-bit' if p['ten_bit'] else ''}, quality {p['crf']}, {p['preset']}", "h264": f"H.264, quality {p['crf']}, {p['preset']}"}[p["video_codec"]]
    return f"{video}. {SUBTITLE_LABELS[p['subtitles']]}."


def all_profiles(conn):
    return [_row(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM conversion_profile ORDER BY builtin DESC, id")]


def get(conn, profile_id):
    r = conn.execute(f"SELECT {_COLUMNS} FROM conversion_profile WHERE id = ?", (profile_id,)).fetchone()
    return _row(r) if r else None


def default_id(conn=None):
    try:
        return int(settings.get("library_default_profile", conn=conn))
    except ValueError:
        return DEFAULT_PROFILE_ID


def default(conn):
    return get(conn, default_id(conn)) or get(conn, DEFAULT_PROFILE_ID)


def validate(data):
    """The cleaned fields of a profile from a form, or ProfileError."""
    name = str(data.get("name") or "").strip()
    if not _NAME.match(name):
        raise ProfileError("Give the profile a name of up to 60 letters, numbers and spaces.")
    codec = str(data.get("video_codec") or "keep")
    if codec not in VIDEO_CODECS:
        raise ProfileError("Choose keep, HEVC or H.264 for the video.")
    subtitles = str(data.get("subtitles") or "remove_non_english")
    if subtitles not in SUBTITLE_POLICIES:
        raise ProfileError("Choose what happens to subtitles.")
    out = {"name": name, "description": str(data.get("description") or "").strip()[:300] or None, "video_codec": codec, "subtitles": subtitles,
           "move_only": 1 if data.get("move_only") in (True, 1, "1", "true", "on") else 0, "crf": None, "preset": None, "ten_bit": 0}
    try:
        out["audio_kbps"] = int(data.get("audio_kbps") or 192)
    except (TypeError, ValueError):
        raise ProfileError("The audio bitrate must be a whole number.")
    if not AUDIO_KBPS_RANGE[0] <= out["audio_kbps"] <= AUDIO_KBPS_RANGE[1]:
        raise ProfileError(f"The audio bitrate must be between {AUDIO_KBPS_RANGE[0]} and {AUDIO_KBPS_RANGE[1]} kbps.")
    if out["move_only"]:
        out.update(video_codec="keep", subtitles="keep_all")
    if codec != "keep" and not out["move_only"]:
        low, high = CRF_RANGE[codec]
        try:
            crf = int(data.get("crf") if data.get("crf") not in (None, "") else DEFAULT_CRF[codec])
        except (TypeError, ValueError):
            raise ProfileError("The quality (CRF) must be a whole number.")
        if not low <= crf <= high:
            raise ProfileError(f"For {codec.upper() if codec == 'hevc' else 'H.264'} the quality must be between {low} and {high} (lower is better and larger).")
        preset = str(data.get("preset") or "medium")
        if preset not in PRESETS:
            raise ProfileError("Choose an encoder speed from the list.")
        out.update(crf=crf, preset=preset, ten_bit=1 if codec == "hevc" and data.get("ten_bit") in (True, 1, "1", "true", "on") else 0)
    return out


def save(conn, data, by=None, profile_id=None):
    """Creates a profile (or changes a custom one). Returns its id."""
    clean = validate(data)
    now = db.now()
    clash = conn.execute("SELECT id FROM conversion_profile WHERE name = ? AND id IS NOT ?", (clean["name"], profile_id)).fetchone()
    if clash:
        raise ProfileError(f"There is already a profile called '{clean['name']}'.")
    if profile_id is None:
        return conn.execute("INSERT INTO conversion_profile (name, description, video_codec, crf, preset, ten_bit, audio_kbps, subtitles, move_only, created_by, created_at, updated_at) "
                            "VALUES (:name, :description, :video_codec, :crf, :preset, :ten_bit, :audio_kbps, :subtitles, :move_only, :by, :now, :now)", {**clean, "by": by, "now": now}).lastrowid
    existing = get(conn, profile_id)
    if existing is None:
        raise ProfileError("That profile no longer exists.")
    if existing["builtin"]:
        raise ProfileError("The built-in profiles cannot be changed. Make a copy under a new name instead.")
    conn.execute("UPDATE conversion_profile SET name = :name, description = :description, video_codec = :video_codec, crf = :crf, preset = :preset, ten_bit = :ten_bit, "
                 "audio_kbps = :audio_kbps, subtitles = :subtitles, move_only = :move_only, updated_at = :now WHERE id = :id", {**clean, "now": now, "id": profile_id})
    return profile_id


def delete(conn, profile_id):
    existing = get(conn, profile_id)
    if existing is None:
        raise ProfileError("That profile no longer exists.")
    if existing["builtin"]:
        raise ProfileError("The built-in profiles cannot be deleted.")
    if default_id(conn) == profile_id:
        raise ProfileError("This is the default profile. Choose another default first.")
    waiting = conn.execute("SELECT COUNT(*) FROM library_file WHERE approved_profile_id = ? AND approval_status = 'approved' AND migration_status IN ('pending', 'in_progress', 'failed')", (profile_id,)).fetchone()[0]
    if waiting:
        raise ProfileError(f"{waiting} approved file(s) still use this profile. Change them first.")
    conn.execute("UPDATE library_file SET approved_profile_id = NULL WHERE approved_profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM conversion_profile WHERE id = ?", (profile_id,))


def for_plan(profile):
    """What goes into a job's plan: only the fields the worker needs."""
    return {"name": profile["name"], "video_codec": profile["video_codec"], "crf": profile["crf"], "preset": profile["preset"], "ten_bit": bool(profile["ten_bit"]),
            "audio_kbps": profile["audio_kbps"]}
