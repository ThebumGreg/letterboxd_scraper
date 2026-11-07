"""The AI quality test: run the usual quality check, an AI model check, or both on one file and see the results side by side.

It is an experiment for comparing the two. It never flags a file, requests a better copy or changes anything about the film. The AI half is a `quality_ai`
job (a `scan`-kind job like the usual check) that only a worker reporting the `quality_ai` feature takes. The worker reports, for each model it ran, a
`mean` already scaled to 0-100 (higher is better) and the unscaled `raw_mean`:

    {"op": "quality_ai", "device": "cpu", "elapsed": 212.4, "slices": 10, "frames": 30, "tonemapped": false,
     "models": {"musiq": {"version": "...", "mean": 71.2, "raw_mean": 71.2, "scores": [..]}, ...}, "errors": {"clipiqa": "why it did not run"}, "notes": []}

The models (pyiqa: MUSIQ, TOPIQ-NR, CLIP-IQA) score single frames, so they do not see motion.
"""
import json
import logging

from synchub import db, jobs, quality, workers
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "quality_ai_test"
MODES = ("standard", "ai", "both")
MODELS = ("musiq", "topiq_nr", "clipiqa")
MODEL_LABELS = {"musiq": "MUSIQ", "topiq_nr": "TOPIQ", "clipiqa": "CLIP-IQA"}
PRIORITY = 70
KEEP = 20                       # tests shown on the page


class AiTestError(ValueError):
    """A sentence for the page."""


def ai_workers(conn):
    """Online workers that can run the AI half."""
    out = []
    for w in workers.list_all(conn):
        caps = w.get("capabilities") or {}
        if w.get("online") and not w.get("revoked") and "quality_ai" in (caps.get("features") or []):
            out.append({"name": w["name"], "device": (caps.get("ai_quality") or {}).get("device"), "models": (caps.get("ai_quality") or {}).get("models") or []})
    return out


def run(conn, file_id, mode, by="someone", now=None):
    """Starts a test. Returns its id. Raises AiTestError for a sentence on the page."""
    if mode not in MODES:
        raise AiTestError("Choose Standard, AI or Both.")
    rec = quality._record(conn, file_id)
    if rec is None:
        raise AiTestError("That file is not in the Library.")
    if mode in ("ai", "both") and not ai_workers(conn):
        raise AiTestError("No online worker has the AI test yet. Standard still works.")
    if mode in ("ai", "both") and (rec["analysis_status"] != v.ANALYSIS_COMPLETED or not rec.get("duration_seconds")):
        raise AiTestError("The file has not been analysed yet. Scan its folder first.")
    now = now or db.now()
    try:
        check_id = quality.request(conn, file_id, by=by, source="manual", now=now) if mode in ("standard", "both") else None
    except quality.QualityError as exc:
        raise AiTestError(str(exc))
    job_id = None
    if mode in ("ai", "both"):
        title = rec.get("recommended_name") or rec["file_name"]
        test_id = conn.execute("INSERT INTO quality_ai_test (library_file_id, mode, standard_check_id, requested_by, created_at) VALUES (?, ?, ?, ?, ?)",
                               (file_id, mode, check_id, by, now)).lastrowid
        plan = {"op": "quality_ai", "needs": ["quality_ai"], "test_id": test_id, "models": list(MODELS), "samples": quality.samples(conn), "seconds": quality.slice_seconds(conn),
                "duration": rec["duration_seconds"], "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "seed": test_id * 7919 + int(rec["size_bytes"] or 0) % 10007,
                "hdr": quality._json(rec.get("video_info_json"), {}).get("hdr")}
        job_id = jobs.create(conn, "scan", title=f"AI quality test: {title}", ref=(REF, test_id), source=(rec["root_name"], rec["relative_path"]), plan=plan,
                             priority=PRIORITY, max_attempts=1, now=now)
        conn.execute("UPDATE quality_ai_test SET ai_job_id = ? WHERE id = ?", (job_id, test_id))
    else:
        test_id = conn.execute("INSERT INTO quality_ai_test (library_file_id, mode, standard_check_id, requested_by, created_at) VALUES (?, ?, ?, ?, ?)",
                               (file_id, mode, check_id, by, now)).lastrowid
    logger.info(f"[ai_test] test {test_id} ({mode}) of file {file_id} started by {by}")
    return test_id


def _models(result):
    out = []
    models = result.get("models") if isinstance(result, dict) else None
    errors = result.get("errors") if isinstance(result, dict) and isinstance(result.get("errors"), dict) else {}
    for key in MODELS:
        data = (models or {}).get(key)
        if isinstance(data, dict) and data.get("mean") is not None:
            out.append({"key": key, "label": MODEL_LABELS[key], "score": round(float(data["mean"]), 1), "version": data.get("version")})
        elif key in errors:
            out.append({"key": key, "label": MODEL_LABELS[key], "score": None, "error": str(errors[key])[:200]})
    return out


def _part(status):
    return {"queued": "queued", "running": "running", "done": "done", "failed": "failed", "needs_review": "failed", "cancelled": "cancelled"}.get(status, "queued")


def _overall(parts):
    parts = [p for p in parts if p]
    for state in ("failed", "cancelled", "running", "queued"):
        if state in parts:
            return state
    return "done"


def recent(conn, limit=KEEP):
    """The latest tests with both halves, for the page."""
    out = []
    for t in conn.execute("""SELECT t.*, f.file_name, f.recommended_name, f.resolution FROM quality_ai_test t JOIN library_file f ON f.id = t.library_file_id
                             ORDER BY t.id DESC LIMIT ?""", (int(limit),)).fetchall():
        d = dict(t)
        std = None
        if d["standard_check_id"]:
            check = quality.get(conn, d["standard_check_id"])
            if check:
                std = {"status": _part(check["status"]), "score": check["score"], "grade": check["grade"], "message": check["message"]}
        ai = None
        if d["ai_job_id"]:
            job = jobs.get(conn, d["ai_job_id"])
            if job:
                result = job["result"] if isinstance(job.get("result"), dict) else {}
                ai = {"status": _part(job["status"]), "models": _models(result), "device": result.get("device"), "elapsed": result.get("elapsed"),
                      "frames": result.get("frames"), "notes": [str(n)[:200] for n in (result.get("notes") or []) if n],
                      "message": job.get("error") or job.get("review_reason") or job.get("message")}
            else:
                ai = {"status": "failed", "models": [], "message": "The job was removed."}
        d.update(standard=std, ai=ai, status=_overall([std and std["status"], ai and ai["status"]]), name=d["recommended_name"] or d["file_name"])
        out.append(d)
    return out


def files(conn, text, limit=12):
    """Analysed library files whose name contains the text, for the picker."""
    like = "%" + str(text or "").strip().replace("%", "").replace("_", "") + "%"
    rows = conn.execute("""SELECT id, file_name, recommended_name, root_name, relative_path, resolution, size_bytes FROM library_file
                           WHERE missing_since IS NULL AND analysis_status = 'completed' AND duration_seconds IS NOT NULL AND (file_name LIKE ? OR relative_path LIKE ?)
                           ORDER BY file_name LIMIT ?""", (like, like, int(limit))).fetchall()
    return [dict(r) for r in rows]


def overview(conn):
    return {"workers": ai_workers(conn), "tests": recent(conn), "models": [(k, MODEL_LABELS[k]) for k in MODELS]}
