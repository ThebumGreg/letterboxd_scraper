"""Pick a stream URL a TV can play straight from Jellyfin (for devices that take a URL instead of a Jellyfin session)."""
from urllib.parse import urlencode

DIRECT_VIDEO = {"h264", "hevc"}
DIRECT_AUDIO = {"aac", "ac3", "eac3", "mp3"}


def stream_url(jf, item, base=None, mode="auto", start_ticks=None):
    """-> (url, format, why). `mode`: auto (direct when the file suits the TV, else transcode), direct, or hls."""
    base = (base or jf.url).rstrip("/")
    item_id = item["Id"]
    source = (item.get("MediaSources") or [{}])[0]
    source_id = source.get("Id") or item_id
    containers = {c.strip().lower() for c in (source.get("Container") or item.get("Container") or "").split(",") if c.strip()}
    streams = source.get("MediaStreams") or item.get("MediaStreams") or []
    video = next((s.get("Codec", "").lower() for s in streams if s.get("Type") == "Video"), "")
    audio = next((s.get("Codec", "").lower() for s in streams if s.get("Type") == "Audio"), "")
    container = "mkv" if containers & {"mkv", "matroska"} else "mp4" if containers & {"mp4", "mov", "m4v"} else None
    suits = container and (not video or video in DIRECT_VIDEO) and (not audio or audio in DIRECT_AUDIO)
    if mode == "direct" or (mode == "auto" and suits):
        query = urlencode({"static": "true", "mediaSourceId": source_id, "api_key": jf.key})
        return (f"{base}/Videos/{item_id}/stream?{query}", container or "mp4",
                f"direct play ({container or 'unknown container'}, {video or '?'} / {audio or '?'})")
    params = {"MediaSourceId": source_id, "VideoCodec": "h264", "AudioCodec": "aac",
              "TranscodingMaxAudioChannels": 2, "api_key": jf.key}
    if start_ticks:
        params["StartTimeTicks"] = int(start_ticks)
    return (f"{base}/Videos/{item_id}/master.m3u8?{urlencode(params)}", "hls",
            f"HLS transcode (the file is {'/'.join(sorted(containers)) or '?'}, {video or '?'} / {audio or '?'})")
