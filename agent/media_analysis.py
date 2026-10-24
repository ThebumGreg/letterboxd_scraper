"""File analysis for the Library: what a worker finds out about one media file.

This is the original "Media Analysis and Migration" tool's analysis, kept as it was (its naming rules, language rules, audio and subtitle
audits, track-change proposals, integrity checks and action choice are the same functions), minus everything that belonged to one machine:
no database, no cache, no reports. The worker looks at a file and reports what it found; SyncHub keeps the records and works out everything
that needs more than one file (two files for one destination, duplicates). Standard library only, like the agent.
"""
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import unicodedata
from datetime import datetime
from pathlib import Path

LOGIC_VERSION = 1            # bump when anything below changes what a file's analysis says: SyncHub then asks for every file to be analysed again

ACTION_COPY_RENAME, ACTION_REMUX_COPY, ACTION_TRANSCODE_COPY, ACTION_MANUAL_REVIEW = "copy_rename", "remux_copy", "transcode_copy", "manual_review"
CONVERT_VIDEO, CONVERT_AUDIO = "h264", "aac"
ENGLISH_PRESENT, ENGLISH_NONE, ENGLISH_UNKNOWN, ENGLISH_NO_AUDIO = "present", "none", "unknown", "no_audio"
ANALYSIS_COMPLETED, ANALYSIS_INCOMPLETE, ANALYSIS_FAILED = "completed", "incomplete", "failed"

# ============================================================================ the original's rules, unchanged
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".ts", ".m4v",
    ".mpg", ".mpeg", ".webm", ".ogv", ".flv", ".3gp", ".vob"
}

# ----- Naming rules (configurable) -----
NAMING_RULES = {
    # Available fields: {title} {season} {episode} {episode_end}
    "tv_format": "{title} - S{season:02d}E{episode:02d}",
    "tv_multi_format": "{title} - S{season:02d}E{episode:02d}-E{episode_end:02d}",
    # Movie years become "Title (2019)", which Jellyfin matches best.
    "movie_year_in_parentheses": True,
    # Only ALL-LOWERCASE titles are title-cased; mixed/upper case is preserved.
    "title_case_if_all_lowercase": True,
    # Infer show name / season from "Show/Season 2/" folders when file lacks one.
    "infer_show_from_folders": True,
    # Strip "-GROUP" only when it directly follows a scene tag (x264-GROUP).
    "strip_release_group_after_scene_tag": True,
    # Replace characters that are invalid on Windows/SMB (":" -> " - ", etc.).
    "sanitize_for_windows_smb": True,
    # Names scoring below this are flagged for manual review.
    "review_confidence_threshold": 0.60,
}
# Extra regex fragments to treat as release junk (matched whole-token, case-insens.)
EXTRA_JUNK_TOKENS = []

# ----- Audio / subtitle policy (report-only) -----
ENGLISH_CODES = {"en", "eng", "english"}
UNKNOWN_LANGUAGE_CODES = {"", "und", "unk", "unknown", "zxx", "mul", "mis"}

AUDIO_POLICY = {
    "preferred_languages": ENGLISH_CODES,
    "flag_multiple_streams": True,
    "recommend_english_only_when_present": True,
}
SUBTITLE_POLICY = {
    "keep_languages": ENGLISH_CODES,
    "flag_non_english_for_optional_removal": True,
    "never_recommend_removing_forced": True,
    "recommend_english_optional_not_forced": True,
    "flag_unknown_language": True,
}

# Metadata-first language resolution (applied before flags are raised).
LANGUAGE_POLICY = {
    # A stream whose language tag is missing/undetermined but whose TITLE names exactly
    # one language ("English 5.1", "Italiano") is treated as that language.
    "infer_from_title": True,
    # If a file has exactly ONE audio stream and its language is still unknown, assume
    # English instead of holding the file for review. OFF by default: it is a guess.
    "assume_english_single_und_audio": False,
}
_TITLE_LANGUAGES = {
    "english": "eng", "eng": "eng", "en": "eng", "italian": "ita", "italiano": "ita",
    "ita": "ita", "french": "fra", "francais": "fra", "fre": "fra", "fra": "fra",
    "german": "deu", "deutsch": "deu", "ger": "deu", "deu": "deu", "spanish": "spa",
    "espanol": "spa", "spa": "spa", "portuguese": "por", "por": "por", "japanese": "jpn",
    "jpn": "jpn", "russian": "rus", "rus": "rus", "korean": "kor", "kor": "kor",
    "chinese": "zho", "mandarin": "zho", "cantonese": "yue", "hindi": "hin",
    "dutch": "nld", "swedish": "swe", "norwegian": "nor", "danish": "dan",
    "finnish": "fin", "polish": "pol", "czech": "ces", "hungarian": "hun",
    "greek": "ell", "turkish": "tur", "arabic": "ara", "hebrew": "heb", "thai": "tha",
}

# Deliberately broad; these tokens are removed from movie names only.
_JUNK_TOKENS = (
    # audio codec + channel layout (e.g. "DDP5 1", "AAC 2.0", "DTS-HD 7.1")
    r"(?:aac|ac3|eac3|dts(?:[\s._-]?hd)?|truehd|atmos|flac|mp3|ddp?\+?|dd\+)"
    r"[\s._-]?[1257][\s._][01]|"
    r"h[\s._]?26[45]|ddp\+?|"
    r"\d{3,4}x\d{3,4}|480p|576p|720p|1080p|2160p|4320p|4k|8k|"
    r"blu[\s._-]?ray|brrip|bdrip|web[\s._-]?dl|web[\s._-]?rip|hdtv|pdtv|dvdrip|remux|"
    r"x264|avc|x265|hevc|av1|xvid|divx|"
    r"aac|ac3|eac3|dts(?:[\s._-]?hd)?|truehd|atmos|flac|mp3|"
    r"hdr10\+?|hdr|dolby[\s._-]?vision|proper|repack|limited|internal|"
    r"10bit|8bit|yify|yts|amzn|nflx|dsnp|hmax|atvp|pcok"
)
if EXTRA_JUNK_TOKENS:
    _JUNK_TOKENS += "|" + "|".join(EXTRA_JUNK_TOKENS)
JUNK_TOKEN_PATTERN = re.compile(r"(?i)(?<!\w)(?:" + _JUNK_TOKENS + r")(?!\w)")
# "-GROUP" directly after a scene tag at the end of the name (x264-GRP, H 264-NTb)
GROUP_AFTER_TOKEN_PATTERN = re.compile(
    r"(?i)(?<!\w)((?:" + _JUNK_TOKENS + r"))[\s._]*-\s*[A-Za-z0-9]+$"
)

# Website / release-site tags. Conservative: loose TLD list only inside
# brackets or after "www."; bare "site.com - " prefixes use a strict TLD list so
# titles like "Take.Me.Home" or "Dr.Who" are not damaged.
_TLDS_LOOSE = (r"(?:com|net|org|info|biz|xyz|club|site|top|cc|pw|ws|vip|link|pro|"
               r"me|tv|to|io|co|mx|ag|lt|is|am|re)")
_TLDS_STRICT = r"(?:com|net|org|info|biz|xyz|club|site|top|cc|vip|link|ws|pw)"
SITE_PATTERNS = [
    re.compile(r"(?i)[\[\(\{]\s*(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
               + _TLDS_LOOSE + r"\s*[\]\)\}]"),
    re.compile(r"(?i)https?://\S+"),
    re.compile(r"(?i)(?<![a-z0-9])www\.[a-z0-9-]+\." + _TLDS_LOOSE
               + r"(?:\.(?:uk|au|nz|za|in|ca))?(?![a-z0-9])"),
    re.compile(r"(?i)^\s*[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\."
               + _TLDS_STRICT + r"\s+-\s+"),
]

# (?<!\d)/(?![\dp]) guards stop "1920x1080" or "- 1080p" being read as episodes.
STANDARD_EPISODE_PATTERN = re.compile(
    r"(?i)(?P<season>s\d{1,2})[\s._]*(?P<episode>e\d{1,3})"
    r"(?P<extra>(?:[\s._]*[-+]?[\s._]*e\d{1,3}|\s*-\s*\d{1,3}(?![\dp]))*)"
)
X_EPISODE_PATTERN = re.compile(
    r"(?i)(?<!\d)(?P<season>\d{1,2})x(?P<episode>\d{1,3})(?![\dp])"
    r"(?P<extra>(?:\s*[-+]\s*\d{1,3}(?![\dp]))*)"
)
YEAR_PATTERN = re.compile(
    r"[\(\[]?\s*(?<![\dxX])((?:19|20)\d{2})(?!\d)(?![xX]\d)\s*[\)\]]?")

LEGACY_VIDEO_CODECS = {
    "mpeg2video", "mpeg4", "msmpeg4v3", "indeo3", "wmv2",
    "vc1", "flv1", "realvideo", "h263"
}
LEGACY_CONTAINERS = {"avi", "asf", "mpegts", "mpeg", "vob", "flv"}
# Audio that is re-encoded (to AAC) when a file is converted. DTS/TrueHD are deliberately
# NOT listed: most clients play or cheaply transcode them. Add codecs here to convert them.
LEGACY_AUDIO_CODECS = {"mp2", "wmav1", "wmav2", "wmapro", "wmalossless", "amr_nb", "amr_wb",
                       "ra_144", "ra_288", "cook", "sipr", "atrac3"}
# Subtitle formats that cannot be stream-copied into MKV when a file is rebuilt from
# another container; they are dropped (and listed in the plan).
MKV_UNSUPPORTED_SUBTITLES = {"dvb_teletext", "eia_608", "mov_text"}

GENERIC_FOLDER_NAMES = {
    "tv", "tv shows", "tv_shows", "television", "series", "shows",
    "media", "videos", "tfiles", "downloads", "incoming"
}
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor",
               "of", "on", "or", "the", "to", "vs", "via"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)

# Integrity statuses. They deliberately say WHAT was proven.
INT_METADATA = "Not Checked (Metadata Only)"      # ffprobe only, nothing decoded
INT_TAIL = "Tail Sample Passed (Partial)"         # only last N seconds decoded
INT_FULL = "Full Decode Passed"                   # whole file decoded, no errors
INT_DECODE_ERROR = "Decode Error (Review)"
INT_TIMEOUT = "Not Verified (Timeout)"
INT_IO_ERROR = "Not Verified (I/O Error)"
INT_AUDIT_ERROR = "Audit Error"
CACHEABLE_INTEGRITY = {INT_METADATA, INT_TAIL, INT_FULL}


# ============================================================================ naming (the original's, unchanged)
def safe_title_case_not_used(value):
    """Preserve original capitalization; normalize only separators and spaces."""
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_")


def strip_site_tags(text):
    """Remove website URLs / release-site tags. Returns (text, removed_flag)."""
    original, removed = text, False
    for _ in range(4):
        updated = text
        for pattern in SITE_PATTERNS:
            updated = pattern.sub(" ", updated)
        if updated == text:
            break
        removed, text = True, updated
    if not removed:
        return original, False
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[\s._\-\u2013\u2014]+", "", text)
    text = re.sub(r"[\s._\-\u2013\u2014]+$", "", text)
    return text, True


def smart_title_case(value):
    words = value.split(" ")
    out = []
    for position, word in enumerate(words):
        lower = word.lower()
        if 0 < position < len(words) - 1 and lower in SMALL_WORDS:
            out.append(lower)
            continue
        match = re.search(r"[a-z]", lower)
        if match:
            out.append(lower[:match.start()] + lower[match.start()].upper()
                       + lower[match.start() + 1:])
        else:
            out.append(lower)
    return " ".join(out)


def normalize_title(value):
    """Normalize separators; title-case ONLY all-lowercase text. Returns (title, flags)."""
    flags = []
    value = safe_title_case_not_used(value)
    if (NAMING_RULES["title_case_if_all_lowercase"] and value
            and value == value.lower() and re.search(r"[a-z]", value)):
        value = smart_title_case(value)
        flags.append("Title-cased (source was all lowercase)")
    elif value.isupper() and len(value) > 4 and " " in value:
        flags.append("Title is ALL UPPERCASE; verify capitalization")
    return value, flags


def sanitize_component(value):
    """Make a name safe for Windows/SMB. Returns (value, changed)."""
    original = value
    value = re.sub(r"\s*:\s*", " - ", value)
    value = INVALID_FILENAME_CHARS.sub("", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value, value != original


def infer_from_folders(source_path):
    """Return (show_name, season_number) guessed from 'Show/Season N/' folders."""
    if not source_path:
        return "", None
    parts = list(Path(source_path).parts)[:-1]
    season, show, idx = None, "", len(parts) - 1
    if idx >= 0:
        folder = parts[idx].strip()
        match = re.fullmatch(r"(?i)(?:season|series|s)\s*0*(\d{1,2})", folder)
        if match:
            season, idx = int(match.group(1)), idx - 1
        elif re.fullmatch(r"(?i)specials?", folder):
            season, idx = 0, idx - 1
    if idx >= 0:
        candidate = parts[idx].strip()
        if (candidate.casefold() not in GENERIC_FOLDER_NAMES
                and not re.match(r"^[A-Za-z]:[\\/]?$", candidate)
                and not candidate.startswith("\\\\")):
            show = candidate
    return show, season


def parse_episode(filename):
    """Return season, episode, episode end, and title prefix, if recognized."""
    stem, _ = strip_site_tags(Path(filename).stem)
    match = STANDARD_EPISODE_PATTERN.search(stem)
    style = "standard"

    if not match:
        match = X_EPISODE_PATTERN.search(stem)
        style = "x"

    if not match:
        return None

    season_token = match.group("season")
    episode_token = match.group("episode")

    if style == "standard":
        season = int(re.search(r"\d+", season_token).group())
    else:
        season = int(season_token)

    episode_digits = re.search(r"\d+", episode_token)

    if episode_digits:
        episode = int(episode_digits.group())
    else:
        episode = None

    extra = match.group("extra") or ""
    end_episode = episode

    # Handles common forms like S01E02-E03, S01E02+E03, S01E02E03 or 1x02-03.
    extra_numbers = re.findall(r"\d{1,3}", extra)
    if extra_numbers:
        end_episode = int(extra_numbers[-1])
        if end_episode < episode:
            end_episode = episode

    title_prefix = stem[:match.start()].strip(" ._-")
    return season, episode, end_episode, title_prefix


def confidence_label(score):
    if score >= 0.85:
        return "High"
    if score >= NAMING_RULES["review_confidence_threshold"]:
        return "Medium"
    return "Low"


def clean_filename(filename, source_path=None):
    """Create a conservative cleaned filename while preserving extension.

    Returns a dict: name, media_type, label, score (0-1), flags, review,
    and (for TV) show/season so nothing needs re-parsing."""
    ext = Path(filename).suffix
    flags = []
    review = False
    stem, site_removed = strip_site_tags(Path(filename).stem)
    if site_removed:
        flags.append("Website/release-site tag removed")

    episode = parse_episode(filename)
    if episode:
        season, ep_start, ep_end, title_prefix = episode
        score = 0.95
        prefix_clean = JUNK_TOKEN_PATTERN.sub(" ", title_prefix)
        if prefix_clean != title_prefix:
            score -= 0.05
        title, title_flags = normalize_title(prefix_clean)
        flags += title_flags
        if title_flags and "ALL UPPERCASE" in " ".join(title_flags):
            score -= 0.05

        folder_show, folder_season = infer_from_folders(source_path)
        if not title and NAMING_RULES["infer_show_from_folders"] and folder_show:
            title, more = normalize_title(folder_show)
            flags += more
            flags.append("Show title inferred from folder name")
            score = 0.70
        if not title:
            title = "Unknown Show"
            flags.append("Show title could not be determined")
            score, review = 0.30, True
        if folder_season is not None and folder_season != season:
            flags.append(f"Season folder ({folder_season}) differs from filename (S{season:02d})")
            score -= 0.20
            review = True
        if re.search(r"[\[\]{}]", title):
            flags.append("Leftover brackets in title")
            score -= 0.10

        if NAMING_RULES["sanitize_for_windows_smb"]:
            title, changed = sanitize_component(title)
            if changed:
                flags.append("Characters invalid on Windows/SMB were replaced/removed")

        if ep_end > ep_start:
            fmt, score = NAMING_RULES["tv_multi_format"], score - 0.10
            flags.append(f"Multi-episode range E{ep_start:02d}-E{ep_end:02d}; verify")
        else:
            fmt = NAMING_RULES["tv_format"]
        try:
            base = fmt.format(title=title, season=season, episode=ep_start,
                              episode_end=ep_end)
        except (KeyError, IndexError, ValueError):
            base = f"{title} - S{season:02d}E{ep_start:02d}"
            flags.append("Invalid tv_format in NAMING_RULES; default used")
        score = max(0.0, min(1.0, score))
        review = review or score < NAMING_RULES["review_confidence_threshold"]
        return {"name": f"{base}{ext}", "media_type": "TV Episode",
                "label": confidence_label(score), "score": round(score, 2),
                "flags": flags, "review": review, "show": title, "season": season}

    # ---- Movie / unknown ----
    # A "-GROUP" directly after a scene tag (x264-GRP, "H 264-NTb") is removed
    # first, so hyphenated titles like "Spider-Man" are not damaged.
    group_removed = False
    if NAMING_RULES["strip_release_group_after_scene_tag"]:
        stripped = GROUP_AFTER_TOKEN_PATTERN.sub(r"\1", stem)
        group_removed = stripped != stem
        stem = stripped
    if group_removed:
        flags.append("Release group suffix removed")

    # Keep likely year tags; strip technical tokens and release debris.
    cleaned, junk_count = JUNK_TOKEN_PATTERN.subn(" ", stem)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", " ", cleaned)
    cleaned = re.sub(r"[\._]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.")

    # Remove common trailing " - GROUP" labels, but only for scene-tagged names.
    if junk_count:
        trimmed = re.sub(r"\s+-\s+[A-Za-z0-9]+$", "", cleaned).strip()
        if trimmed != cleaned:
            flags.append("Trailing release-group label removed")
        cleaned = trimmed

    if not cleaned:
        cleaned = stem
        flags.append("Name was empty after cleaning; original kept")

    score = 0.65
    years = [m for m in YEAR_PATTERN.finditer(cleaned) if m.start() > 0]
    if years:
        score = 0.80
        if len(years) > 1:
            flags.append("Multiple year-like numbers; last one used")
            score -= 0.15
        match = years[-1]
        before = cleaned[:match.start()].rstrip()
        after = cleaned[match.end():].strip()
        if NAMING_RULES["movie_year_in_parentheses"]:
            cleaned = f"{before} ({match.group(1)})" + (f" {after}" if after else "")
        if after:
            flags.append("Text after year retained; verify edition/extra info")
            score -= 0.10
    else:
        flags.append("No release year found; Jellyfin matching may be ambiguous")
        score -= 0.10

    cleaned, title_flags = normalize_title(cleaned)
    flags += title_flags
    if re.search(r"[\[\]{}]", cleaned):
        flags.append("Leftover brackets in title")
        score -= 0.10
    if NAMING_RULES["sanitize_for_windows_smb"]:
        cleaned, changed = sanitize_component(cleaned)
        if changed:
            flags.append("Characters invalid on Windows/SMB were replaced/removed")
    if len(re.sub(r"[^A-Za-z0-9]", "", cleaned)) < 2:
        flags.append("Title is extremely short")
        score -= 0.30
    score = max(0.0, min(1.0, score))
    return {"name": f"{cleaned}{ext}", "media_type": "Movie/Unknown",
            "label": confidence_label(score), "score": round(score, 2),
            "flags": flags,
            "review": score < NAMING_RULES["review_confidence_threshold"],
            "show": "", "season": None}


def is_probably_tv_path(path):
    parts = {p.casefold() for p in Path(path).parts}
    return bool(parts & {
        "tv", "tv shows", "tv_shows", "television", "series", "shows"
    }) or any(re.fullmatch(r"season\s*\d{1,2}", p) for p in parts)


# ---------------- RECOMMENDED-NAME CHECKS ----------------
# Destination paths are NOT calculated here: the migration script owns the
# destination root and builds paths from the recorded name, media type, show
# title and season. Only the recommended FILENAME is validated.

def tv_path_naming_error(source_path, media_type):
    """Source looks like TV but no episode pattern was recognized."""
    if media_type != "TV Episode" and is_probably_tv_path(source_path):
        return "Source appears to be TV, but episode pattern was not recognized"
    return ""


def recommended_name_problems(name):
    """Problems that would make the recommended filename unusable on Windows/SMB."""
    problems = []
    stem = Path(name).stem
    if INVALID_FILENAME_CHARS.search(name):
        problems.append("invalid character(s) in recommended filename")
    if name != name.strip() or stem != stem.strip() or stem.endswith("."):
        problems.append("leading/trailing space or trailing dot in recommended filename")
    if stem.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        problems.append(f"reserved Windows name '{stem}'")
    if len(name.encode("utf-8")) > 255:
        problems.append("recommended filename longer than 255 bytes")
    return problems

# ============================================================================ streams, languages, audits, track proposals (the original's, unchanged)
def frame_rate(stream):
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not raw or raw == "0/0":
        return ""
    try:
        numerator, denominator = raw.split("/", 1)
        denominator = float(denominator)
        if denominator == 0:
            return ""
        return round(float(numerator) / denominator, 3)
    except (ValueError, ZeroDivisionError):
        return ""


def stream_languages(streams, kind):
    values = []
    for stream in streams:
        if stream.get("codec_type") != kind:
            continue
        tags = stream.get("tags") or {}
        language = tags.get("language") or tags.get("LANGUAGE") or "und"
        label = tags.get("title")
        value = f"{language} ({label})" if label else language
        if value not in values:
            values.append(value)
    return ", ".join(values)


def summarize_streams(streams):
    """Raw audio/subtitle facts stored in the cache (policy is applied later)."""
    out = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        tags = {str(k).lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}
        out.append({
            "index": stream.get("index"), "type": kind,
            "codec": stream.get("codec_name", ""),
            "lang": str(tags.get("language", "") or "").strip().lower(),
            "title": str(tags.get("title", "") or ""),
            "channels": stream.get("channels"),
            "default": bool(disposition.get("default")),
            "forced": bool(disposition.get("forced")),
            "hearing_impaired": bool(disposition.get("hearing_impaired")),
            "comment": bool(disposition.get("comment")),
        })
    return out


def classify_language(code, keep=ENGLISH_CODES):
    """'english' (confirmed) | 'unknown' (missing/undetermined) | 'other' (confirmed non-English)."""
    base = (code or "").lower().replace("_", "-").split("-")[0]
    if base in keep:
        return "english"
    if base in UNKNOWN_LANGUAGE_CODES:
        return "unknown"
    return "other"


def infer_language_from_title(title):
    """ISO code if the title names exactly one known language, else ''."""
    found = {_TITLE_LANGUAGES[w] for w in re.findall(r"[a-z]+", (title or "").lower())
             if w in _TITLE_LANGUAGES}
    return next(iter(found)) if len(found) == 1 else ""


def resolve_languages(streams):
    """Copies of the stream list with unknown languages resolved from metadata
    (title) or by policy (single audio stream assumed English). The original tag is
    kept in 'lang_original' and the method in 'lang_source'. The raw cached stream
    data is never modified."""
    out = [dict(s) for s in streams]
    if LANGUAGE_POLICY["infer_from_title"]:
        for stream in out:
            if classify_language(stream["lang"]) == "unknown":
                code = infer_language_from_title(stream["title"])
                if code:
                    stream.update(lang_original=stream["lang"], lang=code, lang_source="title")
    audio = [s for s in out if s["type"] == "audio"]
    if (LANGUAGE_POLICY["assume_english_single_und_audio"] and len(audio) == 1
            and classify_language(audio[0]["lang"]) == "unknown"):
        audio[0].update(lang_original=audio[0]["lang"], lang="eng", lang_source="assumed")
    return out


def language_note_flags(streams):
    """Informational flags recording every language that was inferred or assumed."""
    audio_flags, sub_flags = [], []
    for s in streams:
        if s.get("lang_source") == "title":
            flag = ("LANG_INFERRED_FROM_TITLE",
                    f"{s['type'].capitalize()} #{s['index']} language '{s['lang']}' inferred "
                    f"from title \"{s['title']}\"", False)
        elif s.get("lang_source") == "assumed":
            flag = ("AUDIO_ASSUMED_ENGLISH",
                    f"Only audio stream #{s['index']} has no language; assumed English by policy",
                    False)
        else:
            continue
        (audio_flags if s["type"] == "audio" else sub_flags).append(flag)
    return audio_flags, sub_flags


def english_audio_status(audio_streams):
    """Marked in the database for every file (the ENGLISH_* values)."""
    if not audio_streams:
        return ENGLISH_NO_AUDIO
    kinds = {classify_language(s["lang"]) for s in audio_streams}
    if "english" in kinds:
        return ENGLISH_PRESENT
    return ENGLISH_UNKNOWN if "unknown" in kinds else ENGLISH_NONE


def removal_container(container):
    """Containers in which tracks can be removed by a lossless stream copy."""
    name = (container or "").lower()
    if "matroska" in name:
        return "matroska"
    if "mp4" in name:
        return "mp4"
    return None


def _track_ref(stream):
    return {"index": stream["index"], "language": stream["lang"], "title": stream["title"]}


def _unsupported_subtitle_ref(stream):
    ref = _track_ref(stream)
    ref["reason"] = "subtitle format cannot be stored in MKV"
    return ref


def propose_track_changes(audio_result, audio_streams, subtitle_streams, container, video_codec=""):
    """Structured PROPOSAL for rebuilding the file (never applied by analysis). One plan
    combines up to three things:
      * format conversion: a legacy container is remuxed losslessly into MKV; a legacy
        video codec is re-encoded to H.264; a legacy audio codec to AAC;
      * English-only tracks: only when the file HAS English audio, tracks whose language is
        CONFIRMED non-English are dropped (unknown-language tracks are always kept);
      * a default audio flag that points at English.
    Files without English audio get NO removals (they are converted if needed, otherwise
    copied unchanged). op == 'convert' when a conversion is involved, else 'keep_tracks'."""
    names = {x.strip().lower() for x in (container or "").split(",")}
    legacy_container = bool(names & LEGACY_CONTAINERS)
    transcode_video = (video_codec or "").lower() in LEGACY_VIDEO_CODECS
    audio_convert = {str(s["index"]): {"from": s["codec"], "codec": CONVERT_AUDIO}
                     for s in audio_streams if (s.get("codec") or "").lower() in LEGACY_AUDIO_CODECS}
    converting = legacy_container or transcode_video or bool(audio_convert)
    kind = removal_container(container)
    out_container = kind if (kind and not legacy_container) else ("matroska" if converting else None)
    if out_container is None:
        return []
    present = english_audio_status(audio_streams) == ENGLISH_PRESENT
    remove_audio = [s for s in audio_streams if present and classify_language(s["lang"]) == "other"]
    remove_subs = [s for s in subtitle_streams if present and classify_language(s["lang"]) == "other"]
    unsupported = [s for s in subtitle_streams
                   if out_container == "matroska" and kind != "matroska"
                   and (s.get("codec") or "").lower() in MKV_UNSUPPORTED_SUBTITLES
                   and s["index"] not in {r["index"] for r in remove_subs}]
    if not (remove_audio or remove_subs or unsupported or converting):
        return []
    removed_audio = {s["index"] for s in remove_audio}
    gone_subs = {s["index"] for s in remove_subs} | {s["index"] for s in unsupported}
    keep_audio = [s for s in audio_streams if s["index"] not in removed_audio]
    plan = {"op": "convert" if converting else "keep_tracks", "container": out_container,
            "keep_audio": [s["index"] for s in keep_audio],
            "remove_audio": [_track_ref(s) for s in remove_audio],
            "keep_subtitle": [s["index"] for s in subtitle_streams if s["index"] not in gone_subs],
            "remove_subtitle": [_track_ref(s) for s in remove_subs] + [_unsupported_subtitle_ref(s) for s in unsupported],
            "default_audio": None, "default_audio_language": None}
    if present and (remove_audio or remove_subs):
        english = [s for s in keep_audio if classify_language(s["lang"]) == "english"]
        current = next((s for s in keep_audio if s["default"]), None)
        chosen = (current if current and classify_language(current["lang"]) == "english"
                  else next((s for s in english if not s.get("comment")), english[0]))
        plan["default_audio"], plan["default_audio_language"] = chosen["index"], chosen["lang"]
    if converting:
        plan.update({"source_container": next(iter(sorted(names - {""})), ""),
                     "target_extension": ".mkv" if legacy_container else None,
                     "video": {"action": "transcode" if transcode_video else "copy",
                               "from": (video_codec or "").lower(), "codec": CONVERT_VIDEO},
                     "audio_convert": audio_convert})
    return [plan]


def describe_plan(plan):
    """One plain-language sentence for a plan (used in review reasons)."""
    parts = []
    if plan["op"] == "convert":
        video = plan["video"]
        if video["action"] == "transcode":
            parts.append(f"re-encode the video ({video['from']} to H.264; lossy, slow)")
        elif plan.get("target_extension"):
            parts.append(f"lossless remux {plan['source_container']} to MKV (nothing re-encoded)")
        if plan["audio_convert"]:
            parts.append("convert audio " + ", ".join(f"#{i} {a['from']} to AAC" for i, a in plan["audio_convert"].items()))
    gone = []
    for label, key in (("audio", "remove_audio"), ("subtitles", "remove_subtitle")):
        if plan[key]:
            gone.append(f"remove {label} " + ", ".join(f"#{t['index']} {t['language'] or 'und'}" for t in plan[key]))
    if gone:
        parts.append(("keep English tracks only: " if any(not t.get("reason") for t in plan["remove_audio"] + plan["remove_subtitle"]) else "")
                     + "; ".join(gone))
    if plan.get("default_audio") is not None:
        parts.append(f"English audio #{plan['default_audio']} is the default")
    return "; ".join(parts)


def review_reasons_from_flags(audio_result, sub_result, proposals):
    """Turn flags into review reasons according to the flag tiers (info, change, hold): informational
    flags raise nothing; a track-change proposal raises ONE 'tracks' reason; holds raise
    'audio'/'subtitle'. Redundant duplicates of the same finding are collapsed."""
    reasons = [("convert" if p["op"] == "convert" else "tracks", "Proposed: " + describe_plan(p))
               for p in proposals]
    covers_default = any(p.get("default_audio") is not None for p in proposals)
    audio_codes = {c for c, _, _ in audio_result["flags"]}
    for code, text, needs_review in audio_result["flags"]:
        if not needs_review:
            continue
        if code == "AUDIO_DEFAULT_NON_ENGLISH" and covers_default:
            continue            # covered by the proposal (the English track becomes the default)
        if code == "AUDIO_DEFAULT_LANG_UNKNOWN" and "AUDIO_ENGLISH_UNCONFIRMED" in audio_codes:
            continue            # the same finding, already covered by ENGLISH_UNCONFIRMED
        reasons.append(("audio", text))
    for code, text, needs_review in sub_result["flags"]:
        if needs_review:
            reasons.append(("subtitle", text))
    return reasons


def describe_stream(stream):
    lang = (stream["lang"] or "und") + ("*" if stream.get("lang_source") else "")
    bits = [f"#{stream['index']}", lang, stream["codec"] or "?"]
    if stream.get("channels"):
        bits.append(f"{stream['channels']}ch")
    marks = [name for name, key in (("default", "default"), ("forced", "forced"),
                                    ("sdh", "hearing_impaired"), ("commentary", "comment"))
             if stream.get(key)]
    text = " ".join(bits)
    if marks:
        text += " [" + ",".join(marks) + "]"
    if stream.get("title"):
        text += f' "{stream["title"]}"'
    return text


def _ids(streams):
    return ", ".join(f"#{s['index']}({s['lang'] or 'und'})" for s in streams)


def audit_audio(audio):
    """Report-only audio audit. Returns dict(flags, recommendation, default_lang, detail).
    flags: list of (code, text, needs_review)."""
    flags = []
    if not audio:
        return {"flags": [("AUDIO_NONE", "No audio streams present", True)],
                "recommendation": "No audio streams; review manually",
                "default_lang": "", "detail": ""}
    keep = AUDIO_POLICY["preferred_languages"]
    classes = [(s, classify_language(s["lang"], keep)) for s in audio]
    english = [s for s, c in classes if c == "english"]
    unknown = [s for s, c in classes if c == "unknown"]
    other = [s for s, c in classes if c == "other"]
    has_default = any(s["default"] for s in audio)
    default = next((s for s in audio if s["default"]), audio[0])
    default_class = classify_language(default["lang"], keep)
    default_note = "" if has_default else " (no default flag set; first stream assumed)"
    default_lang = default["lang"] or "und"

    if default_class == "other":
        flags.append(("AUDIO_DEFAULT_NON_ENGLISH",
                      f"Default/primary audio #{default['index']} is confirmed non-English "
                      f"({default_lang}){default_note}"
                      + ("; an English track exists but is not default" if english else ""),
                      True))
    elif default_class == "unknown":
        flags.append(("AUDIO_DEFAULT_LANG_UNKNOWN",
                      f"Default/primary audio #{default['index']} has missing/undetermined "
                      f"language metadata{default_note}; English not confirmed", True))
    if not english:
        if unknown:
            flags.append(("AUDIO_ENGLISH_UNCONFIRMED",
                          f"English cannot be confirmed: {len(unknown)} stream(s) have "
                          f"missing/undetermined language ({_ids(unknown)})", True))
        else:
            flags.append(("AUDIO_NO_ENGLISH",
                          f"No English audio present (confirmed non-English: {_ids(other)})",
                          True))

    recommendation = "No change suggested"
    if len(audio) > 1 and AUDIO_POLICY["flag_multiple_streams"]:
        if english and AUDIO_POLICY["recommend_english_only_when_present"]:
            parts = []
            if other:
                parts.append(f"Report-only: retain English {_ids(english)}; non-English "
                             f"{_ids(other)} could be removed")
            if unknown:
                parts.append(f"Identify unknown-language {_ids(unknown)} before any removal")
            if not other and not unknown:
                parts.append("Multiple English streams; no language-based removal suggested")
            recommendation = "; ".join(parts)
            flags.append(("AUDIO_MULTIPLE_STREAMS",
                          f"{len(audio)} audio streams (English present)", False))
        else:
            recommendation = ("Report-only: do NOT strip audio; no English confirmed "
                              "(manual review needed)")
            flags.append(("AUDIO_MULTIPLE_NO_ENGLISH",
                          f"{len(audio)} audio streams and no confirmed English", False))
    elif not english:
        recommendation = "No stripping suggested; English not confirmed (manual review)"
    return {"flags": flags, "recommendation": recommendation, "default_lang": default_lang,
            "detail": " | ".join(describe_stream(s) for s in audio)}


def audit_subtitles(subs):
    """Report-only subtitle audit. Forced subtitles are never recommended for removal."""
    flags = []
    keep = SUBTITLE_POLICY["keep_languages"]

    def is_forced(stream):
        return stream["forced"] or "forced" in stream["title"].lower()

    english = [s for s in subs if classify_language(s["lang"], keep) == "english"]
    unknown = [s for s in subs if classify_language(s["lang"], keep) == "unknown"]
    other = [s for s in subs if classify_language(s["lang"], keep) == "other"]
    forced = [s for s in subs if is_forced(s)]
    english_regular = [s for s in english if not is_forced(s)]
    recommendations = []

    if not english:
        flags.append(("SUB_NO_ENGLISH",
                      "No English subtitle streams" if not subs else
                      "No confirmed English subtitle streams", False))
        recommendations.append("No English subtitles present")
    if forced:
        flags.append(("SUB_FORCED_PRESENT",
                      f"Forced subtitle stream(s) {_ids(forced)} identified separately", False))
        recommendations.append(f"Keep forced {_ids(forced)} (may be needed for foreign dialogue)")
    removable = [s for s in other if not is_forced(s)]
    if removable and SUBTITLE_POLICY["flag_non_english_for_optional_removal"]:
        flags.append(("SUB_NON_ENGLISH_OPTIONAL_REMOVAL",
                      f"Non-English subtitle stream(s) {_ids(removable)}", False))
        recommendations.append(f"Report-only: non-English {_ids(removable)} could optionally be removed")
    if unknown and SUBTITLE_POLICY["flag_unknown_language"]:
        flags.append(("SUB_UNKNOWN_LANGUAGE",
                      f"Subtitle stream(s) with missing/undetermined language {_ids(unknown)}",
                      True))
        recommendations.append(f"Identify language of {_ids(unknown)} before any removal")

    if SUBTITLE_POLICY["recommend_english_optional_not_forced"]:
        for stream in english:
            if stream["forced"] and "forced" not in stream["title"].lower() and not english_regular:
                flags.append(("SUB_ENGLISH_MARKED_FORCED",
                              f"English subtitle #{stream['index']} has the forced flag but "
                              f"is the only English track; it may be a full-dialogue track "
                              f"mislabeled as forced", True))
                recommendations.append(
                    f"If #{stream['index']} holds full dialogue, clear its forced flag so it "
                    f"is selectable/optional")
        defaults = [s for s in english_regular if s["default"]]
        if defaults:
            flags.append(("SUB_ENGLISH_DEFAULT_FLAG",
                          f"Ordinary English subtitle(s) {_ids(defaults)} flagged default",
                          False))
            recommendations.append(
                f"Consider clearing default flag on {_ids(defaults)} so subtitles are optional")
    return {"flags": flags,
            "recommendation": "; ".join(recommendations) or "No change suggested",
            "detail": " | ".join(describe_stream(s) for s in subs)}

# ============================================================================ the action to recommend (the original's, unchanged)
def determine_action(metadata, media_type, naming_error):
    """Return (structured action, reason text). Actions are the ACTION_* values;
    they are recommendations only - nothing is executed by this script."""
    integrity = metadata.get("Integrity Status", "")
    codec = metadata.get("Video Codec", "").lower()
    container_names = {
        x.strip().lower()
        for x in metadata.get("Container", "").split(",")
    }

    if naming_error:
        return ACTION_MANUAL_REVIEW, naming_error
    if integrity.startswith("Audit Error") or integrity.startswith("Not Verified"):
        return ACTION_MANUAL_REVIEW, "Audit incomplete; do not migrate automatically"
    if integrity.startswith("Decode Error"):
        return ACTION_MANUAL_REVIEW, "Decode error reported; inspect before deciding"
    if integrity.startswith("Not Checked"):
        review = "Integrity not verified (metadata only)"
    elif integrity == INT_TAIL:
        review = "Only the tail was decoded; whole-file health is unproven"
    else:
        review = ""

    if media_type == "TV Episode" and not parse_episode(
        metadata.get("_Suggested Clean Name", "")
    ):
        return ACTION_MANUAL_REVIEW, "TV episode could not be parsed safely"

    # Recommendations only; the analysis never converts anything. A re-encode (lossy) is
    # recommended for legacy video/audio codecs; a lossless remux for a legacy container
    # that holds modern codecs.
    audio_codecs = {c.strip().lower() for c in metadata.get("Audio Codecs", "").split(",") if c.strip()}
    if codec in LEGACY_VIDEO_CODECS or audio_codecs & LEGACY_AUDIO_CODECS:
        return (ACTION_TRANSCODE_COPY,
                review or "Legacy video/audio codec; re-encode to a Jellyfin-friendly format")
    if container_names & LEGACY_CONTAINERS:
        return (ACTION_REMUX_COPY,
                review or "Legacy container with modern codecs; lossless remux into MKV")

    if integrity == INT_FULL:
        return ACTION_COPY_RENAME, review

    return (ACTION_COPY_RENAME,
            review or "Metadata readable; integrity not fully verified")

# ============================================================================ running the tools (rewritten: settings instead of globals, cancellable)
class Settings:
    """What a scan needs to know. `check` is called often and raises to stop the work (a cancelled or lost job, a shutdown)."""

    def __init__(self, ffprobe="ffprobe", ffmpeg="ffmpeg", integrity_mode="tail", tail_seconds=30, ffprobe_timeout=60, tail_timeout=120, full_timeout=None,
                 retry_attempts=3, retry_delay=3.0, partial_read_bytes=1024 * 1024, hash_chunk=8 * 1024 * 1024, check=None, log=None):
        self.ffprobe, self.ffmpeg = ffprobe, ffmpeg
        self.integrity_mode, self.tail_seconds = integrity_mode, int(tail_seconds)
        self.ffprobe_timeout, self.tail_timeout, self.full_timeout = ffprobe_timeout, tail_timeout, full_timeout
        self.retry_attempts, self.retry_delay = max(1, int(retry_attempts)), float(retry_delay)
        self.partial_read_bytes, self.hash_chunk = partial_read_bytes, hash_chunk
        self.check = check or (lambda: None)
        self.log = log or (lambda level, message: None)


TRANSIENT_TEXT_PATTERN = re.compile(
    r"(?i)input/output error|i/o error|connection (?:reset|refused|timed out)|"
    r"network|stale file|host is down|no route to host|broken pipe|"
    r"semaphore timeout|temporarily unavailable|operation timed out|no such file"
)


class AuditIncomplete(RuntimeError):
    """Audit could not finish for a temporary reason (timeout / I/O)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _sleep(settings, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        settings.check()
        time.sleep(min(0.25, max(end - time.monotonic(), 0)))


def with_retries(settings, func, description):
    """Run func(); retry an OSError (except PermissionError) a limited number of times."""
    for attempt in range(1, settings.retry_attempts + 1):
        try:
            return func()
        except PermissionError:
            raise
        except OSError as exc:
            if attempt >= settings.retry_attempts:
                settings.log("ERROR", f"Giving up on {description} after {attempt} attempt(s): {exc}")
                raise
            settings.log("WARNING", f"Retry {attempt}/{settings.retry_attempts - 1} for {description}: {exc}")
            _sleep(settings, settings.retry_delay * attempt)


def _kill(proc):
    """Stops the tool and anything it started (a wrapper script's child would otherwise keep the output pipe open)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        proc.kill()
    proc.communicate()


def _run_once(settings, command, timeout):
    """One run of a tool, polled so the work can be cancelled. Returns the CompletedProcess, or None on timeout."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            return subprocess.CompletedProcess(command, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            try:
                settings.check()
            except BaseException:
                _kill(proc)
                raise
            if deadline is not None and time.monotonic() > deadline:
                _kill(proc)
                return None


def run_tool(settings, command, timeout, description):
    """Run ffprobe/ffmpeg. Returns (result, outcome); outcome is 'ok' | 'timeout' | 'transient'. Timeouts and I/O-looking failures are retried a
    limited number of times and are never treated as success."""
    result, outcome = None, "ok"
    for attempt in range(1, settings.retry_attempts + 1):
        result = _run_once(settings, command, timeout)
        if result is None:
            outcome = "timeout"
        elif result.returncode == 0 or not TRANSIENT_TEXT_PATTERN.search(result.stderr or ""):
            outcome = "ok"
        else:
            outcome = "transient"
        if outcome == "ok":
            break
        if attempt < settings.retry_attempts:
            settings.log("WARNING", f"Retry {attempt}/{settings.retry_attempts - 1} ({outcome}) for {description}")
            _sleep(settings, settings.retry_delay * attempt)
    return result, outcome


def run_ffprobe(settings, path):
    command = [settings.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]
    result, outcome = run_tool(settings, command, settings.ffprobe_timeout, f"ffprobe {path}")
    if outcome == "timeout":
        raise AuditIncomplete(INT_TIMEOUT, f"ffprobe timed out ({settings.retry_attempts} attempt(s)): {path}")
    if outcome == "transient":
        detail = ((result.stderr if result else "") or "").strip()[-400:]
        raise AuditIncomplete(INT_IO_ERROR, f"ffprobe I/O error for {path}: {detail}")
    if result.returncode != 0:
        detail = (result.stderr or "ffprobe returned nonzero exit status").strip()
        raise RuntimeError(f"ffprobe failed for {path}: {detail[-800:]}")
    if not result.stdout or not result.stdout.strip():
        raise RuntimeError(f"ffprobe returned empty output for: {path}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ffprobe returned unexpected data for: {path}")
    return data


def decode_check(settings, path):
    """Return (status, error). Distinguishes what was actually proven."""
    mode = settings.integrity_mode
    if mode == "metadata":
        return INT_METADATA, ""
    if mode == "tail":
        command = [settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-sseof", f"-{settings.tail_seconds}", "-i", path, "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
        timeout, passed = settings.tail_timeout, INT_TAIL
    elif mode == "full":
        command = [settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
        timeout, passed = settings.full_timeout, INT_FULL
    else:
        return INT_AUDIT_ERROR, f"Unknown integrity mode: {mode}"
    try:
        result, outcome = run_tool(settings, command, timeout, f"ffmpeg {mode} check {path}")
    except OSError as exc:
        return INT_AUDIT_ERROR, f"Could not run ffmpeg: {exc}"
    if outcome == "timeout":
        return INT_TIMEOUT, f"Integrity check timed out ({settings.retry_attempts} attempt(s))"
    stderr = (result.stderr or "").strip()
    if outcome == "transient":
        return INT_IO_ERROR, f"Temporary I/O error persisted: {stderr[-600:]}"
    if result.returncode == 0 and not stderr:
        return passed, ""
    # A nonzero exit, OR exit 0 with errors on stderr (ffmpeg can conceal decode errors yet still exit 0): both are findings for a person.
    detail = stderr[-1200:] if stderr else f"ffmpeg exit code {result.returncode}"
    if result.returncode == 0:
        detail = "ffmpeg exited 0 but reported errors: " + detail
    return INT_DECODE_ERROR, detail


def integrity_scope(settings, status):
    return {
        INT_METADATA: "Metadata only - nothing decoded",
        INT_TAIL: f"Last ~{settings.tail_seconds}s decoded only - NOT proof the whole file is healthy",
        INT_FULL: "Entire file decoded",
        INT_DECODE_ERROR: "Decode errors reported",
        INT_TIMEOUT: "Incomplete (timeout)",
        INT_IO_ERROR: "Incomplete (I/O error)",
        INT_AUDIT_ERROR: "Incomplete (audit error)",
    }.get(status, "")


# ============================================================================ hashing for duplicate detection
def sha256_file(settings, path, expected_size=None, progress=None):
    """SHA-256 with resume-on-error (the digest keeps its state, so a transient read error continues from the byte offset)."""
    digest = hashlib.sha256()
    offset, failures = 0, 0
    while True:
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                for chunk in iter(lambda: handle.read(settings.hash_chunk), b""):
                    digest.update(chunk)
                    offset += len(chunk)
                    settings.check()
                    if progress:
                        progress(len(chunk))
            break
        except PermissionError:
            raise
        except OSError as exc:
            failures += 1
            if failures >= settings.retry_attempts:
                raise
            settings.log("WARNING", f"Read error while hashing {os.path.basename(path)} at {offset}: {exc}; retry {failures}/{settings.retry_attempts - 1}")
            _sleep(settings, settings.retry_delay * failures)
    if expected_size is not None and offset != expected_size:
        raise OSError(f"File size changed during hashing ({offset} read, {expected_size} expected): {path}")
    return digest.hexdigest()


def partial_signature(settings, path, size):
    """First+last megabyte hashed together. Used ONLY to rule out duplicates; equal signatures never confirm one."""
    def read():
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            digest.update(handle.read(settings.partial_read_bytes))
            handle.seek(max(0, size - settings.partial_read_bytes))
            digest.update(handle.read(settings.partial_read_bytes))
        return digest.hexdigest()
    return with_retries(settings, read, f"partial read of {path}")


def hash_group(settings, files, known, progress=None, on_result=None):
    """The reading half of the original's duplicate detection, for ONE group of same-size files that SyncHub has not read yet.
    `files` are dicts with id, path, size, mtime; `known` is how many other files of that size already have a hash. With nothing known
    and more than one file, a cheap partial read first rules out files that cannot match anything; the rest are hashed in full.
    Each result {id, size, mtime, sha256 | ruled_out [+ partial] | error} goes to on_result as soon as it exists."""
    results = []

    def emit(file, **fields):
        item = {"id": file["id"], "size": file["size"], "mtime": file["mtime"], **fields}
        results.append(item)
        if on_result:
            on_result(item)

    need = list(files)
    size = need[0]["size"] if need else 0
    if not known and len(need) > 1 and size > 2 * settings.partial_read_bytes:
        by_signature, partial_of = {}, {}
        for file in need:
            try:
                signature = partial_signature(settings, file["path"], size)
                partial_of[file["id"]] = signature
            except OSError:
                signature = f"error:{file['id']}"             # the partial read failed: fall back to reading the whole file
            by_signature.setdefault(signature, []).append(file)
        survivors = []
        for signature, same in by_signature.items():
            if len(same) > 1 or signature.startswith("error:"):
                survivors += same
            else:
                emit(same[0], ruled_out=True, partial=signature)
        need = survivors
        if len(need) < 2:                                     # a lone survivor cannot match anything else in its size group
            for file in need:
                if file["id"] in partial_of:
                    emit(file, ruled_out=True, partial=partial_of[file["id"]])
            need = [f for f in need if f["id"] not in partial_of]
    for file in need:
        try:
            emit(file, sha256=sha256_file(settings, file["path"], size, progress))
        except OSError as exc:
            emit(file, error=f"Hash failed: {exc}")
    return results


# ============================================================================ the analysis of one file (the original's scan loop body, per file)
def _none(value):
    return None if value in ("", None) else value


def empty_result():
    return {
        "Container": "", "Duration (Seconds)": "", "Bitrate (kbps)": "", "Video Codec": "", "Resolution": "", "Frame Rate": "",
        "Audio Codecs": "", "Audio Languages": "", "Subtitle Languages": "", "Video Streams": 0, "Audio Streams": 0, "Subtitle Streams": 0,
        "Integrity Status": "", "Audit Error": "", "_streams": [], "_probed": False,
    }


def inspect_file(settings, path):
    """ffprobe the file and check its integrity (the original's inspect_file without the cache: SyncHub keeps the results)."""
    result = empty_result()
    try:
        data = run_ffprobe(settings, path)
        fmt = data.get("format") or {}
        result["Container"] = fmt.get("format_name", "")
        duration = fmt.get("duration")
        if duration not in (None, ""):
            result["Duration (Seconds)"] = round(float(duration), 3)
        bitrate = fmt.get("bit_rate")
        if bitrate:
            result["Bitrate (kbps)"] = round(int(bitrate) / 1000, 1)
        streams = data.get("streams") or []
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
        result["Video Streams"], result["Audio Streams"], result["Subtitle Streams"] = len(videos), len(audios), len(subtitles)
        result["Audio Codecs"] = ", ".join(sorted({s.get("codec_name", "unknown") for s in audios}))
        result["Audio Languages"] = stream_languages(streams, "audio")
        result["Subtitle Languages"] = stream_languages(streams, "subtitle")
        result["_streams"] = summarize_streams(streams)
        result["_probed"] = True
        if videos:
            video = videos[0]
            result["Video Codec"] = video.get("codec_name", "unknown")
            width, height = video.get("width"), video.get("height")
            if width and height:
                result["Resolution"] = f"{width}x{height}"
            result["Frame Rate"] = frame_rate(video)
        try:
            integrity, integrity_error = decode_check(settings, path)
            result["Integrity Status"] = integrity
            if integrity_error:
                result["Audit Error"] = integrity_error
        except OSError as exc:
            result["Integrity Status"] = INT_AUDIT_ERROR
            result["Audit Error"] = f"Integrity check failed: {exc}"
    except AuditIncomplete as exc:
        result["Integrity Status"] = exc.status
        result["Audit Error"] = str(exc)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        result["Integrity Status"] = INT_AUDIT_ERROR
        result["Audit Error"] = str(exc)
    return result


def analysis_status_for(integrity):
    """completed / incomplete / failed, from what happened during the analysis."""
    if integrity == INT_AUDIT_ERROR:
        return ANALYSIS_FAILED
    if integrity not in (INT_METADATA, INT_TAIL, INT_FULL, INT_DECODE_ERROR):
        return ANALYSIS_INCOMPLETE
    return ANALYSIS_COMPLETED


def analyze_file(settings, rel, path, size, mtime):
    """Everything one worker can say about one file: the original's per-file analysis (naming, tracks, integrity, action), as a record.
    `rel` is the path inside the share and is what naming looks at (so a mount point's own folder names never count as a show)."""
    name = os.path.basename(path)
    naming = clean_filename(name, rel)
    clean_name, media_type = naming["name"], naming["media_type"]
    try:
        metadata = inspect_file(settings, path)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:       # a per-file safety net: never abort the whole scan
        metadata = empty_result()
        metadata["Integrity Status"] = INT_AUDIT_ERROR
        metadata["Audit Error"] = f"Unexpected error: {exc!r}"
    metadata["_Suggested Clean Name"] = clean_name

    path_error = tv_path_naming_error(rel, media_type)
    action, notes = determine_action(metadata, media_type, path_error)
    if metadata["Integrity Status"] == INT_AUDIT_ERROR:
        action = ACTION_MANUAL_REVIEW

    reasons = []
    if path_error:
        reasons.append(("naming", path_error))
    if naming["review"]:
        reasons.append(("naming", f"Naming confidence {naming['score']}: " + "; ".join(naming["flags"])))
    if is_probably_tv_path(rel) and media_type != "TV Episode":
        action = ACTION_MANUAL_REVIEW
        notes = "Source path suggests TV, but episode naming was not recognized"
        reasons.append(("naming", notes))
    if size == 0:
        action = ACTION_MANUAL_REVIEW
        notes = "File is empty"
        reasons.append(("integrity", notes))

    streams = resolve_languages(metadata.get("_streams", []))
    audio_streams = [s for s in streams if s["type"] == "audio"]
    sub_streams = [s for s in streams if s["type"] == "subtitle"]
    audio_result, sub_result = audit_audio(audio_streams), audit_subtitles(sub_streams)
    probed = bool(metadata.get("_probed"))
    if probed:
        note_audio, note_sub = language_note_flags(streams)
        audio_result["flags"] += note_audio
        sub_result["flags"] += note_sub
    else:
        # Streams are unknown when probing failed; don't report "no audio".
        audio_result = {"flags": [], "recommendation": "Not assessed (probe incomplete)", "default_lang": "", "detail": ""}
        sub_result = {"flags": [], "recommendation": "Not assessed (probe incomplete)", "detail": ""}
    proposals = (propose_track_changes(audio_result, audio_streams, sub_streams, metadata.get("Container", ""), metadata.get("Video Codec", "")) if probed else [])
    for plan in proposals:                  # the recommended name carries the new extension
        extension = plan.get("target_extension")
        if extension and not clean_name.lower().endswith(extension):
            clean_name = os.path.splitext(clean_name)[0] + extension
    english_status = english_audio_status(audio_streams) if probed else None
    reasons.extend(review_reasons_from_flags(audio_result, sub_result, proposals))
    for problem in recommended_name_problems(clean_name):
        reasons.append(("naming", problem))

    integrity = metadata["Integrity Status"]
    warnings = [f"[naming] {flag}" for flag in naming["flags"]]
    if integrity in (INT_METADATA, INT_TAIL):
        warnings.append(f"[integrity] {integrity_scope(settings, integrity)}")
    warnings += [f"[audio] {text}" for _, text, _ in audio_result["flags"]]
    warnings += [f"[subtitle] {text}" for _, text, _ in sub_result["flags"]]
    errors = [metadata["Audit Error"]] if metadata["Audit Error"] else []
    return {
        "file_name": name, "size_bytes": size, "mtime": mtime, "logic_version": LOGIC_VERSION,
        "analysis_status": analysis_status_for(integrity), "analyzed_at": datetime.now().isoformat(timespec="seconds"),
        "integrity_mode": settings.integrity_mode, "integrity_status": _none(integrity), "integrity_scope": _none(integrity_scope(settings, integrity)),
        "audit_error": _none(metadata["Audit Error"]),
        "container": _none(metadata["Container"]), "duration_seconds": _none(metadata["Duration (Seconds)"]), "bitrate_kbps": _none(metadata["Bitrate (kbps)"]),
        "video_codec": _none(metadata["Video Codec"]), "resolution": _none(metadata["Resolution"]), "frame_rate": _none(metadata["Frame Rate"]),
        "audio_codecs": _none(metadata["Audio Codecs"]), "audio_languages": _none(metadata["Audio Languages"]), "subtitle_languages": _none(metadata["Subtitle Languages"]),
        "video_streams": metadata["Video Streams"], "audio_streams": metadata["Audio Streams"], "subtitle_streams": metadata["Subtitle Streams"],
        "streams_json": json.dumps(metadata["_streams"], ensure_ascii=False),
        "media_type": media_type, "recommended_name": clean_name, "show_title": _none(naming.get("show")), "season_number": naming.get("season"),
        "naming_confidence": naming["label"], "naming_score": naming["score"],
        "default_audio_language": _none(audio_result["default_lang"]), "audio_detail": _none(audio_result["detail"]),
        "audio_flags": _none("; ".join(c for c, _, _ in audio_result["flags"])), "audio_recommendation": audio_result["recommendation"],
        "subtitle_detail": _none(sub_result["detail"]), "subtitle_flags": _none("; ".join(c for c, _, _ in sub_result["flags"])),
        "subtitle_recommendation": sub_result["recommendation"],
        "english_audio_status": english_status, "proposed_track_changes_json": json.dumps(proposals) if proposals else None,
        "warnings_json": json.dumps(warnings, ensure_ascii=False), "errors_json": json.dumps(errors, ensure_ascii=False),
        # what SyncHub needs to finish the picture once it has seen the other files
        "findings": {"action": action, "action_reason": _none(notes) or "", "naming_flags": "; ".join(naming["flags"]), "naming_error": bool(path_error),
                     "reasons": [[c, t] for c, t in reasons]},
    }


# ============================================================================ finding the files
def clean_relative(relative):
    return unicodedata.normalize("NFC", str(relative or "").replace("\\", "/").strip("/"))


def under(relative, prefix):
    rel, pre = clean_relative(relative).casefold(), clean_relative(prefix).casefold()
    return not pre or rel == pre or rel.startswith(pre + "/")


class ExcludeRules:
    """Folders a scan leaves out. An entry is a folder name or wildcard matched against every folder name (case-insensitive, 'Extras', '*Sample*', '@eaDir'),
    or 'share:folder/path' to leave out one folder of one share and everything under it. (The original's rules, with a share name instead of a machine path.)"""

    def __init__(self, patterns, root_name):
        self.root, self.names, self.paths = root_name.lower(), [], []
        for pattern in patterns or []:
            pattern = str(pattern).strip()
            if not pattern:
                continue
            match = re.match(r"^([A-Za-z0-9_-]{2,}):(.*)$", pattern)
            if match:
                if match.group(1).lower() == self.root:
                    self.paths.append(clean_relative(match.group(2)))
            else:
                self.names.append(pattern.casefold())

    def folder(self, rel_dir):
        rel_dir = clean_relative(rel_dir)
        name = rel_dir.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(name.casefold(), n) for n in self.names) or any(under(rel_dir, p) for p in self.paths)

    def path(self, rel_file):
        """Is the file inside a folder that is left out?"""
        parts = clean_relative(rel_file).split("/")[:-1]
        return any(self.folder("/".join(parts[:i + 1])) for i in range(len(parts)))


def walk_inventory(settings, root_path, prefix, rules, on_skip=None, progress=None):
    """Every video file under root/prefix: (files, unreadable, walk_errors). files are {rel, size, mtime}; a file that exists but cannot be
    read is listed as unreadable with the reason; a folder that cannot be listed is reported so the files in it are NOT assumed deleted."""
    base = os.path.join(root_path, *[p for p in clean_relative(prefix).split("/") if p]) if clean_relative(prefix) else root_path
    files, unreadable, walk_errors = [], [], []

    def on_error(exc):
        walk_errors.append(str(exc))

    for folder, dirs, names in os.walk(base, onerror=on_error):
        settings.check()
        rel_folder = clean_relative(os.path.relpath(folder, root_path)) if folder != root_path else ""
        kept = [d for d in dirs if not rules.folder("/".join(filter(None, [rel_folder, d])))]
        for skipped in sorted(set(dirs) - set(kept)):
            if on_skip:
                on_skip("/".join(filter(None, [rel_folder, skipped])))
        dirs[:] = sorted(kept)
        for filename in sorted(names):
            if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            rel = "/".join(filter(None, [rel_folder, filename]))
            path = os.path.join(folder, filename)
            try:
                stat = with_retries(settings, lambda p=path: os.stat(p), f"stat {path}")
            except OSError as exc:
                unreadable.append({"rel": rel, "file_name": filename, "unreadable_reason": f"stat failed: {exc}"})
                continue
            files.append({"rel": rel, "size": stat.st_size, "mtime": stat.st_mtime})
            if progress and len(files) % 100 == 0:
                progress(len(files))
    return files, unreadable, walk_errors
