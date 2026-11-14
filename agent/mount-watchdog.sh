#!/usr/bin/env bash
# mount-watchdog.sh: keeps a worker's network shares mounted, and the worker's container looking at them, through network outages.
#
#   sudo ./agent/mount-watchdog.sh --install        run once; from then on a systemd timer runs one check a minute
#   sudo ./agent/mount-watchdog.sh --status         what the timer is doing (and the last log lines)
#   sudo ./agent/mount-watchdog.sh --uninstall      remove the timer, the script and the settings file
#   sudo ./agent/mount-watchdog.sh                  one check, by hand
#
# Why: a share that cannot be mounted at boot ("Network is unreachable"), or an Unraid server that restarts, leaves the mount point an empty folder
# or a stale mount. The worker then reports every share as no_marker and a container started before the mount keeps looking at the empty folder.
# One check does this, and only this:
#   - a share is fine when its marker file (.synchub-root, written by --init-roots) can be read within 10 seconds. Nothing else is done.
#   - a share that hangs or errors (a stale mount) is lazily unmounted and mounted again from /etc/fstab.
#   - a mount point with no network file system on it is mounted from /etc/fstab.
#   - a share that IS mounted but has no marker is left alone and logged: it was not set up yet (run --init-roots), or it is the wrong share.
#     The watchdog never creates a marker.
#   - a stale share's real error (e.g. "Stale file handle") is written to the log, so the cause can be read later.
#   - after 3 remounts in a row that did not help, a share is retried only every 10 minutes instead of every minute.
#   - once every share is fine, a running worker container that cannot see the markers, or that was running while a share was just
#     remounted (its bind mount may still point at the old mount), is restarted (at most once in 10 minutes), and a container that
#     failed to start because a share was missing is started.
# Settings (optional) are read from /etc/synchub-mount-watchdog.conf, a shell file:
#   SYNCHUB_MOUNT_PATHS="/mnt/tower/Media /mnt/tower/TFiles"   SYNCHUB_CONTAINER=synchub-worker   SYNCHUB_MARKER=.synchub-root
# Log: journalctl -t synchub-watchdog
set -uo pipefail

CONF="${SYNCHUB_WATCHDOG_CONF:-/etc/synchub-mount-watchdog.conf}"
[ -f "$CONF" ] && . "$CONF"
PATHS="${SYNCHUB_MOUNT_PATHS:-/mnt/tower/Media /mnt/tower/TFiles}"
CONTAINER="${SYNCHUB_CONTAINER:-synchub-worker}"
MARKER="${SYNCHUB_MARKER:-.synchub-root}"
SETTLE="${SYNCHUB_SETTLE_SECONDS:-20}"
RESTART_GAP="${SYNCHUB_RESTART_GAP_SECONDS:-600}"
LOCK="${SYNCHUB_WATCHDOG_LOCK:-/run/synchub-mount-watchdog.lock}"
STAMP="${SYNCHUB_WATCHDOG_STAMP:-/run/synchub-mount-watchdog.restarted}"
STATE_DIR="${SYNCHUB_WATCHDOG_STATE:-/run/synchub-mount-watchdog.d}"     # per share: how many remounts in a row did not help, and when
BACKOFF_AFTER="${SYNCHUB_BACKOFF_AFTER:-3}"                              # after this many useless remounts in a row ...
BACKOFF_SECONDS="${SYNCHUB_BACKOFF_SECONDS:-600}"                        # ... try again only this often
BIN="${SYNCHUB_WATCHDOG_BIN:-/usr/local/bin/synchub-mount-watchdog}"
UNIT_DIR="${SYNCHUB_UNIT_DIR:-/etc/systemd/system}"

log() { echo "$*"; logger -t synchub-watchdog -- "$*" 2>/dev/null || true; }

# ------------------------------------------------------------------ install / uninstall / status
install_watchdog() {
    local dry="$1"
    run() { if [ "$dry" = 1 ]; then echo "[dry run] $*"; else "$@"; fi; }
    write() {   # write FILE <stdin>
        if [ "$dry" = 1 ]; then echo "[dry run] write $1:"; sed 's/^/    /'; else cat > "$1"; fi
    }
    [ "$dry" = 1 ] || [ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }
    echo "==> Installing the mount watchdog"
    run install -m 0755 "$(readlink -f "$0")" "$BIN"
    if [ ! -f "$CONF" ] || [ "$dry" = 1 ]; then
        write "$CONF" <<EOF
# Read by $BIN. Space-separated mount points, the worker container's name, and the marker file name.
SYNCHUB_MOUNT_PATHS="$PATHS"
SYNCHUB_CONTAINER="$CONTAINER"
SYNCHUB_MARKER="$MARKER"
EOF
    else
        echo "    keeping the existing $CONF"
    fi
    write "$UNIT_DIR/synchub-mount-watchdog.service" <<EOF
[Unit]
Description=Check the SyncHub worker's shares and remount them after a network outage
After=network-online.target

[Service]
Type=oneshot
ExecStart=$BIN
TimeoutStartSec=300
EOF
    write "$UNIT_DIR/synchub-mount-watchdog.timer" <<EOF
[Unit]
Description=Check the SyncHub worker's shares every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
    run systemctl daemon-reload
    run systemctl enable --now synchub-mount-watchdog.timer
    echo "==> Done. Shares watched: $PATHS   container: $CONTAINER"
    echo "    Log: journalctl -t synchub-watchdog      Status: $0 --status"
}

uninstall_watchdog() {
    [ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }
    systemctl disable --now synchub-mount-watchdog.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/synchub-mount-watchdog.service" "$UNIT_DIR/synchub-mount-watchdog.timer" "$BIN" "$CONF"
    systemctl daemon-reload
    echo "Removed the mount watchdog."
}

status_watchdog() {
    systemctl list-timers synchub-mount-watchdog.timer --no-pager 2>&1 | head -4
    echo "--- last log lines"
    journalctl -t synchub-watchdog -n 15 --no-pager 2>&1 | tail -15
}

case "${1:-}" in
    --install) if [ "${2:-}" = "--dry-run" ]; then install_watchdog 1; else install_watchdog 0; fi; exit 0 ;;
    --uninstall) uninstall_watchdog; exit 0 ;;
    --status) status_watchdog; exit 0 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

# ------------------------------------------------------------------ one check
# The file system type of whatever is mounted at $1 ("" when it is only a folder of the local disk).
netfs() { findmnt -n -o FSTYPE -T "$1" 2>/dev/null | tail -n1 | grep -E '^(cifs|smb3|nfs|nfs4|fuse\..*)$' || true; }

# ok | stale (hangs or errors) | unmounted (a plain folder) | unmarked (a share is mounted but has no marker)
probe() {
    local p="$1"
    timeout 10 ls -- "$p" >/dev/null 2>&1 || { echo stale; return; }
    if timeout 10 test -f "$p/$MARKER"; then echo ok; return; fi
    if [ -n "$(netfs "$p")" ]; then echo unmarked; else echo unmounted; fi
}

# Waits up to $SETTLE seconds for the share to turn ok.
settle() {
    local p="$1" waited=0
    while [ "$waited" -lt "$SETTLE" ]; do
        [ "$(probe "$p")" = ok ] && return 0
        sleep 2; waited=$((waited + 2))
    done
    [ "$(probe "$p")" = ok ]
}

# Why a share is stale, in the system's own words (first line only).
why() { timeout 10 ls -- "$1" 2>&1 >/dev/null | head -n1; }

# Remount bookkeeping: $STATE_DIR/<path with / as _> holds "<useless remounts in a row> <time of the last one>".
state_file() { printf '%s/%s' "$STATE_DIR" "$(printf '%s' "$1" | tr / _)"; }
backed_off() {   # true when this share's remounts did not help several times in a row and the last try was recent
    local f count last now; f="$(state_file "$1")"
    [ -f "$f" ] || return 1
    read -r count last < "$f" 2>/dev/null || return 1
    now="$(date +%s)"
    [ "${count:-0}" -ge "$BACKOFF_AFTER" ] && [ $((now - ${last:-0})) -lt "$BACKOFF_SECONDS" ]
}
note_failure() {
    local f count=0 last; f="$(state_file "$1")"
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    [ -f "$f" ] && read -r count last < "$f" 2>/dev/null
    echo "$(( ${count:-0} + 1 )) $(date +%s)" > "$f" 2>/dev/null || true
}
note_success() { rm -f "$(state_file "$1")" 2>/dev/null || true; }

if { exec 9>"$LOCK"; } 2>/dev/null; then flock -n 9 || exit 0; fi

failed=0
remounted=0
for p in $PATHS; do
    state="$(probe "$p")"
    case "$state" in
        ok) note_success "$p" ;;
        unmarked)
            log "WARNING $p is mounted but has no $MARKER file: not touching it. If it is the right share, run the worker's --init-roots; if not, check /etc/fstab."
            failed=1 ;;
        stale|unmounted)
            if backed_off "$p"; then
                log "WARNING $p is still $state; remounting did not help the last few times, so trying again only every $((BACKOFF_SECONDS / 60)) minutes"
                failed=1
                continue
            fi
            if [ "$state" = stale ]; then log "$p is stale ($(why "$p")): mounting it again"; else log "$p is $state: mounting it again"; fi
            [ "$state" = stale ] && { timeout 30 umount -f -l -- "$p" 2>/dev/null || true; }
            if ! out="$(timeout 60 mount -- "$p" 2>&1)"; then
                log "WARNING mounting $p failed: $out"
                note_failure "$p"
                failed=1
            elif settle "$p"; then
                log "$p is mounted again"
                note_success "$p"
                remounted=1
            else
                log "WARNING $p mounted but its marker $MARKER is still not readable (state: $(probe "$p"); $(why "$p"))"
                note_failure "$p"
                failed=1
            fi ;;
    esac
done

# The container only matters once the host sees every share.
if [ "$failed" = 0 ] && command -v docker >/dev/null 2>&1; then
    status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)" || status=""
    if [ "$status" = running ]; then
        # A share that was just mounted again is a new mount: the container's bind mount can still point at the old one (and then reports
        # "Stale file handle"), so it gets a restart even if the marker test still passes.
        blind=$remounted
        [ "$blind" = 1 ] && reason="a share was just mounted again" || reason="it cannot see the markers although the host can"
        for p in $PATHS; do
            timeout 20 docker exec "$CONTAINER" test -f "$p/$MARKER" >/dev/null 2>&1 || blind=1
            timeout 20 docker exec "$CONTAINER" ls -- "$p" >/dev/null 2>&1 || blind=1
        done
        if [ "$blind" = 1 ]; then
            now="$(date +%s)"; last="$(cat "$STAMP" 2>/dev/null || echo 0)"
            if [ $((now - last)) -ge "$RESTART_GAP" ]; then
                echo "$now" > "$STAMP" 2>/dev/null || true
                log "Container $CONTAINER: restarting it ($reason)"
                docker restart "$CONTAINER" >/dev/null 2>&1 || log "WARNING could not restart $CONTAINER"
            else
                log "WARNING container $CONTAINER still cannot see the shares; it was restarted less than $((RESTART_GAP / 60)) minutes ago, so not again. Are the paths inside the container the same as on the host?"
            fi
        fi
    elif [ -n "$status" ] && [ -n "$(docker inspect -f '{{.State.Error}}' "$CONTAINER" 2>/dev/null)" ]; then
        log "Container $CONTAINER is $status after a start error: starting it"
        docker start "$CONTAINER" >/dev/null 2>&1 || log "WARNING could not start $CONTAINER"
    fi
fi
exit "$failed"
