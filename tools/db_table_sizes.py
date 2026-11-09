"""Prints SyncHub's database size and the size of each table, biggest first (read-only).
Run on Unraid:  docker exec synchub python /app/tools/db_table_sizes.py /data/synchub.db
"""
import os, sqlite3, sys
path = sys.argv[1] if len(sys.argv) > 1 else "/data/synchub.db"
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
page = conn.execute("PRAGMA page_size").fetchone()[0]
total, free = conn.execute("PRAGMA page_count").fetchone()[0], conn.execute("PRAGMA freelist_count").fetchone()[0]
print(f"file {os.path.getsize(path)/1e6:.1f} MB, wal {os.path.getsize(path+'-wal')/1e6 if os.path.exists(path+'-wal') else 0:.1f} MB, "
      f"free (reclaimable by VACUUM) {free*page/1e6:.1f} MB of {total*page/1e6:.1f} MB")
try:
    rows = conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    owner = {r[0]: r[1] for r in conn.execute("SELECT name, CASE WHEN type='index' THEN tbl_name ELSE name END FROM sqlite_master")}
    sizes = {}
    for name, b in rows:
        sizes[owner.get(name, name)] = sizes.get(owner.get(name, name), 0) + b
    how = "dbstat (includes indexes)"
except sqlite3.Error:
    sizes, how = {}, "estimate from row counts"
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
out = []
for t in tables:
    n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    out.append((sizes.get(t, 0), t, n))
print(f"\nPer table, by {how}:")
for b, t, n in sorted(out, reverse=True)[:20]:
    print(f"{b/1e6:10.2f} MB  {n:>9} rows  {t}")
