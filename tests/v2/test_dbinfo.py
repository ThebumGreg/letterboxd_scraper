"""The database size card on Tools, the 7-day event default and the scan default (Details only)."""
from synchub import dbinfo, jellyfin_events, jobs, settings


def test_sizes_lists_the_biggest_tables_and_compact_runs_when_idle(client, v2):
    out = client.get("/api/tools/db-sizes").get_json()
    assert out["ok"] and out["file_mb"] >= 0 and out["tables"] and {"name", "rows", "mb"} <= set(out["tables"][0])
    done = client.post("/api/tools/db-compact", json={}).get_json()
    assert done["ok"] and "before_mb" in done and "Compacted" in done["message"]
    assert "Compact database" in client.get("/tools").get_data(as_text=True)


def test_compact_is_refused_while_a_job_runs(client, v2):
    with v2.connection() as conn:
        job_id = jobs.create(conn, "library", title="Busy")
        conn.execute("UPDATE job SET status = 'running' WHERE id = ?", (job_id,))
    refused = client.post("/api/tools/db-compact", json={})
    assert refused.status_code == 409 and "running" in refused.get_json()["message"]


def test_events_are_kept_seven_days_and_scans_look_at_details_only_by_default(v2):
    assert jellyfin_events.keep_days() == 7
    assert settings.DEFAULTS["library_integrity_mode"] == "metadata"
