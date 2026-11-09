"""Times every page of SyncHub against a COPY of a database, so the real one is never touched.

    python tools/benchmark_pages.py /path/to/synchub.db            # every page, 3 runs each
    python tools/benchmark_pages.py /path/to/sample.db --runs 5 --only library
    docker exec synchub python /app/tools/benchmark_pages.py /data/synchub.db      (on Unraid)

It starts the app in-process with the background jobs off and the network blocked, opens each page the way a browser would (as someone who chose
"just looking"), and prints the time, the number of database calls and the size of each, slowest first. It also times the heavy steps behind the Library
page one by one. Save the output before and after a change to compare. Pages that would call Jellyfin, Radarr or another service are left out.
"""
import argparse
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SYNCHUB_SCHEDULER"] = "0"

SKIP = {"/logout", "/tools/export-csv", "/library/export.csv", "/library/quality/export"}
OUTBOUND = ("/api/stream/events", "/api/tools/library-paths", "/api/tools/live-session")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("database")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="", help="only pages whose address contains this text")
    ap.add_argument("--profile", default="", help="instead of the table, show where one page spends its time (for example --profile /): the 30 slowest functions")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="synchub-bench-"))
    copy = work / "synchub.db"
    shutil.copy(args.database, copy)
    for suffix in ("-wal", "-shm"):
        if Path(str(args.database) + suffix).exists():
            shutil.copy(str(args.database) + suffix, str(copy) + suffix)

    from synchub import db
    db.DB_FILE = str(copy)

    real = socket.socket.connect

    def guarded(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("/"):
            raise OSError(f"benchmark: network blocked ({host})")
        return real(self, address, *a, **k)

    socket.socket.connect = guarded

    from synchub import quality, timing, versions
    from synchub.library import recommend as rc, scan
    from synchub.web import create_app, fleet, media
    from synchub.web import library as library_web
    app = create_app(csrf=False)
    client = app.test_client()
    client.set_cookie("synchub_profile", "0")

    if args.profile:
        import cProfile
        import pstats
        client.get(args.profile)                                      # warm: imports, caches
        timing.reset()
        profiler = cProfile.Profile()
        started = time.perf_counter()
        profiler.enable()
        response = client.get(args.profile)
        profiler.disable()
        print(f"{args.profile}: {response.status_code}, {(time.perf_counter() - started) * 1000:.0f} ms (with profiling overhead), {len(response.get_data()) / 1024:.0f} KB\n")
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
        pstats.Stats(profiler).sort_stats("tottime").print_stats(15)
        shutil.rmtree(work, ignore_errors=True)
        return

    paths = set()
    with db.connection() as conn:
        first = conn.execute("SELECT id FROM library_file ORDER BY id LIMIT 1").fetchone()
        row = conn.execute("SELECT value FROM app_setting WHERE key = 'dash_token'").fetchone()
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and rule.endpoint != "static" and not rule.arguments and rule.rule not in SKIP and rule.rule not in OUTBOUND and "stream" not in rule.rule:
            paths.add(rule.rule)
    paths.update(f"/library/queue/{group}" for group in rc.GROUP_ORDER)
    if first:
        paths.add(f"/library/record/{first['id']}")
    if row:
        paths.update({f"/dash/{row[0]}", f"/dash/{row[0]}/data"})
    paths = sorted(p for p in paths if args.only in p)

    print(f"Database copy: {copy} ({copy.stat().st_size / 1e6:.0f} MB). {args.runs} runs per page; the first run is cold.\n")
    print(f"{'page':44}{'first ms':>10}{'best ms':>10}{'avg ms':>10}{'queries':>9}{'KB':>8}  status")
    results = []
    for path in paths:
        times, status, queries, size = [], "", 0, 0
        print(f"  ... {path}", file=sys.stderr, flush=True)
        for _ in range(args.runs):
            timing.reset()
            started = time.perf_counter()
            try:
                response = client.get(path)
            except Exception as exc:                      # a page that needs a service that is not there
                status = f"error: {type(exc).__name__}"
                break
            times.append((time.perf_counter() - started) * 1000)
            status, size = str(response.status_code), len(response.get_data())
            seen = timing.report()["slowest"]
            queries = seen[0]["queries"] if seen else 0
        if times:
            results.append((path, times[0], min(times), sum(times) / len(times), queries, size / 1024, status))
        else:
            print(f"{path[:43]:44}{'':>10}{'':>10}{'':>10}{'':>9}{'':>8}  {status}")
    for path, first_ms, best, avg, queries, kb, status in sorted(results, key=lambda r: -r[1]):
        print(f"{path[:43]:44}{first_ms:>10.0f}{best:>10.0f}{avg:>10.0f}{queries:>9}{kb:>8.0f}  {status}")

    print("\nSteps behind the Library page (each run once, warm):")

    def step(label, fn):
        started = time.perf_counter()
        fn()
        print(f"  {(time.perf_counter() - started) * 1000:8.0f} ms  {label}")

    def in_request(fn, *a):
        with app.test_request_context("/"):
            return fn(*a)

    with db.connection() as conn:
        cfg = scan.config(conn)
        print(f"  {conn.execute('SELECT COUNT(*) FROM library_file').fetchone()[0]} files in library_file")
        step("quality.held_ids", lambda: quality.held_ids(conn))
        step("recommend.classify (every file)", lambda: rc.classify(conn, cfg))
        step("library.overview (what /library/fragment builds)", lambda: library_web.overview(conn))
        step("versions.recommendations (Versions tab)", lambda: versions.recommendations(conn))
        step("fleet.snapshot (Workers and Jobs tabs)", lambda: in_request(fleet.snapshot, conn))
        for key in ("library", "versions", "originals", "workers", "players", "jobs", "storage", "options"):
            step(f"media._data('{key}')", lambda key=key: in_request(media._data, key, conn))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
