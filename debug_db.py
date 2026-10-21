import sqlite3
import json

DB_PATH = "data/letterboxd.db"
TARGET_SLUG = "tangled-2010" # Replace if needed

def run_diagnostics():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    print(f"\n--- 1. CATALOG ENTRY FOR: {TARGET_SLUG} ---")
    catalog = conn.execute("SELECT * FROM media_catalog WHERE movie_slug = ?", (TARGET_SLUG,)).fetchone()
    if catalog:
        cat_dict = dict(catalog)
        print(json.dumps(cat_dict, indent=2))
        tvdb_id = cat_dict.get('tvdb_id')
    else:
        print("Not found in media_catalog.")
        tvdb_id = None

    print(f"\n--- 2. JELLYFIN EPISODE CACHE ---")
    jf_cache = conn.execute(
        "SELECT season_number, episode_number, title FROM service_cache WHERE service='jellyfin' AND (movie_slug=? OR tvdb_id=?)", 
        (TARGET_SLUG, str(tvdb_id) if tvdb_id else "NONE")
    ).fetchall()
    
    if jf_cache:
        print(f"Found {len(jf_cache)} episodes in Jellyfin cache.")
        for r in jf_cache[:5]: # Print first 5
            print(dict(r))
        if len(jf_cache) > 5: print("... (truncated)")
    else:
        print("No episodes found in Jellyfin cache.")

    conn.close()

if __name__ == "__main__":
    run_diagnostics()