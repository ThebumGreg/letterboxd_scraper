"""The Jellyfin library layout, in ONE place so the page's path preview and the processing jobs can never disagree.

    TV Shows/<Show>/Season NN/<name>
    Movies/<name without extension>/<name>        (one folder per movie, as Jellyfin expects)

<name> is vocab.final_name(record): the person-approved name if one was edited, else the recommended name. The destination share is a
setting; only relative paths are made here.
"""
import os
import re

from synchub.library import vocab as v


class LayoutError(Exception):
    """No valid destination can be planned for this record."""


def _problems(label, text):
    return [f"{label} '{text}': {p}" for p in v.filename_problems(text)]


_TV_DASH = re.compile(r"^(?P<show>.*?) - (?P<code>S\d{2,}E\d{2,}(?:-E\d{2,})?)(?P<rest>.*)$")


def jellyfin_tv_name(name):
    """`Show - S01E01.mkv` -> `Show S01E01.mkv`, the form Jellyfin's documentation recommends. Names that do not look like that are returned unchanged."""
    m = _TV_DASH.match(name)
    return f"{m['show']} {m['code']}{m['rest']}" if m else name


def file_name(record, cfg):
    """The file name a record gets in the library: the person's approved name, else (when the profile cleans names) the recommended one in the configured style,
    else the file's own name."""
    if record.get("approved_name"):
        return record["approved_name"]
    if not cfg.clean_names:
        return record.get("file_name") or v.final_name(record)
    name = v.final_name(record)
    return jellyfin_tv_name(name) if name and cfg.naming_style == "jellyfin" and record.get("media_type") == "TV Episode" else name


def relative_parts(record, cfg=None):
    """The path components below the destination share. Raises LayoutError."""
    cfg = cfg or v.LibraryConfig()
    name = file_name(record, cfg)
    if not name:
        raise LayoutError("record has no filename")
    problems = _problems("filename", name)
    if record["media_type"] == "TV Episode":
        show, season = record["show_title"], record["season_number"]
        if not show or season is None:
            raise LayoutError("TV episode without show title / season")
        problems += _problems("show folder", show)
        parts = [cfg.tv_folder, show, f"Season {int(season):02d}", name]
    else:
        folder = os.path.splitext(name)[0]
        problems += _problems("movie folder", folder)
        parts = [cfg.movie_folder, folder, name]
    if problems:
        raise LayoutError("; ".join(problems))
    return parts


def relative_display(record, cfg=None):
    """The '/'-joined path, e.g. 'Movies/Heat (1995)/Heat (1995).mkv'."""
    return "/".join(relative_parts(record, cfg))
