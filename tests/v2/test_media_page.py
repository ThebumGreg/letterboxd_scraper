import re

from synchub import workers
from tests.v2.media_helpers import all_tabs


def tabs(page):
    return re.findall(r'id="media-tab-(\w+)"', page)


def test_library_cleanup_workers_and_storage_are_tabs_of_one_page(client):
    for path, active in (("/library", "library"), ("/workers", "workers"), ("/jobs", "jobs"), ("/originals", "originals"), ("/storage", "storage"), ("/library/options", "options")):
        page = client.get(path).get_data(as_text=True)
        assert tabs(page) == ["library", "originals", "workers", "jobs", "storage", "options"]
        assert re.search(r'class="nav-link active" id="media-tab-%s"' % active, page)
        for key in ("library", "originals", "workers", "jobs", "storage", "options"):
            assert 'id="media-pane-%s"' % key in page
        built = re.findall(r'data-lazy-pane="(\w+)"', page)
        assert active not in built and len(built) == 5              # only the open tab is built; the other five are fetched when opened
        assert all_tabs(client, path).count('id="fleet"') == 1 and 'id="libraryLive"' in all_tabs(client, path) and 'id="shareModal"' in all_tabs(client, path)


def test_a_tab_is_fetched_when_opened_and_respects_permissions(client):
    data = client.get("/media/pane/jobs").get_json()
    assert data["ok"] and "jobsLive" in data["tab"] and "<script" in data["scripts"]
    assert client.get("/media/pane/nope").status_code == 403


def test_the_workers_menu_opens_outside_the_scrolling_table(client, v2):
    with v2.connection() as conn:
        workers.create(conn, "encoder-1")
    page = client.get("/workers").get_data(as_text=True)
    assert """data-bs-popper-config='{"strategy":"fixed"}'""" in page
