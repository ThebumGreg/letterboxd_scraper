"""Audio compatibility: a compatible track beside (or instead of) one that many players cannot decode.

Jellyfin does not pick an audio track by codec; it uses the file's default flag and the person's language. So the track that plays everywhere must be
first and flagged default. A profile's `audio_mode` chooses:

  keep             leave the audio alone
  add_compatible   add a E-AC-3 5.1 (or AAC stereo) track made from the main track, first and default, and keep the original behind it
  compatible_only  convert the main track to that and keep nothing of the original

Tracks that already play almost everywhere (AAC, AC-3, E-AC-3, MP3, Opus) are never touched. This module only decides; the worker makes the track.
"""
import json

COMPATIBLE = {"aac", "ac3", "eac3", "mp3", "opus", "vorbis"}
ENGLISH = {"en", "eng", "english"}
LABELS = {"eac3": "E-AC-3", "ac3": "AC-3", "aac": "AAC"}


def _streams(rec):
    try:
        data = json.loads(rec.get("streams_json") or "[]")
    except ValueError:
        return []
    return [s for s in data if isinstance(s, dict) and s.get("type") == "audio" and isinstance(s.get("index"), int)]


def _kept(audio, change):
    if isinstance(change.get("keep_audio"), list):
        return [i for i in change["keep_audio"] if i in {s["index"] for s in audio}]
    gone = {t.get("index") for t in change.get("remove_audio") or [] if isinstance(t, dict)}
    return [s["index"] for s in audio if s["index"] not in gone]


def main_track(audio, kept, change):
    """The track the plan treats as the main one: the chosen default, else the first English one that is kept, else the first kept."""
    by_index = {s["index"]: s for s in audio}
    if change.get("default_audio") in kept:
        return by_index[change["default_audio"]]
    kept_streams = [by_index[i] for i in kept]
    return next((s for s in kept_streams if (s.get("lang") or "").lower() in ENGLISH), kept_streams[0] if kept_streams else None)


def target_for(track):
    """(codec, channels, kbps) of the compatible track made from `track`."""
    channels = int(track.get("channels") or 2)
    if channels > 2:
        channels = min(channels, 6)
        return "eac3", channels, 640 if channels >= 6 else 448
    return "aac", 2, 192


def title_for(codec, channels):
    return f"Compatible ({LABELS[codec]} {'5.1' if channels == 6 else '2.0' if channels == 2 else str(channels) + 'ch'})"


def apply(rec, change, mode):
    """(change, note) with the audio mode applied. `change` is the plan's track change (or None); the note says in plain words what was done or why not."""
    if mode not in ("add_compatible", "compatible_only"):
        return change, None
    audio = _streams(rec)
    change = dict(change or {})
    kept = _kept(audio, change)
    track = main_track(audio, kept, change)
    if track is None:
        return (change or None), None
    if (track.get("codec") or "").lower() in COMPATIBLE:
        return (change or None), f"The main audio ({(track.get('codec') or '').upper()}) already plays on almost every device."
    codec, channels, kbps = target_for(track)
    change["keep_audio"] = kept
    if mode == "add_compatible":
        change["audio_add"] = {"from": track["index"], "codec": codec, "channels": channels, "kbps": kbps, "title": title_for(codec, channels)}
        change.pop("default_audio", None)
        note = f"Adds a compatible {LABELS[codec]} track (first and default) and keeps the original {(track.get('codec') or '').upper()}."
    else:
        converting = dict(change.get("audio_convert") or {})
        converting[str(track["index"])] = {"codec": codec, "channels": channels, "kbps": kbps}
        change["audio_convert"] = converting
        change["default_audio"] = track["index"]
        note = f"Converts the main audio ({(track.get('codec') or '').upper()}) to {LABELS[codec]} and does not keep the original."
    change.setdefault("op", "keep_tracks")
    return change, note
