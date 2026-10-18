import sqlite3

# 1. Connect to your database file (replace 'your_database.db' with your actual file name)

from config import DB_PATH


DB_FILE = str(DB_PATH)


conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

# 2. Execute the UPDATE command using placeholders (?) for safety
sql_command = "UPDATE enable_sonarr_automation SET Key = ? WHERE Key = ?"
cursor.execute(sql_command, (1, 0))

# 3. Commit the changes to the database
conn.commit()

# 4. Close the connection
conn.close()

print("Table updated successfully!")