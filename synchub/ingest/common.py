"""Helpers shared by every source sync: upsert without ever deleting, and mark rows a source stopped reporting."""


def touch(conn, table, conflict_cols, data, now, keep=()):
    """Inserts or updates a source row and stamps last_seen; clears removed_at.

    Columns listed in `keep` are not overwritten with NULL when the source gives no value (e.g. a TMDB ID that a
    later resolver step filled in, which the next scrape of the list page does not repeat).
    """
    cols = list(data) + ["first_seen", "last_seen"]
    values = list(data.values()) + [now, now]
    updates = []
    for col in data:
        if col in conflict_cols:
            continue
        updates.append(f"{col} = COALESCE(excluded.{col}, {table}.{col})" if col in keep else f"{col} = excluded.{col}")
    updates += ["last_seen = excluded.last_seen", "removed_at = NULL"]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT({', '.join(conflict_cols)}) DO UPDATE SET {', '.join(updates)}",
        values,
    )


def mark_unseen(conn, table, key_cols, seen, now, scope_sql="1=1", scope_params=(), allow_empty=False):
    """Sets removed_at on active rows (inside the scope) whose key was not in `seen`. Returns how many.

    An empty `seen` is treated as a failed fetch and removes nothing, unless allow_empty is set.
    """
    seen = list(seen)
    if not seen and not allow_empty:
        return 0
    conn.execute("DROP TABLE IF EXISTS temp._seen")
    columns = ", ".join(f"k{i} TEXT" for i in range(len(key_cols)))
    pk = ", ".join(f"k{i}" for i in range(len(key_cols)))
    conn.execute(f"CREATE TEMP TABLE _seen ({columns}, PRIMARY KEY ({pk}))")
    conn.executemany(f"INSERT OR IGNORE INTO _seen VALUES ({', '.join('?' * len(key_cols))})", [(k,) if not isinstance(k, tuple) else k for k in seen])
    match = " AND ".join(f"_seen.k{i} = {table}.{col}" for i, col in enumerate(key_cols))
    cur = conn.execute(
        f"UPDATE {table} SET removed_at = ? WHERE removed_at IS NULL AND ({scope_sql}) AND NOT EXISTS (SELECT 1 FROM _seen WHERE {match})",
        (now, *scope_params),
    )
    conn.execute("DROP TABLE temp._seen")
    return cur.rowcount
