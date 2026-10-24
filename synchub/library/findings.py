"""Findings that depend on more than one file, worked out on SyncHub from what the workers reported about each one.

A worker looks at a single file and reports what it found (its name, tracks, integrity, a plan). Whether two files would end up at the same
place, whether a file is already where it belongs, whether it is an exact copy of another file: only SyncHub knows, because it holds every record
from every scan on every share. This is the original tool's `finalize_rows`, `detect_name_collisions` and duplicate handling, applied to
the whole library instead of one scan, and re-run whenever the picture changes.
"""
import json
from collections import defaultdict

from synchub import db
from synchub.library import layout, store
from synchub.library import vocab as v


def base_findings(rec):
    try:
        data = json.loads(rec.get("findings_json") or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def destination_path(rec, cfg):
    """The layout path for a record, or None when no valid destination exists yet."""
    try:
        return layout.relative_display(rec, cfg)
    except layout.LayoutError:
        return None


def in_place(rec, cfg):
    """Is this file already at the right place in the destination share?"""
    path = destination_path(rec, cfg)
    return bool(path) and rec["root_name"] == cfg.destination_root and v.clean_relative(rec["relative_path"]).casefold() == path.casefold()


def collision_notes(rows, cfg):
    """{record id: [notes]} for files that would land on the same destination as a different file. Exact copies of one another are one file,
    not a collision. A file already sitting at the destination is not at fault: the others are told about it."""
    groups = defaultdict(list)
    for rec in rows:
        if base_findings(rec).get("naming_error") or not v.final_name(rec):
            continue
        path = destination_path(rec, cfg)
        if path:
            groups[path.casefold()].append(rec)
    notes = defaultdict(list)
    for members in groups.values():
        sources = {("dup", m["duplicate_group"]) if m["duplicate_group"] else ("file", m["id"]) for m in members}
        if len(sources) < 2:
            continue
        holder = next((m for m in members if in_place(m, cfg)), None)
        for member in members:
            if member is holder:
                continue
            own = ("dup", member["duplicate_group"]) if member["duplicate_group"] else ("file", member["id"])
            others = [f"{m['root_name']}:{m['relative_path']}" for m in members if (("dup", m["duplicate_group"]) if m["duplicate_group"] else ("file", m["id"])) != own][:2]
            if holder is not None:
                note = f"Destination collision: the destination already holds a different file ({holder['root_name']}:{holder['relative_path']})"
            else:
                note = f"Destination collision with {len(sources) - 1} other source file(s), e.g. {others}"
            notes[member["id"]].append(note)
    return notes


def derive(rec, notes=()):
    """Everything about a record that depends on other files, from its own findings plus what is now known about the rest of the library."""
    found = base_findings(rec)
    action = found.get("action") or rec.get("recommended_action")
    base = found.get("base_action")
    reason = found.get("action_reason") or ""
    flags = found.get("naming_flags") or ""
    reasons = [list(r) for r in found.get("reasons") or []]
    state = {"action": action, "base": base, "reason": reason}

    def escalate(text):
        """Switches the action to manual review and keeps the reason (the original action stays as the base)."""
        if state["action"] != v.ACTION_MANUAL_REVIEW:
            state["base"], state["action"] = state["action"], v.ACTION_MANUAL_REVIEW
            state["reason"] = "; ".join(filter(None, [state["reason"], text]))

    group, status = rec.get("duplicate_group"), rec.get("duplicate_status") or v.DUP_NOT_CHECKED
    if group and status == v.DUP_OTHER:
        note = "Exact SHA-256 duplicate; verify preferred copy before removal"
        reasons.append(["duplicate", note])
        escalate(note)
    for note in notes:
        flags = "; ".join(filter(None, [flags, note]))
        reasons.append(["naming", note])
    if group:
        reasons.append(["duplicate", f"Member of exact duplicate group {group}"])
    elif status.startswith("Hash incomplete"):
        reasons.append(["duplicate", status])
    if rec.get("integrity_status") in v.INT_BAD:
        reasons.append(["integrity", f"{rec['integrity_status']}: {(rec.get('audit_error') or '')[:200]}"])
    categories = sorted({c for c, _ in reasons})
    if set(categories) & {"naming", "integrity"}:
        escalate("; ".join(t for c, t in reasons if c in ("naming", "integrity") and t))
    action, base = state["action"], state["base"] or (state["action"] if state["action"] in v.MIGRATABLE_ACTIONS else None)
    return {"recommended_action": action, "base_action": base, "action_reason": state["reason"] or None, "naming_flags": flags or None,
            "review_categories": "; ".join(categories) or None, "review_reason": " | ".join(f"[{c}] {t}" for c, t in reasons if t) or None,
            "review_reasons_json": json.dumps(reasons, ensure_ascii=False), "duplicate_group": group, "duplicate_status": status,
            "new_review": v.REVIEW_MANUAL if (categories or action == v.ACTION_MANUAL_REVIEW) else v.REVIEW_NOT_REQUIRED}


def refresh(conn, cfg, run_id=None, now=None):
    """Recomputes the findings that depend on other files, for every readable file that is still there. Returns how many records changed."""
    now = now or db.now()
    rows = [dict(r) for r in conn.execute("SELECT * FROM library_file WHERE missing_since IS NULL AND unreadable_reason IS NULL AND findings_json IS NOT NULL")]
    notes = collision_notes(rows, cfg)
    store.begin(conn)
    changed = 0
    for rec in rows:
        if rec["migration_status"] == v.MIGRATION_IN_PROGRESS:
            continue
        if store.apply_derived(conn, rec, derive(rec, notes.get(rec["id"], ())), run_id, now):
            changed += 1
    return changed
