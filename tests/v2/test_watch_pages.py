"""The movie and show pages say where the person using them is: their status, position, last time and what to continue with."""
import html
import re

import pytest

from synchub import settings

from .test_watch_state import ALICE, BOB, HOUR, give, heat, severance      # noqa: F401  (the movie and the show these tests are about)


def text(response):
    return response.get_data(as_text=True)


def person(app, conn, name="Alice", jellyfin=ALICE):
    pid = conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES (?, ?)", (name, jellyfin)).lastrowid
    conn.commit()
    browser = app.test_client()
    browser.set_cookie("synchub_profile", str(pid))
    return browser


@pytest.fixture
def library(heat, severance, conn):
    """Heat and Severance are in the Jellyfin library, with a Jellyfin address so the Watch now buttons appear."""
    settings.set_value("jellyfin_url", "http://jf:8096")
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (heat,))
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('series1', 'series', 'a', 'a')")
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'series1', 'x', 1.0)", (severance,))
    conn.commit()
    return {"movie": heat, "show": severance}


def block(page):
    """The words of the 'Your progress' block (everything inside the div with id watchProgress), tags removed and spaces folded."""
    start = page.find('id="watchProgress"')
    if start < 0:
        return None
    start = page.index(">", start) + 1
    depth, position = 1, start
    for tag in re.finditer(r"<(/?)div\b", page[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            position = start + tag.start()
            break
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page[start:position]))).strip()


# ================================================================ a movie
def test_a_guest_sees_no_progress_block(client, library):
    assert 'id="watchProgress"' not in text(client.get(f"/movie/{library['movie']}"))


def test_a_profile_without_a_jellyfin_user_is_told_how_to_get_progress(app, conn, library):
    page = text(person(app, conn, "Dana", None).get(f"/movie/{library['movie']}"))
    assert 'id="watchUnlinked"' in page and "Dana's watch progress" in page and 'href="/people"' in page and 'data-resume' not in page


def test_a_movie_the_person_has_not_watched(app, conn, library):
    page = text(person(app, conn).get(f"/movie/{library['movie']}"))
    assert block(page) == "Your progress Not watched" and 'data-watch-state="unwatched"' in page and "data-resume" not in page


def test_a_movie_part_way_through_shows_where_they_stopped_and_resumes_from_there(app, conn, library):
    give(conn, ALICE, "m1", position=42 * 600_000_000, last="2026-10-20 19:03:11")
    page = text(person(app, conn).get(f"/movie/{library['movie']}"))
    assert block(page) == "Your progress In progress 42:00 of 2:00:00 (35%) Last watched Oct 20, 2026"
    assert 'aria-valuenow="35"' in page and 'style="width: 35%"' in page
    assert 'data-item="m1" data-title="Heat" data-size="lg" data-resume="at 42:00"' in page


def test_a_movie_they_have_finished(app, conn, library):
    give(conn, ALICE, "m1", played=1, count=3, last="2026-10-05 19:03:11")
    page = text(person(app, conn).get(f"/movie/{library['movie']}"))
    assert block(page) == "Your progress Watched Last watched Oct 5, 2026 · watched 3 times" and "data-resume" not in page and "progress-bar" not in page


def test_a_movie_watched_once_does_not_say_how_many_times(app, conn, library):
    give(conn, ALICE, "m1", played=1, count=1, last="2026-10-05 19:03:11")
    assert "times" not in block(text(person(app, conn).get(f"/movie/{library['movie']}")))


def test_watching_it_again_is_not_the_same_as_watching_it(app, conn, library):
    give(conn, ALICE, "m1", position=HOUR, played=1, count=1, last="2026-10-05 19:03:11")
    page = text(person(app, conn).get(f"/movie/{library['movie']}"))
    assert "Watching again" in block(page) and 'data-resume="at 1:00:00"' in page


def test_nobody_sees_someone_elses_progress(app, conn, library):
    give(conn, BOB, "m1", position=HOUR, last="2026-10-20 19:03:11")
    page = text(person(app, conn).get(f"/movie/{library['movie']}"))
    assert "Not watched" in block(page) and "1:00:00" not in page and "data-resume" not in page


def test_progress_that_jellyfin_stopped_reporting_is_gone(app, conn, library):
    give(conn, ALICE, "m1", position=HOUR, last="2026-10-20 19:03:11", removed="2026-11-01 00:00:00")
    assert "Not watched" in block(text(person(app, conn).get(f"/movie/{library['movie']}")))


# ================================================================ a show
def test_a_show_nobody_has_started_offers_its_first_episode(app, conn, library):
    page = text(person(app, conn).get(f"/show/{library['show']}"))
    assert block(page) == "You have watched 0 of 5 episodes in the library 0% Next up: S1E1 “Ep 1.1”" and 'data-size="lg" data-resume' not in page


def test_a_show_part_way_through_says_how_far_and_what_is_next(app, conn, library):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", played=1, count=1, last="2026-10-02 20:00:00")
    page = text(person(app, conn).get(f"/show/{library['show']}"))
    assert block(page) == "You have watched 2 of 5 episodes in the library 40% Next up: S1E3 “Ep 1.3” Last watched S1E2 on Oct 2, 2026"
    assert 'data-size="lg" data-resume' not in page                                        # between episodes there is nothing to resume


def test_an_episode_in_progress_is_what_to_continue_with_and_the_button_says_so(app, conn, library):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", position=HOUR // 2, runtime=HOUR, last="2026-10-02 20:00:00")
    page = text(person(app, conn).get(f"/show/{library['show']}"))
    assert block(page) == "You have watched 1 of 5 episodes in the library 20% Continue with S1E2 “Ep 1.2”, at 30:00 (50%) Last watched S1E2 on Oct 2, 2026"
    assert 'data-item="series1" data-title="Severance" data-size="lg" data-resume="S1E2 at 30:00"' in page


def test_a_show_they_have_finished(app, conn, library):
    for i, item_id in enumerate(("e11", "e12", "e13", "e21", "e22")):
        give(conn, ALICE, item_id, played=1, count=1, last=f"2026-10-0{i + 1} 20:00:00")
    page = text(person(app, conn).get(f"/show/{library['show']}"))
    assert block(page) == "You have watched 5 of 5 episodes in the library 100% You are all caught up with what is in the library. Last watched S2E2 on Oct 5, 2026"


def test_a_show_page_for_a_guest_and_for_an_unlinked_profile(client, app, conn, library):
    assert 'id="watchProgress"' not in text(client.get(f"/show/{library['show']}"))
    assert 'id="watchUnlinked"' in text(person(app, conn, "Dana", None).get(f"/show/{library['show']}"))


def test_the_show_page_knows_how_to_draw_watched_and_in_progress_episodes(app, conn, library):
    page = text(person(app, conn).get(f"/show/{library['show']}"))
    for needle in ('data-watch-state="watched"', 'data-watch-state="in_progress"', "const resume = w && w.state === 'in_progress'", "${mine}", "${resume}"):
        assert needle in page


# ================================================================ the episodes of a season
def season(browser, media_id, number=1):
    return browser.get(f"/api/show/{media_id}/season/{number}").get_json()["episodes"]


def test_each_episode_carries_what_the_person_has_watched_of_it(app, conn, library):
    give(conn, ALICE, "e11", played=1, count=2, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", position=HOUR // 2, runtime=HOUR, last="2026-10-02 20:00:00")
    episodes = {e["episode_number"]: e for e in season(person(app, conn), library["show"])}
    assert episodes[1]["watch"] == {"state": "watched", "state_label": "Watched", "percent": 100, "position_label": None, "runtime_label": "2:00:00",
                                    "last_played_label": "Oct 1, 2026", "play_count": 2, "resume_label": None}
    assert episodes[2]["watch"]["state"] == "in_progress" and episodes[2]["watch"]["position_label"] == "30:00" and episodes[2]["watch"]["resume_label"] == "30:00"
    assert episodes[2]["watch"]["percent"] == 50 and episodes[3]["watch"]["state"] == "unwatched"


def test_an_episode_that_is_not_in_the_library_has_nothing_to_say(app, conn, library):
    episodes = {e["episode_number"]: e for e in season(person(app, conn), library["show"], 2)}
    assert episodes[3]["watch"] is None and episodes[1]["watch"]["state"] == "unwatched"


def test_a_guest_and_an_unlinked_profile_get_no_watch_data(client, app, conn, library):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    assert all(e["watch"] is None for e in season(client, library["show"]))
    assert all(e["watch"] is None for e in season(person(app, conn, "Dana", None), library["show"]))


def test_someone_elses_episodes_are_not_yours(app, conn, library):
    give(conn, BOB, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    assert all(e["watch"]["state"] == "unwatched" for e in season(person(app, conn), library["show"]) if e["watch"])
