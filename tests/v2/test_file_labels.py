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


# ---------------------------------------------------------------- pages and buttons
from synchub import jobs  # noqa: E402
from synchub.library import aside  # noqa: E402


def two_copies(conn):
    media_id = film(conn)
    a = fixture_file(conn, rel=HEAT, size=7_000_000_000)
    b = fixture_file(conn, rel="Heat (1995)/Heat copy.mkv", size=7_000_000_000)
    conn.execute("UPDATE library_file SET duplicate_group = 'DUP-1', duplicate_status = ?, sha256 = 'x' WHERE id = ?", (v.DUP_PRIMARY, a))
    conn.execute("UPDATE library_file SET duplicate_group = 'DUP-1', duplicate_status = ?, sha256 = 'x' WHERE id = ?", (v.DUP_OTHER, b))
    conn.commit()
    return media_id, a, b


def test_the_film_page_has_one_files_section_with_labels_and_actions(client, conn):
    media_id, a, b = two_copies(conn)
    page = client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert "Why more than one file?" in page and "Duplicate · keep this one" in page and "Duplicate file" in page
    assert f'data-file-act="set-aside" data-record="{b}"' in page and f'data-file-act="set-aside" data-record="{a}"' not in page
    assert "Play this version" not in page                      # Jellyfin has no address in the test settings


def test_edition_main_not_this_film_and_keep_both_buttons(client, conn):
    media_id, a, b = two_copies(conn)
    quality.copies(conn, media_id)
    conn.commit()
    assert client.post(f"/api/library/file/{b}/edition", json={"on": True, "name": "Director's Cut"}).get_json()["ok"]
    assert rec_of(conn, b)["is_edition"] == 1 and rec_of(conn, b)["file_label"] == "edition"
    assert client.post(f"/api/library/file/{b}/main", json={"on": True}).get_json()["ok"] and rec_of(conn, b)["is_main"] == 1
    assert client.post(f"/api/library/file/{a}/duplicate-note", json={"value": "keep_both"}).get_json()["ok"] and rec_of(conn, a)["dup_review"] == "keep_both"
    assert not client.post(f"/api/library/file/{a}/duplicate-note", json={"value": "nonsense"}).get_json()["ok"]
    assert client.post(f"/api/library/file/{a}/not-this-film").get_json()["ok"] and rec_of(conn, a)["media_id"] is None


def test_a_duplicate_can_be_set_aside_and_put_back_but_never_the_kept_copy_or_the_last_one(client, conn):
    media_id, a, b = two_copies(conn)
    quality.copies(conn, media_id)
    conn.commit()
    assert not client.post(f"/api/library/file/{a}/set-aside").get_json()["ok"]                # the copy to keep
    ok = client.post(f"/api/library/file/{b}/set-aside").get_json()
    assert ok["ok"]
    job = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside'").fetchone())
    plan = jobs.get(conn, job["id"])["plan"]
    assert plan["op"] == "rename" and plan["aside"] and plan["needs"] == ["aside"]
    assert job["dest_rel"] == "Heat (1995)/Heat copy.mkv.synchub-duplicate"
    assert not client.post(f"/api/library/file/{b}/set-aside").get_json()["ok"]                # already being done
    conn.execute("UPDATE job SET status = 'done', result = '{}' WHERE id = ?", (job["id"],))
    conn.commit()
    assert aside.sync(conn, job["id"])
    conn.commit()
    assert rec_of(conn, b)["dup_review"] == "aside" and rec_of(conn, b)["file_label"] is None
    page = client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert "Hidden files" in page and f'data-file-act="put-back" data-record="{b}"' in page
    back = client.post(f"/api/library/file/{b}/put-back").get_json()
    assert back["ok"] and conn.execute("SELECT source_rel, dest_rel FROM job WHERE ref_type = 'library_aside' ORDER BY id DESC").fetchone()[0].endswith(".synchub-duplicate")


def test_home_and_the_library_show_the_same_counts_and_filters(client, conn):
    media_id = film(conn)
    fixture_file(conn, rel=HEAT, size=40_000_000_000, resolution="3840x2160")
    mobile = fixture_file(conn, rel=MOBILE, size=4_000_000_000)
    film_link.relink_all(conn)
    conn.commit()
    home = client.get("/").get_data(as_text=True)
    assert "1 film with more than one file" in home and "1 mobile copy" in home and 'value="file_mobile"' in home and f"1 film" in home
    listing = client.get("/library/queue?status=all&label=mobile").get_data(as_text=True)
    assert "Heat (1995) - 1080p.mkv" in listing and "Heat (1995).mkv<" not in listing.split("queueTable")[-1].replace("Heat (1995) - 1080p.mkv", "")
    assert mobile and client.post("/api/library/links/refresh").get_json()["ok"]


def test_the_record_page_shows_the_label_and_the_other_files_of_the_film(client, conn):
    media_id = film(conn)
    a = fixture_file(conn, rel=HEAT, size=40_000_000_000)
    b = fixture_file(conn, rel=MOBILE, size=4_000_000_000)
    film_link.relink_all(conn)
    conn.commit()
    page = client.get(f"/library/record/{b}").get_data(as_text=True)
    assert "Mobile copy" in page and f"/movie/{media_id}" in page and "other files of this film" in page and a


# ---------------------------------------------------------------- old versions and hiding any file that is not the main one
def old_and_new(conn):
    media_id = film(conn)
    new = fixture_file(conn, rel=HEAT, size=40_000_000_000, resolution="3840x2160")
    old = fixture_file(conn, rel="Heat (1995)/Heat.1995.DVDRip.mkv", size=1_000_000_000)
    quality.copies(conn, media_id)
    conn.commit()
    return media_id, new, old


def test_an_old_version_is_labelled_at_once_and_hidden_with_its_own_suffix(client, conn):
    media_id, new, old = old_and_new(conn)
    assert client.post(f"/api/library/file/{old}/set-aside", json={"reason": "old_version"}).get_json()["ok"]
    assert rec_of(conn, old)["file_label"] == "old_version"
    job = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside'").fetchone())
    assert job["dest_rel"] == "Heat (1995)/Heat.1995.DVDRip.mkv.synchub-old" and jobs.get(conn, job["id"])["plan"]["reason"] == "old_version"
    conn.execute("UPDATE job SET status = 'done', result = '{}' WHERE id = ?", (job["id"],))
    conn.commit()
    assert aside.sync(conn, job["id"])
    conn.commit()
    row = rec_of(conn, old)
    assert row["dup_review"] == "aside" and row["hidden_reason"] == "old_version" and row["hidden_at"]
    assert [f["id"] for f in quality.copies(conn, media_id)["files"]] == [new]


def test_the_main_file_and_the_last_file_are_never_hidden(client, conn):
    media_id, new, old = old_and_new(conn)
    refused = client.post(f"/api/library/file/{new}/set-aside", json={"reason": "old_version"}).get_json()
    assert not refused["ok"] and "main file" in refused["message"]
    client.post(f"/api/library/file/{old}/main", json={"on": True})
    assert not client.post(f"/api/library/file/{old}/set-aside", json={"reason": "other"}).get_json()["ok"]           # the main file you chose
    client.post(f"/api/library/file/{old}/main", json={"on": False})
    assert not client.post(f"/api/library/file/{old}/set-aside", json={}).get_json()["ok"]                            # no reason given


def test_make_this_the_main_hides_the_old_main_as_an_old_version(client, conn):
    media_id, new, old = old_and_new(conn)
    out = client.post(f"/api/library/file/{old}/replace-main").get_json()
    assert out["ok"] and rec_of(conn, old)["is_main"] == 1
    hidden = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside'").fetchone())
    assert hidden["ref_id"] == new and jobs.get(conn, hidden["id"])["plan"]["reason"] == "old_version"
    assert rec_of(conn, new)["file_label"] == "old_version"
    assert not client.post(f"/api/library/file/{old}/replace-main").get_json()["ok"]                                  # it is the main file already


def test_a_failed_hide_gives_the_old_version_label_back(client, conn):
    media_id, new, old = old_and_new(conn)
    client.post(f"/api/library/file/{old}/set-aside", json={"reason": "old_version"})
    job = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside'").fetchone())
    conn.execute("UPDATE job SET status = 'failed' WHERE id = ?", (job["id"],))
    conn.commit()
    assert not aside.sync(conn, job["id"])
    conn.commit()
    assert rec_of(conn, old)["is_old_version"] == 0 and rec_of(conn, old)["file_label"] == "unexplained"


def test_putting_a_hidden_file_back_uses_the_suffix_of_its_reason(client, conn):
    media_id, new, old = old_and_new(conn)
    client.post(f"/api/library/file/{old}/set-aside", json={"reason": "old_version"})
    job = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside'").fetchone())
    conn.execute("UPDATE job SET status = 'done', result = '{}' WHERE id = ?", (job["id"],))
    conn.commit()
    aside.sync(conn, job["id"])
    conn.commit()
    assert client.post(f"/api/library/file/{old}/put-back").get_json()["ok"]
    back = dict(conn.execute("SELECT * FROM job WHERE ref_type = 'library_aside' ORDER BY id DESC").fetchone())
    assert back["source_rel"].endswith(".synchub-old") and back["dest_rel"] == "Heat (1995)/Heat.1995.DVDRip.mkv"
    conn.execute("UPDATE job SET status = 'done', result = '{}' WHERE id = ?", (back["id"],))
    conn.commit()
    aside.sync(conn, back["id"])
    conn.commit()
    assert rec_of(conn, old)["dup_review"] is None and rec_of(conn, old)["hidden_reason"] is None and rec_of(conn, old)["is_old_version"] == 0
