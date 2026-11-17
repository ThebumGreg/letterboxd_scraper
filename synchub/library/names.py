"""A film file named in another alphabet ("500 дней лета.2009.WEB-DL.2160p.mkv") gets the English name of the film it is linked to ("(500) Days of Summer (2009).mkv").

The worker's name cleaner only tidies the text it is given, so a Russian, Greek, Chinese or Arabic title stays as it was. Once the file is linked to its film
(film_link.relink_all, after every scan) the recommended name is rebuilt from the film's own title and year. A name a person approved is never changed.
"""
import logging
import os
import re

from synchub.library import store

logger = logging.getLogger(__name__)
LATIN_MAX = 0x24F                      # Latin letters with accents end here; anything above is another alphabet
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def has_foreign_letters(name):
    """True when a good share of the letters in a file name (without its extension) are not Latin."""
    stem = os.path.splitext(name or "")[0]
    letters = [c for c in stem if c.isalpha()]
    foreign = [c for c in letters if ord(c) > LATIN_MAX]
    return len(foreign) >= 2 and len(foreign) >= len(letters) * 0.3


def english_name(title, year, ext):
    """'Title (Year).ext' from the film's own title, with the characters Windows shares refuse taken out; None without a usable title."""
    clean = re.sub(r"\s+", " ", INVALID.sub(" ", title or "")).strip(" .")
    if not clean or has_foreign_letters(clean):
        return None                       # the film's own title is not in Latin letters either: nothing better to offer
    return f"{clean}" + (f" ({int(year)})" if year else "") + ext


def fix_foreign(conn):
    """Rebuilds the recommended name of every linked film file whose name is in another alphabet. Returns how many were renamed."""
    changed = 0
    rows = conn.execute("""SELECT f.id, f.recommended_name, f.file_name, m.title, m.year FROM library_file f JOIN media m ON m.id = f.media_id
                           WHERE f.media_id IS NOT NULL AND f.missing_since IS NULL AND f.approved_name IS NULL AND f.media_type LIKE 'Movie%' AND m.kind = 'movie'""").fetchall()
    for r in rows:
        current = r["recommended_name"] or r["file_name"]
        if not has_foreign_letters(current):
            continue
        new = english_name(r["title"], r["year"], os.path.splitext(current)[1])
        if not new or new == current:
            continue
        conn.execute("UPDATE library_file SET recommended_name = ? WHERE id = ?", (new, r["id"]))
        store.event(conn, r["id"], "system", "name_translated", None, {"from": current, "to": new, "note": f"The name was not in Latin letters, so it now follows the film's title: {new}"})
        changed += 1
    if changed:
        logger.info(f"[library] {changed} file name(s) in another alphabet were given the film's own title")
    return changed
