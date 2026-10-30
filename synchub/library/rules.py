"""The rules a conversion profile can set on top of the global Options.

A profile stores only what it overrides (`conversion_profile.rules_json`); everything else follows the global setting, so a new profile behaves like
the Options page until it is edited, and changing a global default still reaches every profile that did not override it. `resolve()` gives the
effective value of every rule for a profile; the size policy, the Versions code and the planner all read their numbers from it.
"""
import json

from synchub import settings

# key: (kind, global setting that supplies the default, allowed values or (low, high))
SPEC = {
    "size_policy": ("bool", "size_policy", None),
    "tv_size_policy": ("bool", "tv_size_policy", None),
    "cap_2160": ("float", "cap_mbps_2160", (2, 80)),            # video bitrate ceiling in Mbps by picture height
    "cap_1080": ("float", "cap_mbps_1080", (1, 40)),
    "cap_720": ("float", "cap_mbps_720", (0.5, 20)),
    "cap_sd": ("float", "cap_mbps_sd", (0.3, 10)),
    "tv_cap_share": ("float", "tv_cap_share", (0.2, 1)),         # an episode's ceiling as a share of the film ceiling
    "trigger_factor": ("float", "size_trigger_factor", (1, 5)),  # compress only when the video is this many times over the ceiling
    "safety_gb_2160": ("float", "safety_gb_2160", (0, 500)),     # a film this big is compressed whatever its bitrate (0 = never)
    "safety_gb_1080": ("float", "safety_gb_1080", (0, 500)),
    "crf": ("int", "size_crf", (14, 35)),
    "preset": ("choice", "size_preset", ("fast", "medium", "slow", "slower")),
    "dv_profile_5": ("choice", "dolby_vision_profile_5", ("review", "hdr10")),
    "dv_profile_7_8": ("choice", "dolby_vision_profile_7_8", ("hdr10", "review", "remux", "leave")),
    "gpu": ("bool", "gpu_encoding", None),
    "vmaf_mode": ("choice", "vmaf_mode", ("off", "warn", "hold")),
    "clean_names": ("bool", "clean_names", None),
    "audio_mode": ("choice", "audio_mode", ("keep", "add_compatible", "compatible_only")),
    "extra_copy": ("bool", "versions_on", None),
    "extra_min_gb": ("float", "variant_min_gb", (0, 500)),
    "extra_height": ("int", "variant_height", (240, 2160)),
    "extra_label": ("label", "variant_label", None),
    "extra_crf": ("int", "variant_crf", (14, 35)),
    "extra_maxrate_kbps": ("int", "variant_maxrate_kbps", (500, 100000)),
    "extra_audio_kbps": ("int", "variant_audio_kbps", (64, 640)),
    "extra_stereo": ("bool", "variant_stereo", None),
}


class RulesError(ValueError):
    """A rule has a value that cannot be used; the message is for the person using the page."""


def _coerce(key, raw):
    """The clean value of one rule, or RulesError."""
    kind, _, allowed = SPEC[key]
    if kind == "bool":
        return raw in (True, 1, "1", "true", "on", "yes")
    if kind == "choice":
        if str(raw) not in allowed:
            raise RulesError(f"'{raw}' is not a choice for {key}.")
        return str(raw)
    if kind == "label":
        import re
        text = str(raw or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,19}", text):
            raise RulesError("The label of the extra copy is 1 to 20 letters, numbers, spaces or . _ + -.")
        return text
    try:
        number = int(raw) if kind == "int" else float(raw)
    except (TypeError, ValueError):
        raise RulesError(f"{key} must be a number.")
    low, high = allowed
    if not low <= number <= high:
        raise RulesError(f"{key} must be between {low:g} and {high:g}.")
    return number


def clean(overrides):
    """The overrides from a form or a stored profile with unknown keys dropped. Empty or None values mean 'follow the Options' and are left out."""
    out = {}
    for key, raw in (overrides or {}).items():
        if key in SPEC and raw not in (None, ""):
            out[key] = _coerce(key, raw)
    return out


def loads(text):
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return clean(data) if isinstance(data, dict) else {}


def dumps(overrides):
    return json.dumps(clean(overrides), sort_keys=True) if clean(overrides) else None


def defaults(conn=None):
    """The global value of every rule."""
    out = {}
    for key, (kind, setting, allowed) in SPEC.items():
        raw = settings.get(setting, conn=conn)
        try:
            out[key] = _coerce(key, raw)
        except RulesError:
            out[key] = _coerce(key, settings.DEFAULTS.get(setting)) if settings.DEFAULTS.get(setting) not in (None, "") else raw
    return out


def resolve(conn=None, profile=None):
    """The effective value of every rule for `profile` (a profile dict with `rules`, or None for the global defaults)."""
    return {**defaults(conn), **((profile or {}).get("rules") or {})}
