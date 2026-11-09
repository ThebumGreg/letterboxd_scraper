"""Finding the films that are not in a folder of their own.

A film is *loose* when the folder it sits in is shared: it holds another film's video file, or it sits directly in a top-level library folder of its share
(`Movies/Heat (1995).mkv`, `Old Movies/Alien.mkv`, `Kids Movies/Cars.mkv`) or at the top of the share. Jellyfin and Radarr want one folder per film. Moving a
loose film into `<its library folder>/Title (Year)/` is the rename-in-place the Library page already offers (see layout._movie_folder). This only reads the
library list; nothing is touched here.
"""
import os
from collections import Counter

from synchub.library import vocab as v

WHAT = (("loose", "Loose in a shared folder"), ("alone", "Already in its own folder"))


def _folder(rec):
    return os.path.dirname(v.clean_relative(rec["relative_path"]))


def loose_ids(conn):
    """The ids of the (present) movie files that are loose, as a set."""
    rows = [dict(r) for r in conn.execute("SELECT id, root_name, relative_path, media_type FROM library_file WHERE missing_since IS NULL")]
    movies = [r for r in rows if str(r["media_type"] or "").startswith("Movie")]
    sharing = Counter((r["root_name"], _folder(r).casefold()) for r in movies)
    out = set()
    for r in movies:
        folder = _folder(r)
        if sharing[(r["root_name"], folder.casefold())] > 1 or folder.count("/") == 0:      # shared folder, or top-level folder / top of the share
            out.add(r["id"])
    return out


def narrow(conn, rows, what):
    """Keeps the (record, recommendation) rows matching the `what` filter ('loose' or 'alone'); any other value keeps them all."""
    if what not in ("loose", "alone"):
        return rows
    loose = loose_ids(conn)
    return [(rec, r) for rec, r in rows if str(rec.get("media_type") or "").startswith("Movie") and ((rec["id"] in loose) == (what == "loose"))]
