import csv
import sqlite3
from pathlib import Path
from config import DB_PATH, EXPORTS_DIR
from logger import logger

def export_database_to_csv() -> None:
    if not DB_PATH.exists():
        logger.error(f"Database file not found at {DB_PATH}.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, username, list_type, movie_title, movie_slug, 
               full_display_name, film_identifier, movie_url, 
               is_present, first_seen_at, last_seen_at
        FROM user_films
        ORDER BY username ASC, list_type ASC, movie_title ASC;
    """)

    rows = cursor.fetchall()
    if not rows:
        logger.warning("No records found in database to export.")
        conn.close()
        return

    headers = [desc[0] for desc in cursor.description]
    main_export_path = EXPORTS_DIR / "letterboxd_export.csv"

    with open(main_export_path, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(list(row))

    logger.info(f"Full database exported to: {main_export_path}")

    for list_type in ["watchlist", "films"]:
        cursor.execute("""
            SELECT username, movie_title, full_display_name, movie_slug, 
                   movie_url, is_present, first_seen_at, last_seen_at
            FROM user_films
            WHERE list_type = ? AND is_present = 1
            ORDER BY movie_title ASC;
        """, (list_type,))
        
        type_rows = cursor.fetchall()
        if type_rows:
            type_headers = [desc[0] for desc in cursor.description]
            type_path = EXPORTS_DIR / f"export_{list_type}.csv"
            with open(type_path, mode="w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(type_headers)
                for r in type_rows:
                    w.writerow(list(r))
            logger.info(f"Exported active '{list_type}' to: {type_path}")

    conn.close()

if __name__ == "__main__":
    export_database_to_csv()