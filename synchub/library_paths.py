"""Finds Jellyfin libraries whose folders overlap, so one file is not shown in two libraries and a parent library does not swallow a child's folder.

Works on what Jellyfin's /Library/VirtualFolders reports (the paths as Jellyfin sees them). It cannot tell that two different paths are the same
share mounted twice; only Jellyfin's own item paths or the container's volume list show that."""


def _norm(path):
    return (path or "").rstrip("/") or "/"


def _inside(child, parent):
    return child != parent and (parent == "/" or child.startswith(parent + "/"))


def find_overlaps(folders):
    """`folders` is Jellyfin's library list. Returns a list of {kind, path, libraries, message}, kind being one of
    same_path, nested, case_only, mixed_parent."""
    entries = []                   # (library name, normalised path, library collection type)
    for lib in folders or []:
        for path in lib.get("Locations") or []:
            entries.append((lib.get("Name") or "?", _norm(path), lib.get("CollectionType")))
    found = []
    by_path = {}
    for name, path, _ in entries:
        by_path.setdefault(path, []).append(name)
    for path, names in sorted(by_path.items()):
        names = sorted(set(names))
        if len(names) > 1:
            found.append({"kind": "same_path", "path": path, "libraries": names,
                          "message": f"{path} is a folder of {len(names)} libraries ({', '.join(names)}), so Jellyfin lists its items in each."})
    lowered = {}
    for path in by_path:
        lowered.setdefault(path.lower(), set()).add(path)
    for spellings in lowered.values():
        if len(spellings) > 1:
            found.append({"kind": "case_only", "path": sorted(spellings)[0], "libraries": sorted({n for p in spellings for n in by_path[p]}),
                          "message": f"{' and '.join(sorted(spellings))} differ only in capital letters. Linux treats them as different folders."})
    for child_name, child, _ in entries:
        for parent_name, parent, parent_type in entries:
            if _inside(child, parent) and child_name != parent_name:
                mixed = not parent_type
                found.append({"kind": "mixed_parent" if mixed else "nested", "path": child, "libraries": sorted({child_name, parent_name}),
                              "message": f"{child} ({child_name}) is inside {parent}, a folder of {parent_name}"
                                         + (", a mixed-content library that will also read it in its own way." if mixed else ".")})
    unique, seen = [], set()
    for f in found:
        key = (f["kind"], f["path"], tuple(f["libraries"]))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique
