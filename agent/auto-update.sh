#!/usr/bin/env bash
# auto-update.sh: keeps a worker on the newest SyncHub code, safely, and tells you what happened.
#
#   sudo ./agent/auto-update.sh --install [--dir /opt/synchub-worker] [--window 02:00-06:00]   run once; a systemd timer then runs every minute
#   sudo ./agent/auto-update.sh --status         what the timer is doing (and the last log lines)
#   sudo ./agent/auto-update.sh --uninstall      remove the timer, the script and the settings file
#   sudo ./agent/auto-update.sh                  one check, by hand
#   sudo ./agent/auto-update.sh --now            one check that updates even if the worker is busy
#
# Every minute the timer asks SyncHub (/update/check) for this worker's settings (automatic updates on or off, the maintenance window) and for a request
# from the Workers page: "Restart worker" restarts the container at once; "Update" updates as soon as the worker is idle. Without a request the check
# below runs once an hour (not at all when automatic updates are off for this worker in SyncHub).
# One check does this, and only this:
#   1. git fetch in the checkout the worker builds from. Nothing new on origin: stop.
#   2. New code, but the same commit already failed here: stop (it is tried again when there is newer code).
#   3. Only updates when SyncHub says the worker is running no job, or when the time is inside the maintenance window (if you set one).
#      Inside the window a running job is cut off: SyncHub gives it to a worker again when the lease runs out.
#   4. git merge --ff-only, then update-worker.sh --no-pull (rebuild and restart, wait until the container is healthy).
#   5. If that fails the running container is kept when the build failed; if the new container does not become healthy, or anything else goes wrong,
#      the checkout is put back to the last good commit and update-worker.sh rebuilds that, so the worker ends up on the old code.
#   6. SyncHub is told, success or failure (version or the error tail): a line on the Workers page and a notification through your
#      notification settings ("A worker updated itself", "A worker's automatic update failed"). If SyncHub cannot be reached the report
#      is kept and sent on the next check.
# Also every minute: if SyncHub's Protect settings choose this worker to run the camera relay, it writes docker-compose.override.yml (go2rtc) next to the
# worker's compose file and starts it when the worker is idle, and tells SyncHub how it went. See docs/CAMERAS.md.
# Settings (optional) are read from /etc/synchub-auto-update.conf, a shell file:
#   SYNCHUB_REPO=/home/greg/letterboxd_scraper   SYNCHUB_WORKER_DIR=/opt/synchub-worker   SYNCHUB_UPDATE_WINDOW=02:00-06:00 (empty: only when idle)
# The SyncHub address and the worker's token are read from SYNCHUB_URL and SYNCHUB_TOKEN in <SYNCHUB_WORKER_DIR>/.env.
# Log: journalctl -t synchub-auto-update
set -uo pipefail

CONF="${SYNCHUB_UPDATE_CONF:-/etc/synchub-auto-update.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
SELF="$(readlink -f "$0")"
REPO="${SYNCHUB_REPO:-$(git -C "$(dirname "$SELF")" rev-parse --show-toplevel 2>/dev/null || true)}"
DIR="${SYNCHUB_WORKER_DIR:-/opt/synchub-worker}"
WINDOW="${SYNCHUB_UPDATE_WINDOW:-}"
STATE="${SYNCHUB_UPDATE_STATE:-/var/lib/synchub-auto-update}"
LOCK="${SYNCHUB_UPDATE_LOCK:-/run/synchub-auto-update.lock}"
UPDATER="${SYNCHUB_UPDATE_SCRIPT:-$REPO/agent/update-worker.sh}"
BIN="${SYNCHUB_UPDATE_BIN:-/usr/local/bin/synchub-auto-update}"
UNIT_DIR="${SYNCHUB_UNIT_DIR:-/etc/systemd/system}"

log() { echo "$*"; logger -t synchub-auto-update -- "$*" 2>/dev/null || true; }

# ------------------------------------------------------------------ install / uninstall / status
install_updater() {
    local dry="$1" dir="$2" window="$3"
    run() { if [ "$dry" = 1 ]; then echo "[dry run] $*"; else "$@"; fi; }
    write() { if [ "$dry" = 1 ]; then echo "[dry run] write $1:"; sed 's/^/    /'; else cat > "$1"; fi; }
    [ "$dry" = 1 ] || [ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }
    [ -n "$REPO" ] && [ -f "$REPO/agent/update-worker.sh" ] || { echo "ERROR: run this from a clone of the SyncHub repository (or set SYNCHUB_REPO)." >&2; exit 1; }
    if [ -n "$window" ]; then parse_window "$window" || { echo "ERROR: --window must look like 02:00-06:00" >&2; exit 1; }; fi
    echo "==> Installing the automatic worker update"
    run install -m 0755 "$SELF" "$BIN"
    run install -d -m 0755 "$STATE"
    if [ ! -f "$CONF" ] || [ "$dry" = 1 ]; then
        write "$CONF" <<EOF
# Read by $BIN. The checkout the worker builds from, the worker's folder, and the maintenance window (HH:MM-HH:MM, local time; empty = only when idle).
SYNCHUB_REPO="$REPO"
SYNCHUB_WORKER_DIR="$dir"
SYNCHUB_UPDATE_WINDOW="$window"
EOF
    else
        echo "    keeping the existing $CONF (edit it to change the folder or the window)"
    fi
    write "$UNIT_DIR/synchub-auto-update.service" <<EOF
[Unit]
Description=Update the SyncHub worker to the newest code when it is idle
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=$BIN
TimeoutStartSec=2h
EOF
    write "$UNIT_DIR/synchub-auto-update.timer" <<EOF
[Unit]
Description=Ask SyncHub every minute for update requests and settings; look for new worker code once an hour

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
    run systemctl daemon-reload
    run systemctl enable --now synchub-auto-update.timer
    echo "==> Done. Checkout: $REPO   worker folder: $dir   window: ${window:-none (only when idle)}"
    echo "    Log: journalctl -t synchub-auto-update      Status: $0 --status"
}

uninstall_updater() {
    [ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }
    systemctl disable --now synchub-auto-update.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/synchub-auto-update.service" "$UNIT_DIR/synchub-auto-update.timer" "$BIN" "$CONF"
    systemctl daemon-reload
    echo "Removed the automatic update. (State kept in $STATE.)"
}

status_updater() {
    systemctl list-timers synchub-auto-update.timer --no-pager 2>&1 | head -4
    [ -f "$STATE/failed" ] && echo "--- skipping commit $(cat "$STATE/failed") until newer code arrives (it failed here)"
    [ -f "$STATE/pending.json" ] && echo "--- a report is waiting to be sent to SyncHub"
    echo "--- last log lines"
    journalctl -t synchub-auto-update -n 15 --no-pager 2>&1 | tail -15
}

# ------------------------------------------------------------------ helpers
# parse_window "02:00-06:00": sets W_START and W_END as minutes since midnight. Fails on anything else.
parse_window() {
    [[ "$1" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$ ]] || return 1
    W_START=$((10#${BASH_REMATCH[1]} * 60 + 10#${BASH_REMATCH[2]}))
    W_END=$((10#${BASH_REMATCH[3]} * 60 + 10#${BASH_REMATCH[4]}))
}

# True inside the maintenance window. No window set means never. A window that ends before it starts runs over midnight.
in_window() {
    [ -n "$WINDOW" ] || return 1
    parse_window "$WINDOW" || { log "WARNING the window '$WINDOW' is not HH:MM-HH:MM: ignoring it"; return 1; }
    local now
    now=$((10#$(date +%H) * 60 + 10#$(date +%M)))
    if [ "$W_START" -le "$W_END" ]; then [ "$now" -ge "$W_START" ] && [ "$now" -lt "$W_END" ]; else [ "$now" -ge "$W_START" ] || [ "$now" -lt "$W_END" ]; fi
}

# git in the checkout, as the folder's owner when run as root (so root never leaves files in someone's repository).
OWNER=""
git_run() {
    if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != root ] && command -v runuser >/dev/null; then runuser -u "$OWNER" -- git -C "$REPO" "$@"; else git -C "$REPO" "$@"; fi
}

env_value() { sed -n "s/^$1=//p" "$DIR/.env" 2>/dev/null | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"; }

# api PATH [JSON]: POST to the worker API with the worker's token; prints the answer. Fails when SyncHub does not answer or refuses.
api() {
    local data="${2:-}"; [ -n "$data" ] || data="{}"
    curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data "$data" "$URL/api/worker/v1$1"
}

json_escape() { python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'; }

agent_version() { git_run show "${1:-HEAD}:agent/synchub_agent.py" 2>/dev/null | sed -n 's/^__version__ *= *["'\'']\([^"'\'']*\)["'\''].*/\1/p' | head -n1; }

# Sends a report to SyncHub now; keeps it for the next check when SyncHub cannot be reached. A report SyncHub refuses is dropped (it would never be accepted).
send_report() {
    local body="$1"
    body="$(printf '%s' "$body" | LOGTEXT="$(journalctl -t synchub-auto-update -n 50 --no-pager -o cat 2>/dev/null | tail -n 50)" python3 -c 'import json,os,sys
d = json.load(sys.stdin)
if os.environ.get("LOGTEXT"): d["log"] = os.environ["LOGTEXT"][-5000:]
sys.stdout.write(json.dumps(d, separators=(",", ":")))' 2>/dev/null || printf '%s' "$1")"
    if [ -z "${URL:-}" ] || [ -z "${TOKEN:-}" ]; then log "WARNING no SYNCHUB_URL or SYNCHUB_TOKEN in $DIR/.env: cannot tell SyncHub"; return 0; fi
    if api /update/report "$body" >/dev/null 2>"$STATE/curl.err"; then rm -f "$STATE/pending.json"; return 0; fi
    if grep -q 'error: 4' "$STATE/curl.err"; then log "WARNING SyncHub refused the report: $(tail -n1 "$STATE/curl.err")"; rm -f "$STATE/pending.json"; return 0; fi
    log "WARNING SyncHub could not be reached; the report is kept for the next check"
    printf '%s' "$body" > "$STATE/pending.json"
}

# ------------------------------------------------------------------ one check
FORCE=0
case "${1:-}" in
    --install)
        shift; dry=0; idir="$DIR"; iwin="$WINDOW"
        while [ $# -gt 0 ]; do
            case "$1" in
                --dry-run) dry=1; shift ;;
                --dir) idir="${2:?--dir needs a folder}"; shift 2 ;;
                --window) iwin="${2?--window needs HH:MM-HH:MM}"; shift 2 ;;
                *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
            esac
        done
        install_updater "$dry" "$idir" "$iwin"; exit 0 ;;
    --uninstall) uninstall_updater; exit 0 ;;
    --status) status_updater; exit 0 ;;
    --now) FORCE=1 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

[ -d "$REPO/.git" ] || [ -f "$REPO/.git" ] || { log "ERROR $REPO is not a git checkout (set SYNCHUB_REPO)"; exit 1; }
mkdir -p "$STATE" 2>/dev/null || true
if { exec 9>"$LOCK"; } 2>/dev/null; then flock -n 9 || exit 0; fi
OWNER="$(stat -c %U "$REPO" 2>/dev/null || true)"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$REPO"      # root running in someone's checkout; passed on to update-worker.sh
URL="$(env_value SYNCHUB_URL)"; URL="${URL%/}"
TOKEN="$(env_value SYNCHUB_TOKEN)"

[ -f "$STATE/pending.json" ] && send_report "$(cat "$STATE/pending.json")"

# Every minute: ask SyncHub for the settings chosen on the Workers page and for a request from its Update / Restart buttons.
answer=""; [ "$FORCE" = 1 ] || answer="$(api /update/check 2>/dev/null || true)"
jget() { printf '%s' "$answer" | python3 -c 'import json,sys
try: v = json.load(sys.stdin).get(sys.argv[1])
except Exception: v = None
print("" if v is None else "true" if v is True else "false" if v is False else v)' "$1" 2>/dev/null || true; }
REQUEST="$(jget request)"
if [ -n "$answer" ] && printf '%s' "$answer" | grep -q '"window"'; then WINDOW="$(jget window)"; fi        # the window chosen in SyncHub wins over the conf file
AUTO="$(jget auto_update)"

# The camera relay (go2rtc) chosen in SyncHub's Protect settings. SyncHub sends the text of a docker-compose.override.yml; Docker Compose loads that file
# next to the worker's own docker-compose.yml, which this never edits. A worker whose compose file already has a go2rtc service (one made by SyncHub's
# setup files) is left alone. The file is only replaced when it carries SyncHub's mark, and only while the worker is idle: starting go2rtc recreates the
# worker container once (it gets the GO2RTC_URL setting), which resumes unfinished jobs but costs them a restart.
RELAY_FILE="$DIR/docker-compose.override.yml"
RELAY_MARK="# Managed by SyncHub (camera relay)."
relay_report() { # relay_report STATUS [ERROR]: tells SyncHub, once per distinct outcome
    local key="$1|${2:-}"
    [ "$(cat "$STATE/relay-reported" 2>/dev/null)" = "$key" ] && return 0
    if api /relay/report "{\"status\":\"$1\",\"error\":$(printf '%s' "${2:-}" | json_escape)}" >/dev/null 2>&1; then printf '%s' "$key" > "$STATE/relay-reported"; fi
}
go2rtc_running() { ( cd "$DIR" && docker compose ps --status running --services 2>/dev/null ) | grep -qx go2rtc; }
relay_sync() {
    local mode hash want idle; mode="$(jget relay_mode)"; [ -n "$mode" ] || return 0
    [ -f "$DIR/docker-compose.yml" ] || return 0
    if [ "$mode" = remove ]; then
        [ -f "$RELAY_FILE" ] && grep -qF "$RELAY_MARK" "$RELAY_FILE" || { rm -f "$STATE/relay-hash"; return 0; }
        rm -f "$RELAY_FILE" "$STATE/relay-hash"
        log "The camera relay is no longer chosen for this worker: removing go2rtc"
        if ( cd "$DIR" && docker compose up -d --remove-orphans ) >"$STATE/relay.out" 2>&1; then relay_report removed; else relay_report failed "removing go2rtc failed: $(tail -n 2 "$STATE/relay.out" | cut -c1-300)"; fi
        return 0
    fi
    hash="$(jget relay_hash)"
    if grep -q '^  go2rtc:' "$DIR/docker-compose.yml"; then        # the worker's own compose file runs it
        go2rtc_running && relay_report native && return 0
        [ "$(jget idle)" = true ] || return 0
        log "go2rtc is in this worker's compose file but not running: starting it"
        if ( cd "$DIR" && docker compose up -d ) >"$STATE/relay.out" 2>&1 && go2rtc_running; then relay_report native; else relay_report failed "starting go2rtc failed: $(tail -n 2 "$STATE/relay.out" | cut -c1-300)"; fi
        return 0
    fi
    if [ -e "$RELAY_FILE" ] && ! grep -qF "$RELAY_MARK" "$RELAY_FILE"; then
        relay_report failed "$RELAY_FILE exists and was not made by SyncHub, so it was left alone. Add the go2rtc service by hand (docs/CAMERAS.md) or move that file away."; return 0
    fi
    want=0
    [ "$(cat "$STATE/relay-hash" 2>/dev/null)" = "$hash" ] || want=1
    go2rtc_running || want=1
    [ "$REQUEST" = relay ] && want=1
    [ "$want" = 1 ] || { relay_report installed; return 0; }
    idle="$(jget idle)"
    if [ "$idle" != true ] && [ "$REQUEST" != relay ]; then
        [ "$(cat "$STATE/relay-waiting" 2>/dev/null)" = "$hash" ] || { log "The camera relay is waiting: the worker is busy (go2rtc is set up when it is idle)"; echo "$hash" > "$STATE/relay-waiting"; relay_report waiting; }
        return 0
    fi
    rm -f "$STATE/relay-waiting"
    local text; text="$(jget relay_override)"; [ -n "$text" ] || return 0
    printf '%s\n' "$text" > "$RELAY_FILE.tmp" && mv -f "$RELAY_FILE.tmp" "$RELAY_FILE"
    log "Setting up the camera relay (go2rtc) for this worker"
    if ( cd "$DIR" && docker compose up -d ) >"$STATE/relay.out" 2>&1; then
        for _ in 1 2 3 4 5 6 7 8 9 10; do go2rtc_running && break; sleep 3; done
        if go2rtc_running; then echo "$hash" > "$STATE/relay-hash"; log "go2rtc is running"; relay_report installed; return 0; fi
        relay_report failed "docker compose started, but go2rtc is not running: $(cd "$DIR" && docker compose logs --tail 3 go2rtc 2>&1 | cut -c1-200 | tail -n 3)"
    else
        relay_report failed "docker compose up failed: $(tail -n 3 "$STATE/relay.out" | cut -c1-300)"
    fi
}
[ "$FORCE" = 1 ] || [ -z "$answer" ] || relay_sync

if [ "$REQUEST" = restart ]; then
    log "SyncHub asked for a restart of the worker"
    if ( cd "$DIR" && docker compose restart ) >"$STATE/restart.out" 2>&1; then log "Restarted the worker"; else log "ERROR restarting the worker failed: $(tail -n 2 "$STATE/restart.out")"; fi
    exit 0
fi

# Without a request the check for new code runs once an hour, and not at all when automatic updates are switched off for this worker.
CHECKED="$STATE/last-check"
if [ "$REQUEST" != update ] && [ "$FORCE" != 1 ]; then
    [ "$AUTO" = false ] && exit 0
    if [ -f "$CHECKED" ] && [ -z "$(find "$CHECKED" -mmin +59 2>/dev/null)" ]; then exit 0; fi
fi
if [ "$REQUEST" = update ]; then log "SyncHub asked for an update now"; rm -f "$STATE/failed"; fi

fetch_origin() { git_run fetch --quiet origin 2>"$STATE/git.err"; }
# problem MESSAGE: logs a problem and tells SyncHub once per distinct message (not every minute); the next good fetch clears it.
problem() {
    log "WARNING $1"
    [ "$(cat "$STATE/problem" 2>/dev/null)" = "$1" ] && return 0
    printf '%s' "$1" > "$STATE/problem"
    send_report "{\"status\":\"problem\",\"error\":$(printf '%s' "$1" | json_escape)}"
}
fetch_origin || {
    # A past `sudo git ...` leaves root-owned files in .git that the owner cannot write: hand them back once.
    if grep -q 'Permission denied' "$STATE/git.err" && [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
        log "Fixing root-owned files in $REPO/.git, then retrying the fetch"
        chown -R "$OWNER": "$REPO/.git" 2>/dev/null
        fetch_origin
    else
        false
    fi
} || { problem "git fetch failed: $(tail -n1 "$STATE/git.err")"; exit 0; }
if [ -f "$STATE/problem" ]; then rm -f "$STATE/problem"; log "git fetch works again"; send_report "{\"status\":\"current\",\"commit\":\"$(git_run rev-parse --short HEAD)\"}"; fi
LOCAL="$(git_run rev-parse HEAD)"
REMOTE="$(git_run rev-parse '@{u}' 2>/dev/null)" || { problem "the checkout's branch has no upstream"; exit 1; }
if [ "$LOCAL" = "$REMOTE" ]; then
    touch "$CHECKED"; rm -f "$STATE/waiting"
    [ "$REQUEST" = update ] && { log "Already on the newest code ($(agent_version HEAD))"; send_report "{\"status\":\"current\",\"version\":\"$(agent_version HEAD)\",\"commit\":\"$(git_run rev-parse --short HEAD)\"}"; }
    exit 0
fi
if ! git_run merge-base --is-ancestor "$LOCAL" "$REMOTE"; then touch "$CHECKED"; log "WARNING $REPO has commits that are not on origin: not updating"; exit 0; fi
if [ -f "$STATE/failed" ] && [ "$(cat "$STATE/failed")" = "$REMOTE" ]; then touch "$CHECKED"; exit 0; fi       # already failed here; wait for newer code

short="$(git_run rev-parse --short "$REMOTE")"
if [ "$FORCE" = 1 ]; then
    log "New code $short: updating now (--now)"
elif [ "$REQUEST" = update ]; then
    log "New code $short: updating (asked for on the Workers page; SyncHub only passes this on when the worker is idle)"
elif printf '%s' "$answer" | grep -Eq '"idle": ?true'; then
    log "New code $short and the worker is idle: updating"
elif in_window; then
    log "New code $short; the worker is busy or SyncHub did not answer, but it is inside the maintenance window ($WINDOW): updating"
else
    # Not touching the stamp: look again next minute, so the update starts as soon as the worker is idle or the window opens.
    [ "$(cat "$STATE/waiting" 2>/dev/null)" = "$short" ] || log "New code $short is waiting: the worker is busy or SyncHub did not answer${WINDOW:+; it updates inside the window $WINDOW}"
    echo "$short" > "$STATE/waiting"
    exit 0
fi
touch "$CHECKED"; rm -f "$STATE/waiting"

previous="$(agent_version HEAD)"
if ! git_run merge --ff-only --quiet "$REMOTE" 2>"$STATE/git.err"; then
    error="git merge --ff-only failed: $(tail -n3 "$STATE/git.err")"
    echo "$REMOTE" > "$STATE/failed"; log "FAILED $error"
    send_report "{\"status\":\"failed\",\"commit\":\"$short\",\"previous\":\"$previous\",\"error\":$(printf '%s' "$error" | json_escape)}"
    exit 1
fi
version="$(agent_version HEAD)"

output="$("$UPDATER" --dir "$DIR" --no-pull 2>&1)"; status=$?
if [ "$status" = 0 ]; then
    rm -f "$STATE/failed"
    log "Updated $previous -> $version ($short)"
    send_report "{\"status\":\"ok\",\"version\":\"$version\",\"previous\":\"$previous\",\"commit\":\"$short\"}"
    exit 0
fi

# Failed. Put the code back and rebuild it, so the folder the worker builds from, the image and the container agree again.
tail_text="$(printf '%s' "$output" | tail -n 25 | cut -c1-300 | tail -c 1800)"
log "FAILED updating to $short: $(printf '%s' "$output" | tail -n 1)"
git_run reset --hard --quiet "$LOCAL"
restored=""
if "$UPDATER" --dir "$DIR" --no-pull >"$STATE/restore.out" 2>&1; then
    restored="$(git_run rev-parse --short "$LOCAL") ($previous)"
    log "Put the worker back on $restored"
else
    log "ERROR putting the worker back on the old code failed too: $(tail -n 1 "$STATE/restore.out")"
    tail_text="$tail_text"$'\n'"-- and putting the old code back failed: $(tail -n 3 "$STATE/restore.out" | cut -c1-300)"
fi
echo "$REMOTE" > "$STATE/failed"
send_report "{\"status\":\"failed\",\"commit\":\"$short\",\"previous\":\"$previous\",\"restored\":\"$restored\",\"error\":$(printf '%s' "$tail_text" | json_escape)}"
exit 1
