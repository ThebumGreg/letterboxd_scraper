import re

from synchub import workers


def tabs(page):
    return re.findall(r'id="media-tab-(\w+)"', page)


def test_library_versions_workers_and_storage_are_tabs_of_one_page(client):
    for path, active in (("/library", "library"), ("/versions", "versions"), ("/workers", "workers"), ("/originals", "originals"), ("/storage", "storage")):
        page = client.get(path).get_data(as_text=True)
        assert tabs(page) == ["library", "versions", "workers", "originals", "storage"]
        assert re.search(r'class="nav-link active" id="media-tab-%s"' % active, page)
        for key in ("library", "versions", "workers", "originals", "storage"):
            assert 'id="media-pane-%s"' % key in page
        assert 'id="fleet"' in page and 'id="libraryLive"' in page and 'id="shareModal"' in page


def test_the_workers_menu_opens_outside_the_scrolling_table(client, v2):
    with v2.connection() as conn:
        workers.create(conn, "encoder-1")
    page = client.get("/workers").get_data(as_text=True)
    assert """data-bs-popper-config='{"strategy":"fixed"}'""" in page
