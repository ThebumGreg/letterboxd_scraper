"""Who is watching what, and on which device: Jellyfin's live sessions, turned into plain rows for the Sessions page."""
import logging

logger = logging.getLogger(__name__)

TICKS = 10_000_000          # Jellyfin counts time in 100-nanosecond ticks
METHODS = {"DirectPlay": ("Direct play", "success"), "DirectStream": ("Direct stream", "success"), "Transcode": ("Transcoding", "warning")}
REASONS = {"ContainerNotSupported": "container", "VideoCodecNotSupported": "video codec", "AudioCodecNotSupported": "audio codec", "SubtitleCodecNotSupported": "subtitles",
           "ContainerBitrateExceedsLimit": "bitrate", "VideoBitrateNotSupported": "video bitrate", "AudioBitrateNotSupported": "audio bitrate", "VideoRangeTypeNotSupported": "HDR format",
           "VideoProfileNotSupported": "video profile", "VideoLevelNotSupported": "video level", "AudioChannelsNotSupported": "audio channels", "DirectPlayError": "playback error"}


def _clock(seconds):
    seconds = int(seconds or 0)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def _title(item):
    if item.get("Type") == "Episode" and item.get("SeriesName"):
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        code = f"S{season:02d}E{episode:02d} " if isinstance(season, int) and isinstance(episode, int) else ""
        return f"{item['SeriesName']}: {code}{item.get('Name') or ''}".strip()
    year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    return f"{item.get('Name') or 'Unknown'}{year}"


def describe(raw):
    """One Jellyfin session as a row; None for something that is not a person's device (SyncHub's own API connection)."""
    if not isinstance(raw, dict) or raw.get("Client") == "SyncHub":
        return None
    item = raw.get("NowPlayingItem") if isinstance(raw.get("NowPlayingItem"), dict) else None
    play = raw.get("PlayState") if isinstance(raw.get("PlayState"), dict) else {}
    row = {"user": raw.get("UserName") or "Not signed in", "device": raw.get("DeviceName") or "Unknown device", "client": raw.get("Client") or "",
           "last_activity": raw.get("LastActivityDate"), "playing": bool(item), "title": None, "state": "Idle", "progress": None, "position": None, "length": None,
           "method": None, "method_color": "secondary", "why": None, "kind": None}
    if not item:
        return row
    runtime = (item.get("RunTimeTicks") or 0) / TICKS
    position = (play.get("PositionTicks") or 0) / TICKS
    method, color = METHODS.get(play.get("PlayMethod"), (None, "secondary"))
    row.update(title=_title(item), kind=item.get("Type"), state="Paused" if play.get("IsPaused") else "Playing", position=_clock(position), length=_clock(runtime) if runtime else None,
               progress=round(100 * position / runtime) if runtime else None, method=method, method_color=color)
    info = raw.get("TranscodingInfo") if isinstance(raw.get("TranscodingInfo"), dict) else {}
    if play.get("PlayMethod") == "Transcode":
        reasons = [REASONS.get(r, r) for r in info.get("TranscodeReasons") or []]
        video = "video copied" if info.get("IsVideoDirect") else f"video re-encoded to {info.get('VideoCodec') or '?'}"
        row["why"] = ", ".join([video] + ([f"because of {', '.join(reasons)}"] if reasons else []))
    return row


def live(client):
    """The current rows: people playing first, then idle devices. Raises whatever the Jellyfin client raises."""
    rows = [r for r in (describe(s) for s in client.sessions()) if r]
    return sorted(rows, key=lambda r: (not r["playing"], r["user"].lower()))
