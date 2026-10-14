from flask import Flask, Response, request
from rss_feed import generate_radarr_rss_xml
from database import init_db

app = Flask(__name__)

# Initialize database schema on web server startup if not present
init_db()

@app.get("/rss/radarr")
def get_radarr_rss():
    """
    Serves the dynamic RSS feed payload for Radarr.
    Query parameters:
      - /rss/radarr (returns all active entries)
      - /rss/radarr?list_type=watchlist
      - /rss/radarr?list_type=films
    """
    list_type = request.args.get("list_type")
    xml_payload = generate_radarr_rss_xml(list_type=list_type)
    return Response(xml_payload, mimetype="application/rss+xml")

if __name__ == "__main__":
    # Host on all network interfaces on port 5000
    app.run(host="0.0.0.0", port=5000)