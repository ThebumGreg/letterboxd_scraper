"""The movie size policy (docs/DESIGN_ENCODING.md 11.2): a movie larger than `movie_max_gb` is compressed toward `movie_target_gb`.

Pure decisions over a Library record's facts; nothing here touches a file. The answer is a `Decision`:

  none      the policy does not apply (off, too small, TV with no cap, or removing the unwanted tracks already brings it under the maximum)
  compress  re-encode to HEVC with a constant-quality encode (`profile.crf`) whose bitrate is capped (`maxrate_kbps`) so the result cannot pass the
            target; the target is a ceiling, not a goal: a film that looks right below it ends smaller
  review    a person has to decide first (the cap would break the quality floor, or the source is a kind we do not compress by default)
  blocked   compressing is not offered at all (Dolby Vision profile 5, or the audio alone leaves no room)

The profile uses the fields of a conversion profile (`video_codec`, `crf`, `preset`, `ten_bit`, `audio_kbps`) plus the size cap
(`maxrate_kbps`, `bufsize_kbps`, `max_bytes`) and the HDR facts the worker needs to keep the colours right.
"""
import dataclasses
import json
from typing import List, Optional

from synchub import settings
from synchub.library import vocab as v

GIB = 1024 ** 3
BUDGET_SHARE = 0.99                  # the video gets 99 % of what is left of the target after audio and subtitles
SOURCE_SHARE = 0.90                  # never ask for more than 90 % of the source's own video bitrate
MAXRATE_HEADROOM = 1.05              # the cap sits 5 % over the target video bitrate
# Bits per pixel per frame below which HEVC starts to look visibly starved, by picture height.
BPP_FLOORS = ((2000, 0.05), (1300, 0.06), (1000, 0.07), (0, 0.09))
# kbps assumed for a track whose bitrate the file does not say.
AUDIO_GUESS = {"truehd": 4000, "mlp": 4000, "dts": 1536, "flac": 1800, "pcm_s24le": 4600, "pcm_s16le": 1536, "eac3": 640, "ac3": 448, "aac": 256, "opus": 128, "mp3": 192}
AUDIO_DEFAULT_KBPS = 640
IMAGE_SUBTITLES = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "vobsub")

NONE, COMPRESS, REVIEW, BLOCKED = "none", "compress", "review", "blocked"


@dataclasses.dataclass
class Settings:
    on: bool = False
    movie_max_gb: float = 30.0
    movie_target_gb: float = 25.0
    tv_max_gb: float = 0.0
    tv_target_gb: float = 0.0
    crf: int = 22
    preset: str = "medium"
    dv_profile_5: str = "review"           # review | hdr10
    dv_profile_7_8: str = "hdr10"          # hdr10 | review


@dataclasses.dataclass
class Decision:
    action: str
    reasons: List[str] = dataclasses.field(default_factory=list)       # why a person has to look (review/blocked)
    notes: List[str] = dataclasses.field(default_factory=list)         # what the plan does that is worth knowing (HDR kept, Dolby Vision layer lost...)
    profile: Optional[dict] = None
    numbers: dict = dataclasses.field(default_factory=dict)

    @property
    def summary(self):
        n = self.numbers
        if self.action == COMPRESS and n:
            return f"Compress {n['size_gb']} GB to at most {n['target_gb']} GB (HEVC, video up to {n['target_video_kbps']} kbps)."
        return " ".join(self.reasons)


def _float(key, default, conn=None):
    try:
        return max(0.0, float(settings.get(key, conn=conn)))
    except (TypeError, ValueError):
        return default


def load(conn=None):
    """The policy's settings. `size_policy` off (the default) means nothing is ever compressed."""
    try:
        crf = min(40, max(10, int(settings.get("size_crf", conn=conn))))
    except (TypeError, ValueError):
        crf = 22
    preset = settings.get("size_preset", conn=conn)
    return Settings(on=settings.get("size_policy", conn=conn) == "1", movie_max_gb=_float("movie_max_gb", 30.0, conn), movie_target_gb=_float("movie_target_gb", 25.0, conn),
                    tv_max_gb=_float("tv_episode_max_gb", 0.0, conn), tv_target_gb=_float("tv_episode_target_gb", 0.0, conn), crf=crf,
                    preset=preset if preset in ("fast", "medium", "slow", "slower") else "medium",
                    dv_profile_5="hdr10" if settings.get("dolby_vision_profile_5", conn=conn) == "hdr10" else "review",
                    dv_profile_7_8="review" if settings.get("dolby_vision_profile_7_8", conn=conn) == "review" else "hdr10")


def _json(value, default):
    try:
        out = json.loads(value) if isinstance(value, str) and value else value
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def _track_kbps(track):
    if track.get("bitrate_kbps"):
        return float(track["bitrate_kbps"])
    if track.get("type") == "subtitle":
        return 30.0 if str(track.get("codec") or "").lower() in IMAGE_SUBTITLES else 1.0
    return float(AUDIO_GUESS.get(str(track.get("codec") or "").lower(), AUDIO_DEFAULT_KBPS))


def _height_floor(height):
    return next(floor for limit, floor in BPP_FLOORS if (height or 0) >= limit)


def _size(plans, tracks):
    """(kept tracks, removed tracks) once the track rules in `plans` have been applied."""
    plan = next((p for p in plans if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")), None)
    if plan is None:
        return list(tracks), []
    keep = {"audio": set(plan.get("keep_audio") or []) if isinstance(plan.get("keep_audio"), list) else None,
            "subtitle": set(plan.get("keep_subtitle") or []) if isinstance(plan.get("keep_subtitle"), list) else None}
    kept, removed = [], []
    for track in tracks:
        wanted = keep.get(track.get("type"))
        (kept if wanted is None or track.get("index") in wanted else removed).append(track)
    return kept, removed


def decide(rec, plans=(), cfg=None):
    """What the size policy says about one Library record, after the track rules in `plans` (the record's proposed or approved track changes)."""
    cfg = cfg or Settings()
    if not cfg.on:
        return Decision(NONE)
    movie = (rec.get("media_type") or "").lower().startswith("movie")
    ceiling, target_gb = (cfg.movie_max_gb, cfg.movie_target_gb) if movie else (cfg.tv_max_gb, cfg.tv_target_gb)
    size = rec.get("size_bytes") or 0
    if ceiling <= 0 or target_gb <= 0 or size <= 0:
        return Decision(NONE)
    tracks = [t for t in _json(rec.get("streams_json"), []) if isinstance(t, dict)]
    kept, removed = _size(list(plans), tracks)
    duration = float(rec.get("duration_seconds") or 0)
    if duration <= 0:
        return Decision(BLOCKED, ["The film's length is not known, so a size budget cannot be worked out. Scan it again."])
    removed_bytes = sum(_track_kbps(t) for t in removed) * 1000 / 8 * duration
    after = size - removed_bytes
    if after <= ceiling * GIB:
        return Decision(NONE, notes=[f"About {after / GIB:.1f} GB once the unwanted tracks are gone: under the {ceiling:g} GB limit."] if removed else [])

    info = _json(rec.get("video_info_json"), {})
    reasons, notes = [], []
    width, height = _resolution(rec, info)
    fps = _fps(rec)
    hdr, bit_depth = info.get("hdr") or "sdr", int(info.get("bit_depth") or 8)

    audio_kbps = sum(_track_kbps(t) for t in kept if t.get("type") == "audio")
    sub_kbps = sum(_track_kbps(t) for t in kept if t.get("type") == "subtitle")
    kept_bytes = (audio_kbps + sub_kbps) * 1000 / 8 * duration
    target_bytes = target_gb * GIB
    budget = target_bytes * BUDGET_SHARE - kept_bytes
    numbers = {"size_gb": round(size / GIB, 1), "after_tracks_gb": round(after / GIB, 1), "target_gb": target_gb, "max_gb": ceiling,
               "audio_kbps": round(audio_kbps), "subtitle_kbps": round(sub_kbps), "duration": round(duration)}
    if budget <= 0:
        return Decision(BLOCKED, [f"The audio and subtitles alone ({kept_bytes / GIB:.1f} GB) leave no room under {target_gb:g} GB. Remove tracks or raise the target for this film."], numbers=numbers)
    target_kbps = budget * 8 / duration / 1000
    source_kbps = info.get("video_kbps") or max(0.0, (rec.get("bitrate_kbps") or 0) - sum(_track_kbps(t) for t in tracks if t.get("type") == "audio"))
    if source_kbps:
        target_kbps = min(target_kbps, source_kbps * SOURCE_SHARE)
    numbers.update(target_video_kbps=round(target_kbps), source_video_kbps=round(source_kbps) if source_kbps else None)

    if width and height and fps:
        bpp = target_kbps * 1000 / (width * height * fps)
        floor = _height_floor(height)
        numbers.update(bpp=round(bpp, 3), bpp_floor=floor)
        if bpp < floor:
            reasons.append(f"To fit {target_gb:g} GB the video would get {bpp:.3f} bits per pixel, under the {floor} that keeps HEVC looking clean at {height}p. "
                           "Accept the lower quality, raise the target for this film, or leave it alone.")

    keep_hdr = None
    if hdr == "dv":
        profile = str(info.get("dv_profile") if info.get("dv_profile") is not None else "")
        if profile == "5":
            if cfg.dv_profile_5 != "hdr10":
                return Decision(BLOCKED, ["Dolby Vision profile 5 cannot be compressed safely: without its Dolby layer the colours turn green and purple. It is left as it is."], numbers=numbers)
            notes.append("Dolby Vision profile 5 is encoded as plain HDR10 (your setting); expect wrong colours on any player.")
        elif profile in ("7", "8"):
            if cfg.dv_profile_7_8 == "review":
                reasons.append(f"Dolby Vision profile {profile}: compressing drops the Dolby layer and keeps HDR10 only.")
            else:
                notes.append(f"Dolby Vision profile {profile} is encoded as HDR10; the Dolby Vision layer is lost.")
        else:
            reasons.append(f"Dolby Vision profile '{profile or 'unknown'}' is not one we know how to compress.")
        keep_hdr = "hdr10" if info.get("transfer") == "smpte2084" or profile in ("7", "8", "5") else None
    elif hdr in ("hdr10", "hlg"):
        keep_hdr = hdr
        notes.append(f"{'HLG' if hdr == 'hlg' else 'HDR10'} is kept (10-bit, colour tags and mastering data copied).")
    if (rec.get("video_codec") or "").lower() in ("hevc", "h265"):
        notes.append("The video is already HEVC, so re-encoding loses some quality (generation loss).")
    if info.get("interlaced"):
        notes.append("Interlaced picture: deinterlaced while encoding.")

    maxrate = round(target_kbps * MAXRATE_HEADROOM)
    profile = {"video_codec": "hevc", "crf": cfg.crf, "preset": cfg.preset, "ten_bit": True if keep_hdr or bit_depth >= 10 else True, "audio_kbps": None,
               "maxrate_kbps": maxrate, "bufsize_kbps": maxrate * 2, "max_bytes": int(ceiling * GIB), "target_bytes": int(target_bytes), "size_policy": True,
               "deinterlace": bool(info.get("interlaced"))}
    if keep_hdr:
        profile["hdr"] = {"kind": keep_hdr, **{k: info.get(k) for k in ("transfer", "primaries", "matrix", "master_display", "max_cll") if info.get(k)}}
    return Decision(REVIEW if reasons else COMPRESS, reasons, notes, profile, numbers)


def _resolution(rec, info):
    if info.get("width") and info.get("height"):
        return int(info["width"]), int(info["height"])
    try:
        w, h = str(rec.get("resolution") or "").lower().split("x")
        return int(w), int(h)
    except ValueError:
        return None, None


def _fps(rec):
    try:
        return float(rec.get("frame_rate") or 0) or None
    except (TypeError, ValueError):
        return None


def apply(plan, decision):
    """The job plan with the compression added: the plan's op becomes `convert`, its video action `transcode` (to HEVC) and the profile rides along."""
    out = dict(plan)
    change = dict(out.get("change") or {})
    change.setdefault("op", "convert")
    change["video"] = {**(change.get("video") or {}), "action": "transcode", "codec": "hevc"}
    out.update(op="convert", change=change, profile=decision.profile, size_policy=decision.numbers)
    return out
