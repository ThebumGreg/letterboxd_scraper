from email.utils import formatdate
from typing import Optional
from database import get_aggregated_active_films
from radarr_client import fetch_radarr_library_titles, normalize_title

def generate_radarr_rss_xml(list_type: Optional[str] = None, exclude_owned: bool = True) -> str:
    active_films = get_aggregated_active_films(list_type=list_type)
    owned_titles = fetch_radarr_library_titles() if exclude_owned else set()

    last_build_date = formatdate(usegmt=True)
    item_nodes = []

    for film in active_films:
        display_name = (film["full_display_name"] or film["movie_title"]).strip()
        normalized_display = normalize_title(display_name)

        if exclude_owned and normalized_display in owned_titles:
            continue

        item_nodes.append(f"""      <item>
          <title><![CDATA[{display_name}]]></title>
          <guid isPermaLink="false">{film['movie_slug']}</guid>
      </item>""")

    items_joined = "\n".join(item_nodes)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
      <title>Letterboxd Custom Multi-User RSS</title>
      <description>Radarr compatible movie list</description>
      <link>http://localhost:5000</link>
      <lastBuildDate>{last_build_date}</lastBuildDate>
{items_joined}
  </channel>
</rss>"""