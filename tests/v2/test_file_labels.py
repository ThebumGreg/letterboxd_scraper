"""What each file of a film is to the film (duplicate, mobile copy, converted copy, original, edition, unexplained), the saved film-to-file link and the counts."""
from synchub import quality
from synchub.library import file_labels, film_link
from synchub.library import vocab as v

from .test_quality import film, fixture_file, rec_of

HEAT = "Heat (1995)/Heat (1995).mkv"
MOBILE = "Heat (1995)/Heat (1995) - 1080p.mkv"
CUT = "Heat (1995)/Heat (1995) - Directors Cut.mkv"


def labels(conn, media_id):
    return {r["relative_path"]: r["file_label"] for r in conn.execute("SELECT relative_path, file_label FROM library_file WHERE media_id = ?", (media_id,))}


def test_a_film_with_one_file_has_no_label(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT)
    quality.copies(conn, media_id)
    assert labels(conn, media_id) == {HEAT: None}


def test_a_1080p_extra_copy_is_a_mobile_copy(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT, size=40_000_000_000, resolution="3840x2160")
    fixture_file(conn, rel=MOBILE, size=4_000_000_000)
    out = quality.copies(conn, media_id)
    assert labels(conn, media_id)[MOBILE] == "mobile"
    assert [f["label"] for f in out["files"] if f["name"].endswith("1080p.mkv")] == ["mobile"]


def test_two_identical_files_are_duplicates_and_the_primary_says_keep_this_one(conn):
    media_id = film(conn)
    a = fixture_file(conn, rel=HEAT, size=7_000_000_000)
    b = fixture_file(conn, rel="Heat (1995)/Heat copy.mkv", size=7_000_000_000)
    conn.execute("UPDATE library_file SET duplicate_group = 'DUP-1', duplicate_status = ? WHERE id = ?", (v.DUP_PRIMARY, a))
    conn.execute("UPDATE library_file SET duplicate_group = 'DUP-1', duplicate_status = ? WHERE id = ?", (v.DUP_OTHER, b))
    conn.commit()
    out = quality.copies(conn, media_id)
    texts = {f["id"]: f["label_text"] for f in out["files"]}
    assert texts == {a: "Duplicate · keep this one", b: "Duplicate file"}
    conn.execute("UPDATE library_file SET dup_review = 'keep_both' WHERE id = ?", (b,))
    conn.commit()
    assert {f["id"]: f["label_text"] for f in quality.copies(conn, media_id)["files"]}[b] == "Duplicate · keeping both"


def test_a_conversion_and_its_original_are_labelled_and_the_saving_is_shown(conn):
    media_id = film(conn)
    original = fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=50_000_000_000)
    converted = fixture_file(conn, rel="Heat (1995)/Heat (1995) HEVC.mkv", size=30_000_000_000)
    conn.execute("UPDATE library_file SET migration_status = ?, migrated_how = 'convert', migration_destination_path = ? WHERE id = ?",
                 (v.MIGRATION_COMPLETED, "media:Heat (1995)/Heat (1995) HEVC.mkv", original))
    conn.commit()
    # the same share: the conversion is "converted", the source is not "original" (that is for another share)
    out = {f["id"]: f for f in quality.copies(conn, media_id)["files"]}
    assert out[converted]["label"] == "converted" and out[converted]["saved_bytes"] == 20_000_000_000


def test_a_person_can_mark_an_edition_and_it_wins(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT)
    cut = fixture_file(conn, rel=CUT)
    conn.execute("UPDATE library_file SET is_edition = 1, edition_name = 'Director''s Cut' WHERE id = ?", (cut,))
    conn.commit()
    out = {f["id"]: f for f in quality.copies(conn, media_id)["files"]}
    assert out[cut]["label_text"] == "Edition: Director's Cut"


def test_a_second_file_nobody_can_explain_is_unexplained(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT)
    other = fixture_file(conn, rel="Heat (1995)/Heat.1995.Remastered.mkv")
    out = {f["id"]: f for f in quality.copies(conn, media_id)["files"]}
    assert out[other]["label"] == "unexplained"


def test_not_this_film_clears_the_link_and_a_scan_does_not_bring_it_back(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT)
    stray = fixture_file(conn, rel="Heat (1995)/Heat.1995.Remastered.mkv")
    quality.copies(conn, media_id)
    film_link.not_this_film(conn, stray)
    conn.commit()
    assert [f["id"] for f in quality.copies(conn, media_id)["files"]] != [] and stray not in [f["id"] for f in quality.copies(conn, media_id)["files"]]
    film_link.relink_all(conn)
    row = rec_of(conn, stray)
    assert row["media_id"] is None and row["media_link_source"] == "manual"


def test_the_link_is_saved_on_the_film_page_and_for_every_film_at_once(conn):
    media_id = film(conn)
    a = fixture_file(conn, rel=HEAT)
    b = fixture_file(conn, rel=MOBILE)
    outsider = fixture_file(conn, rel="Other (2000)/Other (2000).mkv")
    result = film_link.relink_all(conn)
    assert rec_of(conn, a)["media_id"] == media_id and rec_of(conn, b)["media_id"] == media_id and rec_of(conn, outsider)["media_id"] is None
    assert result["with_more_than_one"] == 1 and rec_of(conn, b)["file_label"] == "mobile"


def test_the_main_file_can_be_chosen_by_hand(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT, size=40_000_000_000, resolution="3840x2160")
    small = fixture_file(conn, rel="Heat (1995)/Heat.1995.alt.mkv", size=2_000_000_000)
    assert quality.copies(conn, media_id)["main"]["id"] != small
    conn.execute("UPDATE library_file SET is_main = 1 WHERE id = ?", (small,))
    conn.commit()
    out = quality.copies(conn, media_id)
    assert out["main"]["id"] == small and out["files"][0]["id"] == small


def test_the_counts_for_home_and_the_library(conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT, size=40_000_000_000)
    fixture_file(conn, rel=MOBILE, size=4_000_000_000)
    fixture_file(conn, rel="Lonely (2001)/Lonely (2001).mkv")
    film_link.relink_all(conn)
    counts = file_labels.counts(conn)
    assert counts["films"] == 1 and counts["files"]["mobile"] == 1 and counts["unlinked"] == 1 and counts["different_shares"] == 0


def test_classify_is_pure_and_empty_for_a_single_file():
    assert file_labels.classify([{"id": 1, "root_name": "media", "relative_path": "A/A.mkv", "file_name": "A.mkv"}]) == {1: None}
    assert file_labels.text("mobile") == "Mobile copy"
