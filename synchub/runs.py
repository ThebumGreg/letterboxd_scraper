"""Past runs of one file's picture quality checks, damage checks and compression tests, laid out so two of them can be compared side by side.

Nothing is stored here: every run already has its own row (`quality_check`, `compression_test`) and the Record page used to show only the newest. This module reads
them all and gives each a small dictionary the page can list and compare."""
import json

from synchub import compression_test, quality

KINDS = {"quality": "Picture quality", "damage": "Damage check", "ctest": "Compression test"}
LIMIT = 30                                  # runs kept in the list per kind; older ones are still stored


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _quality_run(row, rec):
    mode = row["mode"] if "mode" in row.keys() and row["mode"] else "quality"
    findings = [f.get("text") for f in _json(row["findings_json"], []) if isinstance(f, dict) and f.get("text")]
    metrics = _json(row["metrics_json"], {})
    done = row["status"] == "done"
    run = {"kind": "damage" if mode == "damage" else "quality", "id": row["id"], "when": (row["finished_at"] or row["created_at"] or "")[:16], "status": row["status"],
           "by": row["requested_by"], "source": row["source"], "current": row["size_bytes"] == rec.get("size_bytes") and row["mtime"] == rec.get("mtime"), "message": row["message"]}
    if mode == "damage":
        run["summary"] = row["message"] or row["status"]
        run["fields"] = [("Decode errors", metrics.get("errors") if done else None)]
    else:
        run["summary"] = (f"{row['score']:g} ({row['grade']})" if done and row["score"] is not None else (row["message"] or row["status"]))
        run["fields"] = [("Score", f"{row['score']:g}" if row["score"] is not None else None), ("Grade", row["grade"]), ("Parts sampled", row["samples"] or None)]
    run["findings"] = findings
    return run


def _ctest_run(row, rec, key):
    result = _json(row["result_json"], {})
    clips = [{"at": compression_test._clock(c.get("start")), "vmaf": c.get("vmaf"), "source_mbps": round((c.get("source_kbps") or 0) / 1000, 1),
              "encoded_mbps": round((c.get("encoded_kbps") or 0) / 1000, 1)} for c in result.get("clips") or []]
    skip = compression_test.atypical(result.get("clips") or [])
    for i, clip in enumerate(clips):
        clip["atypical"] = i in skip
    done = row["status"] == "done"
    encoder = result.get("encoder")
    return {"kind": "ctest", "id": row["id"], "when": (row["finished_at"] or row["created_at"] or "")[:16], "status": row["status"], "by": row["requested_by"],
            "current": row["size_bytes"] == rec.get("size_bytes") and row["mtime"] == rec.get("mtime") and row["profile_key"] == key, "message": row["message"],
            "summary": (f"{compression_test.VERDICT_LABELS.get(row['verdict'], 'Not scored')}" + (f", VMAF {row['mean_vmaf']:g}" if row["mean_vmaf"] is not None else "") +
                        (f", about {row['est_gb']} GB" if row["est_gb"] else "")) if done else (row["message"] or row["status"]),
            "fields": [("Profile", row["profile_name"]), ("Encoder", ("GPU" if encoder != "cpu" else "CPU") if encoder else None), ("Verdict", compression_test.VERDICT_LABELS.get(row["verdict"])),
                       ("Average VMAF", row["mean_vmaf"]), ("Worst clip", row["min_vmaf"]), ("VMAF to keep", row["floor"]), ("Estimated size (GB)", row["est_gb"]),
                       ("From (GB)", row["from_gb"]), ("Clips", f"{row['clips']} of {row['clip_seconds']} s"), ("Scored on", "tone-mapped HDR (rough)" if result.get("tonemapped") else None),
                       ("Minutes taken", round(result["elapsed"] / 60, 1) if result.get("elapsed") else None)],
            "clips": clips, "findings": []}


def for_file(conn, rec):
    """Every run of this file, newest first, as {"quality": [...], "damage": [...], "ctest": [...]} (each list at most LIMIT long). `current` says the run still describes the file as it is."""
    out = {"quality": [], "damage": [], "ctest": []}
    for row in conn.execute("SELECT * FROM quality_check WHERE library_file_id = ? ORDER BY id DESC", (rec["id"],)).fetchall():
        run = _quality_run(row, rec)
        if len(out[run["kind"]]) < LIMIT:
            out[run["kind"]].append(run)
    try:
        key = compression_test._profile_key(compression_test._plan_for(conn, rec, force=True)[0]["profile"])
    except compression_test.CompressionTestError:
        key = None
    for row in conn.execute("SELECT * FROM compression_test WHERE library_file_id = ? ORDER BY id DESC LIMIT ?", (rec["id"], LIMIT)).fetchall():
        out["ctest"].append(_ctest_run(row, rec, key))
    return out


def has_several(runs):
    return any(len(v) > 1 for v in runs.values())
