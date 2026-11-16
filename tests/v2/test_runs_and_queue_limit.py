"""Past runs of a file's checks and tests (compare two side by side), and the limit on conversions at once."""
import json

from synchub import compression_test, jobs, quality, runs, settings
from synchub.library import migrate, store
from synchub.library import vocab as v

from .library_helpers import CFG
from .test_compression_test import big, finish, result   # noqa: F401  (big is a fixture)
from .test_conversion_module import approved_record


def rec_of(conn, file_id):
    return dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone())


def test_every_compression_test_is_kept_and_listed_newest_first(conn, big):
    first = finish(conn, compression_test.request(conn, big, encoder="cpu"), result(vmaf=(95.0, 94.0, 96.0, 93.5)))
    second = finish(conn, compression_test.request(conn, big, encoder="gpu"), result(vmaf=(90.0, 91.0, 92.0, 89.0), encoder="gpu"))
    listed = runs.for_file(conn, rec_of(conn, big))["ctest"]
    assert [r["id"] for r in listed] == [second["id"], first["id"]] and all(r["current"] for r in listed)
    assert dict(listed[0]["fields"])["Encoder"] == "GPU" and dict(listed[1]["fields"])["Encoder"] == "CPU"
    assert dict(listed[1]["fields"])["Average VMAF"] == first["mean_vmaf"] and len(listed[0]["clips"]) == 4
    assert compression_test.get(conn, first["id"])["verdict"] == "pass"             # the older run is untouched by the newer one


def test_a_run_of_an_older_file_is_marked_not_current(conn, big):
    finish(conn, compression_test.request(conn, big), result())
    conn.execute("UPDATE library_file SET mtime = mtime + 1 WHERE id = ?", (big,))
    assert not runs.for_file(conn, rec_of(conn, big))["ctest"][0]["current"]


def test_quality_and_damage_checks_are_listed_apart(conn, big):
    first = quality.request(conn, big)
    second = quality.request(conn, big, mode="damage")
    listed = runs.for_file(conn, rec_of(conn, big))
    assert [r["id"] for r in listed["quality"]] == [first] and [r["id"] for r in listed["damage"]] == [second]


def test_the_record_page_lists_past_runs_with_the_data_to_compare_them(client, conn, big):
    finish(conn, compression_test.request(conn, big, encoder="cpu"), result())
    finish(conn, compression_test.request(conn, big, encoder="gpu"), result(vmaf=(90.0, 91.0, 92.0, 89.0), encoder="gpu"))
    page = client.get(f"/library/record/{big}").get_data(as_text=True)
    assert 'id="pastRuns"' in page and page.count('class="form-check-input run-pick"') == 2 and "Compare the two ticked" in page
    data = json.loads(page.split('id="runData">')[1].split("</script>")[0].replace("<\\/", "</"))
    assert len(data["ctest"]) == 2 and data["ctest"][0]["clips"][0]["vmaf"] == 90.0


def test_the_history_line_of_a_test_has_the_numbers(conn, big):
    finish(conn, compression_test.request(conn, big), result())
    note = [e["detail"]["note"] for e in store.events_for(conn, big) if e["event_type"] == "compression_test_done"][0]
    assert "Pass: VMAF 94.6" in note and "Standard" in note


# ---------------------------------------------------------------- conversions at once
def test_the_limit_holds_back_approved_files_and_feeds_them_as_places_free_up(conn):
    ids = [approved_record(conn, rel=f"Movies/{n}.mkv", name=f"{n} (200{i}).mkv", action="copy_rename", video="h264", profile_id=4) for i, n in enumerate("ABC")]
    settings.set_value("max_queued_conversions", "1", conn=conn)
    first = migrate.queue(conn, ids[0], CFG)
    assert first and migrate.queue(conn, ids[1], CFG) is None and migrate.queue(conn, ids[2], CFG) is None
    assert migrate.in_flight(conn) == 1 and migrate.waiting_for_slot(conn, rec_of(conn, ids[1]))
    assert [e["event_type"] for e in store.events_for(conn, ids[1])].count("waiting_for_slot") == 1
    migrate.queue(conn, ids[1], CFG)                                                  # asking again does not repeat the history line
    assert [e["event_type"] for e in store.events_for(conn, ids[1])].count("waiting_for_slot") == 1
    assert migrate.release_throttled(conn, CFG) == 0                                  # the place is still taken
    conn.execute("UPDATE job SET status = 'done' WHERE id = ?", (first,))
    conn.execute("UPDATE library_file SET migration_status = 'completed' WHERE id = ?", (ids[0],))
    assert migrate.release_throttled(conn, CFG) == 1 and migrate.in_flight(conn) == 1
    assert not migrate.waiting_for_slot(conn, rec_of(conn, ids[1])) and migrate.waiting_for_slot(conn, rec_of(conn, ids[2]))


def test_no_limit_by_default_and_forcing_ignores_it(conn):
    ids = [approved_record(conn, rel=f"Movies/{n}.mkv", name=f"{n} (200{i}).mkv", action="copy_rename", video="h264", profile_id=4) for i, n in enumerate("AB")]
    assert migrate.max_in_flight(conn) == 0 and all(migrate.queue(conn, i, CFG) for i in ids)
    settings.set_value("max_queued_conversions", "1", conn=conn)
    extra = approved_record(conn, rel="Movies/C.mkv", name="C (2005).mkv", action="copy_rename", video="h264", profile_id=4)
    assert migrate.queue(conn, extra, CFG) is None and migrate.queue(conn, extra, CFG, force=True)


def test_the_limit_is_saved_from_options(client, conn):
    assert client.post("/api/library/quality/settings", json={"max_queued_conversions": "6"}).get_json()["ok"] and migrate.max_in_flight(conn) == 6
    assert not client.post("/api/library/quality/settings", json={"max_queued_conversions": "900"}).get_json()["ok"]
