#!/usr/bin/env bash
# update-worker.sh: updates an existing SyncHub worker to the latest agent code and nothing else.
#
#   cd letterboxd_scraper && sudo ./agent/update-worker.sh --dir /opt/synchub-worker
#
# Use this instead of setup-worker.sh on a worker with a hand-made setup (your own /etc/fstab lines, a customised docker-compose.yml).
# It never touches /etc/fstab, the compose file, the .env file or any mount. It only:
#   1. pulls the latest code into the checkout this script lives in (fast-forward only)
#   2. finds the build folder the worker's compose file builds from, and copies the agent files into it if it is not this checkout
#   3. runs 'docker compose up -d --build' in the worker folder and waits for the container to be healthy
# Exit status: 0 done, 1 failed.
set -Eeuo pipefail

DIR="/opt/synchub-worker"
BRANCH=""
DO_PULL=1
DRY_RUN=0
REPORT=0
AI_MODE=keep
AI_STATUS=0
AGENT_FILES=(Dockerfile synchub_agent.py media_analysis.py)

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "[dry run] $*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="${2:?--dir needs a folder}"; shift 2 ;;
        --branch) BRANCH="${2:?--branch needs a name}"; shift 2 ;;
        --no-pull) DO_PULL=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --report) REPORT=1; shift ;;
        --ai-quality) AI_MODE="${2:?--ai-quality needs on, off or keep}"; case "$AI_MODE" in on|off|keep) ;; *) die "--ai-quality must be on, off or keep" ;; esac; shift 2 ;;
        --ai-status) AI_STATUS=1; shift ;;
        -h|--help)
            echo "Usage: update-worker.sh [--dir WORKER_FOLDER] [--branch NAME] [--no-pull] [--dry-run] [--report]"
            echo "  --dir      folder with the worker's docker-compose.yml (default /opt/synchub-worker)"
            echo "  --branch   check out and pull this branch first (default: the branch already checked out)"
            echo "  --no-pull  use the checkout as it is"
            echo "  --dry-run  show what would happen without changing anything"
            echo "  --ai-quality on|off|keep  build the image with the AI quality test layer (PyTorch and the models, about 1 GB) or without it; keep = as it was (default)"
            echo "  --ai-status  print 'on' or 'off': whether the worker's build folder is set to include the AI layer, and exit"
            echo "  --report   print the worker's current state (changes nothing) and exit; paste it when asking for help"
            exit 0 ;;
        *) die "unknown option '$1' (try --help)" ;;
    esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SELF_DIR"                      # the agent folder of this checkout
REPO="$(git -C "$SELF_DIR" rev-parse --show-toplevel 2>/dev/null)" || die "$SELF_DIR is not in a git checkout. Run this script from a clone of the SyncHub repository."
[ -f "$SRC/synchub_agent.py" ] || die "$SRC has no synchub_agent.py."

COMPOSE=""
for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do [ -f "$DIR/$f" ] && { COMPOSE="$DIR/$f"; break; }; done
[ -n "$COMPOSE" ] || die "No compose file in $DIR. Pass the worker's folder with --dir."
command -v docker >/dev/null || die "docker is not installed."

if [ "$REPORT" = 1 ]; then
    # Read-only snapshot of the worker, to paste into the thread before an upgrade or when something is wrong.
    section() { echo; echo "##### $*"; }
    section "date / host";     date -u +%FT%TZ; hostname; uname -sr
    section "checkout $REPO";  git -C "$REPO" log -1 --format='%H %ci %s'; git -C "$REPO" status -sb | head -n 15
    git -C "$REPO" stash list | head -n 5
    section "behind origin?";  git -C "$REPO" fetch --quiet origin 2>&1 | head -n 3; git -C "$REPO" rev-list --count HEAD..@{u} 2>/dev/null | sed 's/$/ commit(s) behind upstream/'
    section "agent version in the checkout"; grep -m1 '^__version__' "$SRC/synchub_agent.py"
    section "compose file $COMPOSE (build + volumes)"
    ( cd "$DIR" && docker compose config 2>&1 | grep -E 'context:|image:|container_name:|source:|target:|SYNCHUB_URL|SYNCHUB_NAME' ) || true
    section "container"
    ( cd "$DIR" && docker compose ps 2>&1 ) || true
    cid="$(cd "$DIR" && docker compose ps -q 2>/dev/null | head -n1)"
    if [ -n "$cid" ]; then
        docker inspect -f 'container created: {{.Created}}  started: {{.State.StartedAt}}  restarts: {{.RestartCount}}  health: {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>&1
        docker image inspect -f 'image created: {{.Created}}' "$(docker inspect -f '{{.Image}}' "$cid")" 2>&1
    fi
    section "mounts under /mnt/tower"; mount | grep ' /mnt/tower/' || echo "(none)"
    section "free space";      df -h / "${WORK_DIR:-$DIR}" 2>&1 | head -n 5
    section "last 40 log lines"; ( cd "$DIR" && docker compose logs --tail 40 2>&1 ) || true
    exit 0
fi

if [ "$DO_PULL" = 1 ]; then
    say "Updating $REPO"
    git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || die "$REPO has local changes. Commit or discard them, or use --no-pull."
    run git -C "$REPO" fetch --quiet origin ${BRANCH:+"$BRANCH"}
    if [ -n "$BRANCH" ]; then run git -C "$REPO" checkout --quiet "$BRANCH"; fi
    [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" != HEAD ] || die "$REPO is not on a branch. Check one out or pass --branch."
    run git -C "$REPO" merge --ff-only --quiet "@{u}" || die "$REPO cannot be fast-forwarded (it has commits of its own). Resolve that, or use --no-pull."
fi
say "Agent code is at $(git -C "$REPO" log -1 --format='%h %s')"

# The build folder the worker's compose file uses (docker resolves relative paths for us).
CONTEXT="$(cd "$DIR" && docker compose config 2>/dev/null | awk '/^ *context:/ {print $2; exit}')"
[ -n "$CONTEXT" ] || die "Could not find the 'build:' folder in $COMPOSE. Does the worker service build from a folder?"
[ -d "$CONTEXT" ] || die "The build folder $CONTEXT from $COMPOSE does not exist."
say "The worker builds from $CONTEXT"

# The AI quality test layer: the Dockerfile installs PyTorch and the models only when this file says "on". It is a file in the build folder (like BUILD_INFO), so the
# choice survives updates and needs no change to the compose file.
AI_FILE="$CONTEXT/AI_QUALITY"
ai_state() { if [ -f "$AI_FILE" ] && grep -qx on "$AI_FILE" 2>/dev/null; then echo on; else echo off; fi; }
if [ "$AI_STATUS" = 1 ]; then ai_state; exit 0; fi
AI_PREV=""
if [ "$AI_MODE" != keep ] && [ "$AI_MODE" != "$(ai_state)" ]; then
    AI_PREV="$(ai_state)"
    say "AI quality test layer: $(ai_state) -> $AI_MODE (the image is rebuilt; switching it on downloads about 1 GB)"
    if [ "$DRY_RUN" != 1 ]; then printf '%s\n' "$AI_MODE" > "$AI_FILE" || die "could not write $AI_FILE"; fi
fi

if [ "$(cd "$CONTEXT" && pwd -P)" != "$(cd "$SRC" && pwd -P)" ]; then
    say "Copying the agent files into $CONTEXT"
    for f in "${AGENT_FILES[@]}"; do
        [ -f "$SRC/$f" ] || die "$SRC/$f is missing."
        run install -m 644 "$SRC/$f" "$CONTEXT/$f"
    done
fi

# Which code this image is made from: the commit of this checkout and the time of the build. The agent reports it, and the Workers page shows it.
if [ "$DRY_RUN" != 1 ]; then
    commit="$(git -c safe.directory='*' -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
    if [ -n "$commit" ]; then printf 'commit=%s\nbuilt=%s\n' "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$CONTEXT/BUILD_INFO" 2>/dev/null || echo "WARNING: could not write $CONTEXT/BUILD_INFO; the Workers page will not show the commit." >&2; fi
fi

# A warning only: the mounts are yours to manage.
if mount | grep -q ' /mnt/tower/'; then :; else echo "WARNING: nothing is mounted under /mnt/tower/. The worker will start but cannot reach the shares." >&2; fi

say "Rebuilding and restarting the worker"
if ! ( cd "$DIR" && run docker compose up -d --build ); then
    [ -z "$AI_PREV" ] || { printf '%s\n' "$AI_PREV" > "$AI_FILE" 2>/dev/null || true; echo "The AI quality test layer is back to '$AI_PREV' because the build failed." >&2; }
    # docker compose builds the image before it touches the container, so a failed build leaves the running worker alone; say so, and prove it.
    if [ -n "$(cd "$DIR" && docker compose ps -q --status running 2>/dev/null)" ]; then
        die "Docker could not rebuild the worker. The worker that was running is still running, on its old code."
    fi
    die "Docker could not rebuild the worker, and no worker container is running. 'cd $DIR && docker compose logs' may say more."
fi
[ "$DRY_RUN" = 1 ] && exit 0

for _ in $(seq 1 18); do
    cid="$(cd "$DIR" && docker compose ps -q | head -n1)"
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$state" in healthy|running) say "Worker is $state."; exit 0 ;; unhealthy|exited) break ;; esac
    sleep 5
done
( cd "$DIR" && docker compose logs --tail 30 ) || true
die "The worker did not become healthy (state: ${state:-unknown})."
