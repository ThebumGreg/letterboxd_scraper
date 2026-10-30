"""What a conversion profile's rules will do to a file, as steps the Library queue can show (compress, Dolby Vision 7 to 8.1, a compatible audio track, an extra copy).

The planner (`migrate.plan_for`) is what really builds the job; this module asks the same rules the same questions so the list a person approves from matches
what will happen. A file with any of these steps sits in the "Format conversion" group.
"""
import dataclasses

from synchub.library import audio, profiles, size_policy


@dataclasses.dataclass
class RuleSteps:
    steps: list
    notes: list
    headline: str


def for_record(conn, rec, plans, cache=None):
    """RuleSteps for a pending Library record, or None when its profile's rules change nothing. `plans` are the analysis' track changes; `cache` ({profile id: (rules, size, versions config)}) spares lookups."""
    from synchub import versions
    cache = cache if cache is not None else {}
    profile = profiles.effective(conn, rec)
    if profile["move_only"]:
        return None
    if profile["id"] not in cache:
        cache[profile["id"]] = (profiles.rules(conn, profile), size_policy.load(conn, profile), versions.load(conn, profile))
    rules, size, extras = cache[profile["id"]]
    steps, notes, headlines = [], [], []
    change, audio_note = audio.apply(rec, plans[0] if plans else None, rules.get("audio_mode"))
    if audio_note:
        steps.append(_audio_step(change))
        notes.append(audio_note)
        headlines.append("Add a compatible audio track")
    decision = size_policy.decide(rec, [change] if change else [], size)
    if decision.action == size_policy.COMPRESS:
        steps.append(decision.summary)
        notes += decision.notes
        headlines.insert(0, "Compress to HEVC")
    elif decision.action == size_policy.REVIEW:
        steps.append("Ask before compressing: " + " ".join(decision.reasons))
        headlines.insert(0, "Compress (needs a look)")
    elif decision.notes and "8.1" in " ".join(decision.notes):
        steps.append("Convert Dolby Vision 7 to 8.1 (no re-encode)")
        headlines.insert(0, "Convert Dolby Vision 7 to 8.1")
    elif versions.dv_entry(rec, extras):
        steps.append("Convert Dolby Vision 7 to 8.1 (no re-encode)")
        headlines.insert(0, "Convert Dolby Vision 7 to 8.1")
    entry = versions.variant_entry(rec, extras)
    if entry and entry["can_approve"]:
        steps.append(f"Make a {entry['height']}p copy named “{extras.label}”")
        headlines.append(f"{extras.label} copy")
    if not steps:
        return None
    return RuleSteps(steps, notes, " + ".join(headlines[:3]))


def _audio_step(change):
    add = (change or {}).get("audio_add")
    if add:
        layout = "5.1" if add["channels"] >= 6 else "stereo"
        return f"Add a compatible {add['codec'].upper()} {layout} audio track, first and default; the original is kept"
    return "Convert the main audio to a compatible track"


def quality_facts(conn, rec, plans=()):
    """What the record page shows about picture quality: the video bitrate against the ceiling for its size, what the compression rule would do and the size it
    would give, and the VMAF score of a finished job when there is one. Keys are left out when unknown."""
    import json
    profile = profiles.effective(conn, rec)
    size = size_policy.load(conn, profile)
    info = size_policy._json(rec.get("video_info_json"), {})
    width, height = size_policy._resolution(rec, info)
    out = {"profile": profile["name"]}
    kbps = info.get("video_kbps")
    if kbps:
        klass = size_policy.picture_class(width, height)
        movie = (rec.get("media_type") or "").lower().startswith("movie")
        if size.mode == "bitrate":
            ceiling = size.caps_mbps[klass] * (1 if movie else size.tv_cap_share)
            out.update(mbps=round(kbps / 1000, 1), ceiling_mbps=round(ceiling, 1), picture=klass, trigger_mbps=round(ceiling * size.trigger_factor, 1))
    decision = size_policy.decide(rec, list(plans), size)
    out["action"] = decision.action
    if decision.action in (size_policy.COMPRESS, size_policy.REVIEW) and decision.numbers.get("target_gb"):
        out.update(from_gb=decision.numbers["size_gb"], to_gb=decision.numbers["target_gb"])
    row = conn.execute("SELECT result FROM job WHERE ref_type = 'library_file' AND ref_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1", (rec["id"],)).fetchone()
    try:
        vmaf = (json.loads(row["result"]) if row and row["result"] else {}).get("vmaf")
    except ValueError:
        vmaf = None
    if isinstance(vmaf, dict) and vmaf.get("mean") is not None:
        out["vmaf"] = {"mean": vmaf["mean"], "min": vmaf.get("min"), "floor": vmaf.get("floor")}
    elif isinstance(vmaf, dict) and vmaf.get("skipped"):
        out["vmaf_skipped"] = vmaf["skipped"]
    return out
