# export_to_csv.py
import csv
import io
import os
import sqlite3
from datetime import datetime
from database import get_db_connection

def generate_consolidated_csv_buffer():
    """Generates a CSV in memory combining the Master Catalog and UI state."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT mc.title, mc.release_year, mc.media_type, mc.status, 
               mc.in_jellyfin, mc.in_radarr, mc.in_sonarr, mc.requested_by_users,
               mc.movie_slug, mc.tmdb_id, mc.tvdb_id, mc.last_updated
        FROM media_catalog mc
        ORDER BY mc.media_type, mc.title
    """)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
            
    return output

def export_database_to_csv():
    buffer = generate_consolidated_csv_buffer()
    export_dir = "exports"
    os.makedirs(export_dir, exist_ok=True)
    
    filename = f"master_catalog_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(export_dir, filename)
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        f.write(buffer.getvalue())
        
    print(f"Master Catalog successfully exported to {filepath}")
    return filepath