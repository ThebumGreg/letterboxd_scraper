import sqlite3
from email.utils import formatdate
from typing import Optional
from config import DB_PATH

def generate_radarr_rss_xml(list_type: Optional[str] = None) -> str:
    """
    Queries active movies from SQLite DB and constructs a Radarr-compliant RSS XML payload.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Query active movies from user_films
    if list_type:
        cursor.execute("""
            SELECT full_display_name, movie_title 
            FROM user_films 
            WHERE is_present = 1 AND list_type = ?
            ORDER BY last_seen_at DESC;
        """, (list_type,))
    else:
        cursor.execute("""
            SELECT full_display_name, movie_title 
            FROM user_films 
            WHERE is_present = 1
            ORDER BY last_seen_at DESC;
        """)
        
    rows = cursor.fetchall()
    conn.close()

    # Current time formatted according to RFC 822 (e.g., "Wed, 14 Oct 2026 02:15:00 -0000")
    last_build_date = formatdate(usegmt=True)

    # Build XML items safely with CDATA wrapping
    item_nodes = []
    for row in rows:
        # Fallback to movie_title if full_display_name is empty
        display_name = (row["full_display_name"] or row["movie_title"]).strip()
        
        item_xml = f"""      <item>
          <title><![CDATA[{display_name}]]></title>
          <guid isPermaLink="false">{display_name}</guid>
      </item>"""
        item_nodes.append(item_xml)

    items_joined = "\n".join(item_nodes)

    # Complete RSS 2.0 Document
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
      <title>My custom RSS list</title>
      <description>Radarr compatible movie list</description>
      <link>http://example.com</link>
      <lastBuildDate>{last_build_date}</lastBuildDate>
{items_joined}
  </channel>
</rss>"""

    return xml_content