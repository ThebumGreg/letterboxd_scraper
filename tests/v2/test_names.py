"""A film file named in another alphabet is renamed from the film's own title."""
from synchub.library import names, store

from .library_helpers import add


def test_foreign_letters_are_found():
    assert names.has_foreign_letters("500 дней лета (2009).mkv")
    assert names.has_foreign_letters("千と千尋の神隠し (2001).mkv")
    assert not names.has_foreign_letters("Amélie (2001).mkv") and not names.has_foreign_letters("Heat (1995).mkv")


def test_the_english_name_comes_from_the_title_and_year():
    assert names.english_name("(500) Days of Summer", 2009, ".mkv") == "(500) Days of Summer (2009).mkv"
    assert names.english_name("What: A Film?", None, ".mkv") == "What A Film.mkv"
    assert names.english_name("500 дней лета", 2009, ".mkv") is None


def film(conn, title, year):
    return conn.execute("INSERT INTO media (kind, title, year) VALUES ('movie', ?, ?)", (title, year)).lastrowid


def test_a_linked_russian_name_is_replaced_and_an_approved_one_is_not(conn):
    mid = film(conn, "(500) Days of Summer", 2009)
    rel = "Movies/(500) Days of Summer (2009)/500 дней лета.2009.WEB-DL.2160p.mkv"
    fid = add(conn, root="media", rel=rel, name=rel.rsplit("/", 1)[-1], size=10)
    approved = add(conn, root="media", rel="Movies/Other/Другой фильм (2010).mkv", name="Другой фильм (2010).mkv", size=10)
    conn.execute("UPDATE library_file SET media_id = ?, media_type = 'Movie/Unknown', recommended_name = '500 дней лета (2009).mkv' WHERE id = ?", (mid, fid))
    conn.execute("UPDATE library_file SET media_id = ?, media_type = 'Movie/Unknown', recommended_name = 'Другой фильм (2010).mkv', approved_name = 'Mine (2010).mkv' WHERE id = ?", (mid, approved))
    assert names.fix_foreign(conn) == 1
    assert conn.execute("SELECT recommended_name FROM library_file WHERE id = ?", (fid,)).fetchone()[0] == "(500) Days of Summer (2009).mkv"
    assert conn.execute("SELECT recommended_name FROM library_file WHERE id = ?", (approved,)).fetchone()[0] == "Другой фильм (2010).mkv"
    assert any(e["event_type"] == "name_translated" for e in store.events_for(conn, fid))
    assert names.fix_foreign(conn) == 0                                   # nothing more to do
