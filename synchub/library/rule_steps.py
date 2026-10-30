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
