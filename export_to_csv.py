import csv
import io
import os
from datetime import datetime
from database import get_db_connection

def generate_consolidated_csv_buffer():
    """Generates a CSV in memory for the Flask Web UI."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Fetch all records (update table name if yours differs)
    cursor.execute("SELECT * FROM scraped_records")
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
    """Writes the CSV to disk for the daily cron/scheduler job."""
    buffer = generate_consolidated_csv_buffer()
    export_dir = "exports"
    os.makedirs(export_dir, exist_ok=True)
    
    filename = f"database_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(export_dir, filename)
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        f.write(buffer.getvalue())
        
    print(f"Database successfully exported to {filepath}")
    return filepath