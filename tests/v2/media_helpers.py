"""The Media page builds only its open tab and fetches the others when they are first opened; tests that look at several tabs of one page read them all with this."""
TAB_KEYS = ("library", "versions", "originals", "workers", "players", "jobs", "storage", "options")


def all_tabs(client, path):
    """The page at `path` followed by the content and scripts of every tab it did not build itself."""
    page = client.get(path).get_data(as_text=True)
    parts = [page]
    for key in TAB_KEYS:
        if f'id="media-pane-{key}"' not in page:
            continue
        if f'data-lazy-pane="{key}"' in page:
            data = client.get(f"/media/pane/{key}").get_json()
            parts.append(data["tab"] + data["scripts"])
    return "\n".join(parts)
